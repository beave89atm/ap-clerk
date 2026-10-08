"""Enter the 2026-10-08 live AP run. Cap 25 new invoices.

Does not post, close a batch, auto-pay, or send mail. One sign-in, then
at most one re-sign-in after a 401. Every write is API Agent user 175.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any

import pymupdf
from openpyxl import Workbook
from openpyxl.styles import Font
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.cli import _find_or_create_batch
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    GraphClient,
    load_graph_credentials,
)
from ap_clerk.kimco import KimcoClient, KimcoError, added_comment_payload
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.rules import (
    ap_clerk_edit_note,
    comments_for,
    due_date_from_terms,
    invoice_number_key,
    kimco_datetime,
    lookup_id,
    lookup_text,
    money,
)
from scripts.sept25_30_attachment_audit import attachment_bytes, receipt_fact
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of, stamp
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept25_30_po59081_receipts import child_receipt_ids
from scripts.sept_missed_entry_2026_10_07 import put, totals

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("ap-run-1008")

OUT = ROOT / "runs" / "ap-run-2026-10-08"
QC = OUT / "qc"
SRC = Path("/tmp/ap-run-1008/splits")
BATCH_NAME = "API Agent - 10/8/26"
TRANSFER = 375
CAP = 25
MIN_NEW_ID = 10540
PPV_LIMIT = 75.0
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime"

# Printed totals read from the invoice page text (math ties to the total line).
JOBS: list[dict[str, Any]] = [
    {
        "number": "367745",
        "vendor": "Luxor Staffing",
        "vendor_id": 112,
        "day": date(2026, 10, 1),
        "amount": 2540.30,
        "kind": "misc",
        "lines": [{"item_id": 47, "description": "Contract labor", "qty": 1, "unit_price": 2540.30}],
        "pdf": "luxor-367745.pdf",
        "received": "2026-10-01T16:09:29Z",
        "po": "",
        "owner": "treyce",
    },
    {
        "number": "PS-INV104078",
        "vendor": "Legacy Wire Products",
        "vendor_id": 292,
        "day": date(2026, 10, 1),
        "amount": 702.00,
        "kind": "po",
        "po": "59334",
        "po_id": 7336,
        "merch": 702.00,
        "lines_match": [{"qty": 27, "amount": 702.00}],
        "pdf": "legacy-104078.pdf",
        "received": "2026-10-01T20:02:59Z",
        "owner": "treyce",
    },
    {
        "number": "PS-INV104079",
        "vendor": "Legacy Wire Products",
        "vendor_id": 292,
        "day": date(2026, 10, 1),
        "amount": 1248.00,
        "kind": "po",
        "po": "59322",
        "po_id": 7324,
        "merch": 1248.00,
        "lines_match": [{"qty": 48, "amount": 1248.00}],
        "pdf": "legacy-104079.pdf",
        "received": "2026-10-01T20:17:17Z",
        "owner": "treyce",
    },
    {
        "number": "1474542",
        "vendor": "RMP Industrial Supply",
        "vendor_id": 322,
        "day": date(2026, 10, 1),
        "amount": 226.14,
        "kind": "misc",
        "lines": [{"item_id": 53, "description": "Toolholders", "qty": 1, "unit_price": 204.00}],
        "freight": 22.14,
        "pdf": "rmp-1474542.pdf",
        "received": "2026-10-01T23:02:00Z",
        "po": "",
        "owner": "treyce",
    },
    {
        "number": "1474543",
        "vendor": "RMP Industrial Supply",
        "vendor_id": 322,
        "day": date(2026, 10, 1),
        "amount": 171.39,
        "kind": "misc",
        "lines": [{"item_id": 53, "description": "Inserts", "qty": 1, "unit_price": 156.02}],
        "freight": 15.37,
        "pdf": "rmp-1474543.pdf",
        "received": "2026-10-01T23:02:00Z",
        "po": "",
        "owner": "treyce",
    },
    {
        "number": "0040483159",
        "vendor": "Gas and Supply",
        "vendor_id": 71,
        "day": date(2026, 10, 1),
        "amount": 2119.68,
        "kind": "misc",
        "lines": [{"item_id": 31, "description": "Shop supplies", "qty": 1, "unit_price": 2119.68}],
        "pdf": "gas-0040483159.pdf",
        "received": "2026-10-02T04:39:10Z",
        "po": "",
        "owner": "none",
    },
    {
        "number": "0040484366",
        "vendor": "Gas and Supply",
        "vendor_id": 71,
        "day": date(2026, 10, 1),
        "amount": 312.00,
        "kind": "misc",
        "lines": [{"item_id": 31, "description": "Shop supplies", "qty": 1, "unit_price": 312.00}],
        "pdf": "gas-0040484366.pdf",
        "received": "2026-10-02T04:39:10Z",
        "po": "",
        "owner": "none",
    },
    {
        "number": "0040482925",
        "vendor": "Gas and Supply",
        "vendor_id": 71,
        "day": date(2026, 10, 1),
        "amount": 43.80,
        "kind": "po",
        "po": "59081",
        "po_id": 7083,
        "merch": 43.80,
        "lines_match": [{"qty": 4, "amount": 43.80, "token": "PRTJ86"}],
        "pdf": "gas-0040482925.pdf",
        "received": "2026-10-02T04:39:10Z",
        "owner": "shawn",
    },
    {
        "number": "0040484376",
        "vendor": "Gas and Supply",
        "vendor_id": 71,
        "day": date(2026, 10, 1),
        "amount": 264.00,
        "kind": "misc",
        "lines": [{"item_id": 31, "description": "Shop supplies", "qty": 1, "unit_price": 264.00}],
        "pdf": "gas-0040484376.pdf",
        "received": "2026-10-02T04:40:07Z",
        "po": "",
        "owner": "none",
    },
    {
        "number": "125624",
        "vendor": "JP Steel",
        "vendor_id": 100,
        "day": date(2026, 10, 1),
        "amount": 394.50,
        "kind": "po",
        "po": "59187",
        "merch": 394.50,
        "lines_match": [{"qty": 789, "amount": 394.50, "uom": "IN"}],
        "strict_inches": True,
        "pdf": "jp-125624.pdf",
        "received": "2026-10-02T13:52:50Z",
        "owner": "shawn",
    },
    {
        "number": "28396",
        "vendor": "Crosslink Powder Coating",
        "vendor_id": 278,
        "day": date(2026, 10, 1),
        "amount": 2671.25,
        "kind": "po",
        "po": "59258",
        "merch": 2644.80,
        "fee": 26.45,
        "lines_match": [{"qty": 10, "amount": 2055.80}, {"qty": 2, "amount": 589.00}],
        "pdf": "crosslink-28396.pdf",
        "received": "2026-10-02T14:46:05Z",
        "owner": "treyce",
    },
    {
        "number": "PS-INV104080",
        "vendor": "Legacy Wire Products",
        "vendor_id": 292,
        "day": date(2026, 10, 2),
        "amount": 2506.50,
        "kind": "po",
        "po": "59271",
        "merch": 2050.00,
        "freight": 456.50,
        "lines_match": [{"qty": 17, "amount": 697.00}, {"qty": 33, "amount": 1353.00}],
        "pdf": "legacy-104080.pdf",
        "received": "2026-10-02T15:47:30Z",
        "owner": "treyce",
    },
    {
        "number": "2610003592",
        "vendor": "Hudson Energy",
        "vendor_id": 88,
        "day": date(2026, 10, 2),
        "amount": 2508.12,
        "kind": "misc",
        "lines": [
            {
                "item_id": 2,
                "description": "Electric bill 08/31/2026 through 10/01/2026",
                "qty": 1,
                "unit_price": 2508.12,
                "gl_id": 75,
            }
        ],
        "pdf": "hudson-2610003592.pdf",
        "received": "2026-10-02T16:01:51Z",
        "po": "",
        "owner": "treyce",
        "due_on_receipt": True,
    },
    {
        "number": "18663",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 29),
        "amount": 6736.92,
        "kind": "po",
        "po": "58966",
        "merch": 6736.92,
        "lines_match": [{"qty": 62, "amount": 6736.92}],
        "pdf": "tpi-18663.pdf",
        "received": "2026-10-02T16:12:16Z",
        "owner": "shawn",
    },
    {
        "number": "18664",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 29),
        "amount": 7171.56,
        "kind": "po",
        "po": "58931",
        "merch": 7171.56,
        "lines_match": [{"qty": 66, "amount": 7171.56}],
        "pdf": "tpi-18664.pdf",
        "received": "2026-10-02T16:13:24Z",
        "owner": "shawn",
    },
    {
        "number": "18676",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 30),
        "amount": 4129.08,
        "kind": "po",
        "po": "58966",
        "merch": 4129.08,
        "lines_match": [{"qty": 38, "amount": 4129.08}],
        "pdf": "tpi-18676.pdf",
        "received": "2026-10-02T16:32:39Z",
        "owner": "shawn",
    },
    {
        "number": "18677",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 30),
        "amount": 10757.34,
        "kind": "po",
        "po": "59043",
        "merch": 10757.34,
        "lines_match": [{"qty": 99, "amount": 10757.34}],
        "pdf": "tpi-18677.pdf",
        "received": "2026-10-02T16:36:46Z",
        "owner": "shawn",
    },
    {
        "number": "18678",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 30),
        "amount": 108.66,
        "kind": "po",
        "po": "59043",
        "merch": 108.66,
        "lines_match": [{"qty": 1, "amount": 108.66}],
        "pdf": "tpi-18678.pdf",
        "received": "2026-10-02T16:37:58Z",
        "owner": "shawn",
    },
    {
        "number": "18679",
        "vendor": "Telecom Products",
        "vendor_id": 183,
        "day": date(2026, 9, 30),
        "amount": 325.98,
        "kind": "po",
        "po": "59061",
        "merch": 325.98,
        "lines_match": [{"qty": 3, "amount": 325.98}],
        "pdf": "tpi-18679.pdf",
        "received": "2026-10-02T16:38:57Z",
        "owner": "shawn",
    },
    {
        "number": "1474645",
        "vendor": "RMP Industrial Supply",
        "vendor_id": 322,
        "day": date(2026, 10, 2),
        "amount": 188.48,
        "kind": "misc",
        "lines": [
            {"item_id": 31, "description": "Tap Magic cutting fluid", "qty": 1, "unit_price": 72.92},
            {"item_id": 53, "description": "Spiral flute pipe taps", "qty": 1, "unit_price": 115.56},
        ],
        "pdf": "rmp-1474645.pdf",
        "received": "2026-10-02T23:01:51Z",
        "po": "",
        "owner": "treyce",
    },
]


def cents(value: Any) -> float | None:
    amount = money(value)
    return None if amount is None else round(float(amount), 2)


def page_text(path: Path) -> str:
    document = pymupdf.open(path)
    return "\n".join(page.get_text("text") for page in document)


def write_pages(src: Path, dest: Path, indexes: list[int]) -> None:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for index in indexes:
        writer.add_page(reader.pages[index])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)


def prepare_pdfs() -> None:
    SRC.mkdir(parents=True, exist_ok=True)
    base = Path("/tmp/ap-run-1008")
    copies = {
        "luxor-367745.pdf": base / "20261001T160929Z-1.pdf",
        "legacy-104078.pdf": base / "20261001T200259Z-1.pdf",
        "legacy-104079.pdf": base / "20261001T201717Z-1.pdf",
        "rmp-1474542.pdf": base / "20261001T230200Z-1.pdf",
        "rmp-1474543.pdf": base / "20261001T230200Z-2.pdf",
        "gas-0040484376.pdf": base / "20261002T044007Z-1.pdf",
        "jp-125624.pdf": base / "20261002T135250Z-1.pdf",
        "crosslink-28396.pdf": base / "20261002T144605Z-1.pdf",
        "legacy-104080.pdf": base / "20261002T154730Z-1.pdf",
        "hudson-2610003592.pdf": base / "20261002T160151Z-1.pdf",
        "tpi-18663.pdf": base / "20261002T161216Z-1.pdf",
        "tpi-18664.pdf": base / "20261002T161324Z-1.pdf",
        "tpi-18676.pdf": base / "20261002T163239Z-1.pdf",
        "tpi-18677.pdf": base / "20261002T163646Z-1.pdf",
        "tpi-18678.pdf": base / "20261002T163758Z-1.pdf",
        "tpi-18679.pdf": base / "20261002T163857Z-1.pdf",
        "rmp-1474645.pdf": base / "20261002T230151Z-1.pdf",
    }
    numbers = {
        "luxor-367745.pdf": "367745",
        "legacy-104078.pdf": "PS-INV104078",
        "legacy-104079.pdf": "PS-INV104079",
        "rmp-1474542.pdf": "1474542",
        "rmp-1474543.pdf": "1474543",
        "gas-0040484376.pdf": "0040484376",
        "jp-125624.pdf": "125624",
        "crosslink-28396.pdf": "28396",
        "legacy-104080.pdf": "PS-INV104080",
        "hudson-2610003592.pdf": "2610003592",
        "tpi-18663.pdf": "18663",
        "tpi-18664.pdf": "18664",
        "tpi-18676.pdf": "18676",
        "tpi-18677.pdf": "18677",
        "tpi-18678.pdf": "18678",
        "tpi-18679.pdf": "18679",
        "rmp-1474645.pdf": "1474645",
    }
    gas = base / "20261002T043910Z-1.pdf"
    write_pages(gas, SRC / "gas-0040483159.pdf", [0, 1])
    write_pages(gas, SRC / "gas-0040484366.pdf", [2])
    write_pages(gas, SRC / "gas-0040482925.pdf", [3])
    for name, src in copies.items():
        reader = PdfReader(str(src))
        keep = []
        for index in range(len(reader.pages)):
            writer = PdfWriter()
            writer.add_page(reader.pages[index])
            scratch = SRC / f"_page-{index}.pdf"
            with scratch.open("wb") as handle:
                writer.write(handle)
            if numbers[name] in page_text(scratch):
                keep.append(index)
        if not keep:
            raise SystemExit(f"{name} has no page showing {numbers[name]}")
        write_pages(src, SRC / name, keep)
    expect = {
        "gas-0040483159.pdf": "0040483159",
        "gas-0040484366.pdf": "0040484366",
        "gas-0040482925.pdf": "0040482925",
        **numbers,
    }
    banned = set(expect.values())
    for name, number in expect.items():
        text = page_text(SRC / name)
        if number not in text:
            raise SystemExit(f"{name} does not show invoice {number}")
        for other in banned:
            if other != number and other in text:
                raise SystemExit(f"{name} also shows invoice {other}")


def uom_text(record: dict[str, Any]) -> str:
    values = record.get("values") or {}
    purchase = values.get("Purchase_UOM") or values.get("Inventory_UOM") or {}
    if isinstance(purchase, dict):
        return str(purchase.get("text") or "")
    return str(purchase or "")


def qty_same(left: float, right: float) -> bool:
    return abs(left - right) < 0.02


def qty_matches(invoice_qty: float, receipt_qty: float, invoice_uom: str, receipt_uom: str) -> bool:
    if qty_same(invoice_qty, receipt_qty):
        return True
    inv = invoice_uom.upper()
    rec = receipt_uom.upper()
    if "IN" in inv and rec.startswith("FT") and qty_same(invoice_qty, receipt_qty * 12):
        return True
    if inv.startswith("FT") and "IN" in rec and qty_same(invoice_qty * 12, receipt_qty):
        return True
    return False


def load_open_receipts(client: KimcoClient, po_numbers: set[str]) -> dict[str, list[dict[str, Any]]]:
    rows = client.list_items("purchase_lines", fields="Purchase_Order_Number,Purchase_Line_Number")
    found: dict[str, list[dict[str, Any]]] = {po: [] for po in po_numbers}
    line_ids: list[tuple[str, int]] = []
    for row in rows:
        values = row.get("values") or {}
        text = json.dumps(values)
        for po in po_numbers:
            if re.search(rf"PO{po}\b", text):
                line_ids.append((po, int(row["id"])))
                break
    for po, line_id in line_ids:
        record = client.get_item("purchase_lines", line_id)
        po_lookup = (record.get("values") or {}).get("Purchase_Order_Number") or {}
        po_id = lookup_id(po_lookup)
        for receipt_id in child_receipt_ids(record):
            full = client.get_item("receipts", receipt_id)
            fact = receipt_fact(full)
            fact.pop("fields", None)
            fact["uom"] = uom_text(full)
            fact["po"] = po
            fact["po_id"] = po_id
            found[po].append(fact)
    return found


def receipt_qty_in_invoice_units(receipt_qty: float, invoice_uom: str, receipt_uom: str) -> float | None:
    inv = invoice_uom.upper()
    rec = receipt_uom.upper()
    if "IN" in inv and rec.startswith("FT"):
        return receipt_qty * 12
    if inv.startswith("FT") and "IN" in rec:
        return receipt_qty / 12
    same_each = inv.startswith("EA") and rec.startswith("EA")
    same_in = "IN" in inv and "IN" in rec and not rec.startswith("FT")
    same_ft = inv.startswith("FT") and rec.startswith("FT")
    if same_each or same_in or same_ft or (not inv and not rec):
        return receipt_qty
    if inv.startswith("EA") and rec.startswith("EA"):
        return receipt_qty
    if "EA" in inv and "EA" in rec:
        return receipt_qty
    if inv[:2] == rec[:2]:
        return receipt_qty
    return None


def eligible_receipts(open_rows: list[dict[str, Any]], used: set[int], token: str, uom: str) -> list[dict[str, Any]]:
    rows = []
    for row in open_rows:
        if int(row["id"]) in used:
            continue
        if token and token not in str(row.get("part") or "") and token not in str(row.get("description") or ""):
            continue
        if receipt_qty_in_invoice_units(float(row.get("qty") or 0), uom, str(row.get("uom") or "")) is None:
            continue
        rows.append(row)
    return rows


def match_lines(job: dict[str, Any], receipts: list[dict[str, Any]]) -> tuple[list[int], float, str]:
    """Return receipt ids, PPV (invoice merch minus receipt extended), and a hold reason."""
    open_rows = [row for row in receipts if row.get("open")]
    used: set[int] = set()
    chosen: list[dict[str, Any]] = []
    for line in job.get("lines_match") or []:
        qty = float(line["qty"])
        token = str(line.get("token") or "")
        uom = str(line.get("uom") or "EA")
        pool = eligible_receipts(open_rows, used, token, uom)
        hits = [
            row
            for row in pool
            if qty_matches(qty, float(row.get("qty") or 0), uom, str(row.get("uom") or ""))
        ]
        picked: list[dict[str, Any]] = []
        if hits:
            hits.sort(key=lambda row: abs(float(row.get("extended") or 0) - float(line["amount"])))
            if len(hits) > 1 and abs(float(hits[0].get("extended") or 0) - float(line["amount"])) > 0.009:
                return [], 0.0, f"More than one open receipt matches quantity {qty}. Nothing was selected."
            picked = [hits[0]]
        else:
            options: list[tuple[dict[str, Any], ...]] = []
            if 2 <= len(pool) <= 8:
                for size in (2, 3, 4):
                    if size > len(pool):
                        break
                    for combo in combinations(pool, size):
                        total_qty = 0.0
                        ok = True
                        for row in combo:
                            converted = receipt_qty_in_invoice_units(
                                float(row.get("qty") or 0), uom, str(row.get("uom") or "")
                            )
                            if converted is None:
                                ok = False
                                break
                            total_qty += converted
                        if ok and qty_same(total_qty, qty):
                            options.append(combo)
                    if options:
                        break
            if not options:
                detail = ", ".join(
                    f"{row['id']} qty {row.get('qty')} {row.get('uom')} ${row.get('extended')} {row.get('part')}"
                    for row in open_rows[:8]
                )
                return [], 0.0, f"No open receipt matches quantity {qty} {uom}. Open receipts: {detail or 'none'}."
            options.sort(
                key=lambda combo: abs(sum(float(row.get("extended") or 0) for row in combo) - float(line["amount"]))
            )
            best_gap = abs(sum(float(row.get("extended") or 0) for row in options[0]) - float(line["amount"]))
            tied = [
                combo
                for combo in options
                if abs(abs(sum(float(row.get("extended") or 0) for row in combo) - float(line["amount"])) - best_gap) < 0.02
            ]
            if len(tied) > 1:
                return [], 0.0, f"More than one set of open receipts adds up to quantity {qty}. Nothing was selected."
            picked = list(options[0])
        chosen.extend(picked)
        used.update(int(row["id"]) for row in picked)
    receipt_ext = round(sum(float(row.get("extended") or 0) for row in chosen), 2)
    merch = round(float(job["merch"]), 2)
    gap = round(merch - receipt_ext, 2)
    if abs(gap) >= PPV_LIMIT:
        return [], gap, (
            f"The price gap is ${abs(gap):,.2f}, which is $75 or more. "
            "Receipts were not selected."
        )
    return [int(row["id"]) for row in chosen], gap, ""


def sample_for(client: KimcoClient, vendor_id: int, cache: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if vendor_id in cache:
        return cache[vendor_id]
    for invoice_id in range(10540, 9800, -1):
        try:
            record = client.get_item("ap_invoices", invoice_id)
        except KimcoError:
            continue
        values = record.get("values") or {}
        if lookup_id(values.get("Vendor")) != vendor_id:
            continue
        if lookup_id(values.get("Remit_To_Address")) is None or lookup_id(values.get("Terms_Code")) is None:
            continue
        cache[vendor_id] = {
            "terms_id": lookup_id(values.get("Terms_Code")),
            "terms": lookup_text(values.get("Terms_Code")),
            "remit_id": lookup_id(values.get("Remit_To_Address")),
            "invoice_id": invoice_id,
        }
        return cache[vendor_id]
    raise SystemExit(f"No remit and terms sample for vendor {vendor_id}")


def create_header(client: KimcoClient, job: dict[str, Any], batch_id: int, sample: dict[str, Any]) -> int:
    day = job["day"]
    terms = "Due Upon Receipt" if job.get("due_on_receipt") else str(sample["terms"] or "")
    due = day if job.get("due_on_receipt") else due_date_from_terms(day, terms)
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(job["vendor_id"])},
        "Invoice_Number": job["number"],
        "Invoice_Type": 3 if job.get("po_id") else 4,
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": float(job["amount"]),
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": int(sample["terms_id"])},
        "Currency": {"id": 3},
        "Remit_To_Address": {"id": int(sample["remit_id"])},
        "Transaction_Date": kimco_datetime(day),
        "Comments": comments_for("live"),
    }
    if job.get("po_id"):
        payload["Purchase_Order"] = {"id": int(job["po_id"])}
    created, _body, status, error = client.create("ap_invoices", payload)
    if created is None:
        raise SystemExit(f"Header create for {job['number']} failed HTTP {status}: {error}")
    created = int(created)
    if created <= MIN_NEW_ID:
        raise SystemExit(f"Create for {job['number']} returned existing id {created}. Not editing it.")
    record = client.get_item("ap_invoices", created)
    values = record.get("values") or {}
    if str(values.get("Invoice_Number") or "") != job["number"]:
        raise SystemExit(f"Created id {created} is not invoice {job['number']}")
    if values.get("Posted") not in (None, "", False):
        raise SystemExit(f"Created bill {created} is posted")
    if str(values.get("Comments") or "") != "API Agent":
        raise SystemExit(f"Created bill {created} is not stamped API Agent")
    if int(lookup_id(values.get("Vendor")) or 0) != int(job["vendor_id"]):
        raise SystemExit(f"Created bill {created} vendor is not {job['vendor_id']}")
    remember_created(job["number"], created, int(job["vendor_id"]))
    LOGGER.info("Created %s as %s", job["number"], created)
    return created


def add_misc(client: KimcoClient, invoice_id: int, job: dict[str, Any]) -> str:
    grouped: dict[int, list[dict[str, Any]]] = {}
    gl: dict[int, int | None] = {}
    for line in job["lines"]:
        grouped.setdefault(int(line["item_id"]), []).append(line)
        if line.get("gl_id"):
            gl[int(line["item_id"])] = int(line["gl_id"])
    for item_id, lines in grouped.items():
        payload = misc_add_item_payload(
            lines,
            invoice_id=invoice_id,
            vendor_id=int(job["vendor_id"]),
            misc_item={"id": item_id},
            gl_account={"id": gl[item_id]} if gl.get(item_id) else None,
        )
        status = put(client, invoice_id, payload)
        if status >= 400:
            return f"http-{status}"
    return "added"


def add_charges(client: KimcoClient, invoice_id: int, job: dict[str, Any], ppv: float) -> str:
    if job.get("freight"):
        status = client.try_post_fees(invoice_id, [{"amount": float(job["freight"]), "freight_external": True}])
        if status != "posted":
            return f"freight-{status}"
    if job.get("fee"):
        status = client.try_post_fees(invoice_id, [{"amount": float(job["fee"])}])
        if status != "posted":
            return f"fee-{status}"
    if abs(ppv) >= 0.005:
        status = client.try_post_ppv(invoice_id, ppv)
        if status != "posted":
            return f"ppv-{status}"
    if job.get("tax"):
        status = client.try_post_sales_tax(
            invoice_id,
            float(job["tax"]),
            taxable_amount=float(job.get("taxable") or 0),
            rate_percent=job.get("tax_rate"),
        )
        if status != "posted":
            return f"tax-{status}"
    return "posted"


def attach(client: KimcoClient, invoice_id: int, path: Path) -> str:
    content = path.read_bytes()
    try:
        return client.try_official_attach(
            invoice_id,
            name=path.name,
            content_type="application/pdf",
            size=len(content),
            content=content,
        )
    except KimcoError:
        return "blocked"


def move_batch(client: KimcoClient, invoice_id: int, batch_id: int) -> None:
    live = totals(client.get_item("ap_invoices", invoice_id))
    if int(live.get("batch_id") or 0) == int(batch_id):
        return
    put(
        client,
        invoice_id,
        {"id": invoice_id, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": int(batch_id)}}},
    )
    after = totals(client.get_item("ap_invoices", invoice_id))
    if int(after.get("batch_id") or 0) != int(batch_id):
        raise SystemExit(f"Bill {invoice_id} did not move to batch {batch_id}")


def note_html(text: str, owner: str) -> str:
    body = text.strip()
    if not body.startswith("AP Clerk:"):
        body = "AP Clerk: " + body
    if owner == "none":
        if "data-mention-id" in body or "@" in body.split("AP Clerk:", 1)[-1][:40]:
            raise SystemExit("This note must not tag an owner")
        return f"<p>{body}</p>"
    if owner == "shawn":
        return ap_clerk_edit_note(body, action="shawn")
    return ap_clerk_edit_note(body, action="treyce")


def write_note(client: KimcoClient, invoice_id: int, html: str) -> int | None:
    before = totals(client.get_item("ap_invoices", invoice_id))
    if any(str(note.get("text") or "").startswith("AP Clerk:") for note in before["notes"]):
        return int(next(note["id"] for note in before["notes"] if str(note.get("text") or "").startswith("AP Clerk:")))
    status = put(client, invoice_id, added_comment_payload(invoice_id, html))
    if status >= 400:
        return None
    after = totals(client.get_item("ap_invoices", invoice_id))
    notes = [note for note in after["notes"] if str(note.get("text") or "").startswith("AP Clerk:")]
    return int(notes[-1]["id"]) if notes else None


def confirm_attachment(client: KimcoClient, invoice_id: int, number: str) -> tuple[int, str]:
    QC.mkdir(parents=True, exist_ok=True)
    items = client.list_attachments(invoice_id)
    if len(items) != 1:
        return 0, f"attachment-count-{len(items)}"
    content = attachment_bytes(client, invoice_id, items[0])
    if not content:
        return 0, "attachment-download-failed"
    document = pymupdf.open(stream=content, filetype="pdf")
    for page in document:
        if number not in page.get_text("text"):
            return document.page_count, "page-missing-invoice-number"
    stem = str(invoice_id)
    for index, page in enumerate(document, start=1):
        image = QC / f"{stem}-p{index}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7), alpha=False).save(str(image))
    return document.page_count, "ok"


def remember_created(number: str, bill_id: int, vendor_id: int) -> None:
    path = OUT / "created-ids.json"
    rows = json.loads(path.read_text()) if path.exists() else []
    if any(int(row.get("bill_id") or 0) == int(bill_id) for row in rows):
        return
    rows.append({"invoice": number, "bill_id": int(bill_id), "vendor_id": int(vendor_id)})
    path.write_text(json.dumps(rows, indent=2))


def scan_live_bills(client: KimcoClient) -> dict[tuple[int, str], int]:
    """Map (vendor id, invoice number) to bill id for headers created after the index."""
    found: dict[tuple[int, str], int] = {}
    misses = 0
    for invoice_id in range(MIN_NEW_ID + 1, MIN_NEW_ID + 60):
        try:
            record = client.get_item("ap_invoices", invoice_id)
        except KimcoError:
            misses += 1
            if misses >= 5:
                break
            continue
        misses = 0
        values = record.get("values") or {}
        vendor_id = lookup_id(values.get("Vendor"))
        number = str(values.get("Invoice_Number") or "")
        if vendor_id and number:
            found[(int(vendor_id), number)] = int(invoice_id)
            remember_created(number, int(invoice_id), int(vendor_id))
            LOGGER.info("Existing new bill %s vendor %s invoice %s", invoice_id, vendor_id, number)
    return found


def existing_id(
    client: KimcoClient,
    number: str,
    vendor_id: int,
    live: dict[tuple[int, str], int] | None = None,
) -> int | None:
    if live and (int(vendor_id), number) in live:
        return int(live[(int(vendor_id), number)])
    index = json.loads((OUT / "kimco-index.json").read_text())
    hits = [row for row in index["invoices"] if row["key"] == invoice_number_key(number)]
    for path in (OUT / "progress.json", OUT / "created-ids.json"):
        if path.exists():
            for row in json.loads(path.read_text()):
                if row.get("invoice") == number and row.get("bill_id"):
                    hits.append({"id": row["bill_id"]})
    for hit in hits:
        record = client.get_item("ap_invoices", int(hit["id"]))
        values = record.get("values") or {}
        if lookup_id(values.get("Vendor")) == vendor_id and str(values.get("Invoice_Number") or "") == number:
            return int(hit["id"])
    return None


def enter_one(
    client: KimcoClient,
    job: dict[str, Any],
    batch_id: int,
    samples: dict[int, dict[str, Any]],
    receipts: dict[str, list[dict[str, Any]]],
    live: dict[tuple[int, str], int] | None = None,
) -> dict[str, Any]:
    number = job["number"]
    path = SRC / job["pdf"]
    row: dict[str, Any] = {
        "vendor": job["vendor"],
        "invoice": number,
        "invoice_date": job["day"].isoformat(),
        "received": job["received"],
        "printed_total": job["amount"],
        "po": job.get("po") or "",
        "bill_id": "",
        "result": "HOLD",
        "owner": "",
        "reason": "",
        "batch": "",
        "note_id": "",
        "note": "",
        "email_moved": "n",
        "pages": 0,
        "receipts": "",
    }
    already = existing_id(client, number, int(job["vendor_id"]), live)
    if already and already <= MIN_NEW_ID:
        row["bill_id"] = already
        row["result"] = "ALREADY"
        row["reason"] = f"Already in KIMCO as bill {already}. Not edited."
        return row
    sample = sample_for(client, int(job["vendor_id"]), samples)
    ppv = 0.0
    hold_reason = ""
    receipt_ids: list[int] = []
    if job["kind"] == "po":
        if not job.get("po_id"):
            for fact in receipts.get(job["po"], []):
                if fact.get("po_id"):
                    job["po_id"] = int(fact["po_id"])
                    break
        if not job.get("po_id"):
            hold_reason = f"Purchase order {job['po']} was not found. Nothing was selected."
        else:
            receipt_ids, ppv, hold_reason = match_lines(job, receipts.get(job["po"], []))
    created = already or create_header(client, job, batch_id, sample)
    if live is not None:
        live[(int(job["vendor_id"]), number)] = int(created)
    row["bill_id"] = created
    before = totals(client.get_item("ap_invoices", created))
    has_lines = bool(before["lines"])
    receipts_already = [int(line["receipt_id"]) for line in before["lines"] if line.get("receipt_id")]
    target_amount = round(float(job["amount"]), 2)
    if job.get("hold_without_lines"):
        hold_reason = str(job["hold_without_lines"])
        line_status = "not-added"
        charge_status = ""
        select_status = ""
    elif job["kind"] == "misc":
        line_status = "already" if has_lines else add_misc(client, created, job)
        if line_status in {"added", "already"} and cents(before["covered"]) == target_amount and has_lines:
            charge_status = "already"
        else:
            charge_status = add_charges(client, created, job, 0.0) if line_status in {"added", "already"} else ""
        select_status = ""
    elif receipt_ids and not hold_reason:
        if receipts_already:
            if set(receipts_already) != set(receipt_ids):
                hold_reason = (
                    f"Bill {created} already has receipts {receipts_already}. "
                    f"Expected {receipt_ids}. Nothing else was selected."
                )
                select_status = "mismatch"
                charge_status = ""
            else:
                select_status = "selected"
                charge_status = "already" if cents(before["covered"]) == target_amount else add_charges(client, created, job, ppv)
        else:
            try:
                select_status = client.try_select_receipts(created, receipt_ids)
            except KimcoError:
                select_status = "blocked"
            charge_status = add_charges(client, created, job, ppv) if select_status == "selected" else ""
        line_status = ""
    else:
        select_status = "not-selected"
        charge_status = ""
        line_status = ""
    attach_status = "attached" if client.list_attachments(created) else attach(client, created, path)
    pages, attach_check = confirm_attachment(client, created, number)
    row["pages"] = pages
    live = totals(client.get_item("ap_invoices", created))
    covered_ok = cents(live["verification"]) == round(float(job["amount"]), 2) and live["covered"] == round(float(job["amount"]), 2)
    passed = (
        not hold_reason
        and covered_ok
        and live["posted"] in (None, "", False)
        and attach_status == "attached"
        and attach_check == "ok"
        and (job["kind"] == "misc" or select_status == "selected")
    )
    if not passed and not hold_reason:
        hold_reason = (
            f"Live lines, charges, and tax are ${live['covered']:,.2f} against ${job['amount']:,.2f}. "
            f"Lines {line_status or select_status}, charges {charge_status or 'n/a'}, PDF {attach_status} {attach_check}."
        )
    target = batch_id if passed else TRANSFER
    move_batch(client, created, target)
    total = f"${job['amount']:,.2f}"
    if job.get("note"):
        text = str(job["note"])
        if not text.startswith("AP Clerk:"):
            text = "AP Clerk: " + text
        result = "PASS" if passed else "HOLD"
        owner = job["owner"] if passed else ("shawn" if job["kind"] == "po" else (job["owner"] if job["owner"] != "none" else "treyce"))
    elif passed:
        if job["owner"] == "none":
            text = (
                f"AP Clerk: {job['vendor']} invoice {number} is entered and is not posted. "
                f"The PDF total is {total}. There is no KIMCO purchase order, so it is miscellaneous shop supplies."
            )
        elif abs(ppv) >= 0.005:
            text = (
                f"AP Clerk: {job['vendor']} invoice {number} is entered and is not posted. "
                f"The PDF total is {total}. Purchase order {job.get('po')} receipts were selected. "
                f"A purchase price variance of ${ppv:,.2f} covers the difference."
            )
            job["owner"] = "shawn"
        else:
            extra = ""
            if job.get("freight"):
                extra = f" Freight of ${job['freight']:,.2f} is an additional charge."
            if job.get("fee"):
                label = str(job.get("fee_name") or "fee")
                extra += f" The {label} of ${job['fee']:,.2f} is an additional charge."
            po_bit = f" Purchase order {job['po']} receipts were selected." if job.get("po") else " There is no purchase order."
            text = (
                f"AP Clerk: {job['vendor']} invoice {number} is entered and is not posted. "
                f"The PDF total is {total}.{po_bit}{extra}"
            )
        result = "PASS"
        owner = job["owner"]
    else:
        owner = "shawn" if job["kind"] == "po" else (job["owner"] if job["owner"] != "none" else "treyce")
        text = (
            f"AP Clerk: {job['vendor']} invoice {number} is on hold and is not posted. "
            f"The PDF total is {total}. {hold_reason} The bill is in Transfer AP."
        )
        result = "HOLD"
    html = note_html(text, owner)
    note_id = write_note(client, created, html)
    final = totals(client.get_item("ap_invoices", created))
    row.update(
        {
            "result": result,
            "owner": "" if owner == "none" and result == "PASS" else owner,
            "reason": "" if result == "PASS" else hold_reason,
            "batch": final.get("batch") or "",
            "batch_id": final.get("batch_id"),
            "note_id": note_id or "",
            "note": text,
            "header_total": final.get("verification"),
            "covered": final.get("covered"),
            "receipts": ",".join(str(item) for item in receipt_ids) if result == "PASS" else "",
            "ppv": ppv if result == "PASS" else "",
            "items": "; ".join(
                f"{line.get('item')} {line.get('description')} qty {line.get('qty')} @ {line.get('unit_price')}"
                for line in final["lines"]
            ),
            "gl": "; ".join(str(line.get("gl") or "") for line in final["lines"]),
        }
    )
    LOGGER.info("%s %s bill %s covered %s", result, number, created, final.get("covered"))
    return row


def save(rows: list[dict[str, Any]]) -> None:
    (OUT / "progress.json").write_text(json.dumps(rows, indent=2, default=str))


def main() -> None:
    prepare_pdfs()
    client = login()
    install_401_guard(client)
    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    if batch["name"] != BATCH_NAME:
        raise SystemExit(f"Refusing batch {batch}")
    record = client.get_item("ap_batches", int(batch["id"]))
    status = (record.get("values") or {}).get("Status")
    if status not in (0, "0", None, ""):
        raise SystemExit(f"Batch {batch['id']} status is {status}. Not using a posted batch.")
    LOGGER.info("Batch %s id %s", BATCH_NAME, batch["id"])
    QC.mkdir(parents=True, exist_ok=True)
    live = scan_live_bills(client)
    po_numbers = {job["po"] for job in JOBS if job.get("po")}
    receipts = load_open_receipts(client, po_numbers)
    for po, facts in receipts.items():
        LOGGER.info("PO %s receipts %s", po, [(row["id"], row.get("qty"), row.get("uom"), row.get("extended"), row.get("open")) for row in facts])
    samples = {
        71: {"terms_id": 4, "terms": "F-N60-Net 60", "remit_id": 192},
        112: {"terms_id": 1, "terms": "Net 30-Net 30", "remit_id": 736},
        322: {"terms_id": 1, "terms": "Net 30-Net 30", "remit_id": 536},
        292: {"terms_id": 1, "terms": "Net 30-Net 30", "remit_id": 482},
        88: {"terms_id": 62, "terms": "Due Upon Receipt", "remit_id": 209},
    }
    rows: list[dict[str, Any]] = []
    created = 0
    for job in JOBS:
        if created >= CAP:
            break
        row = enter_one(client, job, int(batch["id"]), samples, receipts, live)
        rows.append(row)
        save(rows)
        if row["result"] in {"PASS", "HOLD"} and row.get("bill_id") and int(row["bill_id"]) > MIN_NEW_ID:
            created += 1
    payload = {
        "batch_name": BATCH_NAME,
        "batch_id": batch["id"],
        "sign_ins": "see log",
        "entered": created,
        "rows": rows,
    }
    (OUT / "result.json").write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps({"batch": batch, "entered": created, "results": [(r["invoice"], r["result"], r["bill_id"]) for r in rows]}))


if __name__ == "__main__":
    main()
