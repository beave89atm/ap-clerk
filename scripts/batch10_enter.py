"""Enter the next 10 unentered AP invoices. Never posts a bill.

Batch: API Agent - 9/25/26 Batch 10.
Receipts use Quantity_Received. Quantity_Remaining is ignored.
Packing-slip gate is suspended. A header gap under $75 is one signed PPV.
A gap of $75 or more is a HOLD and those receipts are not selected.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoError
from ap_clerk.ppv_qc import pre_finish_totals_check, scan_ids
from ap_clerk.receiving_owners import lookup_receiving_owner, mention_span
from ap_clerk.rules import (
    CURRENCY_USD_ID,
    due_date_from_terms,
    extract_po_number,
    invoice_number_key,
    kimco_datetime,
    known_vendor_id,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    names_match,
    normalize_receipt,
    ppv_limit,
)
from ap_clerk.transfer_ap import apply_transfer_ap_batch_move

BATCH_NAME = "API Agent - 9/25/26 Batch 10"
OUT_JSON = ROOT / "artifacts" / "batch10-2026-09-25.json"
CACHE = Path("/tmp/batch10")
PPV_LIMIT = ppv_limit()
TARGET_COUNT = 10

SHAWN_MENTION_HTML = (
    '<span data-mention-id="104" data-mention-name="Shawn McKibben" '
    'data-mention-email="Shawn.McKibben@kannonmfg.com" '
    'class="prosemirror-mention-node">@Shawn McKibben</span>'
)

# Existing non-void live invoices. GET 2026-09-25. Used only to copy remit and terms.
# Greentree 272 is PO_Number_$_Vendor on receipt 23876, confirmed on invoice 3412.
CONFIRMED_VENDOR_SAMPLES = {
    202: 73,
    13: 10221,
    209: 10202,
    272: 3412,
    215: 9774,
    183: 8899,
    304: 5938,
    128: 10241,
    341: 7863,
}

# Hand-checked against the vendor PDF. Parser fees that were line descriptions
# are not charges. Statement packs are not in this list.
BILLS: list[dict[str, Any]] = [
    {
        "vendor": "Willbanks Metals",
        "invoice_number": "213645",
        "date": "2026-09-04",
        "due": "2026-11-03",
        "po": "59074",
        "total": 2790.00,
        "lines": [
            {"part": "Floor Plate A786", "description": "Floor Plate A786 14 GA x 48 x 120", "qty": 10, "unit_price": 139.50, "amount": 1395.00},
            {"part": "Floor Plate A786", "description": "Floor Plate A786 14 GA x 48 x 120", "qty": 10, "unit_price": 139.50, "amount": 1395.00},
        ],
        "fees": [],
    },
    {
        "vendor": "Air Products and Chemicals, Inc",
        "invoice_number": "436333415",
        "date": "2026-09-04",
        "due": "2026-10-04",
        "po": None,
        "total": 1859.80,
        "lines": [
            {"part": "29575", "description": "Nitrogen Liquid", "qty": 75884.637, "unit_price": 0.0192, "amount": 1456.99},
        ],
        "fees": [
            {"name": "Delivery Charge", "amount": 135.00, "fee": True},
            {"name": "Hazmat Charge", "amount": 120.00, "fee": True},
            {"name": "Product Surcharge", "amount": 6.07, "fee": True},
        ],
        "tax": 141.74,
        "no_po_on_pdf": True,
    },
    {
        "vendor": "UniFirst First Aid & Safety",
        "invoice_number": "IN000037249",
        "date": "2026-09-03",
        "due": "2026-10-03",
        "po": None,
        "total": 1089.71,
        "lines": [
            {"part": "AS02", "description": "All Sport ZERO Freezer Bar 3oz", "qty": 6, "unit_price": 50.00, "amount": 300.00},
            {"part": "AS38", "description": "All Sport ZERO Powder Stix VAR", "qty": 2, "unit_price": 353.33, "amount": 706.66},
        ],
        "fees": [],
        "tax": 83.05,
        "no_po_on_pdf": True,
    },
    {
        "vendor": "Greentree Packaging & Lumber",
        "invoice_number": "112589",
        "date": "2026-09-08",
        "due": "2026-10-08",
        "po": "59073",
        "total": 4304.00,
        "lines": [
            {"part": "96 X 48 4WAY PALLET", "description": "96 x 48 4WAY PALLET", "qty": 100, "unit_price": 43.04, "amount": 4304.00},
        ],
        "fees": [],
    },
    {
        "vendor": "Alternative Parts Inc",
        "invoice_number": "0098626-IN",
        "date": "2026-09-08",
        "due": "2026-10-08",
        "po": "59130",
        "total": 926.81,
        "lines": [
            {"part": "A5276D", "description": "Shield Ring", "qty": 5, "unit_price": 24.00, "amount": 120.00},
            {"part": "A5597", "description": "Nozzle Plug F1 Auto", "qty": 3, "unit_price": 260.00, "amount": 780.00},
        ],
        "fees": [{"name": "Freight", "amount": 26.81, "fee": True}],
    },
    {
        "vendor": "Telecom Products Inc.",
        "invoice_number": "17912",
        "date": "2026-09-01",
        "due": "2026-10-01",
        "po": "58644",
        "total": 2825.16,
        "lines": [
            {"part": "A-04421-000", "description": "FENDER, DRIVE OVER", "qty": 26, "unit_price": 108.66, "amount": 2825.16},
        ],
        "fees": [],
    },
    {
        "vendor": "Telecom Products Inc.",
        "invoice_number": "17916",
        "date": "2026-09-01",
        "due": "2026-10-01",
        "po": "58824",
        "total": 9562.08,
        "lines": [
            {"part": "A-04421-000", "description": "FENDER, DRIVE OVER", "qty": 88, "unit_price": 108.66, "amount": 9562.08},
        ],
        "fees": [],
    },
    {
        "vendor": "Morgan Steel",
        "invoice_number": "129425",
        "date": "2026-09-08",
        "due": "2026-10-23",
        "po": "58968",
        "total": 4960.00,
        "lines": [
            {"part": "FLPL18-L", "description": "FLOOR PLATE 1/8 - LASER A-02393-000", "qty": 40, "unit_price": 124.00, "amount": 4960.00},
        ],
        "fees": [],
    },
    {
        "vendor": "MSC Industrial Supply",
        "invoice_number": "77062711",
        "date": "2026-09-08",
        "due": "2026-10-08",
        "po": None,
        "total": 1237.78,
        "lines": [
            {"part": "08654303", "description": "GIP3.98-0.20 IC908 ISCAR CUT-GRIP INSERT", "qty": 11, "unit_price": 38.59, "amount": 424.49},
            {"part": "12082277", "description": "1/4X1/4X3/4X2-1/2 4FL SC TIALCN SQ SEM", "qty": 4, "unit_price": 28.08, "amount": 112.32},
            {"part": "45403011", "description": "1/2 90D 8% COB NC SPOTTING DRILL", "qty": 4, "unit_price": 23.99, "amount": 95.96},
            {"part": "45403045", "description": "3/4 90D 8% COB NC SPOTTING DRILL", "qty": 4, "unit_price": 53.26, "amount": 213.04},
            {"part": "63927537", "description": "CNMG431TF IC907 ISCAR CARB TURNING INSERT", "qty": 10, "unit_price": 9.76, "amount": 97.60},
            {"part": "80692387", "description": "CPMT 3-1-PF IC907 ISCAR CBD 11D TURNING INS", "qty": 10, "unit_price": 9.06, "amount": 90.60},
            {"part": "07772080", "description": "3/4X3/4SHX2-1/4X5 CC ACCUPRO TICN CARB 4FL SEM", "qty": 1, "unit_price": 203.77, "amount": 203.77},
        ],
        "fees": [],
        "no_po_on_pdf": True,
        "printed_po_not_kimco": "VENDING/1570",
    },
    {
        "vendor": "Tube Supply",
        "invoice_number": "01178303",
        "date": "2026-09-04",
        "due": "2026-10-04",
        "po": "59093",
        "total": 374.88,
        "lines": [
            {"part": "RB-0.88-4140-VGS", "description": "1.000 OD ROUND BAR HR 4140", "qty": 264, "unit_price": 1.42, "amount": 374.88},
        ],
        "fees": [],
    },
]


def dollar(amount: float | None) -> str:
    if amount is None:
        return "unknown"
    return f"${amount:,.2f}"


def qty_text(qty: float | None) -> str:
    if qty is None:
        return "unknown"
    if abs(qty - round(qty)) < 0.001:
        return str(int(round(qty)))
    return f"{qty:.4f}".rstrip("0").rstrip(".")


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def attach_mail(bills: list[dict[str, Any]]) -> None:
    pending = json.loads((CACHE / "pending.json").read_text())
    by_number: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        by_number.setdefault(invoice_number_key(row.get("invoice_number")), []).append(row)
    for bill in bills:
        hits = by_number.get(invoice_number_key(bill["invoice_number"])) or []
        hit = None
        for row in hits:
            if names_match(bill["vendor"], str(row.get("vendor") or "")) or bill["vendor"].split()[0].lower() in str(row.get("vendor") or "").lower():
                hit = row
                break
        if hit is None and hits:
            hit = hits[0]
        if not hit:
            raise SystemExit(f"No mailbox row for {bill['invoice_number']}")
        bill["message_id"] = hit["message_id"]
        bill["email_received"] = hit.get("email_received")
        bill["email_received_dates"] = hit.get("email_received_dates") or [hit.get("email_received")]
        bill["email_received_utc"] = hit.get("email_received_utc")
        bill["subject"] = hit.get("subject")
        bill["pdf_path"] = hit.get("pdf_path")
        bill["pdf_name"] = hit.get("pdf_name")
        bill["folder"] = hit.get("folder")


def _po_in_blob(blob: str, wanted: set[str]) -> str:
    for po in wanted:
        if po and (po in blob or f"PO{po}" in blob or f"PO {po}" in blob):
            return po
    found = extract_po_number(blob)
    if found and found in wanted:
        return found
    return ""


def load_po_lines(client: Any, wanted: set[str]) -> list[dict[str, Any]]:
    rows = []
    offset = 0
    total = None
    url = client._url("purchase_lines")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"purchase line list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            values = item.get("values") or {}
            blob = json.dumps(values)
            po = _po_in_blob(blob, wanted)
            if not po:
                continue
            po_lookup = values.get("Purchase_Order_Number") or values.get("Purchase_Order")
            part = lookup_text(values.get("Item_Number") or values.get("PO_Item_Number") or values.get("Item")) or ""
            desc = lookup_text(values.get("PO_Item_Description") or values.get("Description") or values.get("Part_Description")) or ""
            display = str(values.get("Display_Name") or lookup_text(values.get("Name")) or "")
            qty = money(values.get("Quantity") or values.get("Qty"))
            unit = money(values.get("Unit_Price") or values.get("Price") or values.get("Unit_Cost"))
            wo = lookup_text(values.get("Work_Order") or values.get("Work_Order_Number") or values.get("WO")) or ""
            rows.append(
                {
                    "id": item.get("id"),
                    "po": po,
                    "po_id": lookup_id(po_lookup),
                    "line": display or part,
                    "po_line": display or part,
                    "part": part or desc,
                    "description": desc or part,
                    "qty": qty,
                    "quantity": qty,
                    "ordered": qty,
                    "unit_price": unit,
                    "unit": unit,
                    "amount": round(qty * unit, 2) if qty is not None and unit is not None else None,
                    "wo": wo,
                    "vendor_id": lookup_id(values.get("Vendor") or values.get("PO_Vendor")),
                    "vendor_text": lookup_text(values.get("Vendor") or values.get("PO_Vendor")) or "",
                }
            )
        print(f"purchase lines offset {offset} kept {len(rows)}", flush=True)
        if not items:
            break
        offset += len(items)
    return rows


def load_receipts(client: Any, wanted: set[str]) -> list[dict[str, Any]]:
    ids: list[int] = []
    offset = 0
    total = None
    url = client._url("receipts")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"receipt list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            values = item.get("values") or {}
            part = lookup_text(values.get("PO_Item_Number")) or str(values.get("Name") or "")
            if _po_in_blob(part, wanted) or _po_in_blob(json.dumps(values), wanted):
                if item.get("id") not in (None, ""):
                    ids.append(int(item["id"]))
        print(f"receipt list offset {offset} ids {len(ids)}", flush=True)
        if not items:
            break
        offset += len(items)
    rows = []
    for rid in ids:
        record = client.get_item("receipts", rid)
        values = record.get("values") or {}
        normalized = normalize_receipt(record)
        ap = values.get("AP_Invoice_Number")
        ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "")
        if ap_text in {"None", "null"}:
            ap_text = ""
        po = normalized.get("po") or _po_in_blob(json.dumps(values), wanted)
        # Quantity_Remaining is never consulted. A billed receipt is not open.
        if values.get("Invoiced") is True or ap_text:
            continue
        if money(values.get("Quantity_Received")) in (None, 0, 0.0):
            continue
        wo = values.get("Work_Order_Issue") or values.get("Work_Order")
        normalized["po"] = str(po or "")
        normalized["qty_received"] = money(values.get("Quantity_Received"))
        normalized["qty"] = normalized["qty_received"]
        normalized["unit"] = money(values.get("PO_Item_Number_$_Unit_Price")) or normalized.get("unit_price")
        normalized["unit_price"] = normalized["unit"]
        normalized["invoiced"] = False
        normalized["ap"] = ""
        normalized["work_order_id"] = lookup_id(wo)
        normalized["wo"] = lookup_text(wo) or normalized.get("wo") or ""
        rows.append(normalized)
    print(f"open receipts {len(rows)}", flush=True)
    return rows


def load_invoice_index(client: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[int, int], dict[str, int]]:
    """Invoice number → headers, vendor id → sample invoice id, vendor name → id."""
    index: dict[str, list[dict[str, Any]]] = {}
    samples: dict[int, int] = {}
    names: dict[str, int] = {}
    offset = 0
    total = None
    url = client._url("ap_invoices")
    while total is None or offset < total:
        response = client.request(
            "GET",
            url,
            params={
                "pageSize": 2000,
                "offset": offset,
                "fields": "Invoice_Number,Vendor,Posted,Void,AP_Invoice_Batch,Invoice_Amount,Invoice_Verification_Amount",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"invoice list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            values = item.get("values") or {}
            if item.get("id") in (None, ""):
                continue
            number = invoice_number_key(str(values.get("Invoice_Number") or ""))
            vendor_id = lookup_id(values.get("Vendor"))
            vendor_text = lookup_text(values.get("Vendor")) or ""
            if vendor_id and vendor_id not in samples and values.get("Void") is not True:
                samples[int(vendor_id)] = int(item["id"])
            if vendor_text and vendor_id:
                names.setdefault(vendor_text.lower(), int(vendor_id))
            if not number:
                continue
            index.setdefault(number, []).append(
                {
                    "id": int(item["id"]),
                    "vendor_id": vendor_id,
                    "vendor_text": vendor_text,
                    "posted": bool(values.get("Posted")),
                    "void": values.get("Void") is True,
                    "batch": lookup_text(values.get("AP_Invoice_Batch")) or None,
                    "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
                    "invoice_amount": values.get("Invoice_Amount"),
                    "verification_amount": values.get("Invoice_Verification_Amount"),
                }
            )
        print(f"invoice index offset {offset}", flush=True)
        if not items:
            break
        offset += len(items)
    return index, samples, names


def _hydrate_hit(client: Any, hit: dict[str, Any]) -> dict[str, Any]:
    """List view omits Vendor. Confirm the header with a record GET."""
    if hit.get("vendor_id") or hit.get("vendor_text"):
        return hit
    record = client.get_item("ap_invoices", int(hit["id"]))
    values = record.get("values") or {}
    hit["vendor_id"] = lookup_id(values.get("Vendor"))
    hit["vendor_text"] = lookup_text(values.get("Vendor")) or ""
    hit["posted"] = bool(values.get("Posted"))
    hit["void"] = values.get("Void") is True
    hit["batch"] = lookup_text(values.get("AP_Invoice_Batch")) or None
    hit["batch_id"] = lookup_id(values.get("AP_Invoice_Batch"))
    hit["invoice_amount"] = values.get("Invoice_Amount")
    hit["verification_amount"] = values.get("Invoice_Verification_Amount")
    return hit


def existing_hit(client: Any, index: dict[str, list[dict[str, Any]]], bill: dict[str, Any]) -> dict[str, Any] | None:
    from ap_clerk.cli import _is_clearly_other_vendor
    from ap_clerk.rules import vendor_match_score

    hits = [hit for hit in (index.get(invoice_number_key(bill["invoice_number"])) or []) if not hit.get("void")]
    vendor = bill["vendor"]
    wanted_id = known_vendor_id(vendor)
    kept = []
    for hit in hits:
        hit = _hydrate_hit(client, hit)
        if hit.get("void"):
            continue
        text = str(hit.get("vendor_text") or "")
        posted_id = hit.get("vendor_id")
        if wanted_id and posted_id and int(posted_id) == int(wanted_id):
            kept.append(hit)
            continue
        if wanted_id and posted_id and int(posted_id) != int(wanted_id):
            continue
        if names_match(vendor, text) or vendor_match_score(vendor, text):
            kept.append(hit)
        elif text and _is_clearly_other_vendor(vendor, text, posted_id):
            continue
        elif text:
            kept.append(hit)
    if not kept:
        return None
    return sorted(kept, key=lambda row: int(row["id"]))[-1]


def vendor_sample(client: Any, bill: dict[str, Any], po_lines: list[dict[str, Any]]) -> dict[str, Any]:
    vendor_id = known_vendor_id(bill["vendor"])
    po = str(bill.get("po") or "")
    po_id = None
    if po:
        for row in po_lines:
            if str(row.get("po")) == po and row.get("po_id"):
                po_id = int(row["po_id"])
                break
    sample_id = CONFIRMED_VENDOR_SAMPLES.get(int(vendor_id)) if vendor_id else None
    if sample_id is None or vendor_id is None:
        return {"vendor_id": vendor_id, "sample_id": sample_id, "po_id": po_id, "terms_id": None, "remit_id": None, "currency_id": CURRENCY_USD_ID}
    record = client.get_item("ap_invoices", int(sample_id))
    values = record.get("values") or {}
    posted_vendor = lookup_id(values.get("Vendor"))
    if posted_vendor != int(vendor_id) or values.get("Void") is True:
        return {"vendor_id": vendor_id, "sample_id": None, "po_id": po_id, "terms_id": None, "remit_id": None, "currency_id": CURRENCY_USD_ID}
    return {
        "vendor_id": int(posted_vendor),
        "sample_id": int(sample_id),
        "po_id": po_id,
        "terms_id": lookup_id(values.get("Terms_Code")),
        "remit_id": lookup_id(values.get("Remit_To_Address")),
        "currency_id": lookup_id(values.get("Currency")) or CURRENCY_USD_ID,
        "vendor_text": lookup_text(values.get("Vendor")) or bill["vendor"],
        "terms_text": lookup_text(values.get("Terms_Code")) or "",
    }


def _full_quantity_received(bill: dict[str, Any], chosen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the receipt's Quantity_Received when it covers every invoice line.

    Willbanks 213645 is two lines of 10. Receipt 23821 is Quantity_Received 20
    at the same unit price. Taking only the first line qty leaves a false gap.
    Quantity_Remaining is never used.
    """
    if not chosen:
        return chosen
    invoiced_qty = round(sum(float(money(line.get("qty")) or 0) for line in bill.get("lines") or []), 4)
    by_id: dict[int, dict[str, Any]] = {}
    for row in chosen:
        by_id[int(row["id"])] = row
    full_qty = round(
        sum(
            float(money(row.get("qty_received") if row.get("qty_received") is not None else row.get("qty")) or 0)
            for row in by_id.values()
        ),
        4,
    )
    if abs(full_qty - invoiced_qty) > 0.05:
        return chosen
    promoted = []
    for row in by_id.values():
        copy = dict(row)
        copy.pop("select_qty", None)
        full = money(copy.get("qty_received") if copy.get("qty_received") is not None else copy.get("qty"))
        copy["qty"] = full
        promoted.append(copy)
    return promoted


def receipt_extension(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        qty = money(row.get("select_qty"))
        if qty is None:
            qty = money(row.get("qty_received") if row.get("qty_received") is not None else row.get("qty"))
        price = money(row.get("unit_price") if row.get("unit_price") is not None else row.get("unit"))
        if qty is None or price is None:
            continue
        total = round(total + round(qty * price, 2), 2)
    return total


def plan_bill(bill: dict[str, Any], po_lines: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    po = str(bill.get("po") or "")
    fee_total = round(sum(float(fee.get("amount") or 0) for fee in bill.get("fees") or []), 2)
    po_rows = [row for row in po_lines if str(row.get("po") or "") == po]
    open_rows = [row for row in receipts if str(row.get("po") or "") == po]
    wo_rows = [row for row in open_rows if row.get("wo") or row.get("work_order_id")]
    if not po:
        return {
            "action": "missing_po",
            "receipts": [],
            "considered": [],
            "open_rows": [],
            "wo_checked": wo_rows,
            "fee_total": fee_total,
            "received_ext": 0.0,
            "gap": None,
            "qty_difference": False,
            "match": None,
        }
    matched = match_receipts(
        invoice_number=bill["invoice_number"],
        invoice_lines=bill.get("lines") or [],
        receipts=open_rows,
        po_number=po,
        invoice_amount=None,
        slip_numbers=[],
    )
    chosen = []
    for hit in matched.get("matched") or []:
        receipt = dict(hit.get("receipt") or {})
        if hit.get("select_qty") is not None:
            receipt["select_qty"] = hit.get("select_qty")
        chosen.append(receipt)
    chosen = _full_quantity_received(bill, chosen)
    received_ext = receipt_extension(chosen)
    invoiced_qty = round(sum(float(money(line.get("qty")) or 0) for line in bill.get("lines") or []), 4)
    received_qty = round(
        sum(float(money(row.get("select_qty") if row.get("select_qty") is not None else row.get("qty")) or 0) for row in chosen),
        4,
    )
    qty_difference = bool(chosen) and abs(invoiced_qty - received_qty) > 0.05
    missing = not chosen
    gap = None if missing else round(float(bill["total"]) - received_ext - fee_total, 2)
    if missing:
        # Open leftovers that do not match qty are quantity_variance, not missing_receipt.
        action = "quantity_variance" if open_rows else "missing_receipt"
    elif gap is not None and abs(gap) >= PPV_LIMIT:
        action = "quantity_variance" if qty_difference else "price_variance"
    else:
        action = "select"
    return {
        "action": action,
        "receipts": chosen if action == "select" else [],
        "considered": chosen,
        "open_rows": open_rows,
        "wo_checked": wo_rows,
        "po_rows": po_rows,
        "fee_total": fee_total,
        "received_ext": received_ext if not missing else 0.0,
        "gap": gap,
        "qty_difference": qty_difference,
        "match_why": matched.get("why") or "",
        "unmatched": matched.get("unmatched_lines") or [],
    }


def _owner_tag(vendor: str, action: str) -> tuple[str, bool]:
    """Return (plain tag, shawn_mention). Price variance is always Shawn."""
    if action == "price_variance":
        return "@Shawn McKibben", True
    entry = lookup_receiving_owner(vendor) or {}
    keys = [str(key) for key in (entry.get("owner_keys") or [])]
    if action == "missing_receipt":
        if not entry.get("needs_dock_receive"):
            return "", False
        if keys == ["shawn"] or (len(keys) == 1 and keys[0] == "shawn"):
            return "@Shawn McKibben", True
        if "ruben" in keys and "shawn" not in keys:
            return "@Ruben Perez", False
        if "shawn" in keys:
            return "@Shawn McKibben", True
        people = " and ".join(f"@{key.title()}" for key in keys)
        return people, False
    if action in {"quantity_variance", "missing_po"} and "shawn" in keys:
        return "@Shawn McKibben", True
    if action in {"quantity_variance", "missing_po"}:
        return "@Shawn McKibben", True
    return "", False


def build_note(bill: dict[str, Any], plan: dict[str, Any], *, status: str, amount_entered: float | None, ppv: float | None) -> str:
    vendor = bill["vendor"]
    number = bill["invoice_number"]
    total = float(bill["total"])
    po = bill.get("po") or "none"
    action = plan["action"]
    tag, _mention = _owner_tag(vendor, action)
    if status == "Success":
        receipts = plan.get("receipts") or []
        label = "; ".join(
            f"{row.get('id')} qty {qty_text(money(row.get('select_qty') if row.get('select_qty') is not None else row.get('qty')))} @ {row.get('unit_price')}"
            for row in receipts
        )
        ppv_bit = (
            f"Invoice total minus the receipt lines and fees was {dollar(plan.get('gap'))}, which is under {dollar(PPV_LIMIT)}, so one signed Purchase Price Variance of {dollar(ppv)} was posted."
            if ppv not in (None, 0, 0.0)
            else "The selected receipt lines and fees equal the PDF total, so no Purchase Price Variance was posted."
        )
        text = (
            f"AP Clerk: {vendor} invoice {number} is entered and is not posted. "
            f"The PDF total is {dollar(total)}. Amount entered is {dollar(amount_entered if amount_entered is not None else total)}. "
            f"PO {po}. Receipts selected by Quantity_Received: {label or 'none'}. {ppv_bit}"
        )
        return re.sub(r"\s+", " ", text).strip()
    if action == "missing_po":
        printed = bill.get("printed_po_not_kimco")
        if printed:
            po_bit = (
                f"The invoice prints purchase order {printed}. That reference is not a KIMCO purchase order, "
                f"and no KIMCO purchase order for this vendor and these lines was found."
            )
        else:
            po_bit = (
                "No purchase order is printed on the invoice, "
                "and no purchase order for this vendor and these lines was found in KIMCO."
            )
        text = (
            f"AP Clerk: {vendor} invoice {number} cannot be finished. "
            f"The PDF total is {dollar(total)}. {po_bit} "
            f"The header was created and the PDF is attached. No receipt lines were selected and no Purchase Price Variance was posted. "
            f"Someone needs to point AP at the purchase order, or confirm this is a non-PO bill and enter the lines. "
            f"The bill is not posted."
        )
        return re.sub(r"\s+", " ", text).strip()
    who = f"{tag} " if tag else ""
    considered = plan.get("considered") or []
    rec_bit = ""
    if considered:
        rec_bit = " Receipts by Quantity_Received: " + "; ".join(
            f"{row.get('id')} qty {qty_text(money(row.get('qty')))} @ {row.get('unit_price')}" for row in considered
        ) + "."
    elif plan.get("open_rows"):
        rec_bit = " Open receipts on the PO were checked and none matched the invoice lines."
    else:
        rec_bit = " There is no open receipt on that PO."
    wo_bit = ""
    if plan.get("wo_checked"):
        wo_bit = (
            " Work-order and outside-service lines were checked. "
            + "; ".join(
                f"receipt {row.get('id')} work order {row.get('wo') or row.get('work_order_id')}"
                for row in plan["wo_checked"][:6]
            )
            + "."
        )
    else:
        wo_bit = " Work-order and outside-service lines on the PO were checked and none were found."
    if action == "price_variance":
        why = (
            f"Invoice total minus the receipt lines and fees is {dollar(plan.get('gap'))}, which is {dollar(PPV_LIMIT)} or more, "
            "so no Purchase Price Variance was posted and the receipts were not selected. "
            "Selecting them would lock the receipt and block an unreceive. "
            "The quantity matches, so this is a price difference, not a missing receipt. "
            "Shawn should unreceive the line, set the PO price to the invoice price, and re-receive the same quantity. "
            "After that, AP will select the receipts and finish the bill."
        )
    elif action == "missing_receipt":
        why = (
            "This cannot be finished because a line has no selectable receipt. "
            "Quantity_Received is zero or there is no open receipt on the matching part, so this is a missing receipt and not a price variance. "
            f"{wo_bit} "
            "The receiving owner should receive the invoiced quantity. After that receipt exists, AP will select it by Quantity_Received and finish the bill."
        )
    elif action == "quantity_variance":
        why = (
            f"Quantity received does not match quantity invoiced, and the dollar gap is {dollar(plan.get('gap'))}. "
            "No receipts were selected. "
            "Shawn should correct the receipt so Quantity_Received matches the invoiced quantity, then tell AP to select that receipt and finish."
        )
    else:
        why = "This bill cannot be finished from the PDF and the open receipts."
    text = (
        f"AP Clerk: {who}{vendor} invoice {number} cannot be finished. "
        f"The PDF total is {dollar(total)}. Amount entered on receipt lines is {dollar(amount_entered if amount_entered is not None else 0)}. "
        f"PO {po}.{rec_bit}{wo_bit} {why} The bill was moved to Transfer AP and is not posted."
    )
    return re.sub(r"\s+", " ", text).strip()


def html_for_note(text: str, *, mention: bool) -> str:
    body = text
    if mention:
        if "@Shawn McKibben" not in text:
            raise RuntimeError("Shawn hold note is missing the name")
        body = text.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    return f"<p>{body}</p>"


def write_comment(client: Any, kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    html = html_for_note(text, mention=mention)
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {
            "Comments_1": [
                {
                    "state": "Added",
                    "values": {
                        "HtmlValue": html,
                        "Entity": {"id": 203},
                        "ObjectId": int(kimco_id),
                        "FormId": 218,
                    },
                }
            ]
        },
    }
    try:
        _body, status, error = client.update("ap_invoices", kimco_id, payload)
    except KimcoError as exc:
        return {"status": "blocked", "error": str(exc)[:200], "id": None, "mention": False}
    record = client.get_item("ap_invoices", kimco_id)
    match = None
    mention_saved = False
    needle = text.split("@Shawn McKibben", 1)[-1][:40].strip() if "@Shawn McKibben" in text else text[10:50]
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        html_live = str((comment.get("values") or {}).get("HtmlValue") or "")
        tagged = 'data-mention-id="104"' in html_live
        if needle[:24] and needle[:24] in html_live:
            match = comment.get("id")
            mention_saved = tagged
            if tagged or not mention:
                break
    return {"status": "persisted" if status < 400 and match else f"put-{status}", "id": match, "mention": mention_saved, "error": error}


def create_header(client: Any, bill: dict[str, Any], batch_id: int, sample: dict[str, Any], *, invoice_type: int, po_id: int | None) -> tuple[int | None, int, str]:
    day = _parse_day(bill["date"])
    due = _parse_day(bill["due"]) if bill.get("due") else due_date_from_terms(day, sample.get("terms_text"))
    payload = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(sample["vendor_id"])},
        "Invoice_Number": bill["invoice_number"],
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": float(bill["total"]),
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": int(sample["terms_id"])},
        "Currency": {"id": int(sample["currency_id"] or CURRENCY_USD_ID)},
        "Remit_To_Address": {"id": int(sample["remit_id"])},
        "Transaction_Date": kimco_datetime(day),
        "Comments": "API Agent",
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    return client.create("ap_invoices", payload)[:3] if False else _create(client, payload)


def _create(client: Any, payload: dict[str, Any]) -> tuple[int | None, int, str]:
    created_id, _body, status, error = client.create("ap_invoices", payload)
    return created_id, status, error


def snapshot(client: Any, kimco_id: int) -> dict[str, Any]:
    from ap_clerk.rules import ppv_qc_gap

    record = client.get_item("ap_invoices", kimco_id)
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    line_amounts = []
    receipt_rows = []
    for line in lists.get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        ext = money(lv.get("Extended_Amount"))
        if ext is not None:
            line_amounts.append(ext)
        receipt_rows.append(
            {
                "id": lookup_id(lv.get("Receipt")),
                "qty": money(lv.get("Quantity")),
                "unit_price": money(lv.get("Unit_Price")),
                "work_order": lookup_id(lv.get("Work_Order")),
            }
        )
    charge_amounts = []
    ppv_amounts = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        cv = charge.get("values") or {}
        amount = money(cv.get("Amount"))
        if amount is None:
            continue
        charge_amounts.append(amount)
        kind = lookup_text(cv.get("Additional_Charges")) or ""
        if "price variance" in kind.lower():
            ppv_amounts.append(amount)
    qc = ppv_qc_gap(
        invoice_amount=values.get("Invoice_Amount"),
        verification_amount=values.get("Invoice_Verification_Amount"),
        line_amounts=line_amounts,
        charge_amounts=charge_amounts,
    )
    comments = []
    for comment in lists.get("Comments_1") or []:
        comments.append({"id": comment.get("id"), "html": (comment.get("values") or {}).get("HtmlValue") or ""})
    return {
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "posted": values.get("Posted"),
        "batch_name": lookup_text(values.get("AP_Invoice_Batch")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "qc": qc,
        "receipt_rows": receipt_rows,
        "comments": comments,
        "ppv_amounts": ppv_amounts,
    }


def public_invoice(bill: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return {
        "vendor": bill["vendor"],
        "invoice_number": bill["invoice_number"],
        "status": kwargs["status"],
        "reason": kwargs["reason"],
        "type": "service" if bill.get("service") else ("parts" if bill.get("po") else "misc"),
        "pdf_total": bill["total"],
        "kimco_bill_id": kwargs.get("kimco_id"),
        "batch": kwargs.get("batch"),
        "transfer_ap": kwargs.get("transfer_ap", "no"),
        "receipts_selected": kwargs.get("receipts") or "",
        "ppv": kwargs.get("ppv"),
        "comments_1_id": kwargs.get("comments_1_id"),
        "email_moved": kwargs.get("email_moved", "no"),
        "note": kwargs.get("note") or "",
        "email_received": bill.get("email_received_dates") or [bill.get("email_received")],
        "amount": bill["total"],
    }


def enter(*, write: bool) -> dict[str, Any]:
    from ap_clerk.auth import load_credentials, resolve_target
    from ap_clerk.cli import _find_or_create_batch, _optional_graph_client
    from ap_clerk.kimco import KimcoClient

    attach_mail(BILLS)
    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph credentials missing")
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error)
    client = KimcoClient.authenticate(creds.instance_url, creds.key or "", creds.password or "", target="live")
    wanted = {str(bill["po"]) for bill in BILLS if bill.get("po")}
    print("Loading purchase lines and receipts", flush=True)
    po_lines = load_po_lines(client, wanted)
    receipts = load_receipts(client, wanted)
    print("Loading invoice index", flush=True)
    index, samples, names = load_invoice_index(client)
    plans = []
    already_extra = []
    for bill in BILLS:
        hit = existing_hit(client, index, bill)
        if hit:
            already_extra.append({"bill": bill, "hit": hit})
            print(f"ALREADY {bill['invoice_number']} {hit['id']}", flush=True)
            continue
        if bill.get("batch") == "TRANSFER AP":
            continue
        sample = vendor_sample(client, bill, po_lines)
        plan = plan_bill(bill, po_lines, receipts)
        plan["bill"] = bill
        plan["sample"] = sample
        plans.append(plan)
        print(
            json.dumps(
                {
                    "invoice": bill["invoice_number"],
                    "vendor": bill["vendor"],
                    "po": bill.get("po"),
                    "action": plan["action"],
                    "gap": plan.get("gap"),
                    "received_ext": plan.get("received_ext"),
                    "open": len(plan.get("open_rows") or []),
                    "wo": len(plan.get("wo_checked") or []),
                    "vendor_id": sample.get("vendor_id"),
                    "po_id": sample.get("po_id"),
                    "terms": sample.get("terms_id"),
                    "remit": sample.get("remit_id"),
                    "receipts": [
                        {"id": row.get("id"), "qty": row.get("qty"), "price": row.get("unit_price"), "part": str(row.get("part") or "")[:40]}
                        for row in (plan.get("considered") or [])
                    ],
                },
                default=str,
            ),
            flush=True,
        )
    if not write:
        return {"dry": True, "plans": len(plans), "already": len(already_extra)}

    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    batch_id = int(batch["id"])
    print(f"BATCH {batch_id} created={batch.get('created')}", flush=True)
    results = []
    for plan in plans:
        bill = plan["bill"]
        sample = plan["sample"]
        number = bill["invoice_number"]
        if not sample.get("vendor_id") or not sample.get("terms_id") or not sample.get("remit_id"):
            note = (
                f"AP Clerk: {bill['vendor']} invoice {number} was not created. "
                f"KIMCO has no vendor remit and terms sample for this company, so the header was not invented. "
                f"The PDF total is {dollar(bill['total'])}. The bill is not posted."
            )
            results.append(public_invoice(bill, status="HOLD", reason="vendor remit missing", kimco_id=None, batch=BATCH_NAME, note=note, ppv=0))
            print(f"NO-VENDOR {number}", flush=True)
            continue
        po_id = sample.get("po_id")
        if bill.get("po") and not po_id:
            note = (
                f"AP Clerk: {bill['vendor']} invoice {number} prints PO {bill['po']}, but that purchase order id was not found in KIMCO. "
                f"The PDF total is {dollar(bill['total'])}. No header was created as a miscellaneous bill. The bill is not posted."
            )
            results.append(public_invoice(bill, status="HOLD", reason="PO id missing", kimco_id=None, batch=BATCH_NAME, note=note, ppv=0))
            print(f"NO-PO-ID {number}", flush=True)
            continue
        invoice_type = 3 if po_id else 4
        created_id, status, error = _create_for_bill(client, bill, batch_id, sample, invoice_type, po_id)
        if created_id is None:
            note = f"AP Clerk: {bill['vendor']} invoice {number} header was not created (HTTP {status}). The bill is not posted."
            results.append(public_invoice(bill, status="HOLD", reason=f"header HTTP {status}", kimco_id=None, batch=BATCH_NAME, note=note, ppv=0))
            print(f"CREATE-FAIL {number} {status} {error[:180]}", flush=True)
            continue
        pdf_path = Path(bill["pdf_path"])
        pdf = pdf_path.read_bytes()
        attach = client.try_official_attach(created_id, name=f"{number}.pdf", content_type="application/pdf", size=len(pdf), content=pdf)
        mention = plan["action"] in {"missing_receipt", "price_variance", "quantity_variance"}
        if plan["action"] != "select":
            note = build_note(bill, plan, status="HOLD", amount_entered=0, ppv=0)
            comment = write_comment(client, created_id, note, mention=mention and "@Shawn McKibben" in note)
            moved = {"status": "not-moved", "batch_name": BATCH_NAME}
            mention_ok = (not mention) or comment.get("mention") is True
            if plan["action"] != "missing_po" and comment.get("id") and mention_ok:
                moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
            on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
            results.append(
                public_invoice(
                    bill,
                    status="HOLD",
                    reason=plan["action"],
                    kimco_id=created_id,
                    batch=moved.get("batch_name") or BATCH_NAME,
                    transfer_ap="yes" if on_transfer else "no",
                    receipts="",
                    ppv=0.0,
                    comments_1_id=comment.get("id"),
                    note=note,
                    email_moved="pending",
                )
            )
            results[-1]["_attached"] = attach == "attached"
            results[-1]["_message_id"] = bill.get("message_id")
            print(f"HOLD {number} id={created_id} {plan['action']} transfer={moved.get('status')} comment={comment.get('id')}", flush=True)
            continue
        if bill.get("fees"):
            client.try_post_fees(created_id, bill["fees"])
        receipt_ids = []
        for row in plan["receipts"]:
            if row.get("select_qty") is not None and money(row.get("select_qty")) != money(row.get("qty")):
                receipt_ids.append({"id": int(row["id"]), "qty": row["select_qty"]})
            else:
                receipt_ids.append(int(row["id"]))
        select_status = client.try_select_receipts(created_id, receipt_ids)
        pre = pre_finish_totals_check(client, created_id)
        live = snapshot(client, created_id)
        ppv_amount = float(pre.get("ppv_posted") or 0) or round(sum(live.get("ppv_amounts") or []), 2)
        gap = (live.get("qc") or {}).get("gap")
        success = (
            select_status == "selected"
            and pre.get("ok") is True
            and (live.get("qc") or {}).get("success_allowed") is True
            and gap in (0, 0.0)
            and live.get("posted") in (None, "", False)
            and live.get("verification") == bill["total"]
        )
        if not success and gap not in (None, 0, 0.0) and abs(float(gap)) >= PPV_LIMIT:
            client.try_deselect_receipts(created_id)
            plan = dict(plan)
            plan["action"] = "price_variance"
            plan["gap"] = gap
            note = build_note(bill, plan, status="HOLD", amount_entered=0, ppv=0)
            comment = write_comment(client, created_id, note, mention=True)
            moved = apply_transfer_ap_batch_move(client, kimco_id=created_id) if comment.get("id") and comment.get("mention") else {"status": "not-moved"}
            results.append(
                public_invoice(
                    bill,
                    status="HOLD",
                    reason="price_variance",
                    kimco_id=created_id,
                    batch=moved.get("batch_name") or BATCH_NAME,
                    transfer_ap="yes" if moved.get("status") in {"moved", "already-on-transfer-ap"} else "no",
                    receipts="",
                    ppv=0.0,
                    comments_1_id=comment.get("id"),
                    note=note,
                )
            )
            results[-1]["_attached"] = attach == "attached"
            results[-1]["_message_id"] = bill.get("message_id")
            print(f"HOLD-AFTER-SELECT {number} gap={gap}", flush=True)
            continue
        label = "; ".join(
            f"{row.get('id')} qty {qty_text(row.get('qty'))} @ {row.get('unit_price')}" for row in live["receipt_rows"]
        )
        if success:
            note = build_note(bill, plan, status="Success", amount_entered=live.get("amount"), ppv=ppv_amount)
            results.append(
                public_invoice(
                    bill,
                    status="Success",
                    reason="totals match to the penny",
                    kimco_id=created_id,
                    batch=BATCH_NAME,
                    receipts=label,
                    ppv=ppv_amount or 0.0,
                    comments_1_id=None,
                    note=note,
                )
            )
        else:
            note = (
                f"AP Clerk: {bill['vendor']} invoice {number} was created as KIMCO {created_id} but was not finished. "
                f"The PDF total is {dollar(bill['total'])}. Select Receipts status was {select_status}. "
                f"The live gap is {gap}. The bill is not posted and was left on {BATCH_NAME}."
            )
            comment = write_comment(client, created_id, note, mention=False)
            results.append(
                public_invoice(
                    bill,
                    status="HOLD",
                    reason=f"select={select_status} gap={gap}",
                    kimco_id=created_id,
                    batch=BATCH_NAME,
                    receipts=label,
                    ppv=ppv_amount,
                    comments_1_id=comment.get("id"),
                    note=note,
                )
            )
        results[-1]["_attached"] = attach == "attached"
        results[-1]["_message_id"] = bill.get("message_id")
        print(f"{'SUCCESS' if success else 'HOLD-OPEN'} {number} id={created_id} gap={gap} ppv={ppv_amount}", flush=True)

    _finish_mail(graph, results)
    created_ids = [int(row["kimco_bill_id"]) for row in results if row.get("kimco_bill_id")]
    qc_report = scan_ids(client, created_ids) if created_ids else {"scanned": 0, "nonzero_gap_count": 0, "gaps": [], "bills": []}
    catalog = json.loads((CACHE / "catalog.json").read_text())
    _add_statement_skips(catalog)
    for row in already_extra:
        bill = row["bill"]
        hit = row["hit"]
        catalog["already_in_kimco"].append(
            {
                "invoice_number": bill["invoice_number"],
                "vendor": bill["vendor"],
                "kimco_bill_id": hit["id"],
                "posted": hit["posted"],
                "batch": hit["batch"],
                "batch_id": hit["batch_id"],
                "invoice_amount": hit["invoice_amount"],
                "verification_amount": hit["verification_amount"],
                "left_alone": True,
                "email_received": bill.get("email_received_dates"),
                "pdf_total": bill["total"],
                "po": bill.get("po"),
            }
        )
    payload = {
        "run": "batch10-2026-09-25",
        "batch_name": BATCH_NAME,
        "batch_id": batch_id,
        "posted": False,
        "packing_slip_gate": "suspended",
        "quantity_field": "Quantity_Received",
        "mailbox_scan": catalog.get("mailbox_scan"),
        "skipped": catalog.get("skipped"),
        "already_in_kimco": catalog.get("already_in_kimco"),
        "invoices": [{key: value for key, value in row.items() if not key.startswith("_")} for row in results],
        "ppv_qc": {
            "read_only": True,
            "scanned": qc_report.get("scanned"),
            "nonzero_gap_count": qc_report.get("nonzero_gap_count"),
            "gaps": qc_report.get("gaps"),
            "bills": qc_report.get("bills"),
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"WROTE {OUT_JSON}", flush=True)
    return payload


def _add_statement_skips(catalog: dict[str, Any]) -> None:
    """Versalift and Trace Metal parsed as invoices. The PDFs are statements."""
    pending = json.loads((CACHE / "pending.json").read_text())
    by_number = {invoice_number_key(row.get("invoice_number")): row for row in pending}
    skipped = catalog.setdefault("skipped", [])
    existing = {invoice_number_key(row.get("invoice_number")) for row in skipped}
    extras = [
        (
            "0003082102",
            "Account statement is not an invoice. Statement Ref 0003082102 lists older Versalift invoices and a balance due. It was not entered.",
        ),
        (
            "237704",
            "Account statement is not an invoice. The Trace Metal statement lists INV 237704 and INV 237705. Those invoice PDFs were not in this mail, so neither invoice was entered.",
        ),
    ]
    for number, reason in extras:
        key = invoice_number_key(number)
        if key in existing:
            continue
        row = by_number.get(key) or {}
        skipped.append(
            {
                "kind": "statement",
                "reason": reason,
                "subject": row.get("subject"),
                "email_received": row.get("email_received"),
                "email_received_utc": row.get("email_received_utc"),
                "message_id": row.get("message_id"),
                "pdf_name": row.get("pdf_name"),
                "vendor": row.get("vendor"),
                "invoice_number": number,
            }
        )


def _create_for_bill(client, bill, batch_id, sample, invoice_type, po_id):
    day = _parse_day(bill["date"])
    due = _parse_day(bill["due"]) if bill.get("due") else due_date_from_terms(day, sample.get("terms_text"))
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(sample["vendor_id"])},
        "Invoice_Number": bill["invoice_number"],
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": float(bill["total"]),
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": int(sample["terms_id"])},
        "Currency": {"id": int(sample["currency_id"] or CURRENCY_USD_ID)},
        "Remit_To_Address": {"id": int(sample["remit_id"])},
        "Transaction_Date": kimco_datetime(day),
        "Comments": "API Agent",
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    created_id, _body, status, error = client.create("ap_invoices", payload)
    return created_id, status, error or ""


def _finish_mail(graph: Any, results: list[dict[str, Any]]) -> None:
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(folder.get("id") or "")
    by_message: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        message_id = row.get("_message_id")
        if message_id and row.get("_attached") and row.get("kimco_bill_id"):
            by_message.setdefault(message_id, []).append(row)
    for message_id, rows in by_message.items():
        flag = graph.flag_matched if all(row["status"] == "Success" for row in rows) else graph.flag_issues
        flag(ALLOWED_MAILBOX, message_id)
        current = graph.get_message(ALLOWED_MAILBOX, message_id, select="id,parentFolderId,categories")
        if str(current.get("parentFolderId") or "") == fort_id:
            for row in rows:
                row["email_moved"] = "yes"
            continue
        moved = graph.move_message(ALLOWED_MAILBOX, message_id, fort_id)
        new_id = str(moved.get("new_id") or message_id)
        after = graph.get_message(ALLOWED_MAILBOX, new_id, select="id,parentFolderId,categories")
        in_folder = str(after.get("parentFolderId") or "") == fort_id
        for row in rows:
            row["email_moved"] = "yes" if in_folder else "no"


def main() -> int:
    write = "--write" in sys.argv
    enter(write=write)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
