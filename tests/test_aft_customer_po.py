"""Customer P.O. No. is the only header number used. No live KIMCO I/O."""

from ap_clerk.aft_customer_po import (
    assign_receipts,
    build_treyce_note,
    customer_po_from_layout,
    customer_po_from_words,
    merchandise_lines,
    payable_amount,
    receipt_matches_line,
)

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


def test_payable_skips_a_zero_rollup_for_the_header_total():
    amount, field = payable_amount(
        {"balance": 0, "net": 0, "amount": 0, "verification": 363}
    )
    assert amount == 363
    assert field == "Invoice_Verification_Amount"
    amount, field = payable_amount({"balance": 190.30, "amount": 190.30, "verification": 190.30})
    assert amount == 190.30
    assert field == "Invoice_Balance"
