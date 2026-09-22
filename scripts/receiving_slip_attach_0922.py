"""Live: attach receiving@ Sharp MFP packing slips to existing AP headers.

GET receiving@, OCR/text each scan, match unique live invoices, attach,
GET-verify, then `AI Completed` only when every identifiable slip on that
email is verified. No new invoices. No Mail.Send.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient
from ap_clerk.packing_slips import (
    is_sharp_mfp_scan,
    logical_slips_from_scan,
    slip_is_identifiable,
)
from ap_clerk.pdf_invoice import extract_pdf_page_texts
from ap_clerk.receiving_attach import (
    attach_name_for_slip,
    attach_slip_to_existing_header,
    finish_receiving_email,
    index_live_ap_invoice,
    pdf_bytes_for_pages,
    slip_result_row,
    unique_invoice_for_slip,
)
from ap_clerk.receiving_mail import ReceivingGraph
from ap_clerk.receiving_probe import RECEIVING_MAILBOX, authenticate_token

LOGGER = logging.getLogger("ap_clerk.receiving")
OUT_DIR = Path("runs") / "receiving-slips-2026-09-22"
PROOF_PATH = Path("runs") / "receiving-slip-attach-2026-09-22.json"


def _live_kimco() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "Live KIMCO credentials missing")
    return KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )


def _index_invoices(client: KimcoClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in client.list_items("ap_invoices"):
        row = index_live_ap_invoice(item)
        if row:
            rows.append(row)
    return rows


def _report_row(
    *,
    filename: str,
    pages: list[int],
    match: dict[str, Any],
    attach: dict[str, Any] | None,
    slip: dict[str, Any],
) -> dict[str, Any]:
    invoice = (match.get("invoice") or {}) if match.get("status") == "matched" else {}
    return {
        "scan_filename": filename,
        "pages": pages,
        "slip_po": slip.get("po"),
        "slip_invoice_number": slip.get("invoice_number") or slip.get("slip_number"),
        "slip_vendor": slip.get("vendor"),
        "match_status": match.get("status"),
        "matched_invoice_number": invoice.get("invoice_number") or "",
        "matched_kimco_id": invoice.get("id") or "",
        "matched_vendor": invoice.get("vendor") or "",
        "matched_po": invoice.get("po") or "",
        "attach_result": (attach or {}).get("status") or match.get("status"),
        "verified": bool((attach or {}).get("verified")),
        "already_present": bool((attach or {}).get("already_present")),
        "unmatched_or_ambiguous": match.get("why") or (attach or {}).get("why") or "",
        "candidates": [
            {
                "id": c.get("id"),
                "invoice_number": c.get("invoice_number"),
                "vendor": c.get("vendor"),
                "po": c.get("po"),
            }
            for c in (match.get("candidates") or [])
            if match.get("status") == "ambiguous"
        ],
    }


def run(*, attach: bool = True) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    token = authenticate_token()
    graph = ReceivingGraph(token)
    messages = graph.list_messages(RECEIVING_MAILBOX, top=15)
    kimco = _live_kimco()
    invoices = _index_invoices(kimco)

    report_rows: list[dict[str, Any]] = []
    email_outcomes: list[dict[str, Any]] = []
    mail_sent = False

    for message in messages:
        message_id = str(message.get("id") or "")
        subject = str(message.get("subject") or "")
        frm = ((message.get("from") or {}).get("emailAddress") or {})
        from_addr = str(frm.get("address") or "")
        categories = list(message.get("categories") or [])
        pdfs = graph.download_pdf_attachments(RECEIVING_MAILBOX, message_id) if message_id else []
        email_slips: list[dict[str, Any]] = []
        for name, raw in pdfs:
            dest = OUT_DIR / name
            dest.write_bytes(raw)
            texts = extract_pdf_page_texts(dest)
            slips = logical_slips_from_scan(
                texts,
                source_filename=name,
                from_addr=from_addr,
                subject=subject,
            )
            if not slips and texts:
                slips = [
                    {
                        "pages": list(range(1, len(texts) + 1)),
                        "po": None,
                        "invoice_number": None,
                        "slip_number": None,
                        "vendor": None,
                        "source_filename": name,
                    }
                ]
            for slip in slips:
                match = unique_invoice_for_slip(slip, invoices)
                attach_info: dict[str, Any] | None = None
                status = match["status"]
                verified = False
                invoice = match.get("invoice")
                if attach and match["status"] == "matched" and invoice:
                    content, split = pdf_bytes_for_pages(dest, list(slip.get("pages") or []))
                    filename = attach_name_for_slip(slip, source_filename=name, split=split)
                    attach_info = attach_slip_to_existing_header(
                        kimco, invoice, content=content, filename=filename
                    )
                    status = str(attach_info.get("status") or status)
                    verified = bool(attach_info.get("verified"))
                elif not attach and match["status"] == "matched":
                    status = "matched-dry"
                row = slip_result_row(
                    slip,
                    status=status if status in {"attached", "failed"} else (
                        "attached" if verified else status
                    ),
                    verified=verified,
                    invoice_id=(invoice or {}).get("id"),
                    invoice_number=(invoice or {}).get("invoice_number") or "",
                    vendor=(invoice or {}).get("vendor") or "",
                    attach_status=(attach_info or {}).get("attach_status") or "",
                )
                email_slips.append(row)
                report_rows.append(
                    _report_row(
                        filename=name,
                        pages=list(slip.get("pages") or []),
                        match=match,
                        attach=attach_info,
                        slip=slip,
                    )
                )

        stamp = {"outlook": "skipped-no-attach", "stamp": False, "leftover_labels": []}
        if attach and email_slips:
            stamp = finish_receiving_email(
                graph,
                mailbox=RECEIVING_MAILBOX,
                message_id=message_id,
                slip_results=email_slips,
            )
        email_outcomes.append(
            {
                "message_id": message_id,
                "subject": subject,
                "from": from_addr,
                "received": message.get("receivedDateTime"),
                "categories_before": categories,
                "sharp_mfp": is_sharp_mfp_scan(
                    from_addr=from_addr, subject=subject, filename=(pdfs[0][0] if pdfs else "")
                ),
                "pdfs": [name for name, _raw in pdfs],
                "slip_count": len(email_slips),
                "identifiable": sum(1 for row in email_slips if slip_is_identifiable(row.get("slip"))),
                "outlook": stamp.get("outlook"),
                "ai_completed": bool(stamp.get("stamp")),
                "leftover": stamp.get("leftover_labels") or [],
                "why": stamp.get("why") or "",
            }
        )

    proof = {
        "mailbox": RECEIVING_MAILBOX,
        "day": date.today().isoformat(),
        "mail_sent": mail_sent,
        "send_mail_invoked": False,
        "invoices_created": 0,
        "live_invoice_index": len(invoices),
        "attach": attach,
        "emails": email_outcomes,
        "rows": report_rows,
    }
    PROOF_PATH.write_text(json.dumps(proof, indent=2) + "\n")
    return proof


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    proof = run(attach=True)
    print(json.dumps({k: proof[k] for k in ("mailbox", "attach", "mail_sent", "invoices_created")}, indent=2))
    print(f"wrote {PROOF_PATH}")
    for row in proof.get("rows") or []:
        print(
            f"{row['scan_filename']} pages={row['pages']} "
            f"slip={row['slip_invoice_number'] or row['slip_po']} "
            f"-> {row['matched_invoice_number'] or '-'} / {row['matched_kimco_id'] or '-'} "
            f"{row['matched_vendor']} PO {row['matched_po'] or '-'} "
            f"| {row['attach_result']} verified={row['verified']} "
            f"| {row['unmatched_or_ambiguous']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
