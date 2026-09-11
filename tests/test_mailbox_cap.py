"""Kyle 2026-09-11: hard email cap 10 + skip already-flagged.

Cap = mailbox messages touched (any outcome). Noise consumes the cap.
Already-flagged mail is walked past and does not count. No live I/O.
"""

from __future__ import annotations

from pathlib import Path

from ap_clerk.cli import _process_invoice
from ap_clerk.finish import apply_grouped_outlook_flags, grouped_flag_status_for_message
from ap_clerk.gates import (
    RESULT_HOLD,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
    counts_toward_email_cap,
    is_bill_attempt_result,
    is_noise_result,
)
from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_AI_HOLD,
    FLAG_ENTERED_WITH_ISSUES,
    FLAG_FLAGGED,
    FLAG_NONE,
    apply_flag_after_match,
    decide_flag_status,
    has_followup_flagged,
    is_already_flagged,
)
from ap_clerk.inbox import (
    HARD_EMAIL_CAP,
    clamp_email_limit,
    pull_recent_bills,
    skip_rows_for_report,
)
from ap_clerk.rules import flag_in_outlook_for, is_noise_reason


class _FakeGraph:
    def __init__(self, messages, pdfs_by_id):
        self.messages = messages
        self.pdfs_by_id = pdfs_by_id
        self.held: list[str] = []
        self.matched: list[str] = []
        self.named: list[str] = []
        self.downloaded: list[str] = []

    def list_messages(self, mailbox, **kwargs):
        assert mailbox == ALLOWED_MAILBOX
        return self.messages

    def list_attachment_names(self, mailbox, message_id):
        self.named.append(message_id)
        return list(self.pdfs_by_id.get(message_id, {}).get("names") or [])

    def download_pdf_attachments(self, mailbox, message_id):
        self.downloaded.append(message_id)
        return list(self.pdfs_by_id.get(message_id, {}).get("pdfs") or [])

    def flag_hold(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.held.append(message_id)
        return FLAG_AI_HOLD

    def flag_matched(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.matched.append(message_id)
        return FLAG_FLAGGED

    def flag_issues(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.held.append(f"issues:{message_id}")
        return FLAG_ENTERED_WITH_ISSUES

    def get_message(self, mailbox, message_id, select="id"):
        return {"id": message_id, "categories": []}


def _msg(
    mid: str,
    subject: str,
    received: str,
    *,
    name="Vendor",
    klass_hint="invoice",
    categories=None,
    flag_status="notFlagged",
):
    return {
        "id": mid,
        "subject": subject,
        "receivedDateTime": received,
        "hasAttachments": True,
        "categories": list(categories or []),
        "flag": {"flagStatus": flag_status},
        "from": {"emailAddress": {"name": name, "address": "ap@vendor.com"}},
        "_klass_hint": klass_hint,
    }


def _fake_parse(path, *, subject="", from_name="", from_address=""):
    number = "UNKNOWN"
    for token in (
        "FIRST",
        "SECOND",
        "THIRD",
        "FOURTH",
        "FIFTH",
        "SIXTH",
        "SEVENTH",
        "EIGHTH",
        "NINTH",
        "TENTH",
        "ELEVENTH",
        "TWELFTH",
        "EXTRA",
    ):
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


def test_clamp_email_limit_hard_caps_at_ten():
    assert HARD_EMAIL_CAP == 10
    assert clamp_email_limit(None) == 10
    assert clamp_email_limit(1) == 1
    assert clamp_email_limit(10) == 10
    assert clamp_email_limit(30) == 10
    assert clamp_email_limit(50) == 10
    assert clamp_email_limit(0) == 1


def test_noise_consumes_email_cap_does_not_walk_to_more_bills(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        _msg("m-statement", "Monthly Account Statement", "2026-08-16T12:00:00Z", name="Bank"),
        _msg("m-payment", "Payment confirmation — thank you", "2026-08-16T13:00:00Z", name="PayCo"),
        _msg("m-bill-ok", "Invoice FIRST", "2026-08-16T16:00:00Z", name="Fastenal Company"),
        _msg("m-bill-two", "Invoice SECOND", "2026-08-16T17:00:00Z", name="EMJ"),
    ]
    graph = _FakeGraph(
        messages,
        {
            "m-statement": {"names": ["statement.pdf"], "pdfs": [("statement.pdf", b"%PDF")]},
            "m-payment": {"names": ["payment.pdf"], "pdfs": [("payment.pdf", b"%PDF")]},
            "m-bill-ok": {"names": ["Invoice-FIRST.pdf"], "pdfs": [("Invoice-FIRST.pdf", b"%PDF")]},
            "m-bill-two": {"names": ["Invoice-SECOND.pdf"], "pdfs": [("Invoice-SECOND.pdf", b"%PDF")]},
        },
    )
    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = _fake_parse
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

    assert selected == []
    assert {s.get("class") for s in skipped} >= {"statement", "payment"}
    assert "FIRST" not in [inv.get("invoice_number") for inv in selected]
    skip_rows = skip_rows_for_report(skipped, "API Agent - 9/11/26")
    assert len(skip_rows) == 2
    assert all(counts_toward_email_cap(row["Result"]) for row in skip_rows)
    assert all(is_noise_result(row["Result"]) for row in skip_rows)
    assert all("bill-vs-noise" in row["Why"] for row in skip_rows)


def test_hard_email_cap_stops_after_10_messages_mix_of_noise_and_bills(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        _msg("m-statement", "Monthly Account Statement", "2026-08-16T12:00:00Z", name="Bank"),
        _msg("m-payment", "Payment confirmation — thank you", "2026-08-16T12:10:00Z", name="PayCo"),
        _msg("m-check", "CHECK STOP Gas and Supply", "2026-08-16T12:20:00Z", name="Gas and Supply"),
        _msg("m-pod", "POD for shipment 99", "2026-08-16T12:30:00Z", name="ShipCo"),
        _msg("m-not-a-bill", "Internal only — do not process", "2026-08-16T12:40:00Z", name="IT"),
        _msg("m-bill-1", "Invoice FIRST", "2026-08-16T13:00:00Z", name="Fastenal Company"),
        _msg("m-bill-2", "Invoice SECOND", "2026-08-16T13:10:00Z", name="EMJ"),
        _msg("m-bill-3", "Invoice THIRD", "2026-08-16T13:20:00Z", name="McMaster-Carr"),
        _msg("m-bill-4", "Invoice FOURTH", "2026-08-16T13:30:00Z", name="Telecom Products Inc."),
        _msg("m-bill-5", "Invoice FIFTH", "2026-08-16T13:40:00Z", name="Air Products"),
        _msg("m-bill-6", "Invoice SIXTH", "2026-08-16T14:00:00Z", name="O'Neal Steel"),
        _msg("m-bill-7", "Invoice SEVENTH", "2026-08-16T14:10:00Z", name="MSC Industrial"),
    ]
    pdfs = {}
    for msg in messages:
        mid = msg["id"]
        if mid.startswith("m-bill"):
            token = msg["subject"].split()[-1]
            pdfs[mid] = {"names": [f"Invoice-{token}.pdf"], "pdfs": [(f"Invoice-{token}.pdf", b"%PDF")]}
        elif mid == "m-statement":
            pdfs[mid] = {"names": ["statement.pdf"], "pdfs": [("statement.pdf", b"%PDF")]}
        elif mid == "m-payment":
            pdfs[mid] = {"names": ["payment.pdf"], "pdfs": [("payment.pdf", b"%PDF")]}
        elif mid == "m-check":
            pdfs[mid] = {"names": ["checkstop.pdf"], "pdfs": [("checkstop.pdf", b"%PDF")]}
        elif mid == "m-pod":
            pdfs[mid] = {"names": ["pod-99.pdf"], "pdfs": [("pod-99.pdf", b"%PDF")]}
        else:
            pdfs[mid] = {"names": ["note.pdf"], "pdfs": [("note.pdf", b"%PDF")]}
    graph = _FakeGraph(messages, pdfs)

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = _fake_parse
    try:
        selected, skipped = pull_recent_bills(
            graph,
            limit=30,
            pdf_dir=tmp_path / "pdfs",
            fifo=True,
            unprocessed_only=True,
            mark_skips=True,
            max_messages=50,
        )
    finally:
        inbox_mod.parse_invoice_pdf = orig

    numbers = [inv["invoice_number"] for inv in selected]
    assert numbers == ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH"]
    assert "SIXTH" not in numbers
    assert "SEVENTH" not in numbers
    assert {s.get("class") for s in skipped} >= {"statement", "payment", "check_stop", "pod", "internal"}
    touched_ids = {inv["graph_message_id"] for inv in selected} | {
        s["graph_message_id"] for s in skipped if s.get("class") != "already-flagged"
    }
    assert len(touched_ids) == HARD_EMAIL_CAP
    assert graph.named.count("m-bill-6") == 0
    assert "m-bill-6" not in graph.downloaded

    skip_rows = skip_rows_for_report(skipped, "API Agent - 9/11/26")
    assert skip_rows
    assert all(row["Result"] == RESULT_SKIPPED for row in skip_rows)
    assert all(row["Flag in Outlook"] == "No" for row in skip_rows)
    assert all(counts_toward_email_cap(row["Result"]) for row in skip_rows)
    assert all(is_noise_result(row["Result"]) for row in skip_rows)
    assert any("Monthly Account Statement" in row["Why"] for row in skip_rows)


def test_already_flagged_walked_past_without_consuming_cap(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        _msg(
            "m-entered",
            "Invoice DONE-AI",
            "2026-08-16T10:00:00Z",
            name="Done Co",
            categories=[ENTERED_IN_AI_CATEGORY],
        ),
        _msg(
            "m-hold",
            "Invoice DONE-HOLD",
            "2026-08-16T10:10:00Z",
            name="Hold Co",
            categories=[AI_HOLD_CATEGORY],
        ),
        _msg(
            "m-issues",
            "Invoice DONE-ISSUES",
            "2026-08-16T10:20:00Z",
            name="Issues Co",
            categories=[ENTERED_WITH_ISSUES_CATEGORY],
        ),
        _msg(
            "m-followup",
            "Invoice DONE-FLAG",
            "2026-08-16T10:30:00Z",
            name="Flag Co",
            flag_status="flagged",
        ),
        _msg("m-noise", "Monthly Account Statement", "2026-08-16T11:00:00Z", name="Bank"),
        _msg("m-bill", "Invoice FIRST", "2026-08-16T12:00:00Z", name="Fastenal Company"),
        _msg("m-extra", "Invoice EXTRA", "2026-08-16T13:00:00Z", name="EMJ"),
    ]
    graph = _FakeGraph(
        messages,
        {
            "m-entered": {"names": ["Invoice-DONE-AI.pdf"], "pdfs": [("Invoice-DONE-AI.pdf", b"%PDF")]},
            "m-hold": {"names": ["Invoice-DONE-HOLD.pdf"], "pdfs": [("Invoice-DONE-HOLD.pdf", b"%PDF")]},
            "m-issues": {"names": ["Invoice-DONE-ISSUES.pdf"], "pdfs": [("Invoice-DONE-ISSUES.pdf", b"%PDF")]},
            "m-followup": {"names": ["Invoice-DONE-FLAG.pdf"], "pdfs": [("Invoice-DONE-FLAG.pdf", b"%PDF")]},
            "m-noise": {"names": ["statement.pdf"], "pdfs": [("statement.pdf", b"%PDF")]},
            "m-bill": {"names": ["Invoice-FIRST.pdf"], "pdfs": [("Invoice-FIRST.pdf", b"%PDF")]},
            "m-extra": {"names": ["Invoice-EXTRA.pdf"], "pdfs": [("Invoice-EXTRA.pdf", b"%PDF")]},
        },
    )
    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = _fake_parse
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

    assert [inv["invoice_number"] for inv in selected] == ["FIRST"]
    assert "EXTRA" not in [inv["invoice_number"] for inv in selected]
    flagged_skips = [s for s in skipped if s.get("class") == "already-flagged"]
    assert {s["graph_message_id"] for s in flagged_skips} == {
        "m-entered",
        "m-hold",
        "m-issues",
        "m-followup",
    }
    assert graph.held == []
    for mid in ("m-entered", "m-hold", "m-issues", "m-followup"):
        assert mid not in graph.named
        assert mid not in graph.downloaded
    skip_rows = skip_rows_for_report(skipped, "API Agent - 9/11/26")
    assert all(row.get("graph_message_id") != "m-entered" for row in skip_rows)
    assert any("Monthly Account Statement" in row["Why"] for row in skip_rows)


def test_is_already_flagged_helpers():
    assert is_already_flagged({"categories": [ENTERED_IN_AI_CATEGORY]})
    assert is_already_flagged({"categories": [AI_HOLD_CATEGORY]})
    assert is_already_flagged({"categories": [ENTERED_WITH_ISSUES_CATEGORY]})
    assert is_already_flagged({"flag": {"flagStatus": "flagged"}})
    assert is_already_flagged({"flag": {"flagStatus": "Flagged"}})
    assert not is_already_flagged({"categories": [], "flag": {"flagStatus": "notFlagged"}})
    assert not is_already_flagged({"categories": ["Purchasing Investigating"]})
    assert not has_followup_flagged({"flag": {"flagStatus": "notFlagged"}})
    assert has_followup_flagged({"flag": {"flagStatus": "flagged"}})


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


def test_price_mismatch_still_holds_and_is_outlook_entered_with_issues():
    created = []

    class FakeKimco:
        target = "live"

        def create(self, service, values):
            created.append(values)
            return 9953, {"id": 9953, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

    class FakeGraph:
        def __init__(self):
            self.held = []
            self.issues = []

        def flag_hold(self, mailbox, message_id):
            raise AssertionError("header+PDF price HOLD must not get AI HOLD")

        def flag_matched(self, mailbox, message_id):
            raise AssertionError("bill HOLD must not get Entered in AI")

        def flag_issues(self, mailbox, message_id):
            self.issues.append(message_id)
            return FLAG_ENTERED_WITH_ISSUES

    graph = FakeGraph()
    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Earle M. Jorgensen Company",
            "invoice_number": "S813859432",
            "date": "2026-08-18",
            "po": "58000",
            "amount": 1164.32,
            "lines": [{"part": "STEEL", "amount": 965.00}],
            "graph_message_id": "AAMk-emj",
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        batch={"id": 703},
        batch_label="API Agent - 9/9/26 (703)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 208, "vendor_text": "EMJ", "invoice_id": 9, "po_text": ""}],
        po_index={
            "58000": {
                "id": 4,
                "text": "58000-EMJ",
                "vendor_id": 208,
                "lines": [{"part": "STEEL", "amount": 1164.32, "unit_price": 1164.32, "qty": 1}],
            }
        },
        pdf_dir=None,
        graph_client=graph,
        flag_outlook=True,
    )
    assert row["Result"] == RESULT_HOLD
    assert row["KIMCO id"] == 9953
    assert created
    assert row["Flag in Outlook"] == "Yes"
    assert row["Flag status"] == FLAG_ENTERED_WITH_ISSUES
    assert graph.issues == ["AAMk-emj"]
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
