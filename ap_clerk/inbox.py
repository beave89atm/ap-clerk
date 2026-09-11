"""Select vendor invoices from the AP mailbox.

Daily FIFO starts 2026-07-28 America/Chicago and walks toward today.
Mail categorized `Entered in AI` is already processed and is skipped.

Hard email cap 10 until further notice (Kyle 2026-09-11). Cap = mailbox
messages *touched* (Success, HOLD, Incomplete, Fail, Skipped/noise). Stop
after that many emails. Do not walk past noise to fill N bill attempts.
Bill-attempt mode is suspended until Kyle lifts this. Noise is still
sheet-noted as Skipped without Outlook `AI HOLD`.

Already-flagged mail is walked past without touching and does **not**
consume the cap: Outlook `Entered in AI`, `AI HOLD`, `Entered with issues`,
or Graph `flag.flagStatus=flagged`. Do not reprocess or re-stamp those.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ap_clerk.cursor import DailyCursor, daily_floor_datetime, should_skip_already_seen
from ap_clerk.gates import GATE_BILL_VS_NOISE, RESULT_SKIPPED, why_skipped
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FLAG_NONE,
    GraphClient,
    assert_allowed_mailbox,
    is_already_flagged,
)
from ap_clerk.pdf_invoice import PO_DOCUMENT_FILE_RE, parse_invoice_pdf
from ap_clerk.pdf_links import REASON_PDF_BEHIND_LINK, download_first_public_pdf
from ap_clerk.rules import (
    CHICAGO,
    LINK_DOWNLOAD_VENDOR_RE,
    classify_mail,
    is_melody_channell,
)

STATEMENT_FILE_RE = re.compile(
    r"statement|custstate|pastdue|past[_ -]?due|aging|account[_ -]?status|accountstatus",
    flags=re.I,
)
# A vendor invoice email may also attach the customer's PO. Do not enter the PO PDF as a bill
# (9/7 created Legacy 9888 / invoice # 58861 from Purchase_Order_58861.pdf).
PO_FILE_RE = PO_DOCUMENT_FILE_RE

LOGGER = logging.getLogger("ap_clerk")

# Kyle 2026-09-11 until further notice. Bill-attempt mode is suspended.
HARD_EMAIL_CAP = 10
DEFAULT_INBOX_LIMIT = HARD_EMAIL_CAP

SKIP_CLASSES = {"statement", "pod", "payment", "not-a-bill", "check_stop", "internal"}  # noise; consumes the email cap
HOLD_SKIP_CLASSES = {
    "statement",
    "pod",
    "payment",
    "not-a-bill",
    "check_stop",
    "internal",
    "unreadable-or-not-a-bill",
}
# Clear noise can skip before PDF download. Vague not-a-bill still inspects vendor PDFs (AQPC).
CLEAR_SKIP_CLASSES = {"statement", "pod", "payment", "check_stop", "internal"}


def _as_start_datetime(value: date | datetime | None) -> datetime | None:
    """Normalize a FIFO floor to an aware datetime (America/Chicago for dates)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=CHICAGO)
        return value
    return datetime.combine(value, datetime.min.time(), tzinfo=CHICAGO)


def fifo_start_datetime(
    *,
    received_from: date | datetime | None = None,
    cursor: DailyCursor | None = None,
) -> datetime:
    """Oldest FIFO start: max(7/28 floor, persisted cursor, explicit from-date).

    An explicit later date (Kyle: start 2026-08-16) wins over an earlier cursor
    so the next 10 does not restart at 7/28 or re-walk 8/15 leftovers.
    """
    candidates = [daily_floor_datetime()]
    cursor_dt = (cursor or DailyCursor()).after_received()
    if cursor_dt is not None:
        candidates.append(cursor_dt)
    explicit = _as_start_datetime(received_from)
    if explicit is not None:
        candidates.append(explicit)
    return max(candidates)


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


def _skip_flag_status(_message: dict[str, Any] | None = None) -> str:
    """Noise is sheet-noted only. Never stamp Outlook AI HOLD."""
    return FLAG_NONE


def clamp_email_limit(requested: int | None, *, default: int = HARD_EMAIL_CAP) -> int:
    """Hard-clamp a run to ≤10 mailbox messages (Kyle 2026-09-11)."""
    raw = default if requested is None else int(requested)
    return max(1, min(raw, HARD_EMAIL_CAP))


def pull_recent_bills(
    graph: GraphClient,
    *,
    mailbox: str = ALLOWED_MAILBOX,
    limit: int = DEFAULT_INBOX_LIMIT,
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

    `limit` is mailbox messages touched, not bill attempts. Hard-clamped
    to HARD_EMAIL_CAP (10) until Kyle lifts the 2026-09-11 rule. Noise,
    HOLD, Incomplete, Fail, and Success each consume one slot. Already
    flagged (process category or follow-up flag), already-seen, and
    pre-floor messages are walked past and do not consume the cap.

    Default (fifo=False): most-recent emails first, then oldest-first among
    selected bills. Daily FIFO (fifo=True): from 2026-07-28 or the persisted
    cursor, oldest received first, toward today. Noise is sheet-noted only
    — never Outlook AI HOLD.
    """
    mailbox = assert_allowed_mailbox(mailbox)
    limit = clamp_email_limit(limit)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    start = received_from
    after = cursor or DailyCursor()
    if fifo:
        start = fifo_start_datetime(received_from=received_from, cursor=after)
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
    touched = 0
    examined: list[dict[str, Any]] = []
    for message in messages:
        if touched >= limit:
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
            floor = start if isinstance(start, datetime) else daily_floor_datetime()
            if received_dt is not None and received_dt < floor:
                continue
        if is_already_flagged(message):
            skipped.append(
                {
                    "subject": str(message.get("subject") or ""),
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "already-flagged",
                    "graph_message_id": str(message.get("id") or ""),
                    "hold_reason": "already-flagged",
                }
            )
            LOGGER.info(
                "Walking past already-flagged mail (does not consume email cap): %s",
                str(message.get("subject") or "")[:80],
            )
            continue
        if sender_address(message) and "kyle" in sender_address(message) and "kannon" not in sender_address(message):
            # Never treat Kyle's personal inbox as a source; this mailbox is AP-only.
            continue
        touched += 1
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
        if klass == "auto-pay":
            selected.append(
                {
                    "vendor": sender_name(message) or "Toyota Commercial Finance",
                    "invoice_number": "",
                    "date": None,
                    "po": None,
                    "amount": None,
                    "hold_reason": "auto-pay",
                    "action": "hold",
                    "graph_message_id": message_id,
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "from_name": sender_name(message),
                    "field_sources": {},
                }
            )
            LOGGER.info("HOLD auto-pay (do not enter): %s", subject[:80])
            continue
        if klass in CLEAR_SKIP_CLASSES:
            flag_status = _skip_flag_status(message)
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
        from_name = sender_name(message)
        link_blob = f"{from_name}\n{subject}\n{preview}"
        wants_link = bool(LINK_DOWNLOAD_VENDOR_RE.search(link_blob))
        if not message.get("hasAttachments") and not wants_link:
            flag_status = _skip_flag_status(message)
            skipped.append(
                {
                    "subject": subject,
                    "receivedDateTime": message.get("receivedDateTime"),
                    "class": "no-attachment",
                    "attachment_names": names,
                    "graph_message_id": message_id,
                    "vendor": from_name,
                    "Flag status": flag_status,
                    "hold_reason": "not-a-bill",
                }
            )
            continue
        pdfs = graph.download_pdf_attachments(mailbox, message_id) if message.get("hasAttachments") else []
        if not pdfs and wants_link:
            pdfs, link_hold = _pdfs_from_body_link(graph, mailbox, message_id, preview)
            if link_hold:
                selected.append(
                    {
                        "vendor": from_name or "American Quality Powder Coating",
                        "invoice_number": "",
                        "date": None,
                        "po": None,
                        "amount": None,
                        "hold_reason": "pdf-behind-link",
                        "pdf_behind_link": True,
                        "action": "hold",
                        "graph_message_id": message_id,
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "from_name": from_name,
                        "field_sources": {},
                    }
                )
                LOGGER.info("HOLD pdf-behind-link: %s", subject[:80])
                continue
        if not pdfs:
            flag_status = _skip_flag_status(message)
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
        from_addr = sender_address(message)
        chosen_bills: list[dict[str, Any]] = []
        check_stopped = False
        for filename, content in pdfs:
            if STATEMENT_FILE_RE.search(filename or "") or PO_FILE_RE.search(filename or ""):
                continue
            dest = pdf_dir / f"{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_{_safe_filename(filename)}"
            if dest.exists():
                dest = pdf_dir / f"{len(selected)+len(skipped)+len(chosen_bills)}_{dest.name}"
            dest.write_bytes(content)
            parsed = parse_invoice_pdf(dest, subject=subject, from_name=from_name, from_address=from_addr)
            if parsed.get("is_purchase_order_doc"):
                LOGGER.info("Skipping PO-not-invoice attachment %s", filename)
                continue
            if parsed.get("check_stop"):
                flag_status = _skip_flag_status(message)
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
                check_stopped = True
                break
            extras = list(parsed.pop("siblings", []) or [])
            for bill in [parsed, *extras]:
                if bill.get("pdf_text_empty") and bill.get("amount") in (None, ""):
                    continue
                if not bill.get("invoice_number") and not bill.get("amount"):
                    continue
                # Vendor invoices with a verified PDF must enter (AQPC).
                bill["pdf_path"] = str(dest)
                bill["graph_message_id"] = message_id
                bill["subject"] = subject
                bill["receivedDateTime"] = message.get("receivedDateTime")
                bill["from_name"] = from_name
                bill["action"] = "create"
                bill["id"] = message_id
                chosen_bills.append(bill)
        if check_stopped:
            continue
        if not chosen_bills:
            if is_melody_channell(f"{from_name} {subject} {preview}") or is_melody_channell(from_addr):
                selected.append(
                    {
                        "vendor": from_name or "Melody Channell",
                        "invoice_number": "",
                        "date": None,
                        "po": None,
                        "amount": None,
                        "hold_reason": "parse-error",
                        "action": "hold",
                        "graph_message_id": message_id,
                        "subject": subject,
                        "receivedDateTime": message.get("receivedDateTime"),
                        "from_name": from_name,
                        "field_sources": {},
                        "pdf_unavailable": True,
                    }
                )
                continue
            flag_status = _skip_flag_status(message)
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
        for chosen in chosen_bills:
            selected.append(chosen)
            LOGGER.info(
                "Touched email %s/%s selected bill vendor=%s invoice=%s received=%s",
                touched,
                limit,
                chosen.get("vendor"),
                chosen.get("invoice_number"),
                chosen.get("receivedDateTime"),
            )

    if not fifo:
        # Process oldest-first among the most-recent `limit`
        selected.sort(key=lambda inv: str(inv.get("receivedDateTime") or ""))
    return selected, skipped


def _pdfs_from_body_link(
    graph: GraphClient,
    mailbox: str,
    message_id: str,
    preview: str,
) -> tuple[list[tuple[str, bytes]], bool]:
    """Best-effort AQPC-style https PDF download. Auth wall → ( [], True )."""
    body_text = preview or ""
    getter = getattr(graph, "get_message", None)
    if callable(getter):
        try:
            detail = getter(mailbox, message_id, select="id,bodyPreview,body")
        except Exception:  # noqa: BLE001 - body fetch must not crash inbox
            detail = {}
        body = (detail or {}).get("body") or {}
        if isinstance(body, dict):
            body_text = str(body.get("content") or body_text)
        body_text = body_text or str((detail or {}).get("bodyPreview") or preview or "")
    downloader = getattr(graph, "download_public_pdf_from_text", None)
    if callable(downloader):
        result = downloader(body_text)
    else:
        result = download_first_public_pdf(body_text)
    if result.get("ok") and result.get("content"):
        return [("download.pdf", result["content"])], False
    if result.get("reason") == REASON_PDF_BEHIND_LINK:
        return [], True
    return [], True if wants_hold_without_pdf(body_text) else ([], False)


def wants_hold_without_pdf(text: str) -> bool:
    """If the body has an https link we tried, treat failure as pdf-behind-link."""
    return "https://" in (text or "").lower()


def skip_rows_for_report(skipped: list[dict[str, Any]], batch_name: str) -> list[dict[str, Any]]:
    """Excel Skipped rows for inbox noise. Never Flag in Outlook / AI HOLD."""
    rows = []
    for item in skipped:
        if item.get("class") not in HOLD_SKIP_CLASSES and item.get("hold_reason") not in HOLD_SKIP_CLASSES:
            continue
        reason = item.get("hold_reason") or item.get("class") or "not-a-bill"
        flag_status = FLAG_NONE
        subject = str(item.get("subject") or "")
        detail = f"{reason}. Do not create a header."
        if subject:
            detail = f"{reason}. Subject: {subject}. Do not create a header."
        why = why_skipped(GATE_BILL_VS_NOISE, detail)
        received = str(item.get("receivedDateTime") or "")
        rows.append(
            {
                "Vendor": item.get("vendor") or subject,
                "Invoice #": item.get("invoice_number") or "",
                "date": received[:10],
                "PO": "",
                "Amount": "",
                "Result": RESULT_SKIPPED,
                "Why": why,
                "KIMCO id": "",
                "Batch": batch_name,
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "no-pdf-on-vm",
                "Flag status": flag_status,
                "Flag in Outlook": "No",
                "Notes": "",
                "graph_message_id": item.get("graph_message_id") or "",
                "receivedDateTime": received,
                "subject": item.get("subject") or "",
            }
        )
    return rows
