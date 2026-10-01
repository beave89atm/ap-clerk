"""Customer P.O. No. is the only header number used. No live KIMCO I/O."""

from ap_clerk.aft_customer_po import (
    assign_receipts,
    build_treyce_note,
    customer_po_from_layout,
    customer_po_from_words,
    decide_aft_industries,
    eachs_unit_price,
    is_aft_industries_vendor,
    is_automated_finishing_vendor,
    merchandise_lines,
    payable_amount,
    receipt_matches_line,
    select_without_work_order_available,
)
from ap_clerk.gates import treyce_finish_selfcheck
from ap_clerk.quality_v12 import RESULT_HOLD, assert_never_success

_AFT_HEADER = """
 CUSTOMER'S SHIPPER            CUSTOMER P.O. NO.            B/O FROM       DATE ENTERED          SALES ORDER NO.       CERT NO.

                               {po}                                       9/4/2026              {sales}                 {cert}
"""


def _word(text: str, x: float, y: float) -> dict:
    return {"text": text, "x": x, "y": y}


def test_customer_po_column_ignores_other_header_numbers():
    words = [
        _word("Invoice", 50, 700),
        _word("No.", 100, 700),
        _word("Customer", 180, 700),
        _word("P.O.", 250, 700),
        _word("No.", 290, 700),
        _word("Order", 400, 700),
        _word("No.", 450, 700),
        _word("52005", 50, 680),
        _word("59106", 200, 680),
        _word("59097", 400, 680),
    ]
    assert customer_po_from_words(words, invoice_number="52005") == "59106"


def test_customer_po_same_line_ignores_a_far_number():
    words = [
        _word("Customer", 40, 640),
        _word("P.O.", 110, 640),
        _word("No.", 150, 640),
        _word("59097", 190, 640),
        _word("59106", 420, 640),
    ]
    assert customer_po_from_words(words, invoice_number="52004") == "59097"


def test_layout_customer_po_ignores_sales_order_and_cert():
    invoice_52005 = _AFT_HEADER.format(po="59106", sales="56902", cert="59030")
    invoice_52004 = _AFT_HEADER.format(po="59097", sales="56847", cert="59029")
    assert customer_po_from_layout(invoice_52005, invoice_number="52005") == "59106"
    assert customer_po_from_layout(invoice_52004, invoice_number="52004") == "59097"


def test_priced_pound_line_does_not_match_each_receipt():
    """52005 is 660 lb at 0.55 = 363.00. A 30-each receipt at 1.50 is not that line."""
    line = {"qty": 660, "unit_price": 0.55, "ext": 363.00}
    each_receipt = {"id": 24118, "qty": 30, "unit_price": 1.50, "ext": 45.00}
    pound_receipt = {"id": 99, "qty": 660, "unit_price": 0.55, "ext": 363.00}
    assert not receipt_matches_line(line, each_receipt)
    assert assign_receipts([line], [each_receipt]) == []
    assert assign_receipts([line], [pound_receipt]) == [pound_receipt]


def test_customer_po_rejects_the_invoice_number():
    words = [
        _word("Customer", 40, 640),
        _word("P.O.", 110, 640),
        _word("No.:", 150, 640),
        _word("52004", 200, 640),
    ]
    assert customer_po_from_words(words, invoice_number="52004") is None


def test_merchandise_lines_must_sum_to_the_invoice_total():
    words = [
        _word("Qty", 40, 500),
        _word("Description", 80, 500),
        _word("Price", 200, 500),
        _word("Amount", 260, 500),
        _word("2", 40, 480),
        _word("Coat", 80, 480),
        _word("95.15", 200, 480),
        _word("190.30", 260, 480),
        _word("Total", 40, 450),
        _word("190.30", 260, 450),
    ]
    rows = merchandise_lines(words, 190.30)
    assert rows == [
        {
            "qty": 2.0,
            "unit_price": 95.15,
            "ext": 190.30,
            "description": "2 Coat 95.15 190.30",
        }
    ]
    assert merchandise_lines(words, 363.00) == []


def test_receipt_must_match_quantity_and_price_to_the_penny():
    line = {"qty": 2, "unit_price": 95.15, "ext": 190.30}
    exact = {"id": 10, "qty": 2, "unit_price": 95.15, "ext": 190.30}
    off_price = {"id": 11, "qty": 2, "unit_price": 95.16, "ext": 190.32}
    off_qty = {"id": 12, "qty": 1, "unit_price": 190.30, "ext": 190.30}
    assert receipt_matches_line(line, exact)
    assert not receipt_matches_line(line, off_price)
    assert not receipt_matches_line(line, off_qty)
    assert assign_receipts([line], [exact, off_price]) == [exact]
    assert assign_receipts([line], [off_price, off_qty]) == []


def test_ambiguous_receipts_are_not_selected():
    line = {"qty": 1, "unit_price": 363.00, "ext": 363.00}
    first = {"id": 1, "qty": 1, "unit_price": 363.00, "ext": 363.00}
    second = {"id": 2, "qty": 1, "unit_price": 363.00, "ext": 363.00}
    assert assign_receipts([line], [first, second]) == []


def test_note_tags_treyce_and_does_not_mention_shawn():
    html = build_treyce_note(
        po="59106",
        invoice="52005",
        linked=True,
        selected=[],
        payable=363.0,
        match_reason="matched-none",
    )
    assert html.startswith("<p>AP Clerk:")
    assert 'data-mention-id="33"' in html
    assert "Customer P.O. No. 59106" in html
    assert "Receipts were not selected" in html
    assert "Payable total is $363.00" in html
    assert "Shawn" not in html
    selected = build_treyce_note(
        po="59097",
        invoice="52004",
        linked=True,
        selected=[{"id": 44, "qty": 2, "unit_price": 95.15}],
        payable=190.30,
        match_reason="selected",
    )
    assert "receipt 44 qty 2 at 95.15" in selected
    assert "Receipts were selected" in selected


def _aft_bill():
    return {"bill_vendor_id": 1383, "bill_vendor_name": "1383-AFT Industries"}


def _aft_po():
    return {"po_vendor_id": 1383, "po_vendor_name": "1383-AFT Industries"}


def test_never_repeat_note60_aft_eachs_match_selects_when_dollars_and_each_qty_match():
    """52004: receipt 25155 is 49 each @ $3.8837 = $190.30. 346 lb is weight, not qty."""
    line = {
        "qty": 346,
        "each_qty": 49,
        "unit_price": 0.55,
        "ext": 190.30,
        "description": "346 lb",
        "uom": "lb",
        "shawn_confirmed_eachs": True,
    }
    each_receipt = {"id": 25155, "qty": 49, "unit_price": 3.8837, "ext": 190.30, "uom": "EA"}
    pound_qty_receipt = {"id": 24117, "qty": 346, "unit_price": 0.55, "ext": 190.30, "uom": "lb"}
    assert eachs_unit_price(190.30, 49) == 3.8837
    assert receipt_matches_line(line, each_receipt)
    assert not receipt_matches_line(line, pound_qty_receipt)
    assert assign_receipts([line], [pound_qty_receipt, each_receipt]) == [each_receipt]
    ea_only = {**line, "shawn_confirmed_eachs": False}
    assert receipt_matches_line(ea_only, each_receipt)
    decision = decide_aft_industries(
        invoice="52004",
        po="59097",
        lines=[line],
        receipts=[each_receipt],
        payable=190.30,
        **_aft_bill(),
        **_aft_po(),
    )
    assert decision["receipts_selected"] is True
    assert decision["selected"] == [each_receipt]
    assert decision["link"] is True
    assert decision["posted"] is False
    assert decision["batch"] == "Transfer AP"
    assert decision["result"] == RESULT_HOLD
    assert_never_success(decision["result"], note_id="NOTE-60", detail=decision["why"])
    assert 'data-mention-id="33"' in decision["note"]
    assert "receipt 25155 qty 49 at 3.8837" in decision["note"]
    assert "not posted" in decision["note"]
    assert "Success" not in decision["note"]
    assert select_without_work_order_available() is False
    blocked = decide_aft_industries(
        invoice="52004",
        po="59097",
        lines=[line],
        receipts=[{**each_receipt, "work_order_rejected": True}],
        payable=190.30,
        **_aft_bill(),
        **_aft_po(),
    )
    assert blocked["receipts_selected"] is False
    assert blocked["selected"] == []
    assert blocked["posted"] is False
    assert blocked["result"] == RESULT_HOLD
    assert "Work_Order" in blocked["why"]
    assert "Success" not in blocked["why"]
    assert_never_success(blocked["result"], note_id="NOTE-60", detail=blocked["why"])


def test_never_repeat_note60_aft_dollars_match_pieces_vs_lb_no_select():
    """52005: dollars can match after Shawn Done and still be pieces vs pounds."""
    line = {
        "qty": 660,
        "unit_price": 0.55,
        "ext": 363.00,
        "description": "660 lb",
        "uom": "lb",
    }
    receipt = {
        "id": 24118,
        "qty": 30,
        "unit_price": 12.10,
        "ext": 363.00,
        "uom": "pcs",
        "shawn_done": True,
    }
    assert not receipt_matches_line(line, receipt)
    assert assign_receipts([line], [receipt]) == []
    decision = decide_aft_industries(
        invoice="52005",
        po="59106",
        lines=[line],
        receipts=[receipt],
        payable=363.00,
        **_aft_bill(),
        **_aft_po(),
    )
    assert decision["receipts_selected"] is False
    assert decision["selected"] == []
    assert decision["link"] is False
    assert decision["posted"] is False
    assert decision["batch"] == "Transfer AP"
    assert decision["result"] == RESULT_HOLD
    assert decision["category"] == "quantity_variance"
    assert "UOM/qty" in decision["why"]
    assert "pieces" in decision["why"]
    assert 'data-mention-id="104"' in decision["note"]
    assert "Transfer AP" in decision["note"]
    assert "not posted" in decision["note"]
    assert "Success" not in decision["note"]
    assert "Success" not in decision["why"]
    assert_never_success(decision["result"], note_id="NOTE-60", detail=decision["why"])
    blocked_finish, finish_why = treyce_finish_selfcheck(
        {"note60_uom_qty_hold": True, "require_pdf_number": False}
    )
    assert blocked_finish is False
    assert_never_success(RESULT_HOLD, note_id="NOTE-60", detail=finish_why)


def test_never_repeat_note60_aft_industries_not_automated_finishing_po():
    """Vendor 1383 never takes vendor 1329's PO, even when each qty and dollars match."""
    assert is_aft_industries_vendor(1383, "1383-AFT Industries")
    assert not is_automated_finishing_vendor(1383, "1383-AFT Industries")
    assert is_automated_finishing_vendor(1329, "Automated Finishing Technology")
    assert is_automated_finishing_vendor(331, "PO59097-AUTOMATED FINISHING TECHNOLOGY")
    assert not is_aft_industries_vendor(1329, "Automated Finishing Technology")
    assert not is_aft_industries_vendor(None, "PO59106-AUTOMATED FINISHING TECHNOLOGY")
    line = {
        "qty": 346,
        "each_qty": 49,
        "unit_price": 0.55,
        "ext": 190.30,
        "description": "346 lb",
        "uom": "lb",
        "shawn_confirmed_eachs": True,
    }
    receipt = {"id": 25155, "qty": 49, "unit_price": 3.8837, "ext": 190.30, "uom": "EA"}
    assert receipt_matches_line(line, receipt)
    decision = decide_aft_industries(
        invoice="52004",
        po="59097",
        lines=[line],
        receipts=[receipt],
        payable=190.30,
        bill_vendor_id=1383,
        bill_vendor_name="1383-AFT Industries",
        po_vendor_id=1329,
        po_vendor_name="PO59097-AUTOMATED FINISHING TECHNOLOGY",
    )
    assert decision["vendor_mismatch"] is True
    assert decision["category"] == "vendor_mismatch"
    assert decision["link"] is False
    assert decision["receipts_selected"] is False
    assert decision["selected"] == []
    assert decision["posted"] is False
    assert decision["result"] == RESULT_HOLD
    assert "1329" in decision["why"]
    assert "1383" in decision["why"]
    assert "Success" not in decision["why"]
    assert "not posted" in decision["note"]
    assert_never_success(decision["result"], note_id="NOTE-60", detail=decision["why"])
    by_name = decide_aft_industries(
        invoice="52005",
        po="59106",
        lines=[line],
        receipts=[receipt],
        payable=363.00,
        bill_vendor_id=1383,
        bill_vendor_name="AFT Industries",
        po_vendor_name="PO59106-AUTOMATED FINISHING TECHNOLOGY",
    )
    assert by_name["vendor_mismatch"] is True
    assert by_name["link"] is False
    assert by_name["selected"] == []
    blocked_finish, finish_why = treyce_finish_selfcheck(
        {"note60_automated_finishing_po": True, "require_pdf_number": False}
    )
    assert blocked_finish is False
    assert "1329" in finish_why
    assert_never_success(RESULT_HOLD, note_id="NOTE-60", detail=finish_why)


def test_payable_skips_a_zero_rollup_for_the_header_total():
    amount, field = payable_amount(
        {"balance": 0, "net": 0, "amount": 0, "verification": 363}
    )
    assert amount == 363
    assert field == "Invoice_Verification_Amount"
    amount, field = payable_amount({"balance": 190.30, "amount": 190.30, "verification": 190.30})
    assert amount == 190.30
    assert field == "Invoice_Balance"
