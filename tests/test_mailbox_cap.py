"""Kyle 2026-09-09 option B: cap = N bill attempts; noise is sheet-only.

No live Graph or KIMCO I/O.
"""

from __future__ import annotations

from pathlib import Path

from ap_clerk.cli import _process_invoice
from ap_clerk.finish import apply_grouped_outlook_flags, grouped_flag_status_for_message
from ap_clerk.gates import (
    RESULT_HOLD,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
    is_bill_attempt_result,
    is_noise_result,
)
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FLAG_AI_HOLD,
    FLAG_FLAGGED,
    FLAG_NONE,
    apply_flag_after_match,
    decide_flag_status,
)
from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report
from ap_clerk.rules import flag_in_outlook_for, is_noise_reason


class _FakeGraph:
    def __init__(self, messages, pdfs_by_id):
        self.messages = messages
        self.pdfs_by_id = pdfs_by_id
        self.held: list[str] = []
        self.matched: list[str] = []

    def list_messages(self, mailbox, **kwargs):
        assert mailbox == ALLOWED_MAILBOX
        return self.messages

    def list_attachment_names(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("names") or [])

    def download_pdf_attachments(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("pdfs") or [])

    def flag_hold(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.held.append(message_id)
        return FLAG_AI_HOLD

    def flag_matched(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.matched.append(message_id)
        return FLAG_FLAGGED

    def get_message(self, mailbox, message_id, select="id"):
        return {"id": message_id, "categories": []}


def _msg(mid: str, subject: str, received: str, *, name="Vendor", klass_hint="invoice"):
    return {
        "id": mid,
        "subject": subject,
        "receivedDateTime": received,
        "hasAttachments": True,
        "categories": [],
        "from": {"emailAddress": {"name": name, "address": "ap@vendor.com"}},
        "_klass_hint": klass_hint,
    }


def test_cap_walks_past_noise_and_fills_n_bill_attempts(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        _msg("m-statement", "Monthly Account Statement", "2026-08-16T12:00:00Z", name="Bank"),
        _msg("m-payment", "Payment confirmation — thank you", "2026-08-16T13:00:00Z", name="PayCo"),
        _msg("m-check", "CHECK STOP Gas and Supply", "2026-08-16T14:00:00Z", name="Gas and Supply"),
        _msg("m-pod", "POD for shipment 99", "2026-08-16T15:00:00Z", name="ShipCo"),
        _msg("m-not-a-bill", "Internal only — do not process", "2026-08-16T15:30:00Z", name="IT"),
        _msg("m-bill-ok", "Invoice FIRST", "2026-08-16T16:00:00Z", name="Fastenal Company"),
        _msg("m-bill-hold", "Invoice SECOND", "2026-08-16T17:00:00Z", name="EMJ"),
        _msg("m-bill-three", "Invoice THIRD", "2026-08-16T19:00:00Z", name="McMaster-Carr"),
    ]
    graph = _FakeGraph(
        messages,
        {
            "m-statement": {"names": ["statement.pdf"], "pdfs": [("statement.pdf", b"%PDF")]},
            "m-payment": {"names": ["payment.pdf"], "pdfs": [("payment.pdf", b"%PDF")]},
            "m-check": {"names": ["checkstop.pdf"], "pdfs": [("checkstop.pdf", b"%PDF")]},
            "m-pod": {"names": ["pod-99.pdf"], "pdfs": [("pod-99.pdf", b"%PDF")]},
            "m-bill-ok": {"names": ["Invoice-FIRST.pdf"], "pdfs": [("Invoice-FIRST.pdf", b"%PDF")]},
            "m-bill-hold": {"names": ["Invoice-SECOND.pdf"], "pdfs": [("Invoice-SECOND.pdf", b"%PDF")]},
            "m-not-a-bill": {"names": ["note.pdf"], "pdfs": [("note.pdf", b"%PDF")]},
            "m-bill-three": {"names": ["Invoice-THIRD.pdf"], "pdfs": [("Invoice-THIRD.pdf", b"%PDF")]},
        },
    )

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        number = "UNKNOWN"
        for token in ("FIRST", "SECOND", "THIRD"):
            if token in path.name or token in subject:
                number = token
                break
        return {
            "vendor": from_name or "Vendor",
            "invoice_number": number,
            "date": "2026-08-16",
            "po": None,
            "pos": [],
            "amount": 10.0,
            "fees": [],
            "check_stop": "CHECK STOP" in subject,
            "pdf_text_empty": False,
        }

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, skipped = pull_recent_bills(
            graph,
            limit=2,
            pdf_dir=tmp_path / "pdfs",
            fifo=True,
            unprocessed_only=True,
            mark_skips=True,
            max_messages=50,
        )
    finally:
        inbox_mod.parse_invoice_pdf = orig

    assert [inv["invoice_number"] for inv in selected] == ["FIRST", "SECOND"]
    assert "THIRD" not in [inv["invoice_number"] for inv in selected]
    assert {s.get("class") for s in skipped} >= {"statement", "payment", "check_stop", "pod", "internal"}
    assert graph.held == []
    assert all(s.get("Flag status") == FLAG_NONE for s in skipped if s.get("class") != "already-processed")

    skip_rows = skip_rows_for_report(skipped, "API Agent - 9/9/26")
    assert skip_rows
    assert all(row["Result"] == RESULT_SKIPPED for row in skip_rows)
    assert all(row["Flag in Outlook"] == "No" for row in skip_rows)
    assert all(row["Flag status"] == FLAG_NONE for row in skip_rows)
    assert all("bill-vs-noise" in row["Why"] for row in skip_rows)
    assert any("Monthly Account Statement" in row["Why"] for row in skip_rows)
    assert all(not is_bill_attempt_result(row["Result"]) for row in skip_rows)
    assert all(is_noise_result(row["Result"]) for row in skip_rows)


def test_grouped_flags_stamp_bill_hold_not_noise():
    graph = _FakeGraph([], {})
    rows = [
        {
            "Invoice #": "S813859432",
            "Result": RESULT_HOLD,
            "KIMCO id": "",
            "Why": "HOLD (price-does-not-match): price does not match.",
            "graph_message_id": "AAMk-bill-hold",
        },
        {
            "Invoice #": "",
            "Vendor": "Bank",
            "Result": RESULT_SKIPPED,
            "KIMCO id": "",
            "Why": "Skipped (bill-vs-noise): statement.",
            "graph_message_id": "AAMk-statement",
        },
        {
            "Invoice #": "S1387370",
            "Result": RESULT_SUCCESS,
            "KIMCO id": 9948,
            "Why": "",
            "graph_message_id": "AAMk-success",
        },
    ]
    invoices = [
        {"invoice_number": "S813859432", "graph_message_id": "AAMk-bill-hold"},
        {"invoice_number": "S1387370", "graph_message_id": "AAMk-success"},
    ]
    apply_grouped_outlook_flags(rows, invoices, graph)
    assert graph.held == ["AAMk-bill-hold"]
    assert graph.matched == ["AAMk-success"]
    assert rows[0]["Flag status"] == FLAG_AI_HOLD
    assert rows[0]["Flag in Outlook"] == "Yes"
    assert rows[1]["Flag status"] == FLAG_NONE
    assert rows[1]["Flag in Outlook"] == "No"
    assert rows[2]["Flag status"] == FLAG_FLAGGED
    assert rows[2]["Flag in Outlook"] == "Yes"


def test_grouped_flag_helper_ignores_noise_rows():
    noise_only = [{"Result": RESULT_SKIPPED}, {"Result": "Noise"}]
    assert grouped_flag_status_for_message(noise_only) == FLAG_NONE
    mixed = [{"Result": RESULT_SUCCESS}, {"Result": RESULT_SKIPPED}]
    assert grouped_flag_status_for_message(mixed) == FLAG_FLAGGED
    hold_plus_noise = [{"Result": RESULT_HOLD}, {"Result": RESULT_SKIPPED}]
    assert grouped_flag_status_for_message(hold_plus_noise) == FLAG_AI_HOLD


def test_check_stop_enter_path_is_skipped_without_outlook_hold():
    class FakeKimco:
        target = "live"

        def create(self, *args, **kwargs):
            raise AssertionError("noise must not create a header")

    class FakeGraph:
        def flag_hold(self, mailbox, message_id):
            raise AssertionError("noise must not get AI HOLD")

        def flag_matched(self, mailbox, message_id):
            raise AssertionError("noise must not get Entered in AI")

    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Gas and Supply North Texas, LLC",
            "invoice_number": "0040325801",
            "date": "2026-07-31",
            "po": None,
            "amount": 418.93,
            "check_stop": True,
            "hold_reason": "CHECK STOP",
            "graph_message_id": "AAMk-check-stop",
        },
        batch={"id": 671},
        batch_label="API Agent - 8/27/26 (671)",
        invoice_by_number={},
        vendor_samples=[],
        po_index={},
        pdf_dir=None,
        graph_client=FakeGraph(),
        flag_outlook=True,
    )
    assert row["Result"] == RESULT_SKIPPED
    assert row["KIMCO id"] == ""
    assert row["Flag in Outlook"] == "No"
    assert row["Flag status"] == FLAG_NONE
    assert "bill-vs-noise" in row["Why"]
    assert not is_bill_attempt_result(row["Result"])


def test_price_mismatch_still_holds_and_is_outlook_ai_hold():
    class FakeKimco:
        target = "live"

        def create(self, *args, **kwargs):
            raise AssertionError("price-does-not-match must not create a header")

    class FakeGraph:
        def __init__(self):
            self.held = []

        def flag_hold(self, mailbox, message_id):
            self.held.append(message_id)
            return FLAG_AI_HOLD

        def flag_matched(self, mailbox, message_id):
            raise AssertionError("bill HOLD must not get Entered in AI")

    graph = FakeGraph()
    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Earle M. Jorgensen Company",
            "invoice_number": "S813859432",
            "date": "2026-08-18",
            "po": "58000",
            "amount": 1164.32,
            "hold_reason": "price does not match",
            "graph_message_id": "AAMk-emj",
        },
        batch={"id": 703},
        batch_label="API Agent - 9/9/26 (703)",
        invoice_by_number={},
        vendor_samples=[],
        po_index={},
        pdf_dir=None,
        graph_client=graph,
        flag_outlook=True,
    )
    assert row["Result"] == RESULT_HOLD
    assert row["Flag in Outlook"] == "Yes"
    assert row["Flag status"] == FLAG_AI_HOLD
    assert graph.held == ["AAMk-emj"]
    assert is_bill_attempt_result(row["Result"])


def test_flag_helpers_for_skipped_vs_bill_hold():
    assert flag_in_outlook_for("Skipped") == "No"
    assert flag_in_outlook_for("Noise") == "No"
    assert flag_in_outlook_for("HOLD") == "Yes"
    assert flag_in_outlook_for("Success") == "Yes"
    assert decide_flag_status(result="Skipped", kimco_id="", message_id="AAMk") == FLAG_NONE
    assert decide_flag_status(result="HOLD", kimco_id="", message_id="AAMk") == "hold-eligible"
    assert decide_flag_status(result="Success", kimco_id=9948, message_id="AAMk") == "eligible"
    row = {"Result": "Skipped", "KIMCO id": "", "Why": "Skipped (bill-vs-noise): statement."}
    status = apply_flag_after_match(row, {"graph_message_id": "AAMk-noise"}, None)
    assert status == FLAG_NONE
    assert row["Flag status"] == FLAG_NONE
    assert is_noise_reason("CHECK STOP")
    assert is_noise_reason("statement")
    assert is_noise_reason("not-a-bill")
    assert not is_noise_reason("price does not match")
    assert not is_noise_reason("parse-error")
