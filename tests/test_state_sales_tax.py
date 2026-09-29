"""NOTE-58: an explicit PDF sales-tax line is an additional charge, not PPV.

A1 Image invoice 67067 / KIMCO 10367: the PDF prints Sales Tax 41.97.
Receipt 24712 is 1 @ 508.67. The PDF total is 550.64. A vendor invoice
with no sales-tax line does not get a computed tax charge.
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
    TREYCE_MENTION_ID,
    is_state_sales_tax_label,
    ppv_qc_gap,
    state_sales_tax_amount_shown,
    state_sales_tax_comment,
    state_sales_tax_gap_decision,
)

A1_PDF = "Sales Tax 41.97\nInvoice total 550.64"


def test_explicit_pdf_sales_tax_line_is_a_charge_not_ppv():
    shown = state_sales_tax_amount_shown({"text": A1_PDF, "tax": 1.00, "sales_tax": 9.99})
    assert shown == 41.97
    assert round(508.67 + 41.97, 2) == 550.64
    decision = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=shown,
        explicit=True,
        receipts_matched=True,
    )
    assert decision["action"] == "sales_tax"
    assert decision["explicit"] is True
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

    qc = ppv_qc_gap(
        invoice_amount=508.67,
        verification_amount=550.64,
        line_amounts=[508.67],
        charge_amounts=[],
        sales_tax=41.97,
        sales_tax_explicit=True,
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
        sales_tax_explicit=True,
    )
    assert closed["action"] == "match"
    assert closed["gap"] == 0.0
    assert closed["success_allowed"] is True


def test_no_explicit_tax_line_follows_ppv_or_hold():
    computed = round(508.67 * 0.0825, 2)
    assert computed == 41.97
    assert state_sales_tax_amount_shown({"tax": computed, "sales_tax": computed, "total": 550.64}) is None
    assert state_sales_tax_amount_shown({"fees": [{"name": "Sales Tax", "amount": computed}]}) is None
    decision = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=computed,
        explicit=False,
        receipts_matched=True,
    )
    assert decision["action"] == "skip"
    assert decision["explicit"] is False
    assert decision["amount"] == 0.0
    assert "Do not compute or infer tax" in decision["reason"]

    under_limit = ppv_qc_gap(
        invoice_amount=508.67,
        verification_amount=550.64,
        line_amounts=[508.67],
        charge_amounts=[],
        sales_tax=computed,
        sales_tax_explicit=False,
    )
    assert under_limit["action"] == "ppv"
    assert under_limit["ppv"] == 41.97

    over_limit = ppv_qc_gap(
        invoice_amount=400.00,
        verification_amount=500.00,
        line_amounts=[400.00],
        charge_amounts=[],
        sales_tax=100.00,
        sales_tax_explicit=False,
    )
    assert over_limit["action"] == "hold"
    assert over_limit["ppv"] == 0.0
    assert over_limit["exception_category"] == "price_variance"


def test_printed_amount_wins_and_a_different_gap_is_not_sales_tax():
    assert is_state_sales_tax_label("Sales Tax")
    assert is_state_sales_tax_label("Texas state sales tax")
    assert is_state_sales_tax_label("State Tax 8.25%")
    assert not is_state_sales_tax_label("City Tax")
    assert not is_state_sales_tax_label("Freight")
    shown = state_sales_tax_amount_shown(
        {
            "total": 550.64,
            "fees": [{"name": "City Tax", "amount": 12.00, "tax": True, "from_pdf": True}],
            "text": A1_PDF,
        }
    )
    assert shown == 41.97
    printed = state_sales_tax_amount_shown({"tax": 41.97, "text": "Sales Tax 1.00"})
    assert printed == 1.00
    from_row = state_sales_tax_amount_shown(
        {"fees": [{"name": "Sales Tax", "amount": 41.97, "from_pdf": True}]}
    )
    assert from_row == 41.97

    mismatch = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=1.00,
        explicit=True,
        receipts_matched=True,
    )
    assert mismatch["action"] == "skip"
    assert mismatch["amount"] == 0.0
    leftover = ppv_qc_gap(
        invoice_amount=508.67,
        verification_amount=550.64,
        line_amounts=[508.67],
        charge_amounts=[],
        sales_tax=1.00,
        sales_tax_explicit=True,
    )
    assert leftover["action"] == "ppv"
    assert leftover["ppv"] == 41.97


def test_other_gaps_and_unmatched_receipts_do_not_use_the_sales_tax_rule():
    extra = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=500.00,
        sales_tax=41.97,
        explicit=True,
        receipts_matched=True,
    )
    assert extra["action"] == "skip"
    assert extra["ppv"] == 0.0

    unmatched = state_sales_tax_gap_decision(
        invoice_total=550.64,
        receipt_amount=508.67,
        sales_tax=41.97,
        explicit=True,
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


def test_transfer_ap_sales_tax_note_tags_treyce_and_selfcheck_rejects_ppv():
    note = state_sales_tax_comment(
        sales_tax=41.97,
        invoice_total=550.64,
        batch_id=375,
        batch_name="TRANSFER AP",
    )
    assert "AP Clerk:" in note
    assert note.index("AP Clerk:") < note.index("data-mention-id")
    assert "per Kyle" in note
    assert "550.64" in note
    assert "41.97" in note
    assert "not posted" in note
    assert f'data-mention-id="{TREYCE_MENTION_ID}"' in note
    assert 'data-mention-name="Treyce Hodges"' in note
    assert "prosemirror-mention-node" in note

    pdf = {"text": A1_PDF, "pdf_text": A1_PDF}
    check = selfcheck_payload(
        {
            "invoice_number": "67067",
            "amount": 550.64,
            "vendor": "A1 Image Office Systems",
            "po": "59295",
            "field_sources": {"invoice_number": "pdf"},
            **pdf,
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
            "vendor": "A1 Image Office Systems",
            "po": "59295",
            "field_sources": {"invoice_number": "pdf"},
            **pdf,
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

    bare = selfcheck_payload(
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
    assert bare["sales_tax_posted_as_ppv"] is False
