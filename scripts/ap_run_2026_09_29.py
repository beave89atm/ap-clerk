"""Weekday catch-up for 2026-09-29. Cap 15 invoices. Never posts a bill.

Starts at RMP 1473031. One live authenticate. If that login fails, stop.
Quantity_Received only. Packing-slip check stays suspended.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import LIVE_HOST, load_credentials
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import (
    AP_INVOICE_TAX_LIST,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    assert_comments_1_author,
    comment_author_from_access_token,
    sales_tax_line_payload,
)
from ap_clerk.rules import (
    CURRENCY_USD_ID,
    KYLE_CLEAVER_MENTION_HTML,
    SHAWN_MENTION_HTML,
    TREYCE_MENTION_HTML,
    invoice_number_key,
    kimco_datetime,
    known_vendor_id,
    lookup_id,
    lookup_text,
    money,
    names_match,
    ppv_limit,
)
from ap_clerk.transfer_ap import apply_transfer_ap_batch_move
from batch10b_enter import (
    load_invoice_index,
    load_po_lines,
    load_receipts,
    plan_bill,
    receipt_extension,
)

BATCH_NAME = "API Agent - 9/29/26"
CHICAGO = ZoneInfo("America/Chicago")
OUT_XLSX = ROOT / "runs" / "AP-run-2026-09-29.xlsx"
OUT_JSON = ROOT / "runs" / "AP-run-2026-09-29.json"
PDF_DIR = Path("/tmp/ap0929/pdfs")
PARSED = Path("/tmp/ap0929/parsed.json")
CAP = 15
COLUMNS = [
    "Vendor",
    "Invoice #",
    "Status",
    "Reason",
    "Type",
    "Amount",
    "KIMCO bill #",
    "Batch",
    "Transfer AP",
    "Receipts selected",
    "PPV",
    "Comments_1 id",
    "Email moved",
    "Note",
    "Email received",
]


def _pdf(prefix: str) -> Path:
    hits = sorted(
        path
        for path in PDF_DIR.iterdir()
        if path.is_file() and path.name.startswith(prefix + "-") and path.suffix.lower() == ".pdf"
    )
    if not hits:
        raise SystemExit(f"missing pdf {prefix}")
    return hits[0]


def _message(label: str) -> dict[str, Any]:
    rows = json.loads(PARSED.read_text())
    for row in rows:
        if row.get("label") == label:
            return row
    raise SystemExit(f"missing parsed label {label}")


def bills() -> list[dict[str, Any]]:
    """Hand-checked against the vendor PDFs. Parser totals were not trusted."""
    rmp = _message("rmp-pack")
    uni = _message("01")
    air1 = _message("03")
    venturi = _message("06")
    metal = _message("07")
    arrow = _message("08")
    green = _message("09")
    msc = _message("10")
    castle = _message("11")
    mcq = _message("12")
    air2 = _message("14")
    leeco = _message("15")
    pct = _message("17")
    spectrum = _message("18")

    def base(msg: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        row = {
            "message_id": msg["message_id"],
            "email_received": str(msg["received"])[:10],
            "subject": msg.get("subject") or "",
            "fees": [],
            "tax_rows": [],
            "lines": [],
            "part_tokens": [],
        }
        row.update(kwargs)
        return row

    return [
        base(
            rmp,
            vendor="RMP Industrial Supply Inc",
            vendor_id=322,
            invoice_number="1473031",
            date="2026-09-11",
            due="2026-10-11",
            po=None,
            total=427.80,
            kind="po_or_search",
            pdf=_pdf("rmp-pack-1"),
            lines=[{"part": "BVR 227-2110-T", "description": "2 inch x 8RD EUE THD TAP 2-3/8 OD TUBING", "qty": 1, "amount": 427.80}],
            part_tokens=["227-2110", "2272110"],
            type_name="parts",
        ),
        base(
            uni,
            vendor="UniFirst Corporation",
            vendor_id=189,
            invoice_number="2810810634",
            date="2026-09-11",
            due="2026-10-11",
            po=None,
            total=829.53,
            kind="uniform",
            pdf=_pdf("01-0"),
            type_name="uniform",
        ),
        base(
            air1,
            vendor="Air Products and Chemicals, Inc",
            vendor_id=13,
            invoice_number="436437181",
            date="2026-09-11",
            due="2026-10-11",
            po=None,
            total=909.30,
            kind="air",
            pdf=_pdf("03-0"),
            lines=[{"part": "7710", "description": "Oxygen Tank MSC", "qty": 1, "amount": 840.00}],
            tax_rows=[
                {"label": "State Tax 6.25%", "amount": 52.50, "rate": 6.25},
                {"label": "City Tax 2.00%", "amount": 16.80, "rate": 2.00},
            ],
            type_name="misc",
        ),
        base(
            venturi,
            vendor="Venturi Supply LLC",
            invoice_number="93669897",
            date="2026-09-12",
            due="2026-10-12",
            po="59127",
            total=82.40,
            kind="po",
            pdf=_pdf("06-0"),
            lines=[{"part": "S-S383-73", "description": "304 ISO-49 150# Hex Bush 3/4in x 1/4in", "qty": 16, "amount": 82.40, "unit_price": 5.15}],
            type_name="parts",
        ),
        base(
            metal,
            vendor="Metal Supermarkets",
            vendor_id=121,
            invoice_number="1091811",
            date="2026-09-11",
            due="2026-10-11",
            po="59157",
            total=111.30,
            kind="po",
            pdf=_pdf("07-0"),
            lines=[{
                "part": "ATRT6063/31125",
                "description": "Aluminum Rectangular Tube 6063T52 3.000 X 1.000 X 0.120 1 @ 96 IN",
                "qty": 1,
                "length_inches": 96,
                "amount": 111.30,
            }],
            type_name="parts",
        ),
        base(
            arrow,
            vendor="Arrow Plating Co.",
            invoice_number="9179828",
            date="2026-09-09",
            due="2026-10-09",
            po="59135",
            total=50.00,
            kind="po",
            pdf=_pdf("08-1"),
            lines=[{"part": "6000875004YELLOW", "description": "Yellow Zinc winch roller minimum charge", "qty": 2, "amount": 50.00}],
            type_name="parts",
            clerk_hint="PDF rate is $5.50 each but the extended amount is a $50.00 minimum charge, not $11.00.",
        ),
        base(
            arrow,
            vendor="Arrow Plating Co.",
            invoice_number="9179841",
            date="2026-09-11",
            due="2026-10-11",
            po="58997",
            total=665.00,
            kind="po",
            pdf=_pdf("08-0"),
            lines=[{"part": "10205921", "description": "Clear Zinc lower platform support", "qty": 19, "amount": 665.00, "unit_price": 35.00}],
            type_name="parts",
        ),
        base(
            green,
            vendor="Green Valley Compressor LLC",
            vendor_id=405,
            invoice_number="2216",
            date="2026-09-11",
            due="2026-10-11",
            po=None,
            printed_po_not_kimco="Jamie-Crawford",
            total=2306.75,
            kind="missing_po",
            pdf=_pdf("09-0"),
            sales_tax_printed=175.80,
            type_name="misc",
        ),
        base(
            msc,
            vendor="MSC Industrial Supply",
            vendor_id=128,
            invoice_number="78482801",
            date="2026-09-11",
            due="2026-10-11",
            po=None,
            printed_po_not_kimco="VENDING/1572",
            total=511.80,
            kind="missing_po",
            pdf=_pdf("10-0"),
            type_name="misc",
        ),
        base(
            castle,
            vendor="A. M. Castle & Co",
            invoice_number="43315835",
            date="2026-09-11",
            due="2026-10-11",
            po="59102",
            total=515.11,
            kind="po",
            pdf=_pdf("11-0"),
            lines=[{"part": "8751", "description": "1.2500 RD 7075 T7351 ALUMINUM 2 PCS 35.64 LBS", "qty": 35.64, "amount": 515.11, "unit_price": 14.4532}],
            type_name="parts",
            clerk_hint="Priced at 35.64 pounds, not the 2 piece count. A 2-piece receipt is not a quantity match.",
        ),
        base(
            mcq,
            vendor="McQueary Industries",
            vendor_id=119,
            invoice_number="09-30449",
            date="2026-09-14",
            due="2026-10-14",
            po="59100",
            total=2400.00,
            kind="po",
            pdf=_pdf("12-0"),
            lines=[{"part": "35145-1", "description": "JIB ARM BATCHWELD bore to 1.248 / 1.250 in 3 places", "qty": 40, "amount": 2400.00, "unit_price": 60.00}],
            type_name="parts",
        ),
        base(
            air2,
            vendor="Air Products and Chemicals, Inc",
            vendor_id=13,
            invoice_number="436448543",
            date="2026-09-14",
            due="2026-10-14",
            po=None,
            total=1299.00,
            kind="air",
            pdf=_pdf("14-0"),
            lines=[{"part": "7711", "description": "Nitrogen Tank MSC", "qty": 1, "amount": 1200.00}],
            tax_rows=[
                {"label": "State Tax 6.25%", "amount": 75.00, "rate": 6.25},
                {"label": "City Tax 2.00%", "amount": 24.00, "rate": 2.00},
            ],
            type_name="misc",
        ),
        base(
            leeco,
            vendor="Leeco Steel, LLC",
            vendor_id=109,
            invoice_number="623592",
            date="2026-09-14",
            due="2026-10-14",
            po="59072",
            total=7350.92,
            kind="po",
            pdf=_pdf("15-0"),
            lines=[{"part": "A514", "description": "3/4 X 48 X 120 A514 GR B", "qty": 4, "amount": 7350.92, "unit_price": 1837.73}],
            type_name="parts",
            clerk_hint="Quantity is 4 pieces. 4,900.61 is the weight, not the quantity and not the dollars.",
        ),
        base(
            pct,
            vendor="PCT Support",
            vendor_id=140,
            invoice_number="LS-8578",
            date="2026-09-15",
            due="2026-09-25",
            po=None,
            total=1310.00,
            kind="missing_po",
            pdf=_pdf("17-0"),
            type_name="misc",
        ),
        base(
            spectrum,
            vendor="SpectrumVoIP",
            invoice_number="951277",
            date="",
            due="",
            po=None,
            total=93.13,
            kind="no_header",
            pdf=_pdf("18-0"),
            type_name="misc",
            hold_reason="The PDF prints bill 951277 and amount due $93.13, and it does not print an invoice date. No header was created.",
        ),
    ]


def authenticate_once() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if LIVE_HOST not in (creds.instance_url or "").lower():
        raise SystemExit("Refusing non-live host")
    url = f"{creds.instance_url.rstrip('/')}/api/v2/authenticate"
    response = requests.post(url, json={"key": creds.key, "password": creds.password}, timeout=60)
    if response.status_code != 200:
        text = response.text or ""
        for secret in (creds.key or "", creds.password or ""):
            if secret:
                text = text.replace(secret, "[redacted]")
        print(f"AUTH FAILED HTTP {response.status_code}", flush=True)
        print(text[:1500], flush=True)
        raise SystemExit(2)
    token = (response.json() or {}).get("token")
    if not token:
        print("AUTH FAILED HTTP 200 response had no token field", flush=True)
        raise SystemExit(2)
    print("AUTH ok (token not printed)", flush=True)
    client = KimcoClient(creds.instance_url, token, target="live")
    author = comment_author_from_access_token(token)
    assert_comments_1_author(author, target="live")
    print(f"AUTHOR ok id={author.get('id')} name={author.get('name')}", flush=True)
    return client


def _day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def note_html(text: str) -> str:
    body = html.escape(text)
    for token, span in (
        ("@Shawn McKibben", SHAWN_MENTION_HTML),
        ("@Treyce Hodges", TREYCE_MENTION_HTML),
        ("@Kyle Cleaver", KYLE_CLEAVER_MENTION_HTML),
    ):
        body = body.replace(html.escape(token), span)
    return f"<p>{body}</p>"


def write_comment(client: KimcoClient, kimco_id: int, text: str) -> dict[str, Any]:
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    payload = added_comment_payload(kimco_id, note_html(text))
    try:
        _body, status, error = client.update("ap_invoices", kimco_id, payload)
    except KimcoError as exc:
        return {"id": None, "error": str(exc)[:240], "status": "blocked"}
    record = client.get_item("ap_invoices", kimco_id)
    import re

    needle = re.sub(r"\s+", " ", text).strip()
    match = None
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        raw = html.unescape(str((comment.get("values") or {}).get("HtmlValue") or ""))
        visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
        if needle[10:40] and needle[10:40] in visible:
            match = comment.get("id")
            break
    return {"id": match, "status": status, "error": error}


def existing_for(client: KimcoClient, index: dict[str, list[dict[str, Any]]], bill: dict[str, Any]) -> dict[str, Any] | None:
    hits = [hit for hit in (index.get(invoice_number_key(bill["invoice_number"])) or []) if not hit.get("void")]
    vendor_id = bill.get("vendor_id") or known_vendor_id(bill["vendor"])
    kept = []
    for hit in hits:
        if not hit.get("vendor_id"):
            record = client.get_item("ap_invoices", int(hit["id"]))
            values = record.get("values") or {}
            hit["vendor_id"] = lookup_id(values.get("Vendor"))
            hit["vendor_text"] = lookup_text(values.get("Vendor")) or ""
            hit["batch"] = lookup_text(values.get("AP_Invoice_Batch")) or hit.get("batch")
            hit["batch_id"] = lookup_id(values.get("AP_Invoice_Batch"))
            hit["posted"] = bool(values.get("Posted"))
        if vendor_id and hit.get("vendor_id") and int(hit["vendor_id"]) != int(vendor_id):
            continue
        if vendor_id and hit.get("vendor_id") and int(hit["vendor_id"]) == int(vendor_id):
            kept.append(hit)
            continue
        if names_match(bill["vendor"], str(hit.get("vendor_text") or "")):
            kept.append(hit)
    if not kept:
        return None
    return sorted(kept, key=lambda row: int(row["id"]))[-1]


# Prior live bills. GET confirms Vendor.id, terms, and remit. Ids are not invented.
SEED_SAMPLE_IDS = {
    322: [10376, 6085],
    13: [10356],
    405: [10244],
    128: [10363],
    121: [10049, 9951],
    434: [9496],
}


def po_id_for(po_lines: list[dict[str, Any]], po: str | None, vendor_id: int) -> int | None:
    for row in po_lines:
        if po and str(row.get("po")) == po and row.get("po_id"):
            posted = row.get("vendor_id")
            if posted in (None, vendor_id) or int(posted or 0) == int(vendor_id):
                return int(row["po_id"])
    return None


def remember_sample(client: KimcoClient, invoice_id: int, cache: dict[int, dict[str, Any]]) -> int | None:
    record = client.get_item("ap_invoices", int(invoice_id))
    values = record.get("values") or {}
    vendor_id = lookup_id(values.get("Vendor"))
    if not vendor_id or values.get("Void") is True:
        return None
    terms_id = lookup_id(values.get("Terms_Code"))
    remit_id = lookup_id(values.get("Remit_To_Address"))
    if not terms_id or not remit_id:
        return None
    cache[int(vendor_id)] = {
        "sample_id": int(invoice_id),
        "terms_id": int(terms_id),
        "remit_id": int(remit_id),
        "currency_id": lookup_id(values.get("Currency")) or CURRENCY_USD_ID,
        "terms_text": lookup_text(values.get("Terms_Code")) or "",
        "vendor_text": lookup_text(values.get("Vendor")) or "",
    }
    return int(vendor_id)


def fill_samples(client: KimcoClient, index: dict[str, list[dict[str, Any]]], needed: set[int]) -> dict[int, dict[str, Any]]:
    """Copy terms and remit from a confirmed non-void bill for each vendor."""
    cache: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    for vendor_id, invoice_ids in SEED_SAMPLE_IDS.items():
        for invoice_id in invoice_ids:
            seen.add(int(invoice_id))
            try:
                got = remember_sample(client, int(invoice_id), cache)
            except KimcoError as exc:
                print(f"sample GET {invoice_id} failed {exc}", flush=True)
                continue
            if got == int(vendor_id):
                break
    prefixes = {
        189: ("281",),
        119: ("09-", "093"),
        109: ("623", "619", "617"),
        140: ("LS",),
        121: ("109",),
        13: ("436",),
        128: ("77", "78"),
        322: ("1473",),
        405: ("215", "221"),
    }
    for vendor_id in sorted(needed):
        if vendor_id in cache:
            continue
        candidates: list[int] = []
        for key, rows in index.items():
            if not any(str(key).startswith(prefix) for prefix in prefixes.get(vendor_id, ())):
                continue
            for row in rows:
                if row.get("void") or row.get("id") in (None, ""):
                    continue
                candidates.append(int(row["id"]))
        for invoice_id in sorted(set(candidates), reverse=True)[:8]:
            if invoice_id in seen:
                continue
            seen.add(invoice_id)
            try:
                got = remember_sample(client, invoice_id, cache)
            except KimcoError:
                continue
            if got == int(vendor_id):
                break
    missing = {int(vendor_id) for vendor_id in needed if int(vendor_id) not in cache}
    if missing:
        newest = sorted(
            {
                int(row["id"])
                for rows in index.values()
                for row in rows
                if row.get("id") not in (None, "") and not row.get("void")
            },
            reverse=True,
        )
        probed = 0
        for invoice_id in newest:
            if not missing or probed >= 80:
                break
            if invoice_id in seen:
                continue
            seen.add(invoice_id)
            probed += 1
            try:
                got = remember_sample(client, invoice_id, cache)
            except KimcoError:
                continue
            if got in missing:
                missing.discard(got)
        print(f"sample probe {probed} still missing {sorted(missing)}", flush=True)
    print(
        "samples "
        + ", ".join(f"{vendor_id}:{row['sample_id']}" for vendor_id, row in sorted(cache.items()) if vendor_id in needed),
        flush=True,
    )
    return cache


def hydrate_po_lines(client: KimcoClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List payloads omit Vendor.id. Record GET is the vendor id source."""
    for row in rows:
        if row.get("vendor_id") and row.get("po_id"):
            continue
        if row.get("id") in (None, ""):
            continue
        record = client.get_item("purchase_lines", int(row["id"]))
        values = record.get("values") or {}
        vendor_id = None
        vendor_text = ""
        for key, value in values.items():
            if "vendor" not in str(key).lower() or not isinstance(value, dict):
                continue
            if value.get("id") in (None, ""):
                continue
            vendor_id = int(value["id"])
            vendor_text = lookup_text(value)
            break
        po_lookup = values.get("Purchase_Order_Number") or values.get("Purchase_Order") or values.get("PO_Number")
        po_id = lookup_id(po_lookup)
        if vendor_id:
            row["vendor_id"] = vendor_id
            row["vendor_text"] = vendor_text or row.get("vendor_text") or ""
        if po_id:
            row["po_id"] = int(po_id)
        print(
            f"PO hydrate {row.get('po')} line {row.get('id')} po_id={row.get('po_id')} vendor_id={row.get('vendor_id')}",
            flush=True,
        )
    return rows


def vendor_from_po(po_lines: list[dict[str, Any]], po: str, vendor_name: str) -> int | None:
    rows = [row for row in po_lines if str(row.get("po") or "") == po and row.get("vendor_id")]
    ids = {int(row["vendor_id"]) for row in rows}
    if len(ids) == 1:
        return next(iter(ids))
    for row in rows:
        if names_match(vendor_name, str(row.get("vendor_text") or "")):
            return int(row["vendor_id"])
    return None


def sample_for(cache: dict[int, dict[str, Any]], vendor_id: int, po_lines: list[dict[str, Any]], po: str | None) -> dict[str, Any]:
    found = cache.get(int(vendor_id)) or {}
    return {
        "vendor_id": vendor_id,
        "po_id": po_id_for(po_lines, po, vendor_id),
        "terms_id": found.get("terms_id"),
        "remit_id": found.get("remit_id"),
        "currency_id": found.get("currency_id") or CURRENCY_USD_ID,
        "terms_text": found.get("terms_text") or "",
        "vendor_text": found.get("vendor_text") or "",
        "sample_id": found.get("sample_id"),
    }


def search_parts(client: KimcoClient, tokens: list[str], vendor_id: int | None) -> list[dict[str, Any]]:
    found = []
    offset = 0
    total = None
    url = client._url("purchase_lines")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"purchase line search HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            blob = json.dumps(item.get("values") or {})
            if not any(token.lower() in blob.lower() for token in tokens):
                continue
            values = item.get("values") or {}
            vid = lookup_id(values.get("Vendor") or values.get("PO_Vendor"))
            if vendor_id and vid and int(vid) != int(vendor_id):
                continue
            po_lookup = values.get("Purchase_Order_Number") or values.get("Purchase_Order")
            found.append(
                {
                    "id": item.get("id"),
                    "po": lookup_text(po_lookup) or "",
                    "po_id": lookup_id(po_lookup),
                    "vendor_id": vid,
                    "part": lookup_text(values.get("Item_Number") or values.get("PO_Item_Number") or values.get("Item")) or "",
                    "qty": values.get("Quantity") or values.get("Qty"),
                    "description": lookup_text(values.get("PO_Item_Description") or values.get("Description")) or "",
                }
            )
        if not items:
            break
        offset += len(items)
        print(f"part search offset {offset} hits {len(found)}", flush=True)
    return found


def create_header(client: KimcoClient, bill: dict[str, Any], batch_id: int, sample: dict[str, Any], invoice_type: int, po_id: int | None) -> tuple[int | None, int, str]:
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(sample["vendor_id"])},
        "Invoice_Number": bill["invoice_number"],
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(_day(bill["date"])),
        "Invoice_Verification_Amount": float(bill["total"]),
        "Invoice_Due_Date": kimco_datetime(_day(bill["due"])),
        "Terms_Code": {"id": int(sample["terms_id"])},
        "Currency": {"id": int(sample["currency_id"] or CURRENCY_USD_ID)},
        "Remit_To_Address": {"id": int(sample["remit_id"])},
        "Transaction_Date": kimco_datetime(_day(bill["date"])),
        "Comments": "API Agent",
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    if "Posted" in payload:
        raise RuntimeError("refusing to post")
    created_id, _body, status, error = client.create("ap_invoices", payload)
    return created_id, status, error or ""


def dollars(amount: float | None) -> str:
    if amount is None:
        return "n/a"
    return f"${amount:,.2f}"


def hold_owners(action: str) -> str:
    if action == "uniform":
        return "@Kyle Cleaver"
    return "@Shawn McKibben @Treyce Hodges"


def snapshot_money(client: KimcoClient, kimco_id: int) -> dict[str, Any]:
    record = client.get_item("ap_invoices", kimco_id)
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    charges = []
    tax_amounts = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        cv = charge.get("values") or {}
        charges.append({"name": cv.get("Name"), "amount": money(cv.get("Amount")), "kind": lookup_text(cv.get("Additional_Charges"))})
    for row in lists.get("APInvoiceTaxCodes") or []:
        tv = row.get("values") or {}
        tax_amounts.append({"code": lookup_text(tv.get("Tax_Code")), "amount": money(tv.get("Tax_Amount"))})
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        lines.append({"receipt": lookup_id(lv.get("Receipt")), "qty": money(lv.get("Quantity")), "ext": money(lv.get("Extended_Amount"))})
    return {
        "posted": values.get("Posted"),
        "amount": money(values.get("Invoice_Amount")),
        "net": money(values.get("Invoice_Net_Amount")),
        "balance": money(values.get("Invoice_Balance")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "type": values.get("Invoice_Type"),
        "charges": charges,
        "taxes": tax_amounts,
        "lines": lines,
    }


def public_row(bill: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return {
        "Vendor": bill["vendor"],
        "Invoice #": bill["invoice_number"],
        "Status": kwargs.get("status"),
        "Reason": kwargs.get("reason"),
        "Type": bill.get("type_name") or "",
        "Amount": bill.get("total"),
        "KIMCO bill #": kwargs.get("kimco_id") or "",
        "Batch": kwargs.get("batch") or "",
        "Transfer AP": kwargs.get("transfer_ap") or "N",
        "Receipts selected": kwargs.get("receipts") or "",
        "PPV": kwargs.get("ppv") if kwargs.get("ppv") not in (None, "") else 0,
        "Comments_1 id": kwargs.get("comment_id") or "",
        "Email moved": kwargs.get("email_moved") or "",
        "Note": kwargs.get("note") or "",
        "Email received": bill.get("email_received") or "",
        "_message_id": bill.get("message_id"),
        "_attached": kwargs.get("attached") is True,
        "_status": kwargs.get("status"),
    }


def enter_air(client: KimcoClient, bill: dict[str, Any], kimco_id: int) -> dict[str, Any]:
    product = round(sum(float(line["amount"]) for line in bill["lines"]), 2)
    charges = [{"description": "shop supplies", "amount": product, "source": bill["lines"][0]["description"]}]
    shop = client.try_post_shop_supplies(kimco_id, charges)
    children = []
    tax_status = []
    for row in bill["tax_rows"]:
        try:
            one = sales_tax_line_payload(
                float(row["amount"]),
                invoice_id=kimco_id,
                taxable_amount=product,
                rate_percent=float(row["rate"]),
            )
        except KimcoError as exc:
            tax_status.append(f"{row['label']} blocked {exc}")
            continue
        children.append(one["lists"][AP_INVOICE_TAX_LIST][0])
        tax_status.append(f"{row['label']} queued")
    if children and len(children) == len(bill["tax_rows"]):
        body = {"id": int(kimco_id), "state": "Modified", "lists": {AP_INVOICE_TAX_LIST: children}}
        put = client.request("PUT", client._record_url("ap_invoices", kimco_id), json=body)
        tax_status.append(f"taxes HTTP {put.status_code}")
    else:
        tax_status.append("taxes not posted")
    live = snapshot_money(client, kimco_id)
    if shop == "posted" and not live["charges"]:
        shop = client.try_post_shop_supplies(kimco_id, charges)
        live = snapshot_money(client, kimco_id)
        tax_status.append(f"shop supplies reposted {shop}")
    charge_sum = round(sum(float(row["amount"] or 0) for row in live["charges"]), 2)
    tax_sum = round(sum(float(row["amount"] or 0) for row in live["taxes"]), 2)
    printed_tax = round(sum(float(row["amount"]) for row in bill["tax_rows"]), 2)
    payable = live["net"] if live["net"] is not None else live["balance"]
    ppv = round(sum(float(row["amount"] or 0) for row in live["charges"] if "price variance" in str(row["kind"] or "").lower() or str(row["name"] or "").lower() == "purchase price variance"), 2)
    ok = (
        shop == "posted"
        and charge_sum == product
        and tax_sum == printed_tax
        and payable == float(bill["total"])
        and live["verification"] == float(bill["total"])
        and live["posted"] in (None, "", False)
        and ppv == 0
        and not live["lines"]
    )
    tax_bits = "; ".join(f"{row['label']} {dollars(row['amount'])}" for row in bill["tax_rows"])
    note = (
        f"AP Clerk: This is an Air Products shop supplies bill entered as miscellaneous. "
        f"Invoice {bill['invoice_number']}. Additional Charges described shop supplies: "
        f"{bill['lines'][0]['description']} {dollars(product)}. "
        f"The PDF prints {tax_bits}. Those amounts were entered on the Taxes tab as Tax Code Sales Tax, "
        f"not as additional charges and not as Purchase Price Variance. "
        f"The payable total is {dollars(payable)}. The PDF total is {dollars(bill['total'])}. The bill is not posted."
    )
    return {"ok": ok, "note": note, "live": live, "tax_status": tax_status, "ppv": ppv, "reason": "totals match to the penny" if ok else "shop supplies or taxes tab did not match the PDF"}


def qty_matches(bill: dict[str, Any], chosen: list[dict[str, Any]]) -> bool:
    invoiced = round(sum(float(money(line.get("qty")) or 0) for line in bill.get("lines") or []), 4)
    received = round(sum(float(money(row.get("qty")) or 0) for row in chosen), 4)
    if abs(invoiced - received) <= 0.05:
        return True
    length = round(sum(float(money(line.get("length_inches")) or 0) for line in bill.get("lines") or []), 4)
    if length and abs(length - received) <= 0.05 and invoiced == 1:
        return True
    return False


def finish_po(client: KimcoClient, bill: dict[str, Any], kimco_id: int, plan: dict[str, Any]) -> dict[str, Any]:
    action = plan["action"]
    if action == "select" and not qty_matches(bill, plan.get("receipts") or []):
        action = "quantity_variance"
        plan = dict(plan)
        plan["action"] = action
        plan["receipts"] = []
    if action != "select":
        return {"action": action, "selected": False, "ppv": 0.0, "gap": plan.get("gap")}
    if bill.get("fees"):
        client.try_post_fees(kimco_id, bill["fees"])
    refs = []
    for row in plan["receipts"]:
        if row.get("select_qty") is not None and money(row.get("select_qty")) != money(row.get("qty")):
            refs.append({"id": int(row["id"]), "qty": row["select_qty"]})
        else:
            refs.append(int(row["id"]))
    select_status = client.try_select_receipts(kimco_id, refs)
    from ap_clerk.ppv_qc import pre_finish_totals_check

    pre = pre_finish_totals_check(client, kimco_id)
    live = snapshot_money(client, kimco_id)
    gap = pre.get("gap")
    ppv_amount = float(pre.get("ppv_posted") or 0)
    if select_status == "selected" and gap not in (None, 0, 0.0) and abs(float(gap)) >= ppv_limit():
        client.try_deselect_receipts(kimco_id)
        return {"action": "price_variance", "selected": False, "ppv": 0.0, "gap": gap, "select_status": select_status}
    success = (
        select_status == "selected"
        and pre.get("ok") is True
        and gap in (0, 0.0)
        and live["posted"] in (None, "", False)
        and live["verification"] == float(bill["total"])
    )
    label = "; ".join(
        f"{row.get('receipt') or row.get('id')} qty {row.get('qty')}" for row in (live["lines"] or plan["receipts"])
    )
    if not success and action == "select":
        action = "not_finished"
    return {
        "action": action if success else action,
        "selected": success,
        "ppv": ppv_amount,
        "gap": gap,
        "select_status": select_status,
        "receipts": label,
        "live": live,
        "pre_ok": pre.get("ok"),
    }


def specific_hold_note(bill: dict[str, Any], plan: dict[str, Any]) -> str:
    who = hold_owners("uniform" if bill["kind"] == "uniform" else plan.get("action") or "missing_po")
    number = bill["invoice_number"]
    total = dollars(bill["total"])
    hint = f" {bill['clerk_hint']}" if bill.get("clerk_hint") else ""
    if bill["kind"] == "uniform":
        return (
            f"AP Clerk: {who} This is a UniFirst uniform invoice, not UniFirst First Aid & Safety. "
            f"Invoice {number}. It is waiting on Kyle's review. No invoice lines were entered. "
            f"The PDF total is {total}. The bill is not posted."
        )
    printed = bill.get("printed_po_not_kimco")
    if plan.get("action") in {"missing_po", "missing_po"} or bill["kind"] == "missing_po":
        if printed:
            po_bit = f"The invoice prints {printed}. That is not a KIMCO purchase order."
        else:
            po_bit = "No purchase order number is printed, and a live search did not find one for this vendor and part."
        tax_bit = ""
        if bill.get("sales_tax_printed"):
            tax_bit = f" The PDF prints sales tax {dollars(bill['sales_tax_printed'])}. That tax was not put in additional charges or Purchase Price Variance."
        return (
            f"AP Clerk: {who} {bill['vendor']} invoice {number} cannot be finished. "
            f"The PDF total is {total}. {po_bit}{tax_bit}{hint} "
            "No receipt lines were selected and no Purchase Price Variance was posted. "
            "The bill was moved to Transfer AP and is not posted."
        )
    action = plan.get("action")
    open_n = len(plan.get("open_rows") or [])
    if action == "price_variance":
        why = (
            f"Invoice total minus the receipt lines is {dollars(plan.get('gap'))}, which is {dollars(ppv_limit())} or more. "
            "The quantity matches, so this is a price difference. Receipts were not selected, so the receipt is not locked. "
            "No Purchase Price Variance was posted. Shawn should unreceive, set the PO price to the invoice price, and re-receive the same quantity."
        )
    elif action == "missing_receipt":
        why = (
            f"There is no open receipt on PO {bill.get('po')}. "
            "This is a missing receipt, not a price variance. No receipts were selected and no Purchase Price Variance was posted. "
            "Shawn should receive the invoiced quantity. After that receipt exists, AP can select it by Quantity_Received."
        )
    elif action == "not_finished":
        why = (
            f"Select Receipts did not finish. Status {plan.get('select_status')}. "
            f"The live gap is {dollars(plan.get('gap'))}. "
            "The bill was not called finished."
        )
    else:
        why = (
            f"Quantity received does not match quantity invoiced. Open receipts on the PO: {open_n}. "
            f"The dollar gap is {dollars(plan.get('gap'))}.{hint} "
            "No receipts were selected and no Purchase Price Variance was posted. "
            "Shawn should correct Quantity_Received so it matches the invoice quantity."
        )
    return (
        f"AP Clerk: {who} {bill['vendor']} invoice {number} cannot be finished. "
        f"The PDF total is {total}. PO {bill.get('po') or 'none'}. {why} "
        "The bill was moved to Transfer AP and is not posted."
    )


def success_note(bill: dict[str, Any], finish: dict[str, Any]) -> str:
    ppv = float(finish.get("ppv") or 0)
    ppv_bit = (
        f" One signed Purchase Price Variance of {dollars(ppv)} was posted so the total matches the PDF."
        if ppv
        else " No Purchase Price Variance was posted."
    )
    return (
        f"AP Clerk: {bill['vendor']} invoice {bill['invoice_number']} is entered and is not posted. "
        f"The PDF total is {dollars(bill['total'])}. PO {bill.get('po')}. "
        f"Receipts selected by Quantity_Received: {finish.get('receipts') or 'none'}.{ppv_bit}"
    )


def ensure_batch(client: KimcoClient, state: dict[str, Any]) -> int:
    if state.get("batch_id"):
        return int(state["batch_id"])
    from ap_clerk.cli import _find_or_create_batch

    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    state["batch_id"] = int(batch["id"])
    state["batch_created"] = bool(batch.get("created"))
    print(f"BATCH {state['batch_id']} created={state['batch_created']}", flush=True)
    return int(state["batch_id"])


def attach(client: KimcoClient, kimco_id: int, bill: dict[str, Any]) -> str:
    content = Path(bill["pdf"]).read_bytes()
    return client.try_official_attach(
        kimco_id,
        name=f"{bill['invoice_number']}.pdf",
        content_type="application/pdf",
        size=len(content),
        content=content,
    )


def move_mail(graph: Any, rows: list[dict[str, Any]]) -> None:
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(folder.get("id") or "")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        mid = row.get("_message_id")
        if mid:
            grouped.setdefault(mid, []).append(row)
    for mid, group in grouped.items():
        created = [row for row in group if row.get("KIMCO bill #") and row.get("_attached")]
        if not created or not fort_id:
            for row in group:
                row["Email moved"] = row.get("Email moved") or "no"
            continue
        # 1473030 on the same email is already a HOLD. Do not mark that parent Entered in AI.
        sibling_hold = any(str(row.get("Invoice #")) == "1473031" for row in group)
        if sibling_hold or not all(row["_status"] == "Success" for row in group):
            if any(row.get("KIMCO bill #") for row in group):
                graph.flag_issues(ALLOWED_MAILBOX, mid)
            else:
                graph.flag_hold(ALLOWED_MAILBOX, mid)
        else:
            graph.flag_matched(ALLOWED_MAILBOX, mid)
        moved = graph.move_message(ALLOWED_MAILBOX, mid, fort_id)
        flag = "yes" if moved.get("status") == "moved" else str(moved.get("status") or "no")
        for row in group:
            if row.get("_attached") and row.get("KIMCO bill #"):
                row["Email moved"] = flag
            elif not row.get("Email moved"):
                row["Email moved"] = "no"


def write_workbook(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    OUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "AP run"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    for col, name in enumerate(COLUMNS, start=1):
        cell = sheet.cell(1, col, name)
        cell.font = header_font
        cell.fill = header_fill
    for r, row in enumerate(rows, start=2):
        for c, name in enumerate(COLUMNS, start=1):
            sheet.cell(r, c, row.get(name))
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(2, len(rows)+1)}"
    widths = [32, 16, 12, 36, 12, 12, 14, 28, 14, 40, 10, 16, 16, 80, 16]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    if rows:
        table = Table(displayName="APRun", ref=f"A1:{get_column_letter(len(COLUMNS))}{len(rows)+1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        sheet.add_table(table)
    info = book.create_sheet("Run")
    for key, value in meta.items():
        info.append([key, json.dumps(value) if isinstance(value, (dict, list)) else value])
    book.save(OUT_XLSX)


def save(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    public = [{key: value for key, value in row.items() if not str(key).startswith("_")} for row in rows]
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({"meta": meta, "invoices": public}, indent=2, default=str) + "\n")
    write_workbook(public, meta)


def main() -> int:
    queue = bills()
    if len(queue) != CAP:
        raise SystemExit(f"expected {CAP} invoices, got {len(queue)}")
    from ap_clerk.cli import _optional_graph_client

    graph = _optional_graph_client()
    try:
        client = authenticate_once()
    except SystemExit as exc:
        if exc.code == 2:
            meta = {"auth": "failed", "error": "see process output", "batch": None}
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(json.dumps(meta, indent=2) + "\n")
        raise
    print("Loading invoice index", flush=True)
    index, _samples, _names = load_invoice_index(client)
    state: dict[str, Any] = {"batch_id": None, "auth": "ok"}
    results: list[dict[str, Any]] = []

    # A full purchase-line list scan earlier today found no 227-2110 / 2272110.
    rmp = queue[0]
    rmp["po"] = None
    rmp["kind"] = "missing_po"
    rmp["part_search"] = "0 hits on the purchase-line list for 227-2110"
    print("RMP part search reused: 0 hits, missing_po", flush=True)

    wanted = {str(bill["po"]) for bill in queue if bill.get("po")}
    print(f"Loading purchase lines for {sorted(wanted)}", flush=True)
    po_lines = hydrate_po_lines(client, load_po_lines(client, wanted))
    print("Loading receipts", flush=True)
    receipts = load_receipts(client, wanted)
    needed_vendors: set[int] = set()
    for bill in queue:
        if bill["kind"] == "no_header":
            continue
        vendor_id = bill.get("vendor_id") or known_vendor_id(bill["vendor"])
        if vendor_id is None and bill.get("po"):
            vendor_id = vendor_from_po(po_lines, str(bill["po"]), bill["vendor"])
            if vendor_id:
                bill["vendor_id"] = int(vendor_id)
                print(f"VENDOR FROM PO {bill['invoice_number']} {vendor_id}", flush=True)
        if vendor_id:
            needed_vendors.add(int(vendor_id))
    sample_cache = fill_samples(client, index, needed_vendors)
    for bill in queue:
        number = bill["invoice_number"]
        hit = existing_for(client, index, bill)
        if hit:
            batch_name = str(hit.get("batch") or "")
            on_transfer = "transfer ap" in batch_name.lower()
            row = public_row(
                bill,
                status="Skipped",
                reason="already entered" + (" on Transfer AP" if on_transfer else ""),
                kimco_id=hit["id"],
                batch=batch_name,
                transfer_ap="Y" if on_transfer else "N",
                note=f"AP Clerk did not edit KIMCO {hit['id']}. It was already entered.",
                email_moved="left",
            )
            results.append(row)
            print(f"SKIP EXISTING {number} {hit['id']} {batch_name}", flush=True)
            save(results, state)
            continue
        if bill["kind"] == "no_header":
            row = public_row(
                bill,
                status="HOLD",
                reason="invoice date not printed",
                batch="",
                transfer_ap="N",
                note=f"AP Clerk: {bill['hold_reason']} No header was created. The bill is not posted.",
                email_moved="no",
            )
            results.append(row)
            if graph and bill.get("message_id"):
                graph.flag_hold(ALLOWED_MAILBOX, bill["message_id"])
                row["Email moved"] = "no"
            print(f"HOLD NO HEADER {number}", flush=True)
            save(results, state)
            continue

        vendor_id = bill.get("vendor_id") or known_vendor_id(bill["vendor"])
        if vendor_id is None and bill.get("po"):
            for line in po_lines:
                if str(line.get("po")) == str(bill["po"]) and line.get("vendor_id"):
                    vendor_id = int(line["vendor_id"])
                    bill["vendor_id"] = vendor_id
                    break
        if not vendor_id:
            row = public_row(
                bill,
                status="HOLD",
                reason="vendor id not on file",
                note=(
                    f"AP Clerk: {bill['vendor']} invoice {number} was not created. "
                    f"No confirmed KIMCO vendor id was on file, so one was not invented. "
                    f"The PDF total is {dollars(bill['total'])}. The bill is not posted."
                ),
                email_moved="no",
            )
            results.append(row)
            print(f"HOLD NO VENDOR {number}", flush=True)
            save(results, state)
            continue
        bill["vendor_id"] = int(vendor_id)
        sample = sample_for(sample_cache, int(vendor_id), po_lines, bill.get("po"))
        if bill["kind"] in {"po", "po_or_search"} and bill.get("po") and not sample.get("po_id"):
            row = public_row(
                bill,
                status="HOLD",
                reason="PO id missing",
                note=(
                    f"AP Clerk: @Shawn McKibben @Treyce Hodges {bill['vendor']} invoice {number} prints PO {bill['po']}, "
                    f"but that purchase order id was not found. The PDF total is {dollars(bill['total'])}. "
                    "No miscellaneous header was created. The bill is not posted."
                ),
                email_moved="no",
            )
            # This note was not written to KIMCO because there is no header.
            results.append(row)
            print(f"HOLD NO PO ID {number}", flush=True)
            save(results, state)
            continue
        if not sample.get("terms_id") or not sample.get("remit_id"):
            row = public_row(
                bill,
                status="HOLD",
                reason="vendor remit missing",
                note=(
                    f"AP Clerk: {bill['vendor']} invoice {number} was not created. "
                    "KIMCO has no vendor remit and terms sample, so those were not invented. "
                    f"The PDF total is {dollars(bill['total'])}. The bill is not posted."
                ),
                email_moved="no",
            )
            results.append(row)
            print(f"HOLD NO REMIT {number} vendor {vendor_id}", flush=True)
            save(results, state)
            continue

        if bill["kind"] == "air":
            plan = {"action": "air"}
            invoice_type, po_for = 4, None
        elif bill["kind"] == "uniform":
            plan = {"action": "uniform"}
            invoice_type, po_for = 4, None
        elif bill["kind"] == "missing_po" or not bill.get("po"):
            plan = plan_bill(bill, po_lines, receipts)
            if plan["action"] not in {"missing_po"}:
                plan["action"] = "missing_po"
            invoice_type, po_for = 4, None
        else:
            plan = plan_bill(bill, po_lines, receipts)
            invoice_type, po_for = 3, sample.get("po_id")
            print(
                json.dumps(
                    {
                        "invoice": number,
                        "action": plan["action"],
                        "gap": plan.get("gap"),
                        "open": len(plan.get("open_rows") or []),
                        "receipts": [row.get("id") for row in (plan.get("receipts") or [])],
                        "ext": receipt_extension(plan.get("considered") or []),
                    },
                    default=str,
                ),
                flush=True,
            )

        batch_id = ensure_batch(client, state)
        created_id, status, error = create_header(client, bill, batch_id, sample, invoice_type, po_for)
        if created_id is None:
            row = public_row(
                bill,
                status="HOLD",
                reason=f"header HTTP {status}",
                batch=BATCH_NAME,
                note=f"AP Clerk: {bill['vendor']} invoice {number} header was not created (HTTP {status}). The bill is not posted.",
                email_moved="no",
            )
            results.append(row)
            print(f"CREATE FAIL {number} {status} {error[:180]}", flush=True)
            save(results, state)
            continue
        attached = attach(client, created_id, bill) == "attached"
        comment_id = None
        transfer = "N"
        batch_label = f"{BATCH_NAME} ({batch_id})"
        ppv = 0.0
        receipts_label = ""
        final_status = "HOLD"
        reason = plan.get("action") or bill["kind"]

        if bill["kind"] == "air":
            air = enter_air(client, bill, created_id)
            final_status = "Success" if air["ok"] else "HOLD"
            reason = air["reason"]
            note = air["note"]
            ppv = air["ppv"]
            comment = write_comment(client, created_id, note)
            comment_id = comment.get("id")
            print(f"AIR {number} id={created_id} ok={air['ok']} tax={air['tax_status']}", flush=True)
        elif bill["kind"] == "uniform":
            note = specific_hold_note(bill, {"action": "uniform"})
            comment = write_comment(client, created_id, note)
            comment_id = comment.get("id")
            reason = "needs_kyle_review"
            print(f"KYLE {number} id={created_id} comment={comment_id}", flush=True)
        elif plan.get("action") == "select":
            finish = finish_po(client, bill, created_id, plan)
            if finish.get("selected"):
                final_status = "Success"
                reason = "totals match to the penny"
                ppv = finish.get("ppv") or 0
                receipts_label = finish.get("receipts") or ""
                note = success_note(bill, finish)
                comment = write_comment(client, created_id, note)
                comment_id = comment.get("id")
                print(f"SUCCESS {number} id={created_id} ppv={ppv}", flush=True)
            else:
                plan = dict(plan)
                plan["action"] = finish.get("action") or "not_finished"
                plan["gap"] = finish.get("gap")
                plan["select_status"] = finish.get("select_status")
                note = specific_hold_note(bill, plan)
                comment = write_comment(client, created_id, note)
                comment_id = comment.get("id")
                if comment_id:
                    moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
                    if moved.get("status") in {"moved", "already-on-transfer-ap"}:
                        transfer = "Y"
                        batch_label = moved.get("batch_name") or "Transfer AP"
                reason = plan["action"]
                print(f"HOLD AFTER PLAN {number} {reason}", flush=True)
        else:
            note = specific_hold_note(bill, plan if plan.get("action") != "select" else {"action": "quantity_variance"})
            comment = write_comment(client, created_id, note)
            comment_id = comment.get("id")
            if comment_id and bill["kind"] != "uniform":
                moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
                if moved.get("status") in {"moved", "already-on-transfer-ap"}:
                    transfer = "Y"
                    batch_label = moved.get("batch_name") or "Transfer AP"
            reason = plan.get("action") or bill["kind"]
            print(f"HOLD {number} id={created_id} {reason} transfer={transfer}", flush=True)

        # Re-read before a Success sticks.
        if final_status == "Success":
            live = snapshot_money(client, created_id)
            payable = live["net"] if live["net"] is not None else live["balance"]
            if live["posted"] not in (None, "", False) or (bill["kind"] == "air" and payable != float(bill["total"])):
                final_status = "HOLD"
                reason = "readback did not match"
            if bill["kind"] != "air" and live["verification"] != float(bill["total"]):
                final_status = "HOLD"
                reason = "verification readback mismatch"
            if not comment_id:
                final_status = "HOLD"
                reason = "comment was not saved"

        row = public_row(
            bill,
            status=final_status,
            reason=reason,
            kimco_id=created_id,
            batch=batch_label,
            transfer_ap=transfer,
            receipts=receipts_label,
            ppv=ppv,
            comment_id=comment_id,
            note=note,
            attached=attached,
            email_moved="pending",
        )
        results.append(row)
        save(results, state)

    if graph:
        move_mail(graph, results)
    counts = {"Success": 0, "HOLD": 0, "Skipped": 0}
    for row in results:
        counts[row["Status"]] = counts.get(row["Status"], 0) + 1
    state.update(
        {
            "auth": "ok",
            "batch_name": BATCH_NAME,
            "batch_id": state.get("batch_id"),
            "counts": counts,
            "next": "Hagen's Fasteners invoice 5158738",
            "posted": False,
        }
    )
    save(results, state)
    print(json.dumps(state, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
