"""NOTE-52 backfill: move processed O'Neal AP emails to 9 - FORT WORTH ARCHIVE.

Kyle 2026-09-22. O'Neal batch 722 headers only. Header must exist and have
a vendor PDF attachment. One Graph move per unique message_id.
Keep Outlook categories. No Mail.Send. invent=false.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook

from ap_clerk.auth import load_credentials
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    FORT_WORTH_FOLDER_DISPLAY_NAME,
    FORT_WORTH_FOLDER_ID,
    MOVE_MOVED,
    MOVE_SKIPPED_ALREADY,
    MOVE_SKIPPED_NO_ATTACH,
    MOVE_SKIPPED_NO_HEADER,
    MOVE_SKIPPED_NO_MESSAGE,
    GraphClient,
    GraphError,
    is_fort_worth_inbox_folder,
    load_graph_credentials,
)
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text

LOGGER = logging.getLogger("ap_clerk.oneal_fort_worth")

TARGETS = (
    {"invoice": "15469453", "kimco_id": 10160},
    {"invoice": "15457895", "kimco_id": 10161},
    {"invoice": "15457907", "kimco_id": 10162},
    {"invoice": "15464707", "kimco_id": 10163},
    {"invoice": "15464854", "kimco_id": 10164},
    {"invoice": "15460544", "kimco_id": 10174},
    {"invoice": "15460995", "kimco_id": 10175},
    {"invoice": "15461007", "kimco_id": 10176},
    {"invoice": "15461157", "kimco_id": 10177},
    {"invoice": "15476365", "kimco_id": 10178},
    {"invoice": "15476879", "kimco_id": 10179},
    {"invoice": "15477248", "kimco_id": 10180},
    {"invoice": "15447737", "kimco_id": 10079},
)
SHEET = ROOT / "runs" / "AP-run-2026-09-21-oneal.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-21-oneal.json"
PROOF = ROOT / "runs" / "oneal-fort-worth-2026-09-22.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"


def exact_invoice(value: Any) -> str:
    return invoice_number_key(value) or str(value or "").strip()


def header_pdf_names(client: KimcoClient, kid: int) -> list[str]:
    try:
        atts = client.list_attachments(int(kid))
    except KimcoError:
        return []
    names: list[str] = []
    for att in atts:
        name = str(att.get("name") or "")
        if name.lower().endswith(".pdf"):
            names.append(name)
    return names


def flatten_invoices(parsed: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for bill in [parsed, *(parsed.get("siblings") or [])]:
        if not isinstance(bill, dict):
            continue
        inv = exact_invoice(bill.get("invoice_number"))
        if inv:
            out.append(inv)
    return out


def is_oneal_invoice_subject(subject: str) -> bool:
    text = (subject or "").lower()
    if "payment status" in text or "payment confirmation" in text:
        return False
    if "credit" in text and "invoice" not in text:
        return False
    return "o'neal" in text or "oneal" in text or "o’neal" in text


def map_invoices_to_messages(graph: GraphClient) -> dict[str, dict[str, Any]]:
    """invoice_number → {id, subject, received} from AP mailbox O'Neal PDFs."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    seen: dict[str, dict[str, Any]] = {}
    messages: dict[str, dict[str, Any]] = {}
    for needle in ("O'Neal Steel Invoice", "O'Neal Steel", "onealsteel"):
        try:
            for msg in graph.search_messages(ALLOWED_MAILBOX, needle, top=50):
                mid = str(msg.get("id") or "")
                if mid:
                    messages[mid] = msg
        except GraphError as exc:
            LOGGER.info("Graph search failed: %s", type(exc).__name__)
    try:
        for msg in graph.list_messages(ALLOWED_MAILBOX, received_from=date(2026, 8, 1)):
            mid = str(msg.get("id") or "")
            if mid:
                messages[mid] = msg
    except GraphError as exc:
        LOGGER.info("Graph list failed: %s", type(exc).__name__)
    wanted = {exact_invoice(t["invoice"]) for t in TARGETS}
    for msg in messages.values():
        mid = str(msg.get("id") or "")
        subject = str(msg.get("subject") or "")
        if not mid or not msg.get("hasAttachments"):
            continue
        if not is_oneal_invoice_subject(subject):
            continue
        try:
            pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, mid)
        except GraphError:
            continue
        if not pdfs:
            continue
        name, content = pdfs[0]
        dest = PDF_DIR / f"fw_{mid[-12:]}_{name.replace(' ', '_')}"
        dest.write_bytes(content)
        parsed = parse_invoice_pdf(
            dest,
            subject=subject,
            from_name=str(((msg.get("from") or {}).get("emailAddress") or {}).get("name") or ""),
        )
        for inv in flatten_invoices(parsed):
            if inv in wanted and inv not in seen:
                seen[inv] = {
                    "id": mid,
                    "subject": subject,
                    "received": msg.get("receivedDateTime"),
                    "pdf": name,
                }
    return seen


def message_folder(graph: GraphClient, message_id: str) -> dict[str, Any]:
    rec = graph.get_message(
        ALLOWED_MAILBOX,
        message_id,
        select="id,subject,parentFolderId,categories",
    )
    return {
        "id": rec.get("id"),
        "parentFolderId": rec.get("parentFolderId"),
        "subject": rec.get("subject"),
        "categories": rec.get("categories") or [],
    }


def unique_moves(rows: list[dict[str, Any]]) -> list[str]:
    """First invoice per message_id keeps the move; later siblings skip."""
    seen: set[str] = set()
    order: list[str] = []
    for row in rows:
        mid = str(row.get("message_id") or "")
        if not mid or mid in seen:
            continue
        if row.get("eligible"):
            seen.add(mid)
            order.append(mid)
    return order


def patch_sheet(results: list[dict[str, Any]]) -> None:
    prior: list[dict[str, Any]] = []
    sidecar: dict[str, Any] = {}
    if SHEET_JSON.is_file():
        sidecar = json.loads(SHEET_JSON.read_text())
        prior = [r for r in (sidecar.get("rows") or []) if isinstance(r, dict)]
    by_inv = {exact_invoice(r.get("invoice")): r for r in results}
    for row in prior:
        inv = exact_invoice(row.get("Invoice #"))
        hit = by_inv.get(inv)
        if not hit:
            continue
        note = str(row.get("Notes") or "").strip()
        extra = (
            f"NOTE-52 Fort Worth {hit.get('folder_name') or FORT_WORTH_FOLDER_DISPLAY_NAME} "
            f"move={hit.get('status')} mid={hit.get('message_id') or 'none'}."
        )
        if extra not in note:
            row["Notes"] = f"{note} {extra}".strip() if note else extra
    sidecar["proof"] = "oneal-0921-fort-worth"
    sidecar["rows"] = prior
    sidecar["fort_worth_0922"] = results
    SHEET_JSON.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    if SHEET.is_file():
        wb = load_workbook(SHEET)
        ws = wb.active
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        if "Invoice #" in headers and "Notes" in headers:
            inv_i = headers.index("Invoice #")
            notes_i = headers.index("Notes")
            for excel_row in ws.iter_rows(min_row=2):
                inv = exact_invoice(excel_row[inv_i].value)
                hit = by_inv.get(inv)
                if not hit:
                    continue
                extra = (
                    f"NOTE-52 Fort Worth {hit.get('folder_name') or FORT_WORTH_FOLDER_DISPLAY_NAME} "
                    f"move={hit.get('status')} mid={hit.get('message_id') or 'none'}."
                )
                current = str(excel_row[notes_i].value or "")
                if extra not in current:
                    excel_row[notes_i].value = f"{current} {extra}".strip()
            wb.save(SHEET)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live.")
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    gcreds = load_graph_credentials()
    if not gcreds.ready:
        raise SystemExit(gcreds.error or "Graph credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    graph = GraphClient.authenticate(
        gcreds.tenant_id or "", gcreds.client_id or "", gcreds.client_secret or ""
    )
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    dest_id = str(folder.get("id") or FORT_WORTH_FOLDER_ID or "")
    dest_name = str(folder.get("displayName") or FORT_WORTH_FOLDER_DISPLAY_NAME or "")
    if not dest_id or not is_fort_worth_inbox_folder(dest_name):
        raise SystemExit(f"Fort Worth folder not resolved: {folder}")
    mapped = map_invoices_to_messages(graph)
    planned: list[dict[str, Any]] = []
    for spec in TARGETS:
        kid = int(spec["kimco_id"])
        inv = exact_invoice(spec["invoice"])
        rec = client.get_item("ap_invoices", kid)
        vals = rec.get("values") or {}
        pdfs = header_pdf_names(client, kid)
        msg = mapped.get(inv) or {}
        mid = str(msg.get("id") or "")
        row = {
            "invoice": inv,
            "kimco_id": kid,
            "header_invoice": lookup_text(vals.get("Invoice_Number")) or vals.get("Invoice_Number"),
            "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
            "pdfs": pdfs,
            "message_id": mid or None,
            "subject": msg.get("subject"),
            "folder_name": dest_name,
            "folder_id": dest_id,
            "eligible": False,
            "status": None,
        }
        if exact_invoice(row["header_invoice"]) not in {inv, exact_invoice(inv)}:
            # header number should still contain the invoice digits
            header_inv = exact_invoice(row["header_invoice"])
            if header_inv and header_inv != inv:
                row["status"] = "invoice-mismatch"
                planned.append(row)
                continue
        if not pdfs:
            row["status"] = MOVE_SKIPPED_NO_ATTACH
            planned.append(row)
            continue
        if not mid:
            row["status"] = MOVE_SKIPPED_NO_MESSAGE
            planned.append(row)
            continue
        row["eligible"] = True
        row["status"] = "pending"
        planned.append(row)
    to_move = unique_moves(planned)
    proof: dict[str, Any] = {
        "proof": "oneal-fort-worth-2026-09-22",
        "invent": False,
        "mail_send": False,
        "note": 52,
        "folder": {"id": dest_id, "displayName": dest_name},
        "dry_run": args.dry_run,
        "unique_message_ids": to_move,
    }
    if args.dry_run:
        proof["rows"] = planned
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 0
    moved_ids: set[str] = set()
    for row in planned:
        mid = str(row.get("message_id") or "")
        if row.get("status") not in {None, "pending"}:
            continue
        if mid in moved_ids:
            row["status"] = MOVE_SKIPPED_ALREADY
            row["reason"] = "sibling-already-moved"
            continue
        loc = message_folder(graph, mid)
        row["parentFolderId"] = loc.get("parentFolderId")
        row["categories"] = loc.get("categories")
        if str(loc.get("parentFolderId") or "") == dest_id:
            row["status"] = MOVE_SKIPPED_ALREADY
            row["reason"] = "already-in-fort-worth"
            moved_ids.add(mid)
            continue
        result = graph.move_message(ALLOWED_MAILBOX, mid, dest_id)
        row["status"] = result.get("status")
        row["new_id"] = result.get("new_id")
        row["http"] = result.get("http")
        if result.get("status") == MOVE_MOVED:
            moved_ids.add(mid)
            if result.get("new_id"):
                moved_ids.add(str(result.get("new_id")))
    patch_sheet(planned)
    proof["rows"] = planned
    proof["moved_count"] = sum(1 for r in planned if r.get("status") == MOVE_MOVED)
    proof["skipped_count"] = len(planned) - proof["moved_count"]
    PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    print(json.dumps(proof, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
