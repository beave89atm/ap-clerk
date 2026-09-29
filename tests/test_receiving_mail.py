"""receiving@ AI Completed category. No live Graph or KIMCO I/O."""

from __future__ import annotations

import pytest

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, GraphError, MailboxRejected
from ap_clerk.packing_slips import AI_COMPLETED_CATEGORY
from ap_clerk.receiving_attach import finish_receiving_email, verify_header_packing_slip
from ap_clerk.receiving_mail import (
    FLAG_AI_COMPLETED,
    FLAG_LEFT_UNCATEGORIZED,
    ReceivingGraph,
    stamp_receiving_ai_completed,
)
from ap_clerk.receiving_probe import RECEIVING_MAILBOX


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class RecordingReceivingGraph(ReceivingGraph):
    def __init__(self):
        self._token = "test-token"
        self.session = None
        self.patches: list[dict] = []
        self.messages = {
            "AAMk-scan": {"id": "AAMk-scan", "categories": ["Human"]},
        }

    def request(self, method, url, **kwargs):
        return super().request(method, url, **kwargs)

    def get_message(self, mailbox, message_id, *, select="id,subject,categories"):
        assert mailbox == RECEIVING_MAILBOX
        return dict(self.messages.get(message_id) or {"id": message_id, "categories": []})

    def flag_ai_completed(self, mailbox, message_id):
        assert mailbox == RECEIVING_MAILBOX
        current = self.get_message(mailbox, message_id)
        cats = [c for c in (current.get("categories") or []) if c != AI_COMPLETED_CATEGORY]
        cats.append(AI_COMPLETED_CATEGORY)
        self.patches.append({"mailbox": mailbox, "id": message_id, "categories": cats})
        self.messages[message_id] = {"id": message_id, "categories": cats}
        return FLAG_AI_COMPLETED


def test_receiving_graph_refuses_sendmail_and_ap_mailbox():
    graph = ReceivingGraph("token")
    with pytest.raises(GraphError, match="sendMail"):
        graph.request("POST", f"https://graph.microsoft.com/v1.0/users/{RECEIVING_MAILBOX}/sendMail")
    with pytest.raises(MailboxRejected):
        graph.request("GET", f"https://graph.microsoft.com/v1.0/users/{ALLOWED_MAILBOX}/messages")
    with pytest.raises(GraphError, match="POST"):
        graph.request("POST", f"https://graph.microsoft.com/v1.0/users/{RECEIVING_MAILBOX}/messages")


def test_invoice_graph_still_refuses_receiving():
    client = GraphClient("token")
    with pytest.raises(MailboxRejected):
        client.request("GET", f"https://graph.microsoft.com/v1.0/users/{RECEIVING_MAILBOX}/messages")
    with pytest.raises(MailboxRejected):
        client.request(
            "PATCH",
            f"https://graph.microsoft.com/v1.0/users/{RECEIVING_MAILBOX}/messages/AAMk",
            json={"categories": [AI_COMPLETED_CATEGORY]},
        )


def test_stamp_ai_completed_only_when_all_verified():
    graph = RecordingReceivingGraph()
    ok = stamp_receiving_ai_completed(
        graph,
        mailbox=RECEIVING_MAILBOX,
        message_id="AAMk-scan",
        slip_results=[
            {
                "slip": {"po": "59008", "pages": [1, 2]},
                "identifiable": True,
                "status": "attached",
                "verified": True,
            }
        ],
    )
    assert ok["stamp"] is True
    assert ok["outlook"] == FLAG_AI_COMPLETED
    assert ok["mail_sent"] is False
    assert graph.patches[0]["categories"][-1] == "AI Completed"
    assert "Human" in graph.patches[0]["categories"]

    graph2 = RecordingReceivingGraph()
    partial = finish_receiving_email(
        graph2,
        mailbox=RECEIVING_MAILBOX,
        message_id="AAMk-scan",
        slip_results=[
            {
                "slip": {"po": "59008", "pages": [1, 2]},
                "identifiable": True,
                "status": "attached",
                "verified": True,
            },
            {
                "slip": {"po": "59128", "pages": [3, 4]},
                "identifiable": True,
                "status": "unmatched",
                "verified": False,
            },
        ],
    )
    assert partial["stamp"] is False
    assert partial["outlook"] == FLAG_LEFT_UNCATEGORIZED
    assert graph2.patches == []
    assert any("59128" in label for label in partial["leftover_labels"])


def test_verify_header_requires_get_packing_slip():
    class Kimco:
        def list_attachments(self, invoice_id):
            return [{"name": "Kannon Manufacturing_20260922_073148.pdf"}]

    ok = verify_header_packing_slip(
        Kimco(),
        10152,
        attach_status="attached",
        expected_name="Kannon Manufacturing_20260922_073148.pdf",
    )
    assert ok["verified"] is True
    assert ok["status"] == "attached"

    class Empty:
        def list_attachments(self, invoice_id):
            return [{"name": "Sales Invoice PS-INV103979.pdf"}]

    missing = verify_header_packing_slip(Empty(), 10152, attach_status="attached")
    assert missing["verified"] is False
    assert missing["status"] == "failed"

    skipped = verify_header_packing_slip(Kimco(), 10152, attach_status="skipped")
    assert skipped["verified"] is False
    assert skipped["status"] == "skipped"
