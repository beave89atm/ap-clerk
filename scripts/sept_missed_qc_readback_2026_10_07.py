"""Read-only QC of live bills 10513-10537.

Downloads the attachment stored on each bill, renders every page, and writes
runs/sept-missed-entry-2026-10-07/qc/readback.csv. No KIMCO writes and no mail.
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.rules import lookup_text, money
from scripts.sept25_30_attachment_audit import attachment_bytes, attachment_name, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_archive_check_v2_2026_10_07 import refuse_writes

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-missed-qc")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)
logging.getLogger("pypdf").setLevel(logging.ERROR)

OUT = ROOT / "runs" / "sept-missed-entry-2026-10-07" / "qc"
BILLS = list(range(10513, 10538))
FABCORP = {
    "470223": ROOT / "runs" / "sept-missed-entry-2026-10-07" / "bill_470223.pdf",
    "470471": ROOT / "runs" / "sept-missed-entry-2026-10-07" / "bill_470471.pdf",
}
PO_WANTED = {"59060", "59216"}

COLUMNS = [
    "bill",
    "vendor",
    "invoice#",
    "header total",
    "sum of lines+charges",
    "gap",
    "items/GL used",
    "receipts selected",
    "batch",
    "note id + note text",
    "attachment page count",
]


def render_pdf(content: bytes, stem: str) -> list[Path]:
    import fitz

    document = fitz.open(stream=content, filetype="pdf")
    saved: list[Path] = []
    for index, page in enumerate(document, start=1):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        path = OUT / f"{stem}-p{index}.png"
        pixmap.save(str(path))
        saved.append(path)
    return saved


def render_file(path: Path, stem: str) -> list[Path]:
    return render_pdf(path.read_bytes(), stem)


def gl_text(values: dict[str, Any]) -> str:
    for key in ("Purchase_GL_Account", "Purchase_GL", "GL_Account", "Expense_GL"):
        text = lookup_text(values.get(key))
        if text:
            return text
    return ""


def item_text(values: dict[str, Any]) -> str:
    return lookup_text(values.get("MFG_Miscellaneous_Item") or values.get("Part_ID") or values.get("Item"))


def note_rows(record: dict[str, Any]) -> list[tuple[int, str]]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        if not isinstance(comment, dict):
            continue
        values = comment.get("values") or {}
        raw_html = str(values.get("HtmlValue") or "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(raw_html))).strip()
        rows.append((int(comment.get("id") or 0), plain))
    return rows


def line_charge_sum(record: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    lists = record.get("lists") or {}
    total = 0.0
    items: list[str] = []
    receipts: list[str] = []
    for line in lists.get("APInvoiceLine") or []:
        values = line.get("values") or {}
        extended = float(money(values.get("Extended_Amount")) or 0)
        total += extended
        item = item_text(values)
        gl = gl_text(values)
        desc = str(values.get("Misc_Description") or "").strip()
        piece = item or desc or "line"
        if desc and item and desc not in item:
            piece = f"{piece} ({desc})"
        piece = f"{piece} / {gl}" if gl else f"{piece} / GL blank"
        if piece not in items:
            items.append(piece)
        receipt = values.get("Receipt")
        if isinstance(receipt, dict) and receipt.get("id") not in (None, ""):
            token = str(receipt.get("id"))
            text = lookup_text(receipt)
            label = token if not text or text == token else f"{token} {text}"
            if label not in receipts:
                receipts.append(label)
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        values = charge.get("values") or {}
        total += float(money(values.get("Amount")) or 0)
        name = lookup_text(values.get("Additional_Charges")) or str(values.get("Name") or "charge")
        amount = money(values.get("Amount"))
        label = f"charge {name} {amount}"
        if label not in items:
            items.append(label)
    for tax in lists.get("APInvoiceTaxCodes") or []:
        values = tax.get("values") or {}
        amount = float(money(values.get("Tax_Amount")) or 0)
        if amount:
            total += amount
            items.append(f"tax {amount}")
    return round(total, 2), items, receipts


def header_total(values: dict[str, Any]) -> float | None:
    return money(values.get("Invoice_Verification_Amount"))


def batch_label(values: dict[str, Any]) -> str:
    batch = values.get("AP_Invoice_Batch")
    if not isinstance(batch, dict):
        return ""
    name = batch.get("text") or batch.get("name") or ""
    return f"{batch.get('id')} {name}".strip()


def po_hits(client) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    offset = 0
    total = None
    while total is None or offset < total:
        response = client.request(
            "GET",
            client._url("purchase_lines"),
            params={"pageSize": 2000, "offset": offset},
        )
        if response.status_code != 200:
            raise SystemExit(f"GET purchase_lines HTTP {response.status_code}")
        payload = response.json()
        chunk = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        LOGGER.info("purchase_lines offset=%s got=%s total=%s", offset, len(chunk), total)
        for item in chunk:
            values = item.get("values") or {}
            blob = json.dumps(values)
            found = [number for number in PO_WANTED if number in blob]
            if not found:
                continue
            hits.append(
                {
                    "id": item.get("id"),
                    "numbers": found,
                    "keys": sorted(values.keys()),
                    "vendor": lookup_text(values.get("PO_Vendor") or values.get("Vendor") or values.get("Purchase_Order_$_Vendor")),
                    "vendor_display": values.get("Vendor_$_Display_Name") or values.get("Purchase_Order_$_Vendor_$_Display_Name"),
                    "po": lookup_text(values.get("Purchase_Order_Number") or values.get("Purchase_Order") or values.get("Name")),
                    "display": lookup_text(values.get("Display_Name")),
                    "description": lookup_text(values.get("Description") or values.get("Item_Description")),
                }
            )
        if not chunk:
            break
        offset += len(chunk)
    return hits


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = login()
    install_401_guard(client)
    refuse_writes(client)
    quotes: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    for bill_id in BILLS:
        record = client.get_item("ap_invoices", bill_id)
        values = record.get("values") or {}
        invoice = str(values.get("Invoice_Number") or "")
        vendor = lookup_text(values.get("Vendor"))
        header = header_total(values)
        covered, items, receipts = line_charge_sum(record)
        gap = None if header is None else round(float(header) - covered, 2)
        notes = note_rows(record)
        note_text = " || ".join(f"{note_id} | {text}" for note_id, text in notes)
        items_list = client.list_attachments(bill_id)
        page_count = 0
        page_quotes = []
        for index, item in enumerate(items_list, start=1):
            content = attachment_bytes(client, bill_id, item)
            name = attachment_name(item) or f"attachment-{index}"
            if not content:
                raise SystemExit(f"Bill {bill_id} attachment {name} did not download")
            pages = render_pdf(content, str(bill_id))
            page_count += len(pages)
            texts = page_texts(content)
            for page_no, text in enumerate(texts, start=1):
                page_quotes.append({"page": page_no, "name": name, "text": text})
        quotes.append(
            {
                "bill": bill_id,
                "vendor": vendor,
                "invoice": invoice,
                "header": header,
                "covered": covered,
                "gap": gap,
                "batch": batch_label(values),
                "posted": values.get("Posted"),
                "comments": values.get("Comments"),
                "po": lookup_text(values.get("Purchase_Order")),
                "pages": page_quotes,
            }
        )
        rows.append(
            {
                "bill": str(bill_id),
                "vendor": vendor,
                "invoice#": invoice,
                "header total": "" if header is None else f"{header:.2f}",
                "sum of lines+charges": f"{covered:.2f}",
                "gap": "" if gap is None else f"{gap:.2f}",
                "items/GL used": "; ".join(items),
                "receipts selected": ", ".join(receipts) if receipts else "none",
                "batch": batch_label(values),
                "note id + note text": note_text,
                "attachment page count": str(page_count),
            }
        )
        LOGGER.info("Bill %s %s pages=%s gap=%s", bill_id, invoice, page_count, gap)

    fabcorp_pages = {}
    for number, path in FABCORP.items():
        if not path.is_file():
            raise SystemExit(f"Missing Fabcorp PDF {path}")
        saved = render_file(path, f"fabcorp-{number}")
        fabcorp_pages[number] = [str(item.relative_to(ROOT)) for item in saved]
        LOGGER.info("Fabcorp %s pages=%s", number, len(saved))

    hits = po_hits(client)
    (OUT / "po-59060-59216.json").write_text(json.dumps(hits, indent=2))
    (OUT / "page-quotes.json").write_text(json.dumps(quotes, indent=2))
    with (OUT / "readback.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    LOGGER.info("Wrote %s rows and %s PO hits", len(rows), len(hits))
    LOGGER.info("Fabcorp renders %s", fabcorp_pages)


if __name__ == "__main__":
    main()
