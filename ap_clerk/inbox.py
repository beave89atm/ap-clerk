"""Select the 20 most recent vendor invoices from the AP mailbox. Does not flag mail."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, assert_allowed_mailbox
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.rules import classify_mail, parse_iso_date

STATEMENT_FILE_RE = re.compile(r"statement|custstate|pastdue|past[_ -]?due|aging", flags=re.I)

LOGGER = logging.getLogger("ap_clerk")

SKIP_CLASSES = {"statement", "pod", "payment", "not-a-bill", "check_stop"}


def sender_name(message: dict[str, Any]) -> str:
    frm = message.get("from") or {}
    email_addr = frm.get("emailAddress") or frm
    if isinstance(email_addr, dict):
        return str(email_addr.get("name") or email_addr.get("address") or "")
    return str(email_addr or "")


def sender_address(message: dict[str, Any]) -> str:
    frm = message.get("from") or {}
    email_addr = frm.get("emailAddress") or frm
    if isinstance(email_addr, dict):
        return str(email_addr.get("address") or "").lower()
    return ""


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "invoice.pdf")
    return cleaned[:120] or "invoice.pdf"


def pull_recent_bills(
    graph: GraphClient,
    *,
    mailbox: str = ALLOWED_MAILBOX,
    limit: int = 20,
    received_from: date | None = None,
    received_to: date | None = None,
    pdf_dir: Path,
    max_messages: int = 200,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (bills oldest-first among the `limit` most recent, skipped).

    Does not flag or move mail. Skips statements, PODs, CHECK STOP, payment
    confirmations when selecting, and replaces each skip with the next older bill.
    """
    mailbox = assert_allowed_mailbox(mailbox)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    messages = graph.list_messages(
        mailbox,
        received_from=received_from,
        received_to=received_to,
        unflagged_only=False,
        include_attachment_names=False,
    )
    # Newest first from Graph $orderby receivedDateTime desc
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    scanned = 0
    for message in messages:
        if len(selected) >= limit:
            break
        if scanned >= max_messages:
            break
        scanned += 1
        if sender_address(message) and "kyle" in sender_address(message) and "kannon" not in sender_address(message):
            # Never treat Kyle's personal inbox as a source; this mailbox is AP-only.
            continue
        subject = str(message.get("subject") or "")
        preview = str(message.get("bodyPreview") or "")
        message_id = str(message.get("id") or "")
        names = graph.list_attachment_names(mailbox, message_id) if message.get("hasAttachments") else []
        message["attachment_names"] = names
        if any(STATEMENT_FILE_RE.search(n or "") for n in names):
            klass = "statement"
        else:
            klass = classify_mail(subject=subject, attachment_names=names, preview=preview)
        if klass in SKIP_CLASSES:
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": klass,
                    "attachment_names": names,
                }
            )
            LOGGER.info("Skipping %s mail: %s", klass, subject[:80])
            continue
        if not message.get("hasAttachments"):
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "no-attachment",
                    "attachment_names": names,
                }
            )
            continue
        pdfs = graph.download_pdf_attachments(mailbox, message_id)
        if not pdfs:
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "no-pdf",
                    "attachment_names": names,
                }
            )
            continue
        from_name = sender_name(message)
        from_addr = sender_address(message)
        chosen: dict[str, Any] | None = None
        for filename, content in pdfs:
            if STATEMENT_FILE_RE.search(filename or ""):
                continue
            dest = pdf_dir / f"{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_{_safe_filename(filename)}"
            if dest.exists():
                dest = pdf_dir / f"{len(selected)+len(skipped)}_{dest.name}"
            dest.write_bytes(content)
            parsed = parse_invoice_pdf(dest, subject=subject, from_name=from_name, from_address=from_addr)
            if parsed.get("check_stop"):
                skipped.append(
                    {
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "class": "check_stop",
                        "attachment_names": names,
                        "invoice_number": parsed.get("invoice_number"),
                    }
                )
                chosen = None
                break
            if parsed.get("pdf_text_empty") and parsed.get("amount") in (None, ""):
                continue
            if not parsed.get("invoice_number") and not parsed.get("amount"):
                continue
            chosen = parsed
            chosen["pdf_path"] = str(dest)
            break
        if not chosen:
            if not any(s.get("subject") == subject and s.get("class") == "check_stop" for s in skipped):
                skipped.append(
                    {
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "class": "unreadable-or-not-a-bill",
                        "attachment_names": names,
                    }
                )
            continue
        chosen["graph_message_id"] = message_id
        chosen["subject"] = subject
        chosen["receivedDateTime"] = message.get("receivedDateTime")
        chosen["from_name"] = from_name
        chosen["action"] = "create"
        if not chosen.get("date") and message.get("receivedDateTime"):
            try:
                chosen["date"] = parse_iso_date(str(message["receivedDateTime"])).isoformat()
            except ValueError:
                pass
        selected.append(chosen)
        LOGGER.info(
            "Selected bill %s/%s vendor=%s invoice=%s received=%s",
            len(selected),
            limit,
            chosen.get("vendor"),
            chosen.get("invoice_number"),
            chosen.get("receivedDateTime"),
        )

    # Process oldest-first among the most-recent `limit`
    selected.sort(key=lambda inv: str(inv.get("receivedDateTime") or ""))
    return selected, skipped
