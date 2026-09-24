"""Attach receiving@ signed packing slips to AP headers. No new invoices. No Mail.Send.

After GET verifies the packing-slip file on the matching AP header, the
receiving@ message may be categorized `AI Completed` — only when every
identifiable slip from that email is attached. Partial success stays
uncategorized and lists leftovers.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from ap_clerk.packing_slips import (
    _norm_key,
    _norm_po,
    packing_slip_verified_on_header,
    slip_attachment_label,
    slip_is_identifiable,
    slip_match_score,
)
from ap_clerk.receiving_mail import (
    FLAG_LEFT_UNCATEGORIZED,
    ReceivingGraph,
    stamp_receiving_ai_completed,
)
from ap_clerk.rules import extract_po_number, lookup_text

LOGGER = logging.getLogger("ap_clerk.receiving")


def index_live_ap_invoice(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a live AP invoice list row. No invented ids."""
    if not isinstance(item, dict):
        return None
    invoice_id = item.get("id")
    if invoice_id in (None, ""):
        return None
    vals = item.get("values") or {}
    if not isinstance(vals, dict):
        vals = {}
    number = lookup_text(vals.get("Invoice_Number")) or str(vals.get("Invoice_Number") or "")
    vendor = lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name"))
    po_text = lookup_text(vals.get("Purchase_Order") or vals.get("Purchase_Order_$_Display_Name"))
    return {
        "id": int(invoice_id),
        "invoice_number": number.strip(),
        "vendor": vendor.strip(),
        "po": extract_po_number(po_text) or "",
        "po_text": po_text,
        "date": lookup_text(vals.get("Invoice_Date")),
        "void": bool(vals.get("Void")),
    }


def unique_invoice_for_slip(
    slip: dict[str, Any] | None,
    invoices: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Match one slip to exactly one live AP invoice. Ambiguous → skip.

    Strong keys are invoice # / slip # / PO. Vendor alone is never enough.
    Do not invent a header.
    """
    data = slip or {}
    if not slip_is_identifiable(data):
        return {
            "status": "unidentifiable",
            "invoice": None,
            "candidates": [],
            "why": "Slip has no PO / invoice # / slip #. Do not guess. Skip attach.",
        }
    rows = [row for row in (invoices or []) if row and not row.get("void")]
    scored: list[tuple[int, dict[str, Any]]] = []
    slip_inv = _norm_key(str(data.get("invoice_number") or data.get("slip_number") or ""))
    slip_po = _norm_po(str(data.get("po") or ""))
    for row in rows:
        score = slip_match_score(
            data,
            po=str(row.get("po") or ""),
            invoice_number=str(row.get("invoice_number") or ""),
            vendor=str(row.get("vendor") or ""),
            invoice_date=str(row.get("date") or ""),
        )
        # Also treat slip # as invoice # when the header number matches.
        if slip_inv and _norm_key(str(row.get("invoice_number") or "")) == slip_inv:
            score = max(score, 80)
        if slip_po and _norm_po(str(row.get("po") or "")) == slip_po:
            score = max(score, 100)
        if score >= 80:
            scored.append((score, row))
    ids = {int(row["id"]) for _score, row in scored}
    if len(ids) == 1:
        winner = max(scored, key=lambda item: item[0])[1]
        return {
            "status": "matched",
            "invoice": winner,
            "candidates": [winner],
            "why": "",
            "score": max(score for score, _row in scored),
        }
    if not ids:
        return {
            "status": "no_match",
            "invoice": None,
            "candidates": [],
            "why": (
                "No live AP invoice matched this slip by PO / invoice #. "
                "Do not invent a header. Skip attach."
            ),
        }
    candidates = []
    seen: set[int] = set()
    for _score, row in sorted(scored, key=lambda item: item[0], reverse=True):
        kid = int(row["id"])
        if kid in seen:
            continue
        seen.add(kid)
        candidates.append(row)
    return {
        "status": "ambiguous",
        "invoice": None,
        "candidates": candidates,
        "why": (
            f"{len(candidates)} live AP invoices match this slip "
            f"({', '.join(str(c.get('invoice_number') or c.get('id')) for c in candidates)}). "
            "Do not guess. Skip attach."
        ),
    }


def pdf_bytes_for_pages(source: Path | bytes, pages: list[int] | None) -> tuple[bytes, bool]:
    """Return (bytes, split). split=False when the original whole PDF is used."""
    raw = source.read_bytes() if isinstance(source, Path) else bytes(source)
    wanted = [int(p) for p in (pages or []) if int(p) >= 1]
    if not wanted:
        return raw, False
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:  # noqa: BLE001 - attach the whole scan
        return raw, False
    total = len(reader.pages)
    unique = sorted(set(wanted))
    if not unique or unique == list(range(1, total + 1)):
        return raw, False
    if any(page > total for page in unique):
        return raw, False
    writer = PdfWriter()
    for page in unique:
        writer.add_page(reader.pages[page - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), True


def attach_name_for_slip(slip: dict[str, Any], *, source_filename: str, split: bool) -> str:
    original = Path(source_filename or slip.get("source_filename") or "packing-slip.pdf").name
    if not split:
        return original
    return slip_attachment_label(slip) if slip.get("pages") else original


def header_already_has_slip(attachments: list[Any] | None, *, expected_name: str = "") -> bool:
    return packing_slip_verified_on_header(attachments, expected_name=expected_name)


def attach_slip_to_existing_header(
    kimco: Any,
    invoice: dict[str, Any],
    *,
    content: bytes,
    filename: str,
) -> dict[str, Any]:
    """Attach to an existing header only. Never create an invoice."""
    invoice_id = invoice.get("id")
    if invoice_id in (None, ""):
        return {
            "status": "skipped",
            "verified": False,
            "attach_status": "no-invoice-id",
            "why": "Live invoice id missing. Do not invent. Skip attach.",
        }
    existing = []
    try:
        existing = list(kimco.list_attachments(invoice_id) or [])
    except Exception:  # noqa: BLE001
        existing = []
    if header_already_has_slip(existing, expected_name=filename) or (
        header_already_has_slip(existing) and any(
            filename.lower() in str(
                item.get("name") or item.get("filename") or ""
            ).lower()
            for item in existing
        )
    ):
        verified = verify_header_packing_slip(
            kimco,
            invoice_id,
            attach_status="attached",
            expected_name=filename,
        )
        if not verified.get("verified"):
            verified = verify_header_packing_slip(
                kimco, invoice_id, attach_status="attached", expected_name=""
            )
        return {
            **verified,
            "status": "attached" if verified.get("verified") else verified.get("status"),
            "already_present": True,
            "why": "Packing slip already on the header.",
        }
    status = kimco.try_official_attach(
        int(invoice_id),
        name=filename,
        content_type="application/pdf",
        size=len(content),
        content=content,
    )
    verified = verify_header_packing_slip(
        kimco,
        invoice_id,
        attach_status=status,
        expected_name=filename,
    )
    return {
        **verified,
        "already_present": False,
        "why": "" if verified.get("verified") else f"Attach {status} not GET-verified.",
    }


def verify_header_packing_slip(
    kimco: Any,
    invoice_id: Any,
    *,
    attach_status: str | None,
    expected_name: str = "",
) -> dict[str, Any]:
    """GET attachments after attach. verified=True only when the slip is on the header."""
    status = (attach_status or "").strip().lower()
    attachments: list[Any] = []
    if status == "attached" and invoice_id not in (None, "") and kimco is not None:
        try:
            attachments = list(kimco.list_attachments(invoice_id) or [])
        except Exception:  # noqa: BLE001 - GET failure means not verified
            LOGGER.info("GET attachments failed for invoice %s; treat as not verified", invoice_id)
            attachments = []
    verified = status == "attached" and packing_slip_verified_on_header(
        attachments, expected_name=expected_name
    )
    if status == "attached" and not verified:
        status = "failed"
    return {
        "attach_status": attach_status,
        "status": status if status else "failed",
        "verified": verified,
        "header_attachments": attachments,
        "invoice_id": invoice_id,
        "expected_name": expected_name,
    }


def slip_result_row(
    slip: dict[str, Any],
    *,
    status: str,
    verified: bool = False,
    invoice_id: Any = None,
    invoice_number: str = "",
    vendor: str = "",
    attach_status: str = "",
) -> dict[str, Any]:
    return {
        "slip": slip,
        "identifiable": slip_is_identifiable(slip),
        "status": status,
        "verified": bool(verified),
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "vendor": vendor,
        "attach_status": attach_status,
        "pages": list(slip.get("pages") or []),
    }


def finish_receiving_email(
    graph: ReceivingGraph,
    *,
    mailbox: str,
    message_id: str,
    slip_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stamp AI Completed only when every identifiable slip is GET-verified."""
    stamped = stamp_receiving_ai_completed(
        graph,
        mailbox=mailbox,
        message_id=message_id,
        slip_results=slip_results,
    )
    if not stamped.get("stamp"):
        LOGGER.info(
            "receiving@ %s left uncategorized: %s",
            message_id,
            stamped.get("why"),
        )
        stamped.setdefault("outlook", FLAG_LEFT_UNCATEGORIZED)
    return stamped
