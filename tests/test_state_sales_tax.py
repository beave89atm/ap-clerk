"""NOTE-58: state sales tax left after receipt matching is an additional charge, not PPV.

A1 Image invoice 67067 / KIMCO 10367: receipt 24712 is 1 @ 508.67. The PDF
total is 550.64. The $41.97 gap is Texas state sales tax at 8.25%.
"""

from __future__ import annotations

from ap_clerk.gates import selfcheck_payload, treyce_finish_selfcheck
from ap_clerk.kimco import (
    FEE_CHARGE_LOOKUP_ID,
    PPV_CHARGE_LOOKUP_ID,
    ppv_payload,
    sales_tax_charge_payload,
)
from ap_clerk.rules import (
    SALES_TAX_CHARGE_DESCRIPTION,
    is_state_sales_tax_label,
    ppv_qc_gap,
    state_sales_tax_amount_shown,
    state_sales_tax_comment,
    state_sales_tax_gap_decision,
)


def test_a1_67067_gap_is_texas_sales_tax_not_ppv():
    assert round(508.67 * 0.0825, 2) == 41.97
    assert round(508.67 + 41.97, 2) == 550.64
    decision = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=41.97,
        receipts_matched=True,
    )
    assert decision["action"] == "sales_tax"
    assert decision["ppv"] == 0.0
    assert decision["amount"] == 41.97
    assert decision["description"] == "Sales tax"
    assert decision["charge_as"] == "additional_charge"
    assert decision["post_bill"] is False
    assert "Purchase Price Variance" in decision["reason"]

    payload = sales_tax_charge_payload(decision["amount"], invoice_id=10367)
    child = payload["lists"]["InvoiceAdditionalCharges"][0]
    assert child["state"] == "Added"
    values = child["values"]
    assert values["Additional_Charges"]["id"] == FEE_CHARGE_LOOKUP_ID
    assert values["Additional_Charges"]["id"] != PPV_CHARGE_LOOKUP_ID
    assert values["Name"] == SALES_TAX_CHARGE_DESCRIPTION == "Sales tax"
    assert values["Quantity"] == 1.0
    assert values["Price"] == 41.97
    assert values["Amount"] == 41.97
    assert "Posted" not in payload
    assert payload["id"] == 10367

    ppv = ppv_payload(41.97, invoice_id=10367)
    assert ppv["lists"]["InvoiceAdditionalCharges"][0]["values"]["Additional_Charges"]["id"] == PPV_CHARGE_LOOKUP_ID
    assert "Name" not in ppv["lists"]["InvoiceAdditionalCharges"][0]["values"]


def test_sales_tax_charge_is_not_posted_as_ppv_when_gap_equals_tax():
    qc = ppv_qc_gap(
        invoice_amount=508.67,
        verification_amount=550.64,
        line_amounts=[508.67],
        charge_amounts=[],
        sales_tax=41.97,
    )
    assert qc["action"] == "sales_tax"
    assert qc["ppv"] == 0.0
    assert qc["gap"] == 41.97
    assert qc["success_allowed"] is False

    closed = ppv_qc_gap(
        invoice_amount=550.64,
        verification_amount=550.64,
        line_amounts=[508.67],
        charge_amounts=[41.97],
        sales_tax=41.97,
    )
    assert closed["action"] == "match"
    assert closed["gap"] == 0.0
    assert closed["success_allowed"] is True


def test_other_gaps_and_unmatched_receipts_do_not_use_the_sales_tax_rule():
    extra = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=500.00,
        sales_tax=41.97,
        receipts_matched=True,
    )
    assert extra["action"] == "skip"
    assert extra["ppv"] == 0.0

    unmatched = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=41.97,
        receipts_matched=False,
        unmatched_count=1,
    )
    assert unmatched["action"] == "skip"

    no_tax = state_sales_tax_gap_decision(
        invoice_total=192.85,
        receipt_amount=192.86,
        sales_tax=None,
        receipts_matched=True,
    )
    assert no_tax["action"] == "skip"
    penny = ppv_qc_gap(
        invoice_amount=192.86,
        verification_amount=192.85,
        line_amounts=[192.86],
        charge_amounts=[],
    )
    assert penny["action"] == "ppv"
    assert penny["ppv"] == -0.01


def test_city_tax_is_not_state_sales_tax():
    assert is_state_sales_tax_label("Sales Tax")
    assert is_state_sales_tax_label("Texas state sales tax")
    assert is_state_sales_tax_label("State Tax 8.25%")
    assert not is_state_sales_tax_label("City Tax")
    assert not is_state_sales_tax_label("Freight")
    shown = state_sales_tax_amount_shown(
        {
            "total": 550.64,
            "fees": [{"name": "City Tax", "amount": 12.00, "tax": True}],
            "text": "Sales Tax 41.97\nInvoice total 550.64",
        }
    )
    assert shown == 41.97
    explicit = state_sales_tax_amount_shown({"tax": 41.97, "text": "Sales Tax 1.00"})
    assert explicit == 41.97


def test_comment_and_selfcheck_reject_booking_the_tax_as_ppv():
    note = state_sales_tax_comment(sales_tax=41.97, invoice_total=550.64)
    assert note.startswith("AP Clerk:")
    assert "per Kyle" in note
    assert "550.64" in note
    assert "41.97" in note
    assert "not posted" in note
    assert "@" not in note

    check = selfcheck_payload(
        {
            "invoice_number": "67067",
            "amount": 550.64,
            "tax": 41.97,
            "vendor": "A1 Image Office Systems",
            "po": "59295",
            "field_sources": {"invoice_number": "pdf"},
        },
        invoice_type=3,
        po="59295",
        price={"ppv_total": 41.97, "items": [{"action": "ppv", "label": "copier"}]},
        receipt_result={
            "matched": [{"receipt": {"qty": 1, "unit_price": 508.67, "amount": 508.67}}],
            "unmatched_lines": [],
        },
    )
    assert check["sales_tax_posted_as_ppv"] is True
    ok, why = treyce_finish_selfcheck(check)
    assert ok is False
    assert "sales tax" in why.lower()
    assert "Purchase Price Variance" in why

    cleared = selfcheck_payload(
        {
            "invoice_number": "67067",
            "amount": 550.64,
            "tax": 41.97,
            "vendor": "A1 Image Office Systems",
            "po": "59295",
            "field_sources": {"invoice_number": "pdf"},
        },
        invoice_type=3,
        po="59295",
        price={"ppv_total": 0.0, "items": []},
        receipt_result={
            "matched": [{"receipt": {"qty": 1, "unit_price": 508.67, "amount": 508.67}}],
            "unmatched_lines": [],
        },
        fees_posted=True,
    )
    assert cleared["sales_tax_posted_as_ppv"] is False
