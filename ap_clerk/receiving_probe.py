"""GET-only Graph probe for receiving@kannonmfg.com.

Invoice GraphClient stays locked to accountspayable@. This module never
sendMail, never PATCH/POST, never writes KIMCO.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ap_clerk.graph import (
    GRAPH_BASE,
    GraphError,
    graph_http_detail,
    load_graph_credentials,
)

LOGGER = logging.getLogger("ap_clerk.receiving")

RECEIVING_MAILBOX = "receiving@kannonmfg.com"


def normalize_receiving_mailbox(mailbox: str | None) -> str:
    return (mailbox or "").strip().lower()


def assert_receiving_mailbox(mailbox: str | None = None) -> str:
    """Refuse every mailbox except receiving@kannonmfg.com."""
    normalized = normalize_receiving_mailbox(mailbox or RECEIVING_MAILBOX)
    if normalized != RECEIVING_MAILBOX:
        raise GraphError(
            f"Refusing mailbox {mailbox!r}. Receiving probe only allows {RECEIVING_MAILBOX}."
        )
    return RECEIVING_MAILBOX


def _user_url(mailbox: str, suffix: str = "") -> str:
    mailbox = assert_receiving_mailbox(mailbox)
    path = f"{GRAPH_BASE}/users/{mailbox}"
    if suffix:
        path = f"{path}/{suffix.lstrip('/')}"
    return path


def needs_application_access_policy(*, http: int, error_code: str, error_message: str) -> bool:
    """True when 403/denied looks like the app is not allowed on this mailbox."""
    if http == 403:
        return True
    blob = f"{error_code} {error_message}".lower()
    if http in {401, 403} and any(
        needle in blob
        for needle in (
            "access is denied",
            "erroraccessdenied",
            "authorization_requestdenied",
            "applicationaccesspolicy",
            "access policy",
        )
    ):
        return True
    return False


def _error_fields(response: requests.Response) -> tuple[str, str]:
    extra = graph_http_detail(response)
    try:
        err = (response.json() or {}).get("error") or {}
    except (ValueError, TypeError):
        err = {}
    if not isinstance(err, dict):
        err = {}
    code = str(err.get("code") or "").strip()
    message = str(err.get("message") or "").replace("\n", " ").strip()[:400]
    if not code and extra:
        code = extra.split(":", 1)[0].strip()
    return code, message or extra


def _get(token: str, url: str, *, params: dict[str, Any] | None = None) -> requests.Response:
    if "graph.microsoft.com" not in (url or "").lower():
        raise GraphError("Receiving probe refuses non-Graph hosts")
    if RECEIVING_MAILBOX not in (url or "").lower():
        raise GraphError(f"Receiving probe refuses Graph URL that is not {RECEIVING_MAILBOX}")
    return requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=60,
    )


def _sender(message: dict[str, Any]) -> tuple[str, str]:
    frm = message.get("from") or {}
    email_addr = frm.get("emailAddress") or frm
    if not isinstance(email_addr, dict):
        return "", ""
    return str(email_addr.get("name") or ""), str(email_addr.get("address") or "")


def _attachment_names(token: str, mailbox: str, message_id: str) -> list[str]:
    if not message_id:
        return []
    url = f"{_user_url(mailbox, 'messages')}/{quote(str(message_id), safe='')}/attachments"
    response = _get(token, url, params={"$select": "id,name,contentType,size"})
    if response.status_code != 200:
        LOGGER.info("Receiving attachment metadata HTTP %s (names skipped)", response.status_code)
        return []
    return [str(item.get("name") or "") for item in (response.json() or {}).get("value") or []]


def sample_row(message: dict[str, Any], *, attachment_names: list[str] | None = None) -> dict[str, Any]:
    name, address = _sender(message)
    return {
        "subject": str(message.get("subject") or ""),
        "from_name": name,
        "from_address": address,
        "received": str(message.get("receivedDateTime") or ""),
        "has_attachments": bool(message.get("hasAttachments")),
        "attachment_names": list(attachment_names or message.get("attachment_names") or []),
    }


def probe_receiving_access(token: str, *, mailbox: str = RECEIVING_MAILBOX, top: int = 15) -> dict[str, Any]:
    """GET /users/receiving@ then GET messages. No sendMail. No PDF download."""
    mailbox = assert_receiving_mailbox(mailbox)
    if not token:
        raise GraphError("Graph token missing")
    user_url = _user_url(mailbox)
    user_resp = _get(token, user_url, params={"$select": "id,displayName,mail,userPrincipalName"})
    user_http = int(user_resp.status_code)
    user_code, user_message = ("", "")
    user: dict[str, Any] = {}
    if user_http == 200:
        payload = user_resp.json() or {}
        user = {
            "id": str(payload.get("id") or ""),
            "displayName": str(payload.get("displayName") or ""),
            "mail": str(payload.get("mail") or ""),
            "userPrincipalName": str(payload.get("userPrincipalName") or ""),
        }
    else:
        user_code, user_message = _error_fields(user_resp)

    messages_http = 0
    messages_code, messages_message = ("", "")
    rows: list[dict[str, Any]] = []
    if user_http == 200:
        msg_url = _user_url(mailbox, "messages")
        msg_resp = _get(
            token,
            msg_url,
            params={
                "$select": "id,subject,from,receivedDateTime,hasAttachments",
                "$orderby": "receivedDateTime desc",
                "$top": max(1, min(int(top), 50)),
            },
        )
        messages_http = int(msg_resp.status_code)
        if messages_http == 200:
            for message in (msg_resp.json() or {}).get("value") or []:
                names: list[str] = []
                if message.get("hasAttachments"):
                    names = _attachment_names(token, mailbox, str(message.get("id") or ""))
                rows.append(sample_row(message, attachment_names=names))
        else:
            messages_code, messages_message = _error_fields(msg_resp)

    blocked_http = user_http if user_http != 200 else messages_http
    error_code = user_code or messages_code
    error_message = user_message or messages_message
    access_ok = user_http == 200 and messages_http == 200
    aap = needs_application_access_policy(
        http=blocked_http if not access_ok else 0,
        error_code=error_code,
        error_message=error_message,
    )
    if access_ok:
        verdict = "ok"
        notes = (
            "GET /users/receiving@kannonmfg.com and GET messages succeeded. "
            "Empty inbox is still access OK. sendMail was not called."
        )
        if not rows:
            notes = "Access OK. Inbox is empty. sendMail was not called."
    elif user_http == 404 or messages_http == 404:
        verdict = "blocked"
        notes = (
            "Graph returned 404. Mailbox may be missing, unlicensed, or not mail-enabled. "
            "If the mailbox exists in Exchange, add receiving@ to the Application Access Policy "
            "and confirm the user is licensed."
        )
    elif aap:
        verdict = "blocked"
        notes = (
            "Graph denied the app on receiving@. Add receiving@kannonmfg.com to the "
            "Microsoft 365 Application Access Policy that already allows accountspayable@, "
            "then wait for policy propagation."
        )
    else:
        verdict = "blocked"
        notes = f"Graph probe failed HTTP {blocked_http} {error_code}: {error_message}".strip()

    return {
        "mailbox": mailbox,
        "user_http": user_http,
        "messages_http": messages_http or None,
        "access": verdict,
        "error_code": error_code,
        "error_message": error_message,
        "needs_application_access_policy": aap,
        "user": user,
        "message_count": len(rows),
        "messages": rows,
        "send_mail_invoked": False,
        "mail_sent_to_anyone": False,
        "kimco_writes": False,
        "notes": notes,
    }


def authenticate_token() -> str:
    creds = load_graph_credentials()
    if not creds.ready:
        raise GraphError(creds.error or "Graph credentials missing")
    from ap_clerk.graph import GraphClient

    client = GraphClient.authenticate(
        creds.tenant_id or "",
        creds.client_id or "",
        creds.client_secret or "",
    )
    return str(client._token)


def default_proof_path(day: date | None = None) -> Path:
    stamp = (day or date.today()).isoformat()
    return Path("runs") / f"receiving-graph-probe-{stamp}.json"


def write_proof(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path
