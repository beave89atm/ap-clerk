"""API-only finish of existing Incomplete AP headers (QUALITY V1.1).

No KIMCO UI. Success requires header + (Select Receipts when PO) + PDF attached.
Never prints secrets.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from ap_clerk.daily import result_counts
from ap_clerk.gates import (
    RESULT_INCOMPLETE,
    RESULT_SUCCESS,
    finish_gate,
    header_created_with_issues,
    is_noise_result,
    receipts_required,
    selfcheck_payload,
    vendor_confirmation_gate,
    why_incomplete,
)
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FLAG_AI_HOLD,
    FLAG_ENTERED_WITH_ISSUES,
    FLAG_FLAGGED,
    FLAG_NONE,
    GraphClient,
    GraphError,
    MailboxRejected,
    apply_flag_after_match,
    score_message_for_invoice,
)
from ap_clerk.inbox import PO_FILE_RE, STATEMENT_FILE_RE
from ap_clerk.kimco import (
    KimcoClient,
    KimcoError,
    invoice_lines_from_record,
    receipt_ids_from_invoice_lines,
)
from ap_clerk.rules import flag_in_outlook_for, match_receipts, posted_vendor_fields

LOGGER = logging.getLogger("ap_clerk.finish")

DRY_SUBJECT = "AP dry run 10 — quality V1.1 (API finish)"
KIND_FINISH_UP = "finish-up"
KIND_NEW = "new"
KIND_PRIOR = "prior"


def dry_subject_for(from_date: date | None = None) -> str:
    """One Mail.Send subject. Kyle's 8/16 follow-up uses the from-date form."""
    if from_date is not None:
        return f"AP dry run 10 — from {from_date.isoformat()}"
    return DRY_SUBJECT


def dry_email_body(
    rows: list[dict[str, Any]],
    *,
    batch_label: str,
    from_date: date | None = None,
) -> str:
    counts = result_counts(rows)
    finish_ups = [row for row in rows if row.get("kind") == KIND_FINISH_UP]
    new_bills = [row for row in rows if row.get("kind") == KIND_NEW]
    start_line = (
        f"FIFO start: {from_date.isoformat()} America/Chicago (do not restart at 7/28).\n"
        if from_date is not None
        else ""
    )
    return (
        "Supervised 10-invoice LIVE dry run for Treyce / Kyle review (QUALITY V1.1).\n"
        "This is a supervised dry run, not the weekday daily 30, and does not re-arm that routine.\n"
        f"{start_line}"
        "API-only finish (record PUT Select Receipts + record attach with x-ms-blob-type BlockBlob).\n"
        "KIMCO UI was not opened.\n"
        f"Batch: {batch_label}\n"
        f"Success: {counts['Success']}\n"
        f"Incomplete: {counts['Incomplete']}\n"
        f"HOLD: {counts['HOLD']}\n"
        f"Fail: {counts['Fail']}\n"
        f"Skipped: {counts.get('Skipped', 0)}\n"
        f"Finish-ups (paused dry-run Incomplete headers): {len(finish_ups)}\n"
        f"New FIFO bills this pass: {len(new_bills)}\n"
        f"Mailbox: {ALLOWED_MAILBOX}\n"
        "Success = finished bill (header + Select Receipts when PO + PDF attached).\n"
        "Success = Entered in AI. Header+PDF unfinished = Entered with issues.\n"
        "Real bill with no header = AI HOLD. Skipped noise is sheet-noted only.\n"
        "Report attached.\n"
    )


def _po_from_row(row: dict[str, Any], inv: dict[str, Any] | None = None) -> str | None:
    po = row.get("PO")
    if po in (None, "", "None", "null"):
        po = (inv or {}).get("po")
    if po in (None, "", "None", "null"):
        return None
    return str(po)


def _pdf_bytes_for_invoice(
    inv: dict[str, Any],
    *,
    graph_client: GraphClient | None,
    mailbox: str,
    pdf_dir: Path | None,
) -> tuple[str | None, bytes | None]:
    explicit = inv.get("pdf_path")
    if explicit:
        path = Path(explicit)
        if path.exists() and path.suffix.lower() == ".pdf":
            return path.name, path.read_bytes()
    number = str(inv.get("invoice_number") or "")
    if pdf_dir and pdf_dir.exists() and number:
        matches = [p for p in pdf_dir.glob(f"*{number}*.pdf") if number.lower() in p.name.lower()]
        if matches:
            return matches[0].name, matches[0].read_bytes()
    message_id = str(inv.get("graph_message_id") or inv.get("graphMessageId") or "")
    if graph_client is None or not message_id:
        return None, None
    try:
        pdfs = graph_client.download_pdf_attachments(mailbox, message_id)
    except (GraphError, MailboxRejected):
        LOGGER.info("Graph PDF download failed without raising run")
        return None, None
    usable = [
        (name, content)
        for name, content in pdfs
        if not STATEMENT_FILE_RE.search(name or "") and not PO_FILE_RE.search(name or "")
    ]
    if number:
        named = [item for item in usable if number.lower() in item[0].lower()]
        if named:
            return named[0]
    if usable:
        return usable[0]
    return None, None


def finish_existing_header(
    client: KimcoClient,
    row: dict[str, Any],
    inv: dict[str, Any],
    *,
    receipts: list[dict[str, Any]] | None = None,
    pdf_dir: Path | None = None,
    graph_client: GraphClient | None = None,
    mailbox: str = ALLOWED_MAILBOX,
    flag_outlook: bool = False,
) -> dict[str, Any]:
    """Finish one existing Incomplete header via the proven API path. No UI."""
    out = dict(row)
    out["Notes"] = ""
    out["kind"] = KIND_FINISH_UP
    invoice_id = out.get("KIMCO id")
    if invoice_id in (None, ""):
        out["Result"] = RESULT_INCOMPLETE
        out["Why"] = why_incomplete("paused dry-run row has no KIMCO id.")
        out["Flag in Outlook"] = flag_in_outlook_for(out["Result"])
        return out

    try:
        record = client.get_item("ap_invoices", int(invoice_id))
    except KimcoError as exc:
        out["Result"] = RESULT_INCOMPLETE
        out["Why"] = why_incomplete(f"could not GET record {invoice_id}: {exc}")
        out["Flag in Outlook"] = flag_in_outlook_for(out["Result"])
        return out

    lines = invoice_lines_from_record(record)
    existing_receipt_ids = receipt_ids_from_invoice_lines(lines)
    receipts_selected = bool(existing_receipt_ids)
    select_status = "already-selected" if receipts_selected else ""

    attach_status = str(out.get("Attach status") or "")
    try:
        existing_atts = client.list_attachments(int(invoice_id))
    except KimcoError:
        existing_atts = []
    if existing_atts:
        attach_status = "attached"
    else:
        pdf_name, pdf_bytes = _pdf_bytes_for_invoice(
            inv, graph_client=graph_client, mailbox=mailbox, pdf_dir=pdf_dir
        )
        if pdf_name and pdf_bytes:
            try:
                attach_status = client.try_official_attach(
                    int(invoice_id),
                    name=pdf_name,
                    content_type="application/pdf",
                    size=len(pdf_bytes),
                    content=pdf_bytes,
                )
            except KimcoError as exc:
                text = str(exc)
                attach_status = text if "405" in text else "blocked-405"
        else:
            attach_status = "no-pdf-on-vm"

    po = _po_from_row(out, inv)
    multi_po = bool(inv.get("multi_po")) or len([p for p in (inv.get("pos") or []) if p]) > 1
    need_receipts = receipts_required(po=po, multi_po=multi_po)
    receipt_note = ""
    if need_receipts and not receipts_selected:
        invoice_lines = list(inv.get("lines") or [])
        search_pos = [str(p) for p in (inv.get("pos") or ([po] if po else [])) if p]
        combined: list[dict[str, Any]] = []
        notes: list[str] = []
        if receipts is not None:
            for search_po in search_pos or [None]:
                one = match_receipts(
                    invoice_number=str(out.get("Invoice #") or inv.get("invoice_number") or ""),
                    invoice_lines=invoice_lines,
                    receipts=receipts,
                    po_number=str(search_po) if search_po else None,
                )
                notes.append(str(one.get("why") or ""))
                combined.extend(one.get("matched") or [])
            receipt_note = " ".join(n for n in notes if n)
        receipt_ids = [
            hit.get("receipt", {}).get("id") if isinstance(hit.get("receipt"), dict) else None
            for hit in combined
        ]
        receipt_ids = [rid for rid in receipt_ids if rid not in (None, "")]
        if receipt_ids:
            try:
                select_status = client.try_select_receipts(int(invoice_id), receipt_ids)
            except KimcoError:
                select_status = "blocked-405"
            receipts_selected = select_status == "selected"
        else:
            select_status = "blocked-no-receipt-ids"
            receipts_selected = False

    posted_name, posted_id = posted_vendor_fields(record)
    vendor_ok, _vendor_why = vendor_confirmation_gate(
        parsed_vendor=str(inv.get("vendor") or out.get("Vendor") or ""),
        posted_name=posted_name or None,
        posted_id=posted_id,
    )
    check = selfcheck_payload(inv, po=po, multi_po=multi_po)
    check["parsed_vendor"] = str(inv.get("vendor") or out.get("Vendor") or "")
    check["posted_vendor"] = posted_name
    check["posted_vendor_id"] = posted_id
    check["vendor_mismatch"] = not vendor_ok
    result, finish_why = finish_gate(
        header_created=True,
        attach_status=attach_status,
        po=po,
        multi_po=multi_po,
        receipts_selected=receipts_selected,
        kimco_id=invoice_id,
        selfcheck=check,
    )
    out["Result"] = result
    out["Attach status"] = attach_status
    out["Flag in Outlook"] = flag_in_outlook_for(result)
    line_note = (
        f"Select Receipts={select_status or ('selected' if receipts_selected else 'not-posted')}. "
        "API record PUT of lists.APInvoiceLine; do not type Add Item. "
    )
    if result == RESULT_SUCCESS:
        out["Why"] = (
            f"Finished bill via API finish of paused dry-run header {invoice_id} (QUALITY V1.1). "
            f"{line_note}{receipt_note} "
            f"Attach status={attach_status}."
        ).strip()
    else:
        out["Why"] = (
            f"{finish_why} API finish of paused dry-run header {invoice_id}. "
            f"{line_note}{receipt_note} Attach status={attach_status}."
        ).strip()
    return out


def resolve_message_id(
    graph_client: GraphClient | None,
    inv: dict[str, Any],
    *,
    mailbox: str = ALLOWED_MAILBOX,
) -> str:
    """Use the stored Graph id, or search the AP mailbox when that id 404s.

    Immutable ids from an earlier run can ErrorItemNotFound. Search by invoice #
    and skip the dry-run report email. Never logs the id value.
    """
    if graph_client is None:
        return str(inv.get("graph_message_id") or "").strip()
    stored = str(inv.get("graph_message_id") or inv.get("graphMessageId") or "").strip()
    if stored:
        try:
            graph_client.get_message(mailbox, stored, select="id,categories")
            return stored
        except (GraphError, MailboxRejected):
            LOGGER.info("Stored Graph message id not found; searching mailbox by invoice #")
    number = str(inv.get("invoice_number") or "").strip()
    if not number:
        return ""
    try:
        hits = graph_client.search_messages(mailbox, number, top=8)
    except (GraphError, MailboxRejected):
        return ""
    usable = []
    for message in hits:
        subject = str(message.get("subject") or "")
        if subject.startswith("AP dry run") or subject.startswith("AP run "):
            continue
        if STATEMENT_FILE_RE.search(subject) and "invoice" not in subject.lower():
            continue
        score = score_message_for_invoice(message, inv)
        if number.lower() in subject.lower():
            score = max(score, 40)
        elif score <= 0:
            # Graph $search already matched the invoice # in the body.
            score = 25
        usable.append((score, message))
    usable.sort(key=lambda pair: pair[0], reverse=True)
    if not usable:
        return ""
    if len(usable) > 1 and usable[0][0] < usable[1][0] + 20:
        # Prefer the vendor invoice over a leftover statement when scores are close.
        vendor = str(inv.get("vendor") or "").lower()
        vendor_hits = [
            pair
            for pair in usable
            if vendor and vendor.split()[0] in str(pair[1].get("subject") or "").lower()
        ]
        if len(vendor_hits) == 1:
            return str(vendor_hits[0][1].get("id") or "")
        invoice_hits = [pair for pair in usable if "invoice" in str(pair[1].get("subject") or "").lower()]
        if len(invoice_hits) == 1:
            return str(invoice_hits[0][1].get("id") or "")
        return ""
    return str(usable[0][1].get("id") or "")


def apply_grouped_outlook_flags(
    rows: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    graph_client: GraphClient | None,
    *,
    mailbox: str = ALLOWED_MAILBOX,
) -> list[dict[str, Any]]:
    """One Outlook category per message. Never Entered in AI + AI HOLD together.

    Shared-PDF emails (Fastenal two TXFT invoices) stay AI HOLD unless every
    bill on that message is Success.
    """
    inv_by_number = {
        str(inv.get("invoice_number") or ""): inv for inv in invoices if inv.get("invoice_number")
    }
    by_message: dict[str, list[dict[str, Any]]] = {}
    orphans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        inv = dict(inv_by_number.get(str(row.get("Invoice #") or "")) or {})
        if not inv:
            inv = {
                "invoice_number": row.get("Invoice #"),
                "vendor": row.get("Vendor"),
                "graph_message_id": row.get("graph_message_id"),
            }
        message_id = resolve_message_id(graph_client, inv, mailbox=mailbox)
        if message_id:
            inv["graph_message_id"] = message_id
            inv_by_number[str(row.get("Invoice #") or "")] = inv
        if not message_id:
            orphans.append((row, inv))
            continue
        by_message.setdefault(message_id, []).append(row)

    for message_id, group in by_message.items():
        bills = [row for row in group if not is_noise_result(str(row.get("Result") or ""))]
        if not bills:
            for row in group:
                row["Flag status"] = FLAG_NONE
                row["Flag in Outlook"] = "No"
            continue
        results = {str(row.get("Result") or "") for row in bills}
        outcome = RESULT_SUCCESS if results == {RESULT_SUCCESS} else RESULT_INCOMPLETE
        if any(str(row.get("Result") or "") in {"HOLD", "Fail"} for row in bills) and outcome != RESULT_SUCCESS:
            outcome = next(
                str(row.get("Result") or "")
                for row in bills
                if str(row.get("Result") or "") in {"HOLD", "Fail", RESULT_INCOMPLETE}
            )
        # Prefer a real header id so grouped flags can choose Entered with issues.
        kimco_id = next((row.get("KIMCO id") for row in bills if row.get("KIMCO id") not in (None, "")), None)
        if outcome != RESULT_SUCCESS and kimco_id in (None, ""):
            kimco_id = ""
        dummy = {
            "Result": outcome,
            "KIMCO id": kimco_id if outcome != RESULT_SUCCESS else (bills[0].get("KIMCO id") or 1),
            "Why": "",
        }
        apply_flag_after_match(dummy, {"graph_message_id": message_id}, graph_client, mailbox=mailbox)
        status = dummy.get("Flag status")
        for row in group:
            if is_noise_result(str(row.get("Result") or "")):
                row["Flag status"] = FLAG_NONE
                row["Flag in Outlook"] = "No"
                continue
            row["Flag status"] = status
            row["Flag in Outlook"] = flag_in_outlook_for(str(row.get("Result") or ""))
            why = str(row.get("Why") or "").rstrip()
            note = f"Flag status={status}."
            if "Flag status=" not in why:
                row["Why"] = f"{why} {note}".strip() if why else note

    for row, inv in orphans:
        apply_flag_after_match(row, inv, graph_client, mailbox=mailbox)
    return rows


def grouped_flag_status_for_message(rows: list[dict[str, Any]]) -> str:
    """Return entered-in-ai only when every bill row on the message is Success.

    Noise / Skipped rows are ignored so they cannot force AI HOLD.
    """
    bills = [row for row in rows if not is_noise_result(str(row.get("Result") or ""))]
    if not bills:
        return FLAG_NONE
    results = {str(row.get("Result") or "") for row in bills}
    if results == {RESULT_SUCCESS}:
        return FLAG_FLAGGED
    if any(header_created_with_issues(result=str(row.get("Result") or ""), kimco_id=row.get("KIMCO id")) for row in bills):
        if all(row.get("KIMCO id") not in (None, "") or str(row.get("Result") or "") == RESULT_SUCCESS for row in bills):
            return FLAG_ENTERED_WITH_ISSUES
    return FLAG_AI_HOLD
