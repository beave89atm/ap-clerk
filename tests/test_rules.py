from datetime import date

from ap_clerk.rules import (
    INVOICE_TYPE_NO_PO,
    INVOICE_TYPE_PO,
    PRICE_DOES_NOT_MATCH,
    SHAWN_MCKIBBEN,
    batch_name_for,
    classify_mail,
    comments_for,
    decide_ppv,
    due_date_from_terms,
    evaluate_bill_price_variance,
    extract_po_number,
    flag_in_outlook_for,
    format_fees,
    format_ppv,
    invoice_type_for,
    is_fee_or_surcharge,
    known_vendor_id,
    match_receipts,
    names_match,
    printed_invoice_number,
    should_create_header,
    vendor_match_score,
)


def test_batch_name_no_leading_zeros():
    assert batch_name_for(date(2026, 8, 27)) == "API Agent - 8/27/26"
    assert batch_name_for(date(2026, 8, 4)) == "API Agent - 8/4/26"


def test_half_percent_terms_are_net_30():
    assert due_date_from_terms(date(2026, 8, 3), "F-0.5/10,N30-0.5% 10, Net 30") == date(2026, 9, 2)
    assert due_date_from_terms(date(2026, 7, 13), "1/2% 10 - Net 30") == date(2026, 8, 12)
    assert due_date_from_terms(date(2026, 7, 10), "F-N60-Net 60") == date(2026, 9, 8)


def test_vendor_name_matching():
    assert names_match("O'Neal Steel - Dallas (GP)", "1135-ONEAL STEEL, LLC.")
    assert names_match("Earle M. Jorgensen Co", "EMJ Earl M. Jorgensen Company")
    assert names_match("Fastenal Company", "FASTENAL INDUSTRIAL & CONSTRUCTION  Acct# TXFT40601")
    assert names_match("Crosslink Powder Coating of TX, LLC", "1276-Crosslink Powder Coating")
    assert names_match("Capital Machine Technologies, Inc", "CAPITAL MACHINE TECHNOLOGIES")


def test_fees_are_not_ppv():
    assert is_fee_or_surcharge("SUPPLY FEE / Shop supplies")
    assert is_fee_or_surcharge("Packaging/Shop Supplies Recovery")
    assert is_fee_or_surcharge("Shipping & Handling")
    assert is_fee_or_surcharge("FUEL SURCHARGE")
    assert format_fees([{"name": "Shipping", "amount": 42.17}]) == "Shipping 42.17"
    assert format_fees([]) == "none"


def test_extract_po_number():
    assert extract_po_number("PO58351-TELECOM PRODUCTS") == "58351"
    assert extract_po_number("PO58634-CAPITAL MACHINE TECHNOLOGIES") == "58634"


def test_no_po_real_bills_still_get_a_header():
    bill = {"vendor": "ENGIE Resources LLC", "po": None, "action": "create"}
    assert should_create_header(bill) == (True, "")
    assert should_create_header({**bill, "action": "hold", "hold_reason": "no-PO"}) == (True, "")
    assert invoice_type_for(None) == INVOICE_TYPE_NO_PO
    assert invoice_type_for("") == INVOICE_TYPE_NO_PO
    assert invoice_type_for("58808") == INVOICE_TYPE_PO


def test_hold_remains_for_check_stop_and_not_a_bill():
    assert should_create_header({"check_stop": True, "hold_reason": "CHECK STOP"}) == (False, "CHECK STOP")
    assert should_create_header({"action": "hold", "hold_reason": "statement"})[0] is False
    assert should_create_header({"action": "hold", "hold_reason": "POD"})[0] is False
    assert should_create_header({"action": "hold", "hold_reason": "not-a-bill"})[0] is False


def test_unifirst_vendors_do_not_collapse():
    corp = "1187-UNIFIRST CORPORATION"
    first_aid = "1207-UNIFIRST FIRST AID & SAFETY"
    assert names_match("UniFirst Corporation", corp)
    assert names_match("UniFirst First Aid & Safety", first_aid)
    assert vendor_match_score("UniFirst Corporation", corp) > vendor_match_score(
        "UniFirst Corporation", first_aid
    )
    assert vendor_match_score("UniFirst First Aid & Safety", first_aid) > vendor_match_score(
        "UniFirst First Aid & Safety", corp
    )


def test_classify_mail_skips_not_a_bill():
    assert classify_mail(subject="Monthly Account Statement") == "statement"
    assert classify_mail(subject="POD for shipment 123", attachment_names=["pod-123.pdf"]) == "pod"
    assert classify_mail(subject="Payment confirmation - thank you") == "payment"
    assert classify_mail(subject="CHECK STOP Gas and Supply") == "check_stop"
    assert classify_mail(subject="Invoice 16960", attachment_names=["Invoice - 16960.pdf"]) == "invoice"


def test_flag_in_outlook_yes_for_success_hold_and_fail():
    assert flag_in_outlook_for("Success") == "Yes"
    assert flag_in_outlook_for("HOLD") == "Yes"
    assert flag_in_outlook_for("Fail") == "Yes"
    assert comments_for("live") == "API Agent"
    assert "prototype" in comments_for("prototype").lower()


def test_kyle_ppv_rule_2026_08_28():
    """Kyle: |var| <= 10% of invoice total AND bill PPV <= $100. Signed PPV."""
    emj = decide_ppv(invoice_line_amount=752.10, po_line_amount=770.16, invoice_total=752.10)
    assert emj["action"] == "ppv"
    assert emj["ppv"] == -18.06
    assert emj["hold"] is False

    oneal = decide_ppv(invoice_line_amount=100.00, po_line_amount=100.10, invoice_total=100.00)
    assert oneal["action"] == "ppv"
    assert oneal["ppv"] == -0.10

    over_abs = decide_ppv(invoice_line_amount=1880.00, po_line_amount=2000.00, invoice_total=2000.00)
    assert over_abs["hold"] is True
    assert over_abs["ppv"] == 0.0
    assert PRICE_DOES_NOT_MATCH in over_abs["reason"]
    assert SHAWN_MCKIBBEN in over_abs["reason"]
    assert "Do not alter receipt unit price in GI" in over_abs["reason"]

    over_pct = decide_ppv(invoice_line_amount=350.00, po_line_amount=400.00, invoice_total=400.00)
    assert over_pct["hold"] is True
    assert PRICE_DOES_NOT_MATCH in over_pct["reason"]

    zero_po = decide_ppv(
        invoice_line_amount=100.00,
        po_line_amount=0.0,
        invoice_total=100.00,
        po_unit_price=0.0,
    )
    assert zero_po["hold"] is True
    assert "Not PPV" in zero_po["reason"]
    assert PRICE_DOES_NOT_MATCH in zero_po["reason"]

    fee = decide_ppv(
        invoice_line_amount=26.25,
        po_line_amount=0.0,
        invoice_total=200.00,
        label="Shipping & Handling",
    )
    assert fee["action"] == "fee"
    assert fee["hold"] is False
    assert fee["ppv"] == 0.0
    assert format_ppv(-18.06) == "-18.06"
    assert format_ppv(0) == "none"


def test_ppv_bill_cap_and_fees_stay_out():
    bill = evaluate_bill_price_variance(
        [
            {"part": "STEEL", "amount": 752.10},
            {"label": "Shop supplies", "amount": 12.00, "fee": True},
        ],
        [{"part": "STEEL", "amount": 770.16, "unit_price": 770.16}],
        invoice_total=752.10,
    )
    assert bill["hold"] is False
    assert bill["ppv_total"] == -18.06
    assert any(item.get("action") == "fee" for item in bill["items"])


def test_vendor_aliases_nsa_and_coherent():
    assert known_vendor_id("National Specialty Alloys") == 1386
    assert known_vendor_id("National Specialty Alloys, Inc") == 1386
    assert known_vendor_id("Coherent Corp.") == 1410
    assert known_vendor_id("Coherent") == 1410
    assert known_vendor_id("Priority 1") == 145
    assert known_vendor_id("MSC Industrial Supply") == 128
    assert known_vendor_id("Metal Supermarkets") == 121
    assert known_vendor_id("Marmon/Keystone") == 115
    assert known_vendor_id("Amada America") == 18
    assert known_vendor_id("Exotic Metals") == 346
    assert known_vendor_id("Fastenal Company") is None


def test_printed_invoice_number_modern_heat_prefix():
    assert printed_invoice_number("220804", vendor="Modern Heat Treat Inc", text="Invoice Number: 8-220804") == "8-220804"
    assert printed_invoice_number("220804", vendor="Modern Heat Treat Inc", text="") == "8-220804"
    assert printed_invoice_number("TXFT499356", vendor="Fastenal Company", text="TXFT499356") == "TXFT499356"
    assert printed_invoice_number("17602", vendor="Telecom Products Inc.", text="Invoice 17602") == "17602"


def test_select_receipts_matches_part_and_po_line_not_first_qty():
    result = match_receipts(
        invoice_number="8-220804",
        invoice_lines=[
            {"part": "625-5200-002", "qty": 1},
            {"part": "400-5200-001", "qty": 1},
        ],
        receipts=[
            {"part": "AAA-1111-000", "qty": 1, "po_line": 1, "slip": "R1"},
            {"part": "BBB-2222-000", "qty": 1, "po_line": 2, "slip": "R2"},
            {"part": "CCC-3333-000", "qty": 1, "po_line": 3, "slip": "R3"},
            {"part": "625-5200-002", "qty": 1, "po_line": 6, "slip": "R6"},
            {"part": "400-5200-001", "qty": 1, "po_line": 7, "slip": "R7"},
        ],
    )
    assert result["hold_no_receipts"] is False
    picked_lines = {str(hit["receipt"]["po_line"]) for hit in result["matched"]}
    picked_parts = {hit["receipt"]["part"] for hit in result["matched"]}
    assert picked_lines == {"6", "7"}
    assert picked_parts == {"625-5200-002", "400-5200-001"}


def test_fastenal_slip_equals_invoice_number_is_findable():
    result = match_receipts(
        invoice_number="TXFT499356",
        invoice_lines=[],
        receipts=[
            {"slip": "OTHERSLIP", "qty": 6, "part": "WRONG", "po_line": 1},
            {"slip": "TXFT499356", "qty": 6, "part": "FAST-42", "po_line": 4, "po": "58700"},
        ],
        po_number="58700",
    )
    assert result["found"] is True
    assert result["hold_no_receipts"] is False
    assert result["matched"][0]["receipt"]["slip"] == "TXFT499356"


def test_qty_only_is_not_a_receipt_match():
    result = match_receipts(
        invoice_number="NO-SLIP",
        invoice_lines=[{"qty": 6, "part": "NEED-THIS"}],
        receipts=[{"slip": "R9", "qty": 6, "part": "DIFFERENT", "po_line": 1}],
    )
    assert result["hold_no_receipts"] is True
    assert result["matched"] == []


def test_no_po_vendor_name_matching():
    assert names_match("Hudson Energy Services, LLC", "1086-HUDSON ENERGY-24312")
    assert names_match("Shoppa's Material Handling, Ltd", "1157-SHOPPA'S MATERIAL HANDLING")
    assert names_match("GRM Information Management Services of Dallas, LLC", "1076-GRM INFORMATION MANAGEMENT SERVICES")
    assert names_match("Gas and Supply North Texas, LLC", "1069-GAS AND SUPPLY")
    assert names_match("Priority1", "1143-PRIORITY 1")
    assert names_match("Luxor Staffing, Inc.", "1110-LUXOR STAFFING, INC.")
    assert names_match("NTEX Electric Inc.", "1132-NTEX ELECTRIC")
