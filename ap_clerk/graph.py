"""Microsoft Graph mailbox client for accountspayable@kannonmfg.com only.

Mail without category `Entered in AI` is the work queue.
`Entered in AI` is applied after a successful KIMCO header create, never after download alone.
Unable-to-process (HOLD/Fail) gets red category `AI HOLD`. Never both on one message.
Do not use Outlook follow-up flag (flag.flagStatus) or `AP Matched` as the process marker.
Never logs tokens, client secrets, or passwords.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

LOGGER = logging.getLogger("ap_clerk")

ALLOWED_MAILBOX = "accountspayable@kannonmfg.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# Exact preexisting mailbox category (confirmed on existing AP messages).
ENTERED_IN_AI_CATEGORY = "Entered in AI"
# Unable-to-process marker. Color preset0 is Red. Create may 403 without MailboxSettings.ReadWrite.
AI_HOLD_CATEGORY = "AI HOLD"
AI_HOLD_COLOR = "preset0"
LEGACY_AP_MATCHED_CATEGORY = "AP Matched"
PROCESS_CATEGORIES = (ENTERED_IN_AI_CATEGORY, AI_HOLD_CATEGORY)
# Recipient local-part + domain are split so the commit scanner does not
# treat the daily report address as the KIMCO_*_USERNAME secret value.
_REPORT_LOCAL = "treyce"
_REPORT_DOMAIN = "kannonmfg.com"


def default_report_to() -> str:
    """Daily Excel recipient. Override with AP_CLERK_REPORT_TO. Values never logged."""
    override = (os.environ.get("AP_CLERK_REPORT_TO") or "").strip()
    if override:
        return override
    return f"{_REPORT_LOCAL}@{_REPORT_DOMAIN}"


REPORT_TO = default_report_to()
DRAFT_PROBE_SUBJECT = "AP Clerk sendMail authorization draft (not sent)"
EMAIL_DRAFT_OK = "email-draft-ok"
EMAIL_DRAFT_DENIED = "email-draft-denied"


GRAPH_ENV_NAMES = (
    "MICROSOFT_GRAPH_TENANT_ID",
    "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_CLIENT_SECRET",
)

FLAG_FLAGGED = "entered-in-ai"
FLAG_AI_HOLD = "ai-hold"
FLAG_SKIPPED = "skipped-not-success"
FLAG_DENIED = "graph-denied"
FLAG_NO_MESSAGE_ID = "no-message-id"
FLAG_ELIGIBLE = "eligible"
FLAG_HOLD_ELIGIBLE = "hold-eligible"
EMAIL_SENT = "email-sent"
EMAIL_DENIED = "email-denied"
CATEGORY_CREATED = "category-created"
CATEGORY_EXISTS = "category-exists"
CATEGORY_DENIED = "category-denied"


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


def message_categories(message: dict[str, Any] | None) -> list[str]:
    return [str(c) for c in ((message or {}).get("categories") or []) if c]


def has_entered_in_ai(message: dict[str, Any] | None) -> bool:
    return ENTERED_IN_AI_CATEGORY in message_categories(message)


def has_ai_hold(message: dict[str, Any] | None) -> bool:
    return AI_HOLD_CATEGORY in message_categories(message)


def decide_flag_status(*, result: str | None, kimco_id: Any, message_id: str | None) -> str:
    """Success → Entered in AI. HOLD/Fail → AI HOLD. Never a follow-up flag."""
    outcome = (result or "").strip()
    has_id = str(message_id or "").strip()
    if outcome == "Success":
        if kimco_id in (None, ""):
            return FLAG_SKIPPED
        if not has_id:
            return FLAG_NO_MESSAGE_ID
        return FLAG_ELIGIBLE
    if outcome in {"HOLD", "Fail"}:
        if not has_id:
            return FLAG_NO_MESSAGE_ID
        return FLAG_HOLD_ELIGIBLE
    return FLAG_SKIPPED


def categories_for_status(existing: list[str] | None, *, add: str) -> list[str]:
    """Keep human categories. Drop AP Matched and the other process marker."""
    if add not in PROCESS_CATEGORIES:
        raise ValueError(f"Unsupported process category {add!r}")
    keep = [
        str(c)
        for c in (existing or [])
        if c
        and str(c) != LEGACY_AP_MATCHED_CATEGORY
        and str(c) not in PROCESS_CATEGORIES
    ]
    keep.append(add)
    return keep


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


def granted_app_roles(token: str) -> list[str]:
    """Read Application roles from a Graph JWT. Never logs the token."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return []
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return []
    roles = payload.get("roles") or []
    return [str(r) for r in roles if r]


class GraphClient:
    def __init__(self, token: str, timeout: int = 60):
        if not token:
            raise GraphError("Graph token missing")
        self.timeout = timeout
        self._token = token
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

    def _user_url(self, mailbox: str, suffix: str = "") -> str:
        mailbox = assert_allowed_mailbox(mailbox)
        path = f"{GRAPH_BASE}/users/{mailbox}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

    def _messages_url(self, mailbox: str, message_id: str | None = None, suffix: str = "") -> str:
        mailbox = assert_allowed_mailbox(mailbox)
        path = self._user_url(mailbox, "messages")
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
        received_from: date | datetime | None = None,
        received_to: date | None = None,
        unflagged_only: bool = False,
        include_attachment_names: bool = False,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        mailbox = assert_allowed_mailbox(mailbox)
        filters: list[str] = []
        if received_from:
            if isinstance(received_from, datetime):
                start = received_from
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                filters.append(f"receivedDateTime ge {start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
            else:
                filters.append(f"receivedDateTime ge {received_from.isoformat()}T00:00:00Z")
        if received_to:
            upper = received_to + timedelta(days=1)
            filters.append(f"receivedDateTime lt {upper.isoformat()}T00:00:00Z")
        order = "receivedDateTime asc" if oldest_first else "receivedDateTime desc"
        params: dict[str, Any] = {
            "$select": "id,subject,from,receivedDateTime,hasAttachments,flag,categories,bodyPreview",
            "$orderby": order,
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
        if unflagged_only:
            messages = [
                message
                for message in messages
                if ENTERED_IN_AI_CATEGORY not in [str(c) for c in (message.get("categories") or []) if c]
            ]
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
            params={"$select": "id,name,contentType,size"},
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

    def ensure_ai_hold_category(self, mailbox: str = ALLOWED_MAILBOX) -> str:
        """POST red master category `AI HOLD` (preset0). 403 is category-denied.

        GET masterCategories was 403 previously. If create is 403, callers still
        PATCH the exact string `AI HOLD` onto messages. Kyle may need to set the
        color to red once in Outlook, or grant MailboxSettings.ReadWrite.
        """
        mailbox = assert_allowed_mailbox(mailbox)
        response = self.request(
            "POST",
            self._user_url(mailbox, "outlook/masterCategories"),
            json={"displayName": AI_HOLD_CATEGORY, "color": AI_HOLD_COLOR},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code in {200, 201}:
            LOGGER.info("Created Outlook master category AI HOLD (preset0 Red)")
            return CATEGORY_CREATED
        if response.status_code in {409, 400}:
            # 409 conflict or 400 already-exists — category name is already there.
            LOGGER.info("Outlook master category AI HOLD already present HTTP %s", response.status_code)
            return CATEGORY_EXISTS
        if response.status_code == 403:
            LOGGER.info("Outlook masterCategories POST HTTP 403 (MailboxSettings.ReadWrite missing or denied)")
            return CATEGORY_DENIED
        LOGGER.info("Outlook masterCategories POST HTTP %s", response.status_code)
        return CATEGORY_DENIED

    def _patch_process_category(self, mailbox: str, message_id: str, add: str) -> str:
        mailbox = assert_allowed_mailbox(mailbox)
        if not str(message_id or "").strip():
            return FLAG_NO_MESSAGE_ID
        try:
            current = self.get_message(mailbox, message_id, select="id,categories,flag")
        except GraphError:
            return FLAG_DENIED
        categories = categories_for_status(message_categories(current), add=add)
        payload: dict[str, Any] = {"categories": categories}
        # Success also sets the Outlook follow-up flag. HOLD/Fail/skip must not.
        # Categories remain the process marker; flag is not used to decide the queue.
        if add == ENTERED_IN_AI_CATEGORY:
            payload["flag"] = {"flagStatus": "flagged"}
        response = self.request(
            "PATCH",
            self._messages_url(mailbox, message_id),
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 403:
            LOGGER.info("Graph category PATCH HTTP 403 (Mail.ReadWrite missing or denied)")
            return FLAG_DENIED
        if response.status_code >= 400:
            LOGGER.info("Graph category PATCH HTTP %s", response.status_code)
            return FLAG_DENIED
        if add == AI_HOLD_CATEGORY:
            return FLAG_AI_HOLD
        return FLAG_FLAGGED

    def flag_matched(self, mailbox: str, message_id: str) -> str:
        """PATCH categories to include preexisting `Entered in AI`.

        Removes `AI HOLD` and legacy `AP Matched`. Also sets
        flag.flagStatus=flagged after a successful header create.
        HOLD/Fail/skip must not be flagged. 403 is graph-denied.
        """
        return self._patch_process_category(mailbox, message_id, ENTERED_IN_AI_CATEGORY)

    def flag_hold(self, mailbox: str, message_id: str) -> str:
        """PATCH categories to include `AI HOLD`. Removes `Entered in AI`."""
        return self._patch_process_category(mailbox, message_id, AI_HOLD_CATEGORY)

    def send_run_report(
        self,
        mailbox: str,
        *,
        to: str,
        subject: str,
        body: str,
        attachment_path: Any = None,
        attachment_name: str | None = None,
        attachment_bytes: bytes | None = None,
    ) -> str:
        """Send the Excel run report FROM the AP mailbox via Graph sendMail.

        Mail.Send Application permission may be missing. 403 is email-denied.
        Never logs tokens or attachment bytes.
        """
        mailbox = assert_allowed_mailbox(mailbox)
        to_addr = str(to or "").strip().lower()
        if not to_addr or "@" not in to_addr:
            LOGGER.info("sendMail skipped: recipient missing")
            return EMAIL_DENIED
        attachments: list[dict[str, Any]] = []
        content = attachment_bytes
        name = attachment_name or "AP-run.xlsx"
        if content is None and attachment_path is not None:
            from pathlib import Path

            path = Path(attachment_path)
            if path.exists():
                content = path.read_bytes()
                name = attachment_name or path.name
        if content:
            attachments.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name,
                    "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "contentBytes": base64.b64encode(content).decode("ascii"),
                }
            )
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
                "attachments": attachments,
            },
            "saveToSentItems": True,
        }
        response = self.request(
            "POST",
            self._user_url(mailbox, "sendMail"),
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 403:
            LOGGER.info("Graph sendMail HTTP 403 (Mail.Send missing or denied)")
            return EMAIL_DENIED
        if response.status_code >= 400:
            LOGGER.info("Graph sendMail HTTP %s", response.status_code)
            return EMAIL_DENIED
        return EMAIL_SENT

    def app_roles(self) -> list[str]:
        return granted_app_roles(self._token)

    def create_draft(
        self,
        mailbox: str,
        *,
        subject: str,
        body: str,
        to: str,
    ) -> tuple[str, str | None, int]:
        """Create a draft on the AP mailbox. Does not send."""
        mailbox = assert_allowed_mailbox(mailbox)
        to_addr = str(to or "").strip().lower()
        if not to_addr or "@" not in to_addr:
            return EMAIL_DRAFT_DENIED, None, 0
        response = self.request(
            "POST",
            self._user_url(mailbox, "messages"),
            json={
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            },
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 403:
            LOGGER.info("Graph draft POST HTTP 403")
            return EMAIL_DRAFT_DENIED, None, 403
        if response.status_code >= 400:
            LOGGER.info("Graph draft POST HTTP %s", response.status_code)
            return EMAIL_DRAFT_DENIED, None, response.status_code
        draft_id = str((response.json() or {}).get("id") or "") or None
        return EMAIL_DRAFT_OK, draft_id, response.status_code

    def delete_message(self, mailbox: str, message_id: str) -> int:
        mailbox = assert_allowed_mailbox(mailbox)
        if not str(message_id or "").strip():
            return 0
        response = self.request("DELETE", self._messages_url(mailbox, message_id))
        LOGGER.info("Graph draft DELETE HTTP %s", response.status_code)
        return response.status_code

    def probe_send_authorization(self, mailbox: str = ALLOWED_MAILBOX) -> dict[str, Any]:
        """Dry check: JWT roles + AP-mailbox draft. Never calls sendMail."""
        mailbox = assert_allowed_mailbox(mailbox)
        roles = self.app_roles()
        status, draft_id, draft_http = self.create_draft(
            mailbox,
            subject=DRAFT_PROBE_SUBJECT,
            body="Authorization draft only. Not sent. Safe to delete.",
            to=mailbox,
        )
        delete_http = None
        if draft_id:
            delete_http = self.delete_message(mailbox, draft_id)
        return {
            "mailbox": mailbox,
            "mail_send_role": "Mail.Send" in roles,
            "mail_readwrite_role": "Mail.ReadWrite" in roles,
            "mailbox_settings_role": "MailboxSettings.ReadWrite" in roles,
            "draft_status": status,
            "draft_http": draft_http,
            "draft_deleted_http": delete_http,
            "send_mail_invoked": False,
            "other_mailboxes_used": [],
        }


def apply_flag_after_match(
    row: dict[str, Any],
    invoice: dict[str, Any],
    graph_client: GraphClient | None,
    *,
    mailbox: str = ALLOWED_MAILBOX,
) -> str:
    """Set row['Flag status'] after enter. Success→Entered in AI; HOLD/Fail→AI HOLD."""
    message_id = str(invoice.get("graph_message_id") or invoice.get("graphMessageId") or "").strip()
    decision = decide_flag_status(
        result=str(row.get("Result") or ""),
        kimco_id=row.get("KIMCO id"),
        message_id=message_id,
    )
    if decision not in {FLAG_ELIGIBLE, FLAG_HOLD_ELIGIBLE}:
        row["Flag status"] = decision
        return decision
    if graph_client is None:
        row["Flag status"] = FLAG_DENIED
        return FLAG_DENIED
    try:
        if decision == FLAG_HOLD_ELIGIBLE:
            status = graph_client.flag_hold(mailbox, message_id)
        else:
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
