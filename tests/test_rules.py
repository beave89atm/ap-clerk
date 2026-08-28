from datetime import date

from ap_clerk.rules import (
    INVOICE_TYPE_NO_PO,
    INVOICE_TYPE_PO,
    batch_name_for,
    classify_mail,
    comments_for,
    due_date_from_terms,
    extract_po_number,
    flag_in_outlook_for,
    format_fees,
    invoice_type_for,
    is_fee_or_surcharge,
    names_match,
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


def test_flag_in_outlook_is_manual_yes_only_on_success():
    assert flag_in_outlook_for("Success") == "Yes"
    assert flag_in_outlook_for("HOLD") == "No"
    assert flag_in_outlook_for("Fail") == "No"
    assert comments_for("live") == "API Agent"
    assert "prototype" in comments_for("prototype").lower()


def test_no_po_vendor_name_matching():
    assert names_match("Hudson Energy Services, LLC", "1086-HUDSON ENERGY-24312")
    assert names_match("Shoppa's Material Handling, Ltd", "1157-SHOPPA'S MATERIAL HANDLING")
    assert names_match("GRM Information Management Services of Dallas, LLC", "1076-GRM INFORMATION MANAGEMENT SERVICES")
    assert names_match("Gas and Supply North Texas, LLC", "1069-GAS AND SUPPLY")
    assert names_match("Priority1", "1143-PRIORITY 1")
    assert names_match("Luxor Staffing, Inc.", "1110-LUXOR STAFFING, INC.")
    assert names_match("NTEX Electric Inc.", "1132-NTEX ELECTRIC")
