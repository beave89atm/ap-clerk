"""Comments_1 writes abort unless the access token authors as API Agent.

The 2026-09-24 form login authored notes as Treyce Hodges (user 33).
A live note whose visible text is only "probe" or "test" is refused.
No live KIMCO I/O.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from ap_clerk.kimco import (
    TREYCE_COMMENT_AUTHOR_ID,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    comment_author_from_access_token,
)

LIVE_URL = "https://live.kimcoerp.com"
PROTO_URL = "https://prototype.kimcoerp.com"


class FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "{}"
        self.headers: dict[str, str] = {}

    def json(self):
        return {"id": 10181}


def token_for(user_id: int, name: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload = {
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier": str(user_id),
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": name,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def test_author_resolver_reads_user_33() -> None:
    author = comment_author_from_access_token(token_for(33, "Treyce Hodges"))
    assert author["id"] == TREYCE_COMMENT_AUTHOR_ID
    assert author["name"] == "Treyce Hodges"


def test_live_comments_1_aborts_when_author_resolves_to_33() -> None:
    client = KimcoClient(LIVE_URL, token_for(33, "Treyce Hodges"), target="live")
    payload = added_comment_payload(10181, "<p>AP Clerk: selected receipt 24596.</p>")
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        with pytest.raises(KimcoError, match="resolves to Treyce Hodges \\(33\\)"):
            client.update("ap_invoices", 10181, payload)
    req.assert_not_called()


def test_live_comments_1_aborts_when_name_is_treyce_even_with_another_id() -> None:
    client = KimcoClient(LIVE_URL, token_for(175, "Treyce Hodges"), target="live")
    payload = added_comment_payload(10181, "<p>AP Clerk: note.</p>")
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        with pytest.raises(KimcoError, match="Treyce Hodges \\(33\\)"):
            client.request(
                "PUT",
                f"{LIVE_URL}/api/v2/{client.services['ap_invoices']}/10181",
                json=payload,
            )
    req.assert_not_called()


@pytest.mark.parametrize("text", ["probe", "<p>probe</p>", "<p>Test</p>", " test "])
def test_live_probe_or_test_comment_aborts_for_api_agent(text: str) -> None:
    client = KimcoClient(LIVE_URL, token_for(175, "API Agent"), target="live")
    payload = added_comment_payload(10181, text)
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        with pytest.raises(KimcoError, match="probe/test"):
            client.update("ap_invoices", 10181, payload)
    req.assert_not_called()


def test_live_api_agent_comment_is_allowed() -> None:
    client = KimcoClient(LIVE_URL, token_for(175, "API Agent"), target="live")
    payload = added_comment_payload(
        10181,
        '<p>AP Clerk: <span data-mention-id="104">@Shawn McKibben</span> note.</p>',
    )
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        _body, status, error = client.update("ap_invoices", 10181, payload)
    assert status == 200
    assert error == ""
    assert req.called


def test_unresolved_token_aborts_comments_1_write() -> None:
    client = KimcoClient(LIVE_URL, "token", target="live")
    payload = added_comment_payload(10181, "<p>AP Clerk: note.</p>")
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        with pytest.raises(KimcoError, match="could not be resolved"):
            client.update("ap_invoices", 10181, payload)
    req.assert_not_called()


def test_non_comment_update_does_not_require_a_jwt() -> None:
    client = KimcoClient(LIVE_URL, "token", target="live")
    with patch.object(client.session, "request", return_value=FakeResp()) as req:
        _body, status, error = client.update("ap_invoices", 10181, {"Comments": "header only"})
    assert status == 200
    assert error == ""
    assert req.called


def test_prototype_api_agent_173_is_allowed_and_175_is_not() -> None:
    allowed = KimcoClient(PROTO_URL, token_for(173, "API Agent"), target="prototype")
    payload = added_comment_payload(1, "<p>AP Clerk: prototype note.</p>")
    with patch.object(allowed.session, "request", return_value=FakeResp()) as req:
        _body, status, _error = allowed.update("ap_invoices", 1, payload)
    assert status == 200
    assert req.called

    refused = KimcoClient(PROTO_URL, token_for(175, "API Agent"), target="prototype")
    with patch.object(refused.session, "request", return_value=FakeResp()) as req:
        with pytest.raises(KimcoError, match="not the API Agent login"):
            refused.update("ap_invoices", 1, payload)
    req.assert_not_called()
