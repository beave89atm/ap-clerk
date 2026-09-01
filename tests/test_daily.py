"""Daily FIFO queue, cursor, and email-denied. No live Graph or KIMCO I/O."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from ap_clerk.cli import main
from ap_clerk.cursor import (
    DAILY_START_DATE,
    DailyCursor,
    cursor_after_messages,
    daily_floor_datetime,
    load_cursor,
    save_cursor,
    should_skip_already_seen,
)
from ap_clerk.daily import DEFAULT_DAILY_LIMIT, email_body_for, email_subject_for
from ap_clerk.graph import ALLOWED_MAILBOX, EMAIL_DENIED, ENTERED_IN_AI_CATEGORY, default_report_to
from ap_clerk.inbox import pull_recent_bills


def test_daily_floor_is_july_28_2026_chicago():
    assert DAILY_START_DATE == date(2026, 7, 28)
    floor = daily_floor_datetime()
    assert floor.year == 2026 and floor.month == 7 and floor.day == 28
    assert str(floor.tzinfo) == "America/Chicago"
    assert DEFAULT_DAILY_LIMIT == 30


def test_cursor_persists_and_skips_last_message(tmp_path: Path):
    path = tmp_path / "daily-cursor.json"
    save_cursor(
        DailyCursor(
            last_receivedDateTime="2026-08-05T14:22:00Z",
            last_message_id="AAMk-last",
            last_run_date="2026-08-28",
            processed_count=30,
        ),
        path,
    )
    loaded = load_cursor(path)
    assert loaded.last_message_id == "AAMk-last"
    assert loaded.after_received() is not None
    assert should_skip_already_seen({"id": "AAMk-last", "receivedDateTime": "2026-08-05T14:22:00Z"}, loaded)
    assert not should_skip_already_seen({"id": "AAMk-next", "receivedDateTime": "2026-08-05T15:00:00Z"}, loaded)
    advanced = cursor_after_messages(
        [
            {"id": "AAMk-a", "receivedDateTime": "2026-08-05T15:00:00Z"},
            {"id": "AAMk-b", "receivedDateTime": "2026-08-06T10:00:00Z"},
        ],
        as_of=date(2026, 8, 29),
        batch="API Agent - 8/29/26 (1)",
    )
    assert advanced.last_message_id == "AAMk-b"
    assert advanced.last_receivedDateTime == "2026-08-06T10:00:00Z"


def test_email_subject_and_body_counts():
    subject = email_subject_for(date(2026, 8, 28))
    assert subject == "AP run 2026-08-28"
    body = email_body_for(
        [
            {"Result": "Success"},
            {"Result": "Success"},
            {"Result": "Fail"},
            {"Result": "HOLD"},
        ],
        batch_label="API Agent - 8/28/26 (700)",
        as_of=date(2026, 8, 28),
    )
    assert "Success: 2" in body
    assert "Fail: 1" in body
    assert "HOLD: 1" in body
    assert "API Agent - 8/28/26 (700)" in body
    assert "accountspayable@kannonmfg.com" in body


class _FakeGraph:
    def __init__(self, messages, pdfs_by_id):
        self.messages = messages
        self.pdfs_by_id = pdfs_by_id
        self.held: list[str] = []
        self.list_kwargs: dict = {}

    def list_messages(self, mailbox, **kwargs):
        assert mailbox == ALLOWED_MAILBOX
        self.list_kwargs = kwargs
        return list(self.messages)

    def list_attachment_names(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("names") or [])

    def download_pdf_attachments(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("pdfs") or [])

    def flag_hold(self, mailbox, message_id):
        assert mailbox == ALLOWED_MAILBOX
        self.held.append(message_id)
        return "ai-hold"


def test_daily_fifo_from_july_28_skips_entered_in_ai_and_replaces_not_a_bill(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        {
            "id": "m-too-old-would-be-ignored-by-filter",
            "subject": "Invoice OLD-BEFORE",
            "receivedDateTime": "2026-07-20T18:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Old Vendor", "address": "ap@old.com"}},
        },
        {
            "id": "m-entered",
            "subject": "Invoice DONE1",
            "receivedDateTime": "2026-07-28T14:00:00Z",
            "hasAttachments": True,
            "categories": [ENTERED_IN_AI_CATEGORY],
            "from": {"emailAddress": {"name": "Done Co", "address": "ap@done.com"}},
        },
        {
            "id": "m-statement",
            "subject": "Monthly Account Statement",
            "receivedDateTime": "2026-07-29T14:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Vendor", "address": "ap@vendor.com"}},
        },
        {
            "id": "m-first-bill",
            "subject": "Invoice FIRST",
            "receivedDateTime": "2026-07-30T14:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Fastenal Company", "address": "billing@fastenal.com"}},
        },
        {
            "id": "m-second-bill",
            "subject": "Invoice SECOND",
            "receivedDateTime": "2026-08-01T14:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "McMaster-Carr", "address": "ar@mcmaster.com"}},
        },
        {
            "id": "m-newest-not-taken",
            "subject": "Invoice NEWEST",
            "receivedDateTime": "2026-08-20T14:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Telecom Products Inc.", "address": "ar@telecom.com"}},
        },
    ]
    graph = _FakeGraph(
        messages,
        {
            "m-too-old-would-be-ignored-by-filter": {
                "names": ["Invoice-OLD.pdf"],
                "pdfs": [("Invoice-OLD.pdf", b"%PDF")],
            },
            "m-entered": {"names": ["Invoice-DONE1.pdf"], "pdfs": [("Invoice-DONE1.pdf", b"%PDF")]},
            "m-statement": {"names": ["statement.pdf"], "pdfs": [("statement.pdf", b"%PDF")]},
            "m-first-bill": {"names": ["Invoice-FIRST.pdf"], "pdfs": [("Invoice-FIRST.pdf", b"%PDF")]},
            "m-second-bill": {"names": ["Invoice-SECOND.pdf"], "pdfs": [("Invoice-SECOND.pdf", b"%PDF")]},
            "m-newest-not-taken": {"names": ["Invoice-NEWEST.pdf"], "pdfs": [("Invoice-NEWEST.pdf", b"%PDF")]},
        },
    )

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        number = "UNKNOWN"
        for token in ("FIRST", "SECOND", "NEWEST", "DONE1", "OLD"):
            if token in path.name or token in subject:
                number = token
                break
        return {
            "vendor": from_name or "Vendor",
            "invoice_number": number,
            "date": "2026-07-30",
            "po": None,
            "pos": [],
            "amount": 10.0,
            "fees": [],
            "check_stop": False,
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

    assert graph.list_kwargs.get("oldest_first") is True
    assert [inv["invoice_number"] for inv in selected] == ["FIRST", "SECOND"]
    assert "NEWEST" not in [inv["invoice_number"] for inv in selected]
    assert "DONE1" not in [inv["invoice_number"] for inv in selected]
    assert any(s.get("class") == "statement" for s in skipped)
    assert "m-statement" in graph.held
    assert str(selected[0]["receivedDateTime"]) < str(selected[1]["receivedDateTime"])


def test_cursor_continues_after_previous_thirty(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        {
            "id": "AAMk-last",
            "subject": "Invoice LAST",
            "receivedDateTime": "2026-08-05T14:22:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Last Co", "address": "ap@last.com"}},
        },
        {
            "id": "AAMk-next",
            "subject": "Invoice NEXT",
            "receivedDateTime": "2026-08-06T10:00:00Z",
            "hasAttachments": True,
            "categories": [],
            "from": {"emailAddress": {"name": "Next Co", "address": "ap@next.com"}},
        },
    ]
    graph = _FakeGraph(
        messages,
        {
            "AAMk-last": {"names": ["Invoice-LAST.pdf"], "pdfs": [("Invoice-LAST.pdf", b"%PDF")]},
            "AAMk-next": {"names": ["Invoice-NEXT.pdf"], "pdfs": [("Invoice-NEXT.pdf", b"%PDF")]},
        },
    )

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        number = "NEXT" if "NEXT" in path.name or "NEXT" in subject else "LAST"
        return {
            "vendor": from_name or "Vendor",
            "invoice_number": number,
            "date": "2026-08-06",
            "po": None,
            "pos": [],
            "amount": 10.0,
            "fees": [],
            "check_stop": False,
            "pdf_text_empty": False,
        }

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, _skipped = pull_recent_bills(
            graph,
            limit=2,
            pdf_dir=tmp_path / "pdfs",
            fifo=True,
            unprocessed_only=True,
            cursor=DailyCursor(last_receivedDateTime="2026-08-05T14:22:00Z", last_message_id="AAMk-last"),
        )
    finally:
        inbox_mod.parse_invoice_pdf = orig
    assert [inv["invoice_number"] for inv in selected] == ["NEXT"]


def test_probe_cli_writes_json_and_never_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MICROSOFT_GRAPH_TENANT_ID", "tenant")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "secret")
    out = tmp_path / "graph-send-probe.json"

    class FakeGraph:
        def ensure_ai_hold_category(self, mailbox):
            assert mailbox == ALLOWED_MAILBOX
            return "category-created"

        def probe_send_authorization(self, mailbox):
            assert mailbox == ALLOWED_MAILBOX
            return {
                "mailbox": mailbox,
                "mail_send_role": True,
                "draft_status": "email-draft-ok",
                "draft_http": 201,
                "draft_deleted_http": 204,
                "send_mail_invoked": False,
                "other_mailboxes_used": [],
            }

        def send_run_report(self, *args, **kwargs):
            raise AssertionError("probe must not sendMail")

    with patch("ap_clerk.cli.GraphClient.authenticate", return_value=FakeGraph()):
        code = main(["probe", "--out", str(out), "--as-of", "2026-08-28"])
    assert code == 0
    text = capsys.readouterr().out
    assert "send_mail_invoked=false" in text
    assert "No mail was sent" in text
    payload = json.loads(out.read_text())
    assert payload["send_mail_invoked"] is False
    assert payload["mail_sent_to_anyone"] is False
    assert payload["ai_hold_category"] == "category-created"


def test_daily_refuses_without_live(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["daily", "--limit", "30"])
    assert code == 2
    out = capsys.readouterr().out
    assert "requires --live" in out
    assert "secret" not in out.lower()


def test_daily_sendmail_403_writes_xlsx_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (
        "KIMCO_PROTOTYPE_API_KEY",
        "KIMCO_PROTOTYPE_API_PASSWORD",
        "KIMCO_API_KEY",
        "KIMCO_API_PASSWORD",
        "KIMCO_LIVE_API_KEY",
        "KIMCO_LIVE_API_PASSWORD",
        "KIMCO_LIVE_INSTANCE_URL",
        "KIMCO_TARGET",
        "MICROSOFT_GRAPH_TENANT_ID",
        "MICROSOFT_GRAPH_CLIENT_ID",
        "MICROSOFT_GRAPH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KIMCO_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("KIMCO_LIVE_API_PASSWORD", "live-pw")
    monkeypatch.setenv("MICROSOFT_GRAPH_TENANT_ID", "tenant")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_ID", "client")
    monkeypatch.setenv("MICROSOFT_GRAPH_CLIENT_SECRET", "secret")

    report = tmp_path / "AP-run-2026-08-28.xlsx"
    cursor = tmp_path / "daily-cursor.json"

    class FakeGraph:
        def ensure_ai_hold_category(self, mailbox):
            assert mailbox == ALLOWED_MAILBOX
            return "category-denied"

        def send_run_report(self, mailbox, **kwargs):
            assert mailbox == ALLOWED_MAILBOX
            assert kwargs["to"] == default_report_to()
            return EMAIL_DENIED

    class FakeKimco:
        target = "live"

    def fake_enter(client, invoices, **kwargs):
        assert client.target == "live"
        assert kwargs.get("flag_outlook") is True
        return [
            {
                "Vendor": "Fastenal Company",
                "Invoice #": "TXFT1",
                "date": "2026-08-01",
                "PO": "58749",
                "Amount": 10,
                "Result": "Success",
                "Why": "Header created",
                "KIMCO id": 100,
                "Batch": "API Agent - 8/28/26 (1)",
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "no-pdf-on-vm",
                "Flag in Outlook": "Yes",
                "Flag status": "entered-in-ai",
            }
        ]

    with patch("ap_clerk.cli._optional_graph_client", return_value=FakeGraph()):
        with patch(
            "ap_clerk.cli.pull_recent_bills",
            return_value=(
                [
                    {
                        "vendor": "Fastenal Company",
                        "invoice_number": "TXFT1",
                        "date": "2026-08-01",
                        "po": "58749",
                        "amount": 10,
                        "graph_message_id": "AAMk-1",
                        "receivedDateTime": "2026-08-01T12:00:00Z",
                    }
                ],
                [],
            ),
        ):
            with patch("ap_clerk.cli.KimcoClient.authenticate", return_value=FakeKimco()):
                with patch("ap_clerk.cli.run_enter", side_effect=fake_enter):
                    code = main(
                        [
                            "daily",
                            "--live",
                            "--limit",
                            "30",
                            "--as-of",
                            "2026-08-28",
                            "--report",
                            str(report),
                            "--cursor",
                            str(cursor),
                        ]
                    )
    assert code == 0
    out = capsys.readouterr().out
    assert "email-denied" in out
    assert report.exists()
    assert cursor.exists()
    assert (tmp_path / "AP-run-2026-08-28.xlsx.email.json").exists()
    assert "token" not in out.lower() or "token not printed" in out.lower()
    loaded = load_cursor(cursor)
    assert loaded.last_message_id == "AAMk-1"


def test_cursor_from_run_keeps_prior_position_and_accumulates():
    from ap_clerk.daily import cursor_from_run

    previous = DailyCursor(
        last_receivedDateTime="2026-07-30T06:29:03Z",
        last_message_id="AAMk-marmon",
        last_run_date="2026-08-31",
        last_batch="API Agent - 8/31/26 (689)",
        processed_count=70,
    )
    empty = cursor_from_run([], [], as_of=date(2026, 9, 1), batch="API Agent - 9/1/26", previous=previous)
    assert empty.last_message_id == "AAMk-marmon"
    assert empty.last_receivedDateTime == "2026-07-30T06:29:03Z"
    assert empty.processed_count == 70

    advanced = cursor_from_run(
        [{"graph_message_id": "AAMk-next", "receivedDateTime": "2026-07-30T07:00:00Z"}],
        [{"graph_message_id": "AAMk-skip", "receivedDateTime": "2026-07-30T06:45:00Z"}],
        as_of=date(2026, 9, 1),
        batch="API Agent - 9/1/26 (690)",
        previous=previous,
    )
    assert advanced.last_message_id == "AAMk-next"
    assert advanced.last_receivedDateTime == "2026-07-30T07:00:00Z"
    assert advanced.processed_count == 72
