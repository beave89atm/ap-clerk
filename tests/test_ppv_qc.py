"""NOTE-57: live PPV QC. Penny gaps, zero, $75, and the Success block."""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from ap_clerk.gates import treyce_finish_selfcheck
from ap_clerk.ppv_qc import (
    apply_batch_ppv_qc,
    apply_post_entry_ppv_gate,
    ppv_qc_from_record,
    stamp_ppv_qc_on_row,
)
from ap_clerk.report import COLUMNS, write_report
from ap_clerk.rules import ppv_qc_gap


def _record(*, amount, verification, lines, charges):
    return {
        "values": {
            "Invoice_Amount": amount,
            "Invoice_Verification_Amount": verification,
        },
        "lists": {
            "APInvoiceLine": [
                {"values": {"Extended_Amount": amount_}} for amount_ in lines
            ],
            "InvoiceAdditionalCharges": [
                {"values": {"Amount": amount_, "Additional_Charges": {"id": 13, "text": "Purchase Price Variance"}}}
                for amount_ in charges
            ],
        },
    }


def test_positive_and_negative_penny_gaps_post_the_exact_sign():
    plus = ppv_qc_gap(invoice_amount=10.01, verification_amount=10.01, line_amounts=[10.00])
    assert plus["action"] == "ppv"
    assert plus["gap"] == 0.01
    assert plus["ppv"] == 0.01
    assert plus["success_allowed"] is False

    minus = ppv_qc_gap(invoice_amount=10.00, verification_amount=9.99, line_amounts=[10.00])
    assert minus["action"] == "ppv"
    assert minus["gap"] == -0.01
    assert minus["ppv"] == -0.01
    assert minus["success_allowed"] is False


def test_gas_0040446744_gap_is_positive_three_cents():
    """Verification 1891.88 vs extensions 1891.85. The PPV is +0.03.

    Invoice_Amount had already rolled to 1891.85, so the rollup gap was 0.
    """
    decision = ppv_qc_gap(
        invoice_amount=1891.85,
        verification_amount=1891.88,
        line_amounts=[275.50, 42.60, 563.00, 511.00, 11.35, 12.95, 39.34, 20.61, 12.00, 403.50],
    )
    assert decision["lines"] == 1891.85
    assert decision["rollup_gap"] == 0.0
    assert decision["gap"] == 0.03
    assert decision["ppv"] == 0.03
    assert decision["header_field"] == "Invoice_Verification_Amount"
    assert decision["success_allowed"] is False

    closed = ppv_qc_gap(
        invoice_amount=1891.88,
        verification_amount=1891.88,
        line_amounts=[1891.85],
        charge_amounts=[0.03],
    )
    assert closed["action"] == "match"
    assert closed["gap"] == 0.0
    assert closed["rollup_gap"] == 0.0
    assert closed["success_allowed"] is True


def test_zero_gap_is_a_match():
    decision = ppv_qc_gap(
        invoice_amount=29.25,
        verification_amount=29.25,
        line_amounts=[29.28],
        charge_amounts=[-0.03],
    )
    assert decision["action"] == "match"
    assert decision["gap"] == 0.0
    assert decision["ppv"] == 0.0
    assert decision["success_allowed"] is True


def test_gap_at_or_above_75_holds_price_variance_and_does_not_post():
    at = ppv_qc_gap(invoice_amount=100.0, verification_amount=100.0, line_amounts=[25.0])
    assert at["gap"] == 75.0
    assert at["action"] == "hold"
    assert at["ppv"] == 0.0
    assert at["exception_category"] == "price_variance"
    assert at["success_allowed"] is False

    over = ppv_qc_gap(invoice_amount=200.0, verification_amount=200.0, line_amounts=[100.0])
    assert over["action"] == "hold"
    assert over["ppv"] == 0.0

    under = ppv_qc_gap(invoice_amount=100.0, verification_amount=100.0, line_amounts=[25.01])
    assert under["action"] == "ppv"
    assert under["ppv"] == 74.99


def test_selfcheck_blocks_success_unless_live_gap_is_zero():
    blocked, why = treyce_finish_selfcheck({"ppv_live_gap": 0.01, "require_pdf_number": False})
    assert blocked is False
    assert "0.00" in why
    assert "Never Success" in why

    negative, why_neg = treyce_finish_selfcheck({"ppv_live_gap": -0.01, "require_pdf_number": False})
    assert negative is False
    assert "-0.01" in why_neg

    over, why_over = treyce_finish_selfcheck({"ppv_live_gap": 75, "require_pdf_number": False})
    assert over is False
    assert "price_variance" in why_over

    allowed, _why = treyce_finish_selfcheck({"ppv_live_gap": 0.0, "require_pdf_number": False})
    assert allowed is True


def test_batch_qc_clears_success_when_live_gap_is_nonzero():
    class Client:
        def get_item(self, service, item_id):
            assert service == "ap_invoices"
            assert item_id == 10318
            return _record(amount=192.86, verification=192.85, lines=[192.86], charges=[])

    rows = [{"Result": "Success", "KIMCO id": 10318, "Invoice #": "15464074", "Why": "Finished.", "Notes": "leave blank"}]
    findings = apply_batch_ppv_qc(Client(), rows)
    assert rows[0]["Result"] == "HOLD"
    assert "0.00" in rows[0]["Why"]
    assert rows[0].get("_ppv_qc_fixed") is not True
    assert findings[0]["gap"] == -0.01


def test_post_entry_gate_posts_one_penny_and_rereads_to_zero():
    open_record = _record(amount=29.28, verification=29.25, lines=[29.28], charges=[])
    closed_record = _record(amount=29.25, verification=29.25, lines=[29.28], charges=[-0.03])

    class Client:
        def __init__(self):
            self.ppv = []

        def get_item(self, service, item_id):
            assert item_id == 10284
            return closed_record if self.ppv else open_record

        def try_post_ppv(self, invoice_id, amount):
            self.ppv.append((invoice_id, amount))
            return "posted"

    client = Client()
    outcome = apply_post_entry_ppv_gate(client, 10284)
    assert client.ppv == [(10284, -0.03)]
    assert outcome["fixed"] is True
    assert outcome["gap"] == 0.0
    assert outcome["success_allowed"] is True
    assert "posted -0.03" in outcome["note"]

    row = {"Result": "Success", "KIMCO id": 10284, "Why": "Finished.", "Notes": ""}
    stamp_ppv_qc_on_row(Client(), row)
    assert row["Result"] == "Success"
    assert row["_ppv_qc_fixed"] is True
    assert "-0.03" in row["Notes"]


def test_post_entry_gate_does_not_post_at_75_or_without_lines():
    class Client:
        def __init__(self, record):
            self.record = record
            self.ppv = []

        def get_item(self, service, item_id):
            return self.record

        def try_post_ppv(self, invoice_id, amount):
            self.ppv.append(amount)
            return "posted"

    held = Client(_record(amount=100.0, verification=100.0, lines=[25.0], charges=[]))
    outcome = apply_post_entry_ppv_gate(held, 1)
    assert held.ppv == []
    assert outcome["action"] == "hold"
    assert outcome["success_allowed"] is False
    assert "price-does-not-match" in outcome["why"]

    empty = Client(_record(amount=40.0, verification=40.0, lines=[], charges=[]))
    skipped = apply_post_entry_ppv_gate(empty, 2)
    assert empty.ppv == []
    assert skipped["action"] == "no-lines"
    assert skipped["success_allowed"] is False


def test_notes_column_is_unchanged_unless_ppv_qc_posted(tmp_path: Path):
    path = tmp_path / "AP-run.xlsx"
    base = {
        "Vendor": "Gas",
        "Invoice #": "0040443847",
        "date": "2026-09-22",
        "PO": "",
        "Amount": 29.25,
        "Result": "Success",
        "Why": "Finished bill.",
        "KIMCO id": 10284,
        "Batch": "API Agent - 9/17/26 Gas & Supply",
        "Fees and surcharges": "none",
        "PPV": "-0.03",
        "Attach status": "attached",
        "Flag in Outlook": "Yes",
        "Flag status": "entered-in-ai",
    }
    write_report(path, [{**base, "Notes": "agent must not write here"}])
    book = load_workbook(path)
    headers = [book.active.cell(1, col).value for col in range(1, len(COLUMNS) + 1)]
    assert headers == COLUMNS
    assert headers[-1] == "Notes"
    assert not book.active.cell(2, len(COLUMNS)).value

    write_report(
        path,
        [{**base, "Notes": "PPV QC posted -0.03 so lines + charges equal the invoice header (live gap 0.00).", "_ppv_qc_fixed": True}],
    )
    book = load_workbook(path)
    assert book.active.cell(1, len(COLUMNS)).value == "Notes"
    assert "posted -0.03" in str(book.active.cell(2, len(COLUMNS)).value)
    assert len(headers) == len([book.active.cell(1, col).value for col in range(1, 30) if book.active.cell(1, col).value])


def test_record_reader_uses_verification_not_the_rollup():
    decision = ppv_qc_from_record(_record(amount=29.28, verification=29.25, lines=[14.76, 14.52], charges=[]))
    assert decision["gap"] == -0.03
    assert decision["rollup_gap"] == 0.0
    assert decision["ppv"] == -0.03
