"""Live GET-only check of KIMCO 10009/10010 receipt lines (invent=false).

Does not create headers, void, or claim Success. Select Receipts PUT is
opt-in via --select-receipts after the GET proof is reviewed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.inbox import _pdfs_from_body_link, sender_address, sender_name
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.rules import lookup_id, lookup_text, money, normalize_receipt

LOGGER = logging.getLogger("ap_clerk.check_10009_10010")

INVOICES = (10009, 10010)
KNOWN_RECEIPTS = {
    10009: (24103,),
    10010: (24106, 24107, 24108, 24111),
}
POS = {
    10009: "59083",
    10010: "59165",
}
INV_NUMBERS = {
    10009: "11003",
    10010: "11004",
}
VENDOR_NEEDLE = "AMERICAN QUALITY POWDER COATING"


def _ref(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"id": lookup_id(value), "text": lookup_text(value), "raw": value}
    return {"id": None, "text": value, "raw": value}


def summarize_invoice(item: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    vals = item.get("values") if isinstance(item.get("values"), dict) else {}
    lines = []
    for line in (item.get("lists") or {}).get("APInvoiceLine") or []:
        lv = (line.get("values") if isinstance(line, dict) else None) or {}
        qty = money(lv.get("Quantity"))
        unit = money(lv.get("Unit_Price"))
        amt = money(lv.get("Amount") or lv.get("Line_Amount") or lv.get("Extended_Amount"))
        if amt is None and qty is not None and unit is not None:
            amt = round(qty * unit, 2)
        lines.append(
            {
                "line_id": line.get("id") if isinstance(line, dict) else None,
                "part": _ref(lv.get("Part_ID") or lv.get("Part") or lv.get("Item")),
                "qty": qty,
                "unit_price": unit,
                "amount": amt,
                "receipt": _ref(lv.get("Receipt")),
                "po": _ref(lv.get("Purchase_Order_Number") or lv.get("Purchase_Order")),
                "po_line": _ref(lv.get("Purchase_Order_Line") or lv.get("PO_Item") or lv.get("PO_Item_Number")),
                "description": lv.get("Description") or lv.get("Name"),
                "keys": sorted(lv.keys()),
            }
        )
    charges = []
    for charge in (item.get("lists") or {}).get("APInvoiceAdditionalCharge") or []:
        cv = (charge.get("values") if isinstance(charge, dict) else None) or {}
        charges.append(
            {
                "type": cv.get("Charge_Type") or cv.get("Additional_Charge"),
                "amount": money(cv.get("Amount")),
                "description": cv.get("Description"),
            }
        )
    return {
        "id": item.get("id"),
        "invoice_number": vals.get("Invoice_Number"),
        "vendor": _ref(vals.get("Vendor")),
        "po": _ref(vals.get("Purchase_Order")),
        "invoice_type": vals.get("Invoice_Type"),
        "invoice_amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Verification_Total") or vals.get("Invoice_Balance") or vals.get("Invoice_Verification_Amount")),
        "invoice_date": vals.get("Invoice_Date"),
        "posted": vals.get("Posted"),
        "void": vals.get("Void"),
        "lines_count": vals.get("Lines_Count"),
        "total_line_net": money(vals.get("Total_Line_Net_Amounts")),
        "batch": _ref(vals.get("AP_Invoice_Batch")),
        "value_keys": sorted(vals.keys()),
        "list_keys": sorted((item.get("lists") or {}).keys()),
        "lines": lines,
        "charges": charges,
        "attachments": [
            {
                "name": a.get("name") or a.get("fileName") or a.get("file_name"),
                "id": a.get("id") or a.get("fileId") or a.get("file_id"),
                "keys": sorted(a.keys()),
            }
            for a in attachments
        ],
    }


def summarize_receipt(item: dict[str, Any]) -> dict[str, Any]:
    norm = normalize_receipt(item)
    raw = item.get("values") if isinstance(item.get("values"), dict) else {}
    return {
        "id": item.get("id"),
        "name": raw.get("Name") or norm.get("name"),
        "slip": norm.get("slip"),
        "part": _ref(raw.get("Part_Number") or raw.get("Part") or raw.get("Item")),
        "part_text": norm.get("part"),
        "description": norm.get("description"),
        "qty": norm.get("qty"),
        "qty_received": money(raw.get("Quantity_Received")),
        "qty_invoiced": money(raw.get("Quantity_Invoiced") or raw.get("Qty_Invoiced")),
        "unit_price": norm.get("unit_price"),
        "amount": norm.get("amount"),
        "po": _ref(raw.get("PO_Number") or raw.get("Purchase_Order") or raw.get("Purchase_Order_Number")),
        "po_text": norm.get("po"),
        "po_line": _ref(raw.get("PO_Item_Number") or raw.get("Purchase_Line_Number")),
        "po_line_text": str(norm.get("po_line") or ""),
        "invoiced": raw.get("Invoiced") if "Invoiced" in raw else raw.get("AP_Invoiced"),
        "ap_invoice": _ref(raw.get("AP_Invoice_Number") or raw.get("AP_Invoice") or raw.get("Invoice_Number")),
        "open": raw.get("Open") if "Open" in raw else raw.get("Is_Open"),
        "value_keys": sorted(raw.keys()),
    }


def summarize_po_line(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("values") if isinstance(item.get("values"), dict) else item
    return {
        "id": item.get("id"),
        "name": raw.get("Name"),
        "part": _ref(raw.get("Part_Number") or raw.get("Part") or raw.get("Item")),
        "description": raw.get("Description") or raw.get("Item_Description"),
        "qty_ordered": money(raw.get("Quantity") or raw.get("Qty_Ordered") or raw.get("Ordered_Quantity")),
        "qty_received": money(raw.get("Quantity_Received") or raw.get("Received_Quantity")),
        "qty_invoiced": money(raw.get("Quantity_Invoiced") or raw.get("Invoiced_Quantity")),
        "unit_price": money(raw.get("Unit_Price") or raw.get("$_Unit_Price") or raw.get("Purchase_Cost")),
        "po": _ref(raw.get("Purchase_Order") or raw.get("PO_Number") or raw.get("Purchase_Order_Number")),
        "line": raw.get("Line_Number") or raw.get("Name"),
        "value_keys": sorted(raw.keys()) if isinstance(raw, dict) else [],
    }


def _po_haystack(item: dict[str, Any]) -> str:
    blob = json.dumps(item, default=str)
    return blob


def filter_by_po(items: list[dict[str, Any]], po: str) -> list[dict[str, Any]]:
    needles = (po, f"PO{po}")
    out = []
    for item in items:
        hay = _po_haystack(item)
        if any(n in hay for n in needles):
            out.append(item)
    return out


def try_filtered_list(client: KimcoClient, service: str, po: str) -> dict[str, Any]:
    """Probe whether the list endpoint accepts a filter; never invents rows."""
    url = client._list_url(service)
    attempts = []
    for params in (
        {"pageSize": 50, "offset": 0, "filter": f"PO_Number.text contains '{po}'"},
        {"pageSize": 50, "offset": 0, "q": po},
        {"pageSize": 50, "offset": 0, "search": po},
    ):
        try:
            response = client.request("GET", url, params=params)
        except KimcoError as exc:
            attempts.append({"params": list(params), "error": str(exc)[:200]})
            continue
        snippet = (response.text or "")[:240]
        attempts.append(
            {
                "params": {k: v for k, v in params.items() if k != "filter" or True},
                "status": response.status_code,
                "snippet": snippet,
            }
        )
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                continue
            items = payload.get("items") or []
            if items:
                return {"ok": True, "params": params, "count": len(items), "total": payload.get("totalCount"), "items": items}
    return {"ok": False, "attempts": attempts}


def find_aqpc_invoice_mail(graph, invoice_number: str) -> list[dict[str, Any]]:
    needle = f"New payment request from {VENDOR_NEEDLE} - invoice {invoice_number}"
    hits = []
    for msg in graph.search_messages(ALLOWED_MAILBOX, needle, top=10):
        subject = str(msg.get("subject") or "")
        if invoice_number in subject and VENDOR_NEEDLE in subject.upper():
            hits.append(msg)
    hits.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    return hits


def refetch_pdf(graph, invoice_number: str, pdf_dir: Path) -> dict[str, Any]:
    messages = find_aqpc_invoice_mail(graph, invoice_number)
    if not messages:
        return {"invoice": invoice_number, "error": "no-graph-message"}
    msg = messages[0]
    subject = str(msg.get("subject") or "")
    message_id = str(msg.get("id") or "")
    preview = str(msg.get("bodyPreview") or "")
    from_name = sender_name(msg)
    from_addr = sender_address(msg)
    pdfs, link_hold = _pdfs_from_body_link(
        graph, ALLOWED_MAILBOX, message_id, preview, subject=subject
    )
    if link_hold or not pdfs:
        return {
            "invoice": invoice_number,
            "subject": subject,
            "receivedDateTime": msg.get("receivedDateTime"),
            "error": "pdf-behind-link",
            "link_hold": {k: link_hold.get(k) for k in ("url", "host", "browser_tried", "browser_failure", "method") if isinstance(link_hold, dict)},
        }
    filename, content = pdfs[0]
    dest = pdf_dir / f"recheck_{invoice_number}_{filename}"
    dest.write_bytes(content)
    parsed = parse_invoice_pdf(dest, subject=subject, from_name=from_name, from_address=from_addr)
    return {
        "invoice": invoice_number,
        "subject": subject,
        "receivedDateTime": msg.get("receivedDateTime"),
        "pdf_path": str(dest),
        "pdf_bytes": len(content),
        "starts_pdf": content[:5] == b"%PDF-",
        "parsed": {
            "invoice_number": parsed.get("invoice_number"),
            "date": parsed.get("date"),
            "po": parsed.get("po"),
            "amount": parsed.get("amount"),
            "lines": parsed.get("lines") or [],
            "fees": parsed.get("fees") or [],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GET-only live check of KIMCO 10009/10010")
    parser.add_argument("--skip-lists", action="store_true", help="Skip full receipt/PO list scans")
    parser.add_argument("--skip-pdf", action="store_true", help="Skip Graph/guest PDF refetch")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / "kimco-10009-10010-recheck.json"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("Target: live  invent=false  no-void  no-new-headers", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    invoices: dict[str, Any] = {}
    for invoice_id in INVOICES:
        item = client.get_item("ap_invoices", invoice_id)
        try:
            attachments = client.list_attachments(invoice_id)
        except KimcoError:
            attachments = []
        invoices[str(invoice_id)] = summarize_invoice(item, attachments)
        print(
            f"GET invoice {invoice_id} lines={len(invoices[str(invoice_id)]['lines'])} "
            f"amount={invoices[str(invoice_id)]['invoice_amount']}",
            flush=True,
        )

    known: dict[str, Any] = {}
    for invoice_id, rids in KNOWN_RECEIPTS.items():
        for rid in rids:
            try:
                rec = client.get_item("receipts", rid)
            except KimcoError as exc:
                known[str(rid)] = {"id": rid, "error": str(exc)[:300]}
                continue
            known[str(rid)] = summarize_receipt(rec)
            print(f"GET receipt {rid} po={known[str(rid)].get('po_text')} qty={known[str(rid)].get('qty')}", flush=True)

    po_lines: dict[str, Any] = {}
    receipts_by_po: dict[str, Any] = {}
    filter_probes: dict[str, Any] = {}
    if not args.skip_lists:
        print("Listing purchase_lines…", flush=True)
        purchase_lines = client.list_items("purchase_lines")
        print(f"purchase_lines total={len(purchase_lines)}", flush=True)
        for invoice_id, po in POS.items():
            matched = filter_by_po(purchase_lines, po)
            po_lines[po] = [summarize_po_line(item) for item in matched]
            print(f"PO {po} purchase_lines={len(matched)}", flush=True)
        print("Probing receipt filters…", flush=True)
        for po in POS.values():
            filter_probes[po] = try_filtered_list(client, "receipts", po)
        print("Listing receipts…", flush=True)
        all_receipts = client.list_items("receipts")
        print(f"receipts total={len(all_receipts)}", flush=True)
        for invoice_id, po in POS.items():
            matched = filter_by_po(all_receipts, po)
            receipts_by_po[po] = [summarize_receipt(item) for item in matched]
            print(f"PO {po} receipts={len(matched)}", flush=True)

    pdfs: dict[str, Any] = {}
    if not args.skip_pdf:
        graph = _optional_graph_client()
        if graph is None:
            pdfs = {"error": "graph-authenticate-failed"}
        else:
            pdf_dir = ROOT / "runs" / "inbox-pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            for invoice_id, number in INV_NUMBERS.items():
                print(f"Refetch PDF {number}…", flush=True)
                pdfs[number] = refetch_pdf(graph, number, pdf_dir)

    proof = {
        "proof": "kimco-10009-10010-recheck",
        "invent": False,
        "void": False,
        "new_headers": False,
        "invoices": invoices,
        "known_receipts": known,
        "po_lines": po_lines,
        "receipts_by_po": receipts_by_po,
        "filter_probes": filter_probes,
        "pdfs": pdfs,
    }
    out = Path(args.out)
    out.write_text(json.dumps(proof, indent=2, default=str))
    print(f"Wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
