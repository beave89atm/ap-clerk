"""Select vendor invoices from the AP mailbox.

Daily FIFO starts 2026-07-28 America/Chicago and walks toward today.
Mail categorized `Entered in AI` is already processed and is skipped.
Not-a-bill / CHECK STOP / statement / POD skips are replaced so a real-bill
limit can still be filled; those skips get `AI HOLD` when Graph can write.
Does not use the Outlook follow-up flag.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ap_clerk.cursor import DailyCursor, daily_floor_datetime, should_skip_already_seen
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FLAG_AI_HOLD,
    FLAG_DENIED,
    GraphClient,
    assert_allowed_mailbox,
    has_ai_hold,
    has_entered_in_ai,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.rules import classify_mail

STATEMENT_FILE_RE = re.compile(r"statement|custstate|pastdue|past[_ -]?due|aging", flags=re.I)

LOGGER = logging.getLogger("ap_clerk")

SKIP_CLASSES = {"statement", "pod", "payment", "not-a-bill", "check_stop"}
HOLD_SKIP_CLASSES = {"statement", "pod", "payment", "not-a-bill", "check_stop", "unreadable-or-not-a-bill"}


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


def _mark_skip_hold(
    graph: GraphClient,
    mailbox: str,
    message: dict[str, Any],
    *,
    mark_skips: bool,
) -> str:
    """Apply AI HOLD on unable-to-process mail. Does not set follow-up flag."""
    if not mark_skips:
        return "skipped-not-success"
    message_id = str(message.get("id") or "")
    if not message_id:
        return "no-message-id"
    if has_entered_in_ai(message):
        return "entered-in-ai"
    try:
        return graph.flag_hold(mailbox, message_id)
    except Exception:  # noqa: BLE001 - skip mark must not fail the run
        LOGGER.info("AI HOLD on skip failed without raising run")
        return FLAG_DENIED


def pull_recent_bills(
    graph: GraphClient,
    *,
    mailbox: str = ALLOWED_MAILBOX,
    limit: int = 20,
    received_from: date | datetime | None = None,
    received_to: date | None = None,
    pdf_dir: Path,
    max_messages: int = 200,
    fifo: bool = False,
    unprocessed_only: bool = False,
    cursor: DailyCursor | None = None,
    mark_skips: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (bills, skipped).

    Default (fifo=False): most-recent `limit` real bills, then oldest-first.
    Daily FIFO (fifo=True): from 2026-07-28 or the persisted cursor, oldest
    received first, toward today. Skip `Entered in AI`. Replace not-a-bill
    skips so `limit` real bills are still attempted when possible.
    """
    mailbox = assert_allowed_mailbox(mailbox)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    start = received_from
    after = cursor or DailyCursor()
    if fifo:
        floor = daily_floor_datetime()
        cursor_dt = after.after_received()
        if cursor_dt is not None and cursor_dt > floor:
            start = cursor_dt
        elif start is None:
            start = floor
        elif isinstance(start, date) and not isinstance(start, datetime):
            start = datetime.combine(start, datetime.min.time(), tzinfo=floor.tzinfo)
            if start < floor:
                start = floor
        if unprocessed_only is False:
            unprocessed_only = True
    messages = graph.list_messages(
        mailbox,
        received_from=start,
        received_to=received_to,
        unflagged_only=unprocessed_only,
        include_attachment_names=False,
        oldest_first=fifo,
    )
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    scanned = 0
    examined: list[dict[str, Any]] = []
    for message in messages:
        if len(selected) >= limit:
            break
        if scanned >= max_messages:
            break
        scanned += 1
        if should_skip_already_seen(message, after):
            continue
        if fifo:
            received = str(message.get("receivedDateTime") or "")
            try:
                received_dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
            except ValueError:
                received_dt = None
            if received_dt is not None and received_dt < daily_floor_datetime():
                continue
        if unprocessed_only and (has_entered_in_ai(message) or has_ai_hold(message)):
            skipped.append(
                {
                    "subject": str(message.get("subject") or ""),
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "already-processed",
                    "graph_message_id": str(message.get("id") or ""),
                    "hold_reason": "already-processed",
                }
            )
            continue
        if sender_address(message) and "kyle" in sender_address(message) and "kannon" not in sender_address(message):
            # Never treat Kyle's personal inbox as a source; this mailbox is AP-only.
            continue
        subject = str(message.get("subject") or "")
        preview = str(message.get("bodyPreview") or "")
        message_id = str(message.get("id") or "")
        names = graph.list_attachment_names(mailbox, message_id) if message.get("hasAttachments") else []
        message["attachment_names"] = names
        examined.append(message)
        if any(STATEMENT_FILE_RE.search(n or "") for n in names):
            klass = "statement"
        else:
            klass = classify_mail(subject=subject, attachment_names=names, preview=preview)
        if klass in SKIP_CLASSES:
            flag_status = _mark_skip_hold(graph, mailbox, message, mark_skips=mark_skips)
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": klass,
                    "attachment_names": names,
                    "graph_message_id": message_id,
                    "vendor": sender_name(message),
                    "Flag status": flag_status,
                    "hold_reason": klass,
                }
            )
            LOGGER.info("Skipping %s mail: %s", klass, subject[:80])
            continue
        if not message.get("hasAttachments"):
            flag_status = _mark_skip_hold(graph, mailbox, message, mark_skips=mark_skips)
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "no-attachment",
                    "attachment_names": names,
                    "graph_message_id": message_id,
                    "vendor": sender_name(message),
                    "Flag status": flag_status,
                    "hold_reason": "not-a-bill",
                }
            )
            continue
        pdfs = graph.download_pdf_attachments(mailbox, message_id)
        if not pdfs:
            flag_status = _mark_skip_hold(graph, mailbox, message, mark_skips=mark_skips)
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "no-pdf",
                    "attachment_names": names,
                    "graph_message_id": message_id,
                    "vendor": sender_name(message),
                    "Flag status": flag_status,
                    "hold_reason": "not-a-bill",
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
                flag_status = _mark_skip_hold(graph, mailbox, message, mark_skips=mark_skips)
                skipped.append(
                    {
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "class": "check_stop",
                        "attachment_names": names,
                        "invoice_number": parsed.get("invoice_number"),
                        "graph_message_id": message_id,
                        "vendor": parsed.get("vendor") or from_name,
                        "Flag status": flag_status,
                        "hold_reason": "CHECK STOP",
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
                flag_status = _mark_skip_hold(graph, mailbox, message, mark_skips=mark_skips)
                skipped.append(
                    {
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "class": "unreadable-or-not-a-bill",
                        "attachment_names": names,
                        "graph_message_id": message_id,
                        "vendor": from_name,
                        "Flag status": flag_status,
                        "hold_reason": "not-a-bill",
                    }
                )
            continue
        chosen["graph_message_id"] = message_id
        chosen["subject"] = subject
        chosen["receivedDateTime"] = message.get("receivedDateTime")
        chosen["from_name"] = from_name
        chosen["action"] = "create"
        chosen["id"] = message_id
        # Invoice date is the date printed on the invoice, never email received.
        selected.append(chosen)
        LOGGER.info(
            "Selected bill %s/%s vendor=%s invoice=%s received=%s",
            len(selected),
            limit,
            chosen.get("vendor"),
            chosen.get("invoice_number"),
            chosen.get("receivedDateTime"),
        )

    if not fifo:
        # Process oldest-first among the most-recent `limit`
        selected.sort(key=lambda inv: str(inv.get("receivedDateTime") or ""))
    return selected, skipped


def skip_rows_for_report(skipped: list[dict[str, Any]], batch_name: str) -> list[dict[str, Any]]:
    """Excel HOLD rows for inbox skips that received AI HOLD (or a skip reason)."""
    rows = []
    for item in skipped:
        if item.get("class") not in HOLD_SKIP_CLASSES and item.get("hold_reason") not in HOLD_SKIP_CLASSES:
            continue
        reason = item.get("hold_reason") or item.get("class") or "not-a-bill"
        flag_status = item.get("Flag status") or FLAG_AI_HOLD
        why = f"HOLD: {reason}. Do not create a header."
        if flag_status:
            why = f"{why} Flag status={flag_status}."
        received = str(item.get("receivedDateTime") or "")
        rows.append(
            {
                "Vendor": item.get("vendor") or "",
                "Invoice #": item.get("invoice_number") or "",
                "date": received[:10],
                "PO": "",
                "Amount": "",
                "Result": "HOLD",
                "Why": why,
                "KIMCO id": "",
                "Batch": batch_name,
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "no-pdf-on-vm",
                "Flag status": flag_status,
                "Flag in Outlook": "Yes",
                "graph_message_id": item.get("graph_message_id") or "",
                "receivedDateTime": received,
            }
        )
    return rows
