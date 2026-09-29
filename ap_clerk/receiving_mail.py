"""receiving@ Graph: GET mail/PDFs + PATCH `AI Completed`. Never sendMail.

Invoice GraphClient stays locked to accountspayable@. This client refuses
every mailbox except receiving@kannonmfg.com and refuses sendMail.
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import quote

import requests

from ap_clerk.graph import (
    FLAG_DENIED,
    FLAG_NO_MESSAGE_ID,
    GRAPH_BASE,
    GraphError,
    MailboxRejected,
    graph_http_detail,
    message_categories,
)
from ap_clerk.packing_slips import AI_COMPLETED_CATEGORY, decide_receiving_ai_completed
from ap_clerk.receiving_probe import RECEIVING_MAILBOX, assert_receiving_mailbox

LOGGER = logging.getLogger("ap_clerk.receiving")

AI_COMPLETED = AI_COMPLETED_CATEGORY
FLAG_AI_COMPLETED = "ai-completed"
FLAG_LEFT_UNCATEGORIZED = "left-uncategorized"


class ReceivingGraph:
    """GET + category PATCH on receiving@ only. No sendMail. No AP mailbox."""

    timeout = 60

    def __init__(self, token: str):
        self._token = token
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _user_url(self, mailbox: str, suffix: str = "") -> str:
        mailbox = assert_receiving_mailbox(mailbox)
        path = f"{GRAPH_BASE}/users/{mailbox}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

    def _messages_url(self, mailbox: str, message_id: str | None = None, suffix: str = "") -> str:
        mailbox = assert_receiving_mailbox(mailbox)
        path = self._user_url(mailbox, "messages")
        if message_id:
            path = f"{path}/{quote(str(message_id), safe='')}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        blob = (url or "").lower()
        if "graph.microsoft.com" not in blob and "login.microsoftonline.com" not in blob:
            raise GraphError("Receiving Graph refuses non-Graph hosts")
        if "sendmail" in blob:
            raise GraphError("Receiving Graph refuses sendMail")
        if RECEIVING_MAILBOX not in blob and "/oauth2/" not in blob:
            raise MailboxRejected(f"Refusing Graph URL that is not {RECEIVING_MAILBOX}")
        verb = (method or "").upper()
        if verb not in {"GET", "PATCH"}:
            raise GraphError(f"Receiving Graph refuses {verb}. GET and category PATCH only.")
        return self.session.request(method, url, timeout=self.timeout, **kwargs)

    def list_messages(self, mailbox: str = RECEIVING_MAILBOX, *, top: int = 15) -> list[dict[str, Any]]:
        mailbox = assert_receiving_mailbox(mailbox)
        response = self.request(
            "GET",
            self._messages_url(mailbox),
            params={
                "$select": "id,subject,from,receivedDateTime,hasAttachments,categories,bodyPreview",
                "$orderby": "receivedDateTime desc",
                "$top": max(1, min(int(top), 50)),
            },
        )
        if response.status_code != 200:
            extra = graph_http_detail(response)
            suffix = f" ({extra})" if extra else ""
            raise GraphError(f"Receiving list messages HTTP {response.status_code}{suffix}")
        return list((response.json() or {}).get("value") or [])

    def get_message(
        self,
        mailbox: str,
        message_id: str,
        *,
        select: str = "id,subject,categories",
    ) -> dict[str, Any]:
        mailbox = assert_receiving_mailbox(mailbox)
        response = self.request(
            "GET",
            self._messages_url(mailbox, message_id),
            params={"$select": select},
        )
        if response.status_code != 200:
            extra = graph_http_detail(response)
            suffix = f" ({extra})" if extra else ""
            raise GraphError(f"Receiving get message HTTP {response.status_code}{suffix}")
        return response.json() or {}

    def list_attachments(self, mailbox: str, message_id: str) -> list[dict[str, Any]]:
        mailbox = assert_receiving_mailbox(mailbox)
        if not message_id:
            return []
        response = self.request(
            "GET",
            self._messages_url(mailbox, message_id, "attachments"),
            params={"$select": "id,name,contentType,size"},
        )
        if response.status_code != 200:
            LOGGER.info("Receiving attachment list HTTP %s", response.status_code)
            return []
        return [item for item in (response.json() or {}).get("value") or [] if isinstance(item, dict)]

    def download_pdf_attachments(self, mailbox: str, message_id: str) -> list[tuple[str, bytes]]:
        mailbox = assert_receiving_mailbox(mailbox)
        out: list[tuple[str, bytes]] = []
        for item in self.list_attachments(mailbox, message_id):
            name = str(item.get("name") or "scan.pdf")
            if not name.lower().endswith(".pdf"):
                continue
            attachment_id = str(item.get("id") or "")
            raw = self._attachment_bytes(mailbox, message_id, item, attachment_id)
            if raw:
                out.append((name, raw))
        return out

    def _attachment_bytes(
        self,
        mailbox: str,
        message_id: str,
        item: dict[str, Any],
        attachment_id: str,
    ) -> bytes | None:
        encoded = item.get("contentBytes")
        if encoded:
            try:
                return base64.b64decode(encoded)
            except (ValueError, TypeError):
                LOGGER.info("Receiving attachment contentBytes were not valid base64")
        if not attachment_id:
            return None
        raw = self.request(
            "GET",
            self._messages_url(mailbox, message_id, f"attachments/{quote(attachment_id, safe='')}/$value"),
        )
        if raw.status_code != 200:
            LOGGER.info("Receiving attachment $value HTTP %s", raw.status_code)
            return None
        return raw.content

    def flag_ai_completed(self, mailbox: str, message_id: str) -> str:
        """PATCH receiving@ categories to include exact `AI Completed`. No sendMail."""
        mailbox = assert_receiving_mailbox(mailbox)
        if not str(message_id or "").strip():
            return FLAG_NO_MESSAGE_ID
        try:
            current = self.get_message(mailbox, message_id, select="id,categories")
        except GraphError:
            return FLAG_DENIED
        keep = [c for c in message_categories(current) if c != AI_COMPLETED_CATEGORY]
        payload = {"categories": keep + [AI_COMPLETED_CATEGORY]}
        response = self.request(
            "PATCH",
            self._messages_url(mailbox, message_id),
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 403:
            LOGGER.info("Receiving AI Completed PATCH HTTP 403")
            return FLAG_DENIED
        if response.status_code >= 400:
            LOGGER.info("Receiving AI Completed PATCH HTTP %s", response.status_code)
            return FLAG_DENIED
        return FLAG_AI_COMPLETED


def stamp_receiving_ai_completed(
    graph: ReceivingGraph,
    *,
    mailbox: str,
    message_id: str,
    slip_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Category the receiving@ email only when decide_receiving_ai_completed says stamp."""
    decision = decide_receiving_ai_completed(slip_results)
    if not decision.get("stamp"):
        return {
            **decision,
            "outlook": FLAG_LEFT_UNCATEGORIZED,
            "mail_sent": False,
        }
    outlook = graph.flag_ai_completed(mailbox, message_id)
    return {
        **decision,
        "outlook": outlook,
        "mail_sent": False,
        "stamp": outlook == FLAG_AI_COMPLETED,
        "category": AI_COMPLETED_CATEGORY if outlook == FLAG_AI_COMPLETED else "",
    }
