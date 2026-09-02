"""Outlook flag-after-match. No live Graph or KIMCO I/O in these tests."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from ap_clerk.cli import _process_invoice, main
from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    ALLOWED_MAILBOX,
    CATEGORY_CREATED,
    CATEGORY_DENIED,
    DRAFT_PROBE_SUBJECT,
    EMAIL_DENIED,
    EMAIL_DRAFT_OK,
    EMAIL_SENT,
    ENTERED_IN_AI_CATEGORY,
    FLAG_AI_HOLD,
    FLAG_DENIED,
    FLAG_FLAGGED,
    FLAG_HOLD_ELIGIBLE,
    FLAG_NO_MESSAGE_ID,
    FLAG_SKIPPED,
    GraphClient,
    MailboxRejected,
    apply_flag_after_match,
    assert_allowed_mailbox,
    attach_message_ids,
    categories_for_status,
    decide_flag_status,
    default_report_to,
    granted_app_roles,
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
    with pytest.raises(MailboxRejected, match="accountspayable@kannonmfg.com"):
        client.flag_hold(WRONG_MAILBOX, "AAMk-message-id")
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
            resp.json.return_value = {"categories": [ENTERED_IN_AI_CATEGORY]}
        else:
            resp.json.return_value = {
                "id": "AAMk-success",
                "categories": ["AP Matched"],
                "flag": {"flagStatus": "notFlagged"},
            }
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
    assert "Flag status=entered-in-ai" in row["Why"]
    assert any((body.get("flag") or {}).get("flagStatus") == "flagged" for body in patches)
    assert any(body.get("categories") == [ENTERED_IN_AI_CATEGORY] for body in patches)
    assert all("AP Matched" not in (body.get("categories") or []) for body in patches)


def test_hold_applies_ai_hold_not_entered_in_ai():
    patches: list[dict] = []

    def fake_request(method, url, **kwargs):
        assert ALLOWED_MAILBOX in url
        resp = Mock()
        resp.status_code = 200
        if method == "PATCH":
            patches.append(kwargs.get("json") or {})
            resp.json.return_value = {"categories": [AI_HOLD_CATEGORY]}
        else:
            resp.json.return_value = {
                "id": "AAMk-hold",
                "categories": [ENTERED_IN_AI_CATEGORY],
                "flag": {"flagStatus": "notFlagged"},
            }
        return resp

    client = GraphClient("token-not-printed")
    client.request = fake_request
    row = {
        "Result": "HOLD",
        "KIMCO id": "",
        "Why": "HOLD: CHECK STOP. Do not create a header.",
    }
    invoice = {"graph_message_id": "AAMk-hold", "check_stop": True, "hold_reason": "CHECK STOP"}
    status = apply_flag_after_match(row, invoice, client)
    assert status == FLAG_AI_HOLD
    assert row["Flag status"] == FLAG_AI_HOLD
    assert "Flag status=ai-hold" in row["Why"]
    assert not any("flag" in (body or {}) for body in patches)
    assert any(body.get("categories") == [AI_HOLD_CATEGORY] for body in patches)
    assert all(ENTERED_IN_AI_CATEGORY not in (body.get("categories") or []) for body in patches)


def test_fail_applies_ai_hold():
    client = GraphClient("token-not-printed")
    client.flag_hold = Mock(return_value=FLAG_AI_HOLD)
    client.flag_matched = Mock(side_effect=AssertionError("Fail must not get Entered in AI"))
    row = {"Result": "Fail", "KIMCO id": "", "Why": "Fail: vendor missing on live."}
    status = apply_flag_after_match(row, {"graph_message_id": "AAMk-fail"}, client)
    assert status == FLAG_AI_HOLD
    client.flag_hold.assert_called_once()
    client.flag_matched.assert_not_called()


def test_never_both_process_categories():
    mixed = categories_for_status([ENTERED_IN_AI_CATEGORY, "Solved!", "AP Matched"], add=AI_HOLD_CATEGORY)
    assert mixed == ["Solved!", AI_HOLD_CATEGORY]
    success = categories_for_status([AI_HOLD_CATEGORY, "Investigating"], add=ENTERED_IN_AI_CATEGORY)
    assert success == ["Investigating", ENTERED_IN_AI_CATEGORY]


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
            self.held: list[tuple[str, str]] = []

        def flag_matched(self, mailbox, message_id):
            assert mailbox == ALLOWED_MAILBOX
            self.flagged.append((mailbox, message_id))
            return FLAG_FLAGGED

        def flag_hold(self, mailbox, message_id):
            assert mailbox == ALLOWED_MAILBOX
            self.held.append((mailbox, message_id))
            return FLAG_AI_HOLD

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
    assert hold_row["Flag status"] == FLAG_AI_HOLD
    assert graph.flagged == [(ALLOWED_MAILBOX, "AAMk-crosslink-27756")]
    assert graph.held == [(ALLOWED_MAILBOX, "AAMk-check-stop")]
    assert hold_row["Flag in Outlook"] == "Yes"


def test_graph_403_is_denied_and_keeps_flag_decision_helpers():
    resp = Mock()
    resp.status_code = 403
    client = GraphClient("token-not-printed")
    client.request = Mock(return_value=resp)
    assert client.flag_matched(ALLOWED_MAILBOX, "AAMk-denied") == FLAG_DENIED
    assert client.flag_hold(ALLOWED_MAILBOX, "AAMk-denied") == FLAG_DENIED
    assert decide_flag_status(result="Success", kimco_id=9499, message_id="") == FLAG_NO_MESSAGE_ID
    assert decide_flag_status(result="Fail", kimco_id=3854, message_id="AAMk") == FLAG_HOLD_ELIGIBLE
    assert decide_flag_status(result="HOLD", kimco_id="", message_id="AAMk") == FLAG_HOLD_ELIGIBLE
    assert decide_flag_status(result="Success", kimco_id="", message_id="AAMk") == FLAG_SKIPPED


def test_ensure_ai_hold_category_create_and_403():
    client = GraphClient("token-not-printed")
    created = Mock()
    created.status_code = 201
    client.request = Mock(return_value=created)
    assert client.ensure_ai_hold_category(ALLOWED_MAILBOX) == CATEGORY_CREATED
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert "outlook/masterCategories" in args[1]
    assert kwargs["json"] == {"displayName": AI_HOLD_CATEGORY, "color": "preset0"}

    denied = Mock()
    denied.status_code = 403
    client.request = Mock(return_value=denied)
    assert client.ensure_ai_hold_category(ALLOWED_MAILBOX) == CATEGORY_DENIED


def test_send_run_report_sent_and_403_does_not_raise(tmp_path):
    xlsx = tmp_path / "AP-run-2026-08-28.xlsx"
    xlsx.write_bytes(b"xlsx")
    client = GraphClient("token-not-printed")
    ok = Mock()
    ok.status_code = 202
    client.request = Mock(return_value=ok)
    assert (
        client.send_run_report(
            ALLOWED_MAILBOX,
            to=default_report_to(),
            subject="AP run 2026-08-28",
            body="Success: 1",
            attachment_path=xlsx,
        )
        == EMAIL_SENT
    )
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert args[1].endswith("/sendMail")
    assert ALLOWED_MAILBOX in args[1]
    message = kwargs["json"]["message"]
    assert message["subject"] == "AP run 2026-08-28"
    assert message["toRecipients"][0]["emailAddress"]["address"] == default_report_to()
    assert message["attachments"][0]["name"] == "AP-run-2026-08-28.xlsx"
    assert "flag" not in message

    denied = Mock()
    denied.status_code = 403
    client.request = Mock(return_value=denied)
    assert (
        client.send_run_report(
            ALLOWED_MAILBOX,
            to=default_report_to(),
            subject="AP run 2026-08-28",
            body="Success: 1",
            attachment_path=xlsx,
        )
        == EMAIL_DENIED
    )


def test_probe_send_authorization_creates_and_deletes_draft_never_sendmail():
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        assert ALLOWED_MAILBOX in url
        assert "/sendMail" not in url
        resp = Mock()
        if method == "POST" and url.endswith("/messages"):
            resp.status_code = 201
            resp.json.return_value = {"id": "AAMk-draft"}
            body = kwargs.get("json") or {}
            assert body.get("subject") == DRAFT_PROBE_SUBJECT
            assert body["toRecipients"][0]["emailAddress"]["address"] == ALLOWED_MAILBOX
        elif method == "DELETE":
            resp.status_code = 204
            resp.json.return_value = {}
        else:
            resp.status_code = 200
            resp.json.return_value = {}
        return resp

    # JWT with roles=["Mail.Send"] — payload only, not a real token.
    import base64
    import json as json_mod

    payload = base64.urlsafe_b64encode(json_mod.dumps({"roles": ["Mail.Send", "Mail.ReadWrite"]}).encode()).rstrip(b"=").decode()
    token = f"aaa.{payload}.ccc"
    client = GraphClient(token)
    client.request = fake_request
    result = client.probe_send_authorization(ALLOWED_MAILBOX)
    assert result["send_mail_invoked"] is False
    assert result["mail_send_role"] is True
    assert result["draft_status"] == EMAIL_DRAFT_OK
    assert result["draft_http"] == 201
    assert result["draft_deleted_http"] == 204
    assert any(method == "POST" and url.endswith("/messages") for method, url in calls)
    assert any(method == "DELETE" for method, url in calls)
    assert not any("/sendMail" in url for _, url in calls)
    assert granted_app_roles(token) == ["Mail.Send", "Mail.ReadWrite"]


def test_default_report_to_is_treyce_at_kannon():
    recipient = default_report_to()
    assert recipient.endswith("@kannonmfg.com")
    assert recipient.split("@", 1)[0] == "treyce"


def test_process_invoice_fail_applies_ai_hold():
    class FakeKimco:
        target = "live"

        def create(self, service, values):
            raise AssertionError("must not create without a vendor")

        def get_item(self, service, item_id):
            raise AssertionError("must not GET")

    class FakeGraph:
        def __init__(self):
            self.held: list[str] = []

        def flag_hold(self, mailbox, message_id):
            self.held.append(message_id)
            return FLAG_AI_HOLD

        def flag_matched(self, mailbox, message_id):
            raise AssertionError("Fail must not get Entered in AI")

    graph = FakeGraph()
    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Unknown Vendor LLC",
            "invoice_number": "ZZ-1",
            "date": "2026-08-27",
            "po": None,
            "amount": 12.0,
            "graph_message_id": "AAMk-missing-vendor",
        },
        batch={"id": 1},
        batch_label="API Agent - 8/27/26 (1)",
        invoice_by_number={},
        vendor_samples=[],
        po_index={},
        pdf_dir=None,
        graph_client=graph,
        flag_outlook=True,
    )
    assert row["Result"] == "Fail"
    assert row["Flag status"] == FLAG_AI_HOLD
    assert graph.held == ["AAMk-missing-vendor"]
    assert "Flag status=ai-hold" in row["Why"]


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
