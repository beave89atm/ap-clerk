"""GET-only receiving@ Graph probe. No live Graph or KIMCO I/O."""

from __future__ import annotations

from unittest.mock import Mock, patch

from ap_clerk.cli import main
from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, MailboxRejected, assert_allowed_mailbox
from ap_clerk.receiving_probe import (
    RECEIVING_MAILBOX,
    assert_receiving_mailbox,
    needs_application_access_policy,
    probe_receiving_access,
    sample_row,
)


def test_receiving_mailbox_is_not_ap_invoice_mailbox():
    assert RECEIVING_MAILBOX == "receiving@kannonmfg.com"
    assert RECEIVING_MAILBOX != ALLOWED_MAILBOX
    assert assert_receiving_mailbox() == RECEIVING_MAILBOX
    assert assert_receiving_mailbox("Receiving@KannonMfg.com") == RECEIVING_MAILBOX
    try:
        assert_allowed_mailbox(RECEIVING_MAILBOX)
        raise AssertionError("invoice Graph must still refuse receiving@")
    except MailboxRejected:
        pass


def test_graph_client_still_refuses_receiving_urls():
    client = GraphClient("token-not-printed")
    try:
        client.list_messages(RECEIVING_MAILBOX)
        raise AssertionError("list_messages must not accept receiving@")
    except MailboxRejected:
        pass


def test_needs_aap_on_403_not_on_empty_200():
    assert needs_application_access_policy(
        http=403, error_code="ErrorAccessDenied", error_message="Access is denied."
    )
    assert not needs_application_access_policy(http=200, error_code="", error_message="")
    assert not needs_application_access_policy(
        http=404, error_code="Request_ResourceNotFound", error_message="Resource not found"
    )


def test_probe_200_empty_inbox_is_access_ok():
    user = Mock()
    user.status_code = 200
    user.json.return_value = {
        "id": "user-id",
        "displayName": "Receiving",
        "mail": RECEIVING_MAILBOX,
        "userPrincipalName": RECEIVING_MAILBOX,
    }
    inbox = Mock()
    inbox.status_code = 200
    inbox.json.return_value = {"value": []}

    with patch("ap_clerk.receiving_probe.requests.get", side_effect=[user, inbox]) as get:
        payload = probe_receiving_access("token-not-printed")
    assert payload["access"] == "ok"
    assert payload["user_http"] == 200
    assert payload["messages_http"] == 200
    assert payload["message_count"] == 0
    assert payload["send_mail_invoked"] is False
    assert payload["kimco_writes"] is False
    assert payload["needs_application_access_policy"] is False
    assert "empty" in payload["notes"].lower()
    assert payload["access"] == "ok"
    assert get.call_count == 2
    for call in get.call_args_list:
        assert call.args[0].startswith("https://graph.microsoft.com/")
        assert RECEIVING_MAILBOX in call.args[0]


def test_probe_403_flags_application_access_policy():
    denied = Mock()
    denied.status_code = 403
    denied.json.return_value = {
        "error": {"code": "ErrorAccessDenied", "message": "Access is denied. Check credentials and try again."}
    }
    with patch("ap_clerk.receiving_probe.requests.get", return_value=denied):
        payload = probe_receiving_access("token-not-printed")
    assert payload["access"] == "blocked"
    assert payload["user_http"] == 403
    assert payload["messages_http"] == 403
    assert payload["error_code"] == "ErrorAccessDenied"
    assert payload["needs_application_access_policy"] is True
    assert "Application Access Policy" in payload["notes"]


def test_probe_user_403_messages_200_is_access_ok():
    user = Mock()
    user.status_code = 403
    user.json.return_value = {
        "error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}
    }
    inbox = Mock()
    inbox.status_code = 200
    inbox.json.return_value = {"value": []}
    with patch("ap_clerk.receiving_probe.requests.get", side_effect=[user, inbox]):
        payload = probe_receiving_access("token-not-printed")
    assert payload["access"] == "ok"
    assert payload["user_http"] == 403
    assert payload["messages_http"] == 200
    assert payload["needs_application_access_policy"] is False


def test_sample_row_shape():
    row = sample_row(
        {
            "subject": "BOL 123",
            "from": {"emailAddress": {"name": "Dock", "address": "dock@example.com"}},
            "receivedDateTime": "2026-09-22T12:00:00Z",
            "hasAttachments": True,
        },
        attachment_names=["bol-123.pdf"],
    )
    assert row == {
        "subject": "BOL 123",
        "from_name": "Dock",
        "from_address": "dock@example.com",
        "received": "2026-09-22T12:00:00Z",
        "has_attachments": True,
        "attachment_names": ["bol-123.pdf"],
    }


def test_cli_receiving_never_uses_ap_mailbox_assert(capsys):
    with patch("ap_clerk.cli.authenticate_token", return_value="token-not-printed"), patch(
        "ap_clerk.cli.probe_receiving_access",
        return_value={
            "mailbox": RECEIVING_MAILBOX,
            "user_http": 200,
            "messages_http": 200,
            "access": "ok",
            "error_code": "",
            "error_message": "",
            "needs_application_access_policy": False,
            "message_count": 0,
            "messages": [],
            "send_mail_invoked": False,
            "kimco_writes": False,
            "notes": "Access OK. Inbox is empty.",
        },
    ), patch("ap_clerk.cli.write_proof"):
        code = main(["receiving", "--as-of", "2026-09-22"])
    assert code == 0
    out = capsys.readouterr().out
    assert "receiving@kannonmfg.com" in out
    assert "send_mail_invoked=false" in out
    assert "sendMail" not in out
