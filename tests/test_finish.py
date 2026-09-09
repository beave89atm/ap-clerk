"""API finish of existing Incomplete headers. No live Graph or KIMCO I/O."""

from __future__ import annotations

from pathlib import Path

from datetime import date

from ap_clerk.finish import (
    DRY_SUBJECT,
    KIND_FINISH_UP,
    apply_grouped_outlook_flags,
    dry_email_body,
    dry_subject_for,
    finish_existing_header,
    grouped_flag_status_for_message,
)
from ap_clerk.gates import RESULT_INCOMPLETE, RESULT_SUCCESS
from ap_clerk.graph import FLAG_AI_HOLD, FLAG_FLAGGED
from ap_clerk.kimco import KimcoError


class FakeKimco:
    target = "live"

    def __init__(
        self,
        *,
        lines=None,
        attachments=None,
        attach="attached",
        select="selected",
    ):
        self.lines = lines if lines is not None else []
        self.attachments = attachments if attachments is not None else []
        self.attach = attach
        self.select = select
        self.selected = []
        self.attached = []
        self.got = []

    def get_item(self, service, item_id):
        self.got.append((service, item_id))
        return {
            "id": item_id,
            "lists": {"APInvoiceLine": list(self.lines)},
            "values": {"Lines_Count": len(self.lines)},
        }

    def list_attachments(self, invoice_id):
        return list(self.attachments)

    def try_official_attach(self, invoice_id, **kwargs):
        self.attached.append((invoice_id, kwargs.get("name")))
        return self.attach

    def try_select_receipts(self, invoice_id, receipt_ids=None):
        self.selected.append((invoice_id, list(receipt_ids or [])))
        return self.select


def _incomplete_row(**overrides):
    row = {
        "Vendor": "Orthman Conveying Systems",
        "Invoice #": "701684",
        "date": "2026-08-14",
        "PO": "58636",
        "Amount": 1242.94,
        "Result": RESULT_INCOMPLETE,
        "Why": "Incomplete (finish): header created (id 9931).",
        "KIMCO id": 9931,
        "Batch": "API Agent - 9/8/26 (701)",
        "Fees and surcharges": "none",
        "PPV": "none",
        "Attach status": "blocked-405",
        "Flag status": "ai-hold",
        "Flag in Outlook": "Yes",
        "Notes": "",
    }
    row.update(overrides)
    return row


def _inv(**overrides):
    inv = {
        "vendor": "Orthman Conveying Systems",
        "invoice_number": "701684",
        "po": "58636",
        "pos": ["58636"],
        "amount": 1242.94,
        "date": "2026-08-14",
        "graph_message_id": "AAMk-orthman",
        "lines": [],
        "multi_po": False,
        "fees": [],
    }
    inv.update(overrides)
    return inv


def test_already_finished_header_is_success_without_rewrites():
    kimco = FakeKimco(
        lines=[{"values": {"Receipt": {"id": 23879}}}],
        attachments=[{"name": "Inv_701684.pdf"}],
    )
    row = finish_existing_header(kimco, _incomplete_row(), _inv(), receipts=[], flag_outlook=False)
    assert row["Result"] == RESULT_SUCCESS
    assert row["Attach status"] == "attached"
    assert row["kind"] == KIND_FINISH_UP
    assert row["Notes"] == ""
    assert kimco.attached == []
    assert kimco.selected == []
    assert "9931" in row["Why"]


def test_missing_attach_and_receipts_calls_api_finish(tmp_path: Path):
    pdf = tmp_path / "701684.pdf"
    pdf.write_bytes(b"%PDF-1.4 finished")
    kimco = FakeKimco(lines=[], attachments=[], attach="attached", select="selected")
    row = finish_existing_header(
        kimco,
        _incomplete_row(),
        _inv(pdf_path=str(pdf)),
        receipts=[{"id": 23879, "slip": "701684", "po": "58636", "part": "1007038-1", "qty": 24}],
        pdf_dir=tmp_path,
        flag_outlook=False,
    )
    assert row["Result"] == RESULT_SUCCESS
    assert kimco.attached == [(9931, "701684.pdf")]
    assert kimco.selected == [(9931, [23879])]


def test_blocked_attach_stays_incomplete_not_success(tmp_path: Path):
    pdf = tmp_path / "1471255.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    kimco = FakeKimco(lines=[], attachments=[], attach="blocked-405", select="selected")
    row = finish_existing_header(
        kimco,
        _incomplete_row(
            Vendor="RMP Industrial Supply Inc",
            **{"Invoice #": "1471255"},
            PO="",
            **{"KIMCO id": 9927},
        ),
        _inv(vendor="RMP", invoice_number="1471255", po=None, pos=[], pdf_path=str(pdf)),
        receipts=[],
        pdf_dir=tmp_path,
        flag_outlook=False,
    )
    assert row["Result"] == RESULT_INCOMPLETE
    assert row["Result"] != RESULT_SUCCESS
    assert "finish" in row["Why"]


def test_nopo_header_succeeds_with_pdf_only(tmp_path: Path):
    pdf = tmp_path / "LS-8507.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    kimco = FakeKimco(lines=[], attachments=[], attach="attached")
    row = finish_existing_header(
        kimco,
        _incomplete_row(
            Vendor="PCT Support",
            **{"Invoice #": "LS-8507"},
            PO="",
            Amount=1310.0,
            **{"KIMCO id": 9929},
        ),
        _inv(vendor="PCT Support", invoice_number="LS-8507", po=None, pos=[], pdf_path=str(pdf)),
        receipts=[],
        pdf_dir=tmp_path,
        flag_outlook=False,
    )
    assert row["Result"] == RESULT_SUCCESS
    assert kimco.selected == []


def test_get_failure_is_incomplete_not_void():
    class Boom(FakeKimco):
        def get_item(self, service, item_id):
            raise KimcoError("GET failed")

    row = finish_existing_header(Boom(), _incomplete_row(), _inv(), flag_outlook=False)
    assert row["Result"] == RESULT_INCOMPLETE
    assert row["KIMCO id"] == 9931


def test_grouped_flags_shared_message_stays_hold_unless_all_success():
    class FakeGraph:
        def __init__(self):
            self.matched = []
            self.held = []

        def flag_matched(self, mailbox, message_id):
            self.matched.append(message_id)
            return FLAG_FLAGGED

        def flag_hold(self, mailbox, message_id):
            self.held.append(message_id)
            return FLAG_AI_HOLD

        def get_message(self, mailbox, message_id, select="id"):
            return {"id": message_id, "categories": []}

    mixed = [
        {"Invoice #": "TXFT4100045", "Result": RESULT_SUCCESS, "KIMCO id": 9924, "Why": ""},
        {"Invoice #": "TXFT499945", "Result": RESULT_INCOMPLETE, "KIMCO id": 9930, "Why": ""},
    ]
    invoices = [
        {"invoice_number": "TXFT4100045", "graph_message_id": "AAMk-fastenal"},
        {"invoice_number": "TXFT499945", "graph_message_id": "AAMk-fastenal"},
    ]
    graph = FakeGraph()
    apply_grouped_outlook_flags(mixed, invoices, graph)
    assert graph.held == ["AAMk-fastenal"]
    assert graph.matched == []
    assert mixed[0]["Flag status"] == FLAG_AI_HOLD
    assert grouped_flag_status_for_message(mixed) == FLAG_AI_HOLD

    both_ok = [
        {"Invoice #": "TXFT4100045", "Result": RESULT_SUCCESS, "KIMCO id": 9924, "Why": ""},
        {"Invoice #": "TXFT499945", "Result": RESULT_SUCCESS, "KIMCO id": 9930, "Why": ""},
    ]
    graph2 = FakeGraph()
    apply_grouped_outlook_flags(both_ok, invoices, graph2)
    assert graph2.matched == ["AAMk-fastenal"]
    assert grouped_flag_status_for_message(both_ok) == FLAG_FLAGGED


def test_resolve_message_id_searches_when_stored_id_404s():
    from ap_clerk.finish import resolve_message_id
    from ap_clerk.graph import GraphError

    class FakeGraph:
        def get_message(self, mailbox, message_id, select="id"):
            raise GraphError("Graph GET message HTTP 404")

        def search_messages(self, mailbox, needle, top=8):
            assert needle == "701684"
            return [
                {"id": "AAMk-report", "subject": "AP dry run 10 — quality V1.1 (API finish)"},
                {
                    "id": "AAMk-live",
                    "subject": "Invoice 701684 from Orthman Conveying Systems",
                    "bodyPreview": "",
                    "attachment_names": [],
                },
            ]

    found = resolve_message_id(
        FakeGraph(),
        {
            "invoice_number": "701684",
            "vendor": "Orthman Conveying Systems",
            "graph_message_id": "AAMk-stale",
        },
    )
    assert found == "AAMk-live"


def test_dry_email_names_api_finish_and_not_daily_30():
    body = dry_email_body(
        [
            {"Result": "Success", "kind": KIND_FINISH_UP},
            {"Result": "Incomplete", "kind": KIND_FINISH_UP},
            {"Result": "Fail"},
            {"Result": "HOLD"},
        ],
        batch_label="API Agent - 9/8/26 (701)",
    )
    assert DRY_SUBJECT.startswith("AP dry run 10")
    assert dry_subject_for(date(2026, 8, 16)) == "AP dry run 10 — from 2026-08-16"
    assert dry_subject_for(None) == DRY_SUBJECT
    assert "not the weekday daily 30" in body
    assert "API-only finish" in body
    assert "KIMCO UI was not opened" in body
    assert "Success: 1" in body
    assert "Incomplete: 1" in body
    assert "Finish-ups" in body
    assert "accountspayable@kannonmfg.com" in body
    from_body = dry_email_body(
        [{"Result": "Success", "kind": "new"}],
        batch_label="API Agent - 9/9/26 (702)",
        from_date=date(2026, 8, 16),
    )
    assert "2026-08-16" in from_body
    assert "do not restart at 7/28" in from_body
