"""Outlook flag-after-match. No live Graph or KIMCO I/O in these tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from ap_clerk.cli import _process_invoice, main
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FLAG_DENIED,
    FLAG_FLAGGED,
    FLAG_NO_MESSAGE_ID,
    FLAG_SKIPPED,
    GraphClient,
    MailboxRejected,
    apply_flag_after_match,
    assert_allowed_mailbox,
    attach_message_ids,
    decide_flag_status,
)


WRONG_MAILBOX = "someone-else@kannonmfg.com"


def test_cli_rejects_wrong_mailbox(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["enter", "--mailbox", WRONG_MAILBOX, "--as-of", "2026-08-27"])
    assert code == 2
    out = capsys.readouterr().out
    assert "accountspayable@kannonmfg.com" in out
    assert WRONG_MAILBOX in out or "Refusing mailbox" in out
    assert "secret" not in out.lower()


def test_wrong_mailbox_rejected():
    with pytest.raises(MailboxRejected, match="accountspayable@kannonmfg.com"):
        assert_allowed_mailbox(WRONG_MAILBOX)
    with pytest.raises(MailboxRejected):
        assert_allowed_mailbox("kyle.cleaver@yahoo.com")
    assert assert_allowed_mailbox(ALLOWED_MAILBOX) == ALLOWED_MAILBOX
    assert assert_allowed_mailbox("AccountsPayable@KannonMfg.com") == ALLOWED_MAILBOX


def test_wrong_mailbox_flag_never_sends_http():
    client = GraphClient("token-not-printed")
    client.request = Mock(side_effect=AssertionError("Graph HTTP must not run for a rejected mailbox"))
    with pytest.raises(MailboxRejected, match="accountspayable@kannonmfg.com"):
        client.flag_matched(WRONG_MAILBOX, "AAMk-message-id")
    client.request.assert_not_called()


def test_success_flags_source_message():
    patches: list[dict] = []

    def fake_request(method, url, **kwargs):
        assert ALLOWED_MAILBOX in url
        assert WRONG_MAILBOX not in url
        resp = Mock()
        resp.status_code = 200
        if method == "PATCH":
            patches.append(kwargs.get("json") or {})
            resp.json.return_value = {"flag": {"flagStatus": "flagged"}}
        else:
            resp.json.return_value = {"id": "AAMk-success", "categories": [], "flag": {"flagStatus": "flagged"}}
        return resp

    client = GraphClient("token-not-printed")
    client.request = fake_request
    row = {
        "Result": "Success",
        "KIMCO id": 9499,
        "Why": "Header created.",
    }
    invoice = {"graph_message_id": "AAMk-success", "invoice_number": "27756"}
    status = apply_flag_after_match(row, invoice, client)
    assert status == FLAG_FLAGGED
    assert row["Flag status"] == FLAG_FLAGGED
    assert "Flag status=flagged" in row["Why"]
    assert {"flag": {"flagStatus": "flagged"}} in patches
    assert any("AP Matched" in (body.get("categories") or []) for body in patches)


def test_hold_does_not_flag():
    client = GraphClient("token-not-printed")
    client.flag_matched = Mock(side_effect=AssertionError("HOLD must not flag"))
    row = {
        "Result": "HOLD",
        "KIMCO id": "",
        "Why": "HOLD: CHECK STOP. Do not create a header.",
    }
    invoice = {"graph_message_id": "AAMk-hold", "check_stop": True, "hold_reason": "CHECK STOP"}
    status = apply_flag_after_match(row, invoice, client)
    assert status == FLAG_SKIPPED
    assert row["Flag status"] == FLAG_SKIPPED
    client.flag_matched.assert_not_called()


def test_process_invoice_success_flags_and_hold_skips():
    created = {"id": 9508, "values": {"Invoice_Number": "27756"}}

    class FakeKimco:
        target = "prototype"

        def create(self, service, values):
            return 9508, created, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "no-pdf-on-vm"

    class FakeGraph:
        def __init__(self):
            self.flagged: list[tuple[str, str]] = []

        def flag_matched(self, mailbox, message_id):
            assert mailbox == ALLOWED_MAILBOX
            self.flagged.append((mailbox, message_id))
            return FLAG_FLAGGED

    graph = FakeGraph()
    success_inv = {
        "vendor": "Crosslink Powder Coating of TX, LLC",
        "invoice_number": "27756",
        "date": "2026-08-03",
        "po": None,
        "amount": 1252.18,
        "fees": [],
        "graph_message_id": "AAMk-crosslink-27756",
    }
    success_row = _process_invoice(
        FakeKimco(),
        success_inv,
        batch={"id": 671},
        batch_label="API Agent - 8/27/26 (671)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 9, "vendor_text": "Crosslink Powder Coating", "invoice_id": 100, "po_text": ""}],
        po_index={},
        pdf_dir=None,
        graph_client=graph,
    )
    assert success_row["Result"] == "Success"
    assert success_row["KIMCO id"] == 9508
    assert success_row["Flag status"] == FLAG_FLAGGED
    assert graph.flagged == [(ALLOWED_MAILBOX, "AAMk-crosslink-27756")]

    hold_inv = {
        "vendor": "Gas and Supply North Texas, LLC",
        "invoice_number": "0040325801",
        "date": "2026-07-31",
        "po": None,
        "amount": 418.93,
        "check_stop": True,
        "hold_reason": "CHECK STOP",
        "graph_message_id": "AAMk-check-stop",
    }
    hold_row = _process_invoice(
        FakeKimco(),
        hold_inv,
        batch={"id": 671},
        batch_label="API Agent - 8/27/26 (671)",
        invoice_by_number={},
        vendor_samples=[],
        po_index={},
        pdf_dir=None,
        graph_client=graph,
    )
    assert hold_row["Result"] == "HOLD"
    assert hold_row["KIMCO id"] == ""
    assert hold_row["Flag status"] == FLAG_SKIPPED
    assert graph.flagged == [(ALLOWED_MAILBOX, "AAMk-crosslink-27756")]


def test_graph_403_is_denied_and_keeps_flag_decision_helpers():
    resp = Mock()
    resp.status_code = 403
    client = GraphClient("token-not-printed")
    client.request = Mock(return_value=resp)
    assert client.flag_matched(ALLOWED_MAILBOX, "AAMk-denied") == FLAG_DENIED
    assert decide_flag_status(result="Success", kimco_id=9499, message_id="") == FLAG_NO_MESSAGE_ID
    assert decide_flag_status(result="Fail", kimco_id=3854, message_id="AAMk") == FLAG_SKIPPED
    assert decide_flag_status(result="Success", kimco_id="", message_id="AAMk") == FLAG_SKIPPED


def test_attach_message_ids_requires_unique_hit():
    invoices = [
        {"vendor": "Telecom Products Inc.", "invoice_number": "16960", "date": "2026-07-27"},
        {"vendor": "Crosslink Powder Coating of TX, LLC", "invoice_number": "27756", "date": "2026-08-03"},
    ]
    messages = [
        {
            "id": "AAMk-telecom",
            "subject": "Invoice - 16960",
            "bodyPreview": "",
            "receivedDateTime": "2026-07-30T21:26:52Z",
            "attachment_names": ["Invoice - 16960.pdf"],
        },
        {
            "id": "AAMk-crosslink",
            "subject": "Invoice #27756 for 58723 (#7931) from Crosslink Powder Coating",
            "bodyPreview": "",
            "receivedDateTime": "2026-08-03T19:42:20Z",
            "attachment_names": ["invoice-27756.pdf"],
        },
        {
            "id": "AAMk-other",
            "subject": "Invoice #27755 for 58723",
            "bodyPreview": "",
            "receivedDateTime": "2026-08-03T18:00:00Z",
            "attachment_names": ["invoice-27755.pdf"],
        },
    ]
    enriched = attach_message_ids(invoices, messages)
    assert enriched[0]["graph_message_id"] == "AAMk-telecom"
    assert enriched[1]["graph_message_id"] == "AAMk-crosslink"
