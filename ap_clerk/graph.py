"""Microsoft Graph mailbox client for accountspayable@kannonmfg.com only.

Unflagged mail is the work queue. Flagged means already processed.
Flag happens after a successful KIMCO header create, never after download alone.
Never logs tokens, client secrets, or passwords.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import requests

LOGGER = logging.getLogger("ap_clerk")

ALLOWED_MAILBOX = "accountspayable@kannonmfg.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
AP_MATCHED_CATEGORY = "AP Matched"

GRAPH_ENV_NAMES = (
    "MICROSOFT_GRAPH_TENANT_ID",
    "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_CLIENT_SECRET",
)

FLAG_FLAGGED = "flagged"
FLAG_SKIPPED = "skipped-not-success"
FLAG_DENIED = "graph-denied"
FLAG_NO_MESSAGE_ID = "no-message-id"
FLAG_ELIGIBLE = "eligible"


class GraphError(RuntimeError):
    pass


class MailboxRejected(GraphError):
    """Raised when any mailbox other than accountspayable@kannonmfg.com is requested."""


@dataclass(frozen=True)
class GraphCredentialStatus:
    presence: dict[str, bool]
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None
    ready: bool
    error: str | None


def graph_env_presence() -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in GRAPH_ENV_NAMES}


def format_graph_presence(presence: dict[str, bool] | None = None) -> str:
    presence = presence or graph_env_presence()
    lines = ["Graph environment variable presence (names only, values never printed):"]
    for name, present in presence.items():
        lines.append(f"  {name}: {'present' if present else 'absent'}")
    return "\n".join(lines)


def load_graph_credentials() -> GraphCredentialStatus:
    presence = graph_env_presence()
    tenant = os.environ.get("MICROSOFT_GRAPH_TENANT_ID") or None
    client_id = os.environ.get("MICROSOFT_GRAPH_CLIENT_ID") or None
    secret = os.environ.get("MICROSOFT_GRAPH_CLIENT_SECRET") or None
    error = None
    if not (tenant and client_id and secret):
        error = "Graph credentials missing. MICROSOFT_GRAPH_TENANT_ID, MICROSOFT_GRAPH_CLIENT_ID, and MICROSOFT_GRAPH_CLIENT_SECRET must all be present."
        tenant = client_id = secret = None
    return GraphCredentialStatus(
        presence=presence,
        tenant_id=tenant,
        client_id=client_id,
        client_secret=secret,
        ready=error is None,
        error=error,
    )


def normalize_mailbox(mailbox: str | None) -> str:
    return (mailbox or "").strip().lower()


def assert_allowed_mailbox(mailbox: str | None) -> str:
    """Refuse every mailbox except accountspayable@kannonmfg.com."""
    normalized = normalize_mailbox(mailbox)
    if normalized != ALLOWED_MAILBOX:
        raise MailboxRejected(
            f"Refusing mailbox {mailbox!r}. Only {ALLOWED_MAILBOX} is allowed."
        )
    return ALLOWED_MAILBOX


def decide_flag_status(*, result: str | None, kimco_id: Any, message_id: str | None) -> str:
    """Flag only after this run created a header (Result=Success and a KIMCO id)."""
    if (result or "").strip() != "Success" or kimco_id in (None, ""):
        return FLAG_SKIPPED
    if not str(message_id or "").strip():
        return FLAG_NO_MESSAGE_ID
    return FLAG_ELIGIBLE


def invoice_number_in_text(invoice_number: str, text: str | None) -> bool:
    needle = str(invoice_number or "").strip()
    if not needle or not text:
        return False
    pattern = rf"(?<![0-9A-Za-z]){re.escape(needle)}(?![0-9A-Za-z])"
    return re.search(pattern, text, flags=re.I) is not None


def score_message_for_invoice(message: dict[str, Any], invoice: dict[str, Any]) -> int:
    """Higher score is a better unique hit. 0 means not this invoice."""
    number = str(invoice.get("invoice_number") or "").strip()
    if not number:
        return 0
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    names = [str(n) for n in (message.get("attachment_names") or []) if n]
    haystacks = [subject, preview, *names]
    if not any(invoice_number_in_text(number, text) for text in haystacks):
        return 0
    score = 0
    if any(invoice_number_in_text(number, name) for name in names):
        score += 100
    if invoice_number_in_text(number, subject):
        score += 40
    if invoice_number_in_text(number, preview):
        score += 20
    vendor = str(invoice.get("vendor") or "").strip()
    if vendor and vendor.lower() in f"{subject} {preview}".lower():
        score += 25
    received = str(message.get("receivedDateTime") or "")
    fixture_date = str(invoice.get("date") or "")[:10]
    if fixture_date and received.startswith(fixture_date):
        score += 15
    elif received[:10] >= "2026-07-27" and received[:10] <= "2026-08-03":
        score += 10
    return score


def attach_message_ids(invoices: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy invoices and set graph_message_id only when exactly one high-confidence hit exists."""
    enriched = []
    for inv in invoices:
        copy = dict(inv)
        if copy.get("graph_message_id"):
            enriched.append(copy)
            continue
        scored: list[tuple[int, dict[str, Any]]] = []
        for message in messages:
            score = score_message_for_invoice(message, copy)
            if score >= 40:
                scored.append((score, message))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) == 1:
            copy["graph_message_id"] = scored[0][1].get("id")
        elif len(scored) > 1 and scored[0][0] >= scored[1][0] + 40:
            copy["graph_message_id"] = scored[0][1].get("id")
        enriched.append(copy)
    return enriched


class GraphClient:
    def __init__(self, token: str, timeout: int = 60):
        if not token:
            raise GraphError("Graph token missing")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    @classmethod
    def authenticate(
        cls,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> "GraphClient":
        url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        response = requests.post(
            url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
                "scope": GRAPH_SCOPE,
            },
            timeout=60,
        )
        if response.status_code != 200:
            raise GraphError(f"Graph token request failed HTTP {response.status_code}")
        token = (response.json() or {}).get("access_token")
        if not token:
            raise GraphError("Graph token response had no access_token")
        LOGGER.info("Authenticated to Microsoft Graph (token present, not printed)")
        return cls(token)

    def _messages_url(self, mailbox: str, message_id: str | None = None, suffix: str = "") -> str:
        mailbox = assert_allowed_mailbox(mailbox)
        path = f"{GRAPH_BASE}/users/{mailbox}/messages"
        if message_id:
            path = f"{path}/{quote(str(message_id), safe='')}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        if "graph.microsoft.com" not in (url or "").lower() and "login.microsoftonline.com" not in (url or "").lower():
            raise GraphError("Graph client refuses non-Graph hosts")
        if ALLOWED_MAILBOX not in (url or "").lower() and "/oauth2/" not in (url or ""):
            raise MailboxRejected(f"Refusing Graph URL that is not {ALLOWED_MAILBOX}")
        return self.session.request(method, url, timeout=self.timeout, **kwargs)

    def list_messages(
        self,
        mailbox: str = ALLOWED_MAILBOX,
        *,
        received_from: date | None = None,
        received_to: date | None = None,
        unflagged_only: bool = False,
        include_attachment_names: bool = False,
    ) -> list[dict[str, Any]]:
        mailbox = assert_allowed_mailbox(mailbox)
        filters: list[str] = []
        if received_from:
            filters.append(f"receivedDateTime ge {received_from.isoformat()}T00:00:00Z")
        if received_to:
            upper = received_to + timedelta(days=1)
            filters.append(f"receivedDateTime lt {upper.isoformat()}T00:00:00Z")
        if unflagged_only:
            filters.append("flag/flagStatus ne 'flagged'")
        params: dict[str, Any] = {
            "$select": "id,subject,from,receivedDateTime,hasAttachments,flag,categories,bodyPreview",
            "$orderby": "receivedDateTime desc",
            "$top": 50,
        }
        if filters:
            params["$filter"] = " and ".join(filters)
        messages: list[dict[str, Any]] = []
        url: str | None = self._messages_url(mailbox)
        first = True
        while url:
            kwargs: dict[str, Any] = {}
            if first:
                kwargs["params"] = params
                first = False
            response = self.request("GET", url, **kwargs)
            if response.status_code != 200:
                raise GraphError(f"Graph list messages HTTP {response.status_code}")
            payload = response.json() or {}
            chunk = payload.get("value") or []
            messages.extend(chunk)
            url = payload.get("@odata.nextLink")
            LOGGER.info("Listed Graph messages got=%s total=%s", len(chunk), len(messages))
        if include_attachment_names:
            for message in messages:
                message["attachment_names"] = self.list_attachment_names(mailbox, message.get("id") or "")
        return messages

    def search_messages(self, mailbox: str, needle: str, *, top: int = 25) -> list[dict[str, Any]]:
        mailbox = assert_allowed_mailbox(mailbox)
        if not str(needle or "").strip():
            return []
        response = self.request(
            "GET",
            self._messages_url(mailbox),
            params={
                "$search": f'"{needle}"',
                "$select": "id,subject,from,receivedDateTime,hasAttachments,flag,categories,bodyPreview",
                "$top": top,
            },
            headers={"ConsistencyLevel": "eventual"},
        )
        if response.status_code != 200:
            raise GraphError(f"Graph search messages HTTP {response.status_code}")
        return (response.json() or {}).get("value") or []

    def get_message(self, mailbox: str, message_id: str, *, select: str = "id,subject,flag,categories") -> dict[str, Any]:
        mailbox = assert_allowed_mailbox(mailbox)
        if not message_id:
            raise GraphError("Graph message id missing")
        response = self.request(
            "GET",
            self._messages_url(mailbox, message_id),
            params={"$select": select},
        )
        if response.status_code != 200:
            raise GraphError(f"Graph GET message HTTP {response.status_code}")
        return response.json() or {}

    def list_attachment_names(self, mailbox: str, message_id: str) -> list[str]:
        mailbox = assert_allowed_mailbox(mailbox)
        if not message_id:
            return []
        response = self.request(
            "GET",
            self._messages_url(mailbox, message_id, "attachments"),
            params={"$select": "id,name,contentType,size"},
        )
        if response.status_code != 200:
            LOGGER.info("Graph attachment metadata HTTP %s (names skipped)", response.status_code)
            return []
        return [str(item.get("name") or "") for item in (response.json() or {}).get("value") or []]

    def list_attachments(self, mailbox: str, message_id: str) -> list[dict[str, Any]]:
        mailbox = assert_allowed_mailbox(mailbox)
        if not message_id:
            return []
        response = self.request(
            "GET",
            self._messages_url(mailbox, message_id, "attachments"),
            params={"$select": "id,name,contentType,size,@odata.type"},
        )
        if response.status_code != 200:
            LOGGER.info("Graph attachment list HTTP %s", response.status_code)
            return []
        return [item for item in (response.json() or {}).get("value") or [] if isinstance(item, dict)]

    def download_pdf_attachments(self, mailbox: str, message_id: str) -> list[tuple[str, bytes]]:
        """Download PDF file attachments only. Never logs bytes or tokens."""
        mailbox = assert_allowed_mailbox(mailbox)
        downloaded: list[tuple[str, bytes]] = []
        for item in self.list_attachments(mailbox, message_id):
            name = str(item.get("name") or "attachment.pdf")
            ctype = str(item.get("contentType") or "").lower()
            odata = str(item.get("@odata.type") or "").lower()
            if "itemattachment" in odata:
                continue
            if not (name.lower().endswith(".pdf") or "pdf" in ctype):
                continue
            att_id = str(item.get("id") or "")
            if not att_id:
                continue
            content = self._download_attachment_bytes(mailbox, message_id, att_id)
            if content:
                downloaded.append((name, content))
                LOGGER.info("Downloaded PDF attachment name=%s bytes=%s", name, len(content))
        return downloaded

    def _download_attachment_bytes(self, mailbox: str, message_id: str, attachment_id: str) -> bytes | None:
        mailbox = assert_allowed_mailbox(mailbox)
        detail = self.request(
            "GET",
            self._messages_url(mailbox, message_id, f"attachments/{quote(attachment_id, safe='')}"),
        )
        if detail.status_code != 200:
            LOGGER.info("Graph attachment GET HTTP %s", detail.status_code)
            return None
        payload = detail.json() or {}
        encoded = payload.get("contentBytes")
        if encoded:
            try:
                return base64.b64decode(encoded)
            except (ValueError, TypeError):
                LOGGER.info("Graph attachment contentBytes were not valid base64")
                return None
        raw = self.request(
            "GET",
            self._messages_url(mailbox, message_id, f"attachments/{quote(attachment_id, safe='')}/$value"),
        )
        if raw.status_code != 200:
            LOGGER.info("Graph attachment $value HTTP %s", raw.status_code)
            return None
        return raw.content

    def flag_matched(self, mailbox: str, message_id: str) -> str:
        """PATCH flagStatus=flagged, then best-effort category AP Matched.

        A category failure does not undo the flag. 403 is graph-denied.
        """
        mailbox = assert_allowed_mailbox(mailbox)
        if not str(message_id or "").strip():
            return FLAG_NO_MESSAGE_ID
        url = self._messages_url(mailbox, message_id)
        response = self.request(
            "PATCH",
            url,
            json={"flag": {"flagStatus": "flagged"}},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 403:
            LOGGER.info("Graph flag PATCH HTTP 403 (Mail.ReadWrite missing or denied)")
            return FLAG_DENIED
        if response.status_code >= 400:
            LOGGER.info("Graph flag PATCH HTTP %s", response.status_code)
            return FLAG_DENIED
        self._try_set_category(mailbox, message_id)
        return FLAG_FLAGGED

    def _try_set_category(self, mailbox: str, message_id: str) -> None:
        try:
            current = self.get_message(mailbox, message_id, select="id,categories,flag")
            categories = [str(c) for c in (current.get("categories") or []) if c]
            if AP_MATCHED_CATEGORY not in categories:
                categories.append(AP_MATCHED_CATEGORY)
            response = self.request(
                "PATCH",
                self._messages_url(mailbox, message_id),
                json={"categories": categories},
                headers={"Content-Type": "application/json"},
            )
            if response.status_code >= 400:
                LOGGER.info("Graph category PATCH HTTP %s (flag kept)", response.status_code)
        except GraphError:
            LOGGER.info("Graph category write failed (flag kept)")


def apply_flag_after_match(
    row: dict[str, Any],
    invoice: dict[str, Any],
    graph_client: GraphClient | None,
    *,
    mailbox: str = ALLOWED_MAILBOX,
) -> str:
    """Set row['Flag status'] after enter. Never flags HOLD/Fail/no-header rows."""
    message_id = str(invoice.get("graph_message_id") or invoice.get("graphMessageId") or "").strip()
    decision = decide_flag_status(
        result=str(row.get("Result") or ""),
        kimco_id=row.get("KIMCO id"),
        message_id=message_id,
    )
    if decision != FLAG_ELIGIBLE:
        row["Flag status"] = decision
        return decision
    if graph_client is None:
        row["Flag status"] = FLAG_DENIED
        return FLAG_DENIED
    try:
        status = graph_client.flag_matched(mailbox, message_id)
    except MailboxRejected:
        raise
    except GraphError:
        status = FLAG_DENIED
    row["Flag status"] = status
    why = str(row.get("Why") or "").rstrip()
    note = f"Flag status={status}."
    row["Why"] = f"{why} {note}".strip() if why else note
    return status
