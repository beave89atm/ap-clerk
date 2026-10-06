"""Read-only attachment audit for bills 10478-10512 and PO 59081.

No KIMCO writes. No mail changes. One API Agent sign-in; a failed password
is not retried. Attachment delete/replace is probed with OPTIONS only.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import KimcoClient
from ap_clerk.pdf_invoice import (
    _GAS_ORDER_SUFFIX,
    _INV_EMJ,
    _INV_LABEL,
    _ONEAL_INV,
    gas_invoice_numbers,
)
from ap_clerk.rules import lookup_id, lookup_text, money
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("attachment-audit")

BILL_IDS = list(range(10478, 10513))
PO_NUMBER = "59081"
KIT_PART = "MLW49-22-4170"
AUDIT_CSV = ROOT / "runs" / "sept25-30-attachment-audit.csv"
PO_JSON = ROOT / "runs" / "po59081-check.json"
INVOICE_LABEL = re.compile(r"invoice\s*(?:number|no\.?|#)", flags=re.I)


def stamp_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value or "")


def mentions_po(item: dict[str, Any], po_id: Any) -> bool:
    values = item.get("values") if isinstance(item.get("values"), dict) else {}
    for key, val in values.items():
        text = stamp_text(val)
        if re.search(rf"\b{PO_NUMBER}\b", text):
            return True
        if (
            po_id not in (None, "")
            and isinstance(val, dict)
            and str(val.get("id")) == str(po_id)
            and re.search(r"po|purchase", str(key), flags=re.I)
        ):
            return True
    return bool(re.search(rf"\b{PO_NUMBER}\b", str(item.get("name") or "")))


def compact_value(value: Any) -> Any:
    if isinstance(value, dict):
        kept = {key: value.get(key) for key in ("id", "text", "name") if key in value}
        return kept or None
    if isinstance(value, list):
        return None
    return value


def interesting(values: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, val in values.items():
        lowered = key.lower()
        if not any(
            word in lowered
            for word in ("qty", "quant", "price", "cost", "receiv", "part", "desc", "item", "invoic", "po")
        ):
            continue
        compact = compact_value(val)
        if compact not in (None, "", {}):
            kept[key] = compact
    return kept


def display_part(part: str, description: str) -> str:
    if re.fullmatch(r"\d+", part or "") or re.match(r"PO\d+", part or ""):
        token = (description or "").split()
        return token[0] if token else part
    return part


def part_text(values: dict[str, Any]) -> str:
    for key in (
        "Part_Number",
        "Item_Number",
        "PO_Item_Number",
        "Item",
        "Work_Order_Number_$_Part_Number",
        "PO_Item_Number_$_Part_Number",
    ):
        text = lookup_text(values.get(key)) if isinstance(values.get(key), dict) else stamp_text(values.get(key))
        if text:
            return text
    return ""


def description_text(values: dict[str, Any]) -> str:
    for key in (
        "Description",
        "Item_Description",
        "Part_Description",
        "PO_Item_Number_$_Part_Description",
        "PO_Item_Description",
        "Misc_Description",
    ):
        text = lookup_text(values.get(key)) if isinstance(values.get(key), dict) else stamp_text(values.get(key))
        if text:
            return text
    return ""


def first_number(values: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        amount = money(values.get(key))
        if amount is not None:
            return amount
    return None


def kit_blob(*parts: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", " ".join(str(part or "") for part in parts).lower())


def is_kit(*parts: Any) -> bool:
    blob = kit_blob(*parts)
    return (
        "mlw49224170" in blob
        or "holedozer" in blob
        or ("bimetal" in blob and "holesaw" in blob)
        or ("20pc" in blob and "holesaw" in blob)
    )


def invoiced_bill(values: dict[str, Any]) -> dict[str, Any]:
    invoice = None
    for key in ("AP_Invoice_Number", "AP_Invoice", "Invoice_Number", "Invoice"):
        field = values.get(key)
        if isinstance(field, dict) and (field.get("id") or field.get("text")):
            invoice = {"id": field.get("id"), "text": field.get("text")}
            break
        if field not in (None, "", False) and not isinstance(field, (dict, list)):
            invoice = {"id": None, "text": str(field)}
            break
    return {
        "invoiced": values.get("Invoiced"),
        "quantity_invoiced": money(values.get("Quantity_Invoiced") or values.get("Quantity_Billed")),
        "bill": invoice,
    }


def looks_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def attachment_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("fileName") or item.get("filename") or "")


def attachment_bytes(client: KimcoClient, bill_id: int, item: dict[str, Any]) -> bytes | None:
    for key in ("url", "downloadUrl", "contentUrl", "fileUrl", "href"):
        url = item.get(key)
        if isinstance(url, str) and url.startswith("http"):
            response = requests.get(url, timeout=60)
            if response.status_code == 200 and response.content:
                return response.content
    attachment_id = item.get("id")
    if attachment_id in (None, ""):
        return None
    for suffix in (
        f"attachments/{attachment_id}",
        f"attachments/{attachment_id}/download",
        f"attachments/{attachment_id}/content",
    ):
        response = client.request("GET", client._record_url("ap_invoices", bill_id, suffix))
        if response.status_code != 200 or not response.content:
            continue
        if looks_pdf(response.content):
            return response.content
        try:
            payload = response.json()
        except ValueError:
            continue
        if isinstance(payload, dict):
            nested = attachment_bytes(client, bill_id, payload)
            if nested:
                return nested
    return None


def page_texts(content: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or "" for page in reader.pages]
    if any(page.strip() for page in pages):
        return pages
    import fitz

    document = fitz.open(stream=content, filetype="pdf")
    return [page.get_text() or "" for page in document]


# Printed on every O'Neal invoice header as the customer account, not an invoice.
ACCOUNT_NUMBERS = {"14748440"}


def add_number(found: list[str], token: str) -> None:
    text = str(token or "").strip().strip(" .,#")
    if text and text not in found and text not in ACCOUNT_NUMBERS:
        found.append(text)


def near_invoice_label(text: str, number: str) -> bool:
    for match in re.finditer(rf"(?<![A-Z0-9]){re.escape(number)}(?![A-Z0-9])", text, flags=re.I):
        window = text[max(0, match.start() - 80) : match.end() + 80]
        if INVOICE_LABEL.search(window):
            return True
    return False


def numbers_on_page(text: str, known: set[str], own: str) -> list[str]:
    found: list[str] = []
    for number in gas_invoice_numbers(text):
        add_number(found, number)
    for match in _INV_EMJ.finditer(text or ""):
        add_number(found, match.group(1))
    if re.search(r"o'?neal|oneal steel", text or "", flags=re.I):
        for match in _ONEAL_INV.finditer(text or ""):
            add_number(found, match.group(1))
    for match in _INV_LABEL.finditer(text or ""):
        add_number(found, match.group(1))
    orderish = {item[:10] for item in _GAS_ORDER_SUFFIX.findall(text or "")}
    for number in sorted(known, key=len, reverse=True):
        if not re.search(rf"(?<![A-Z0-9]){re.escape(number)}(?![A-Z0-9])", text or "", flags=re.I):
            continue
        if number in orderish and not same_invoice(number, own):
            continue
        short = len(re.sub(r"\W", "", number)) < 7
        if short and not same_invoice(number, own) and not near_invoice_label(text or "", number):
            continue
        add_number(found, number)
    return found


def same_invoice(left: str, right: str) -> bool:
    return left.strip().upper() == right.strip().upper()


def allow_header(client: KimcoClient, url: str) -> dict[str, Any]:
    response = client.request("OPTIONS", url)
    allow = response.headers.get("Allow") or response.headers.get("allow") or ""
    return {"http": response.status_code, "allow": allow}


def po_line_fact(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") if isinstance(record.get("values"), dict) else {}
    part = part_text(values)
    description = description_text(values)
    qty_ordered = first_number(values, "Quantity_Ordered", "Quantity", "Qty_Ordered", "Order_Quantity")
    unit_price = first_number(values, "Unit_Price", "Price", "Unit_Cost", "Purchase_Cost")
    qty_received = first_number(values, "Quantity_Received", "Qty_Received", "Received_Quantity")
    return {
        "id": record.get("id"),
        "line": lookup_text(values.get("Purchase_Line_Number") or values.get("Line_Number"))
        or stamp_text(values.get("Purchase_Line_Number") or values.get("Line_Number")),
        "part": display_part(part, description),
        "description": description,
        "qty_ordered": qty_ordered,
        "unit_price": unit_price,
        "qty_received": qty_received,
        "kit": is_kit(part, description, json.dumps(interesting(values), default=str)),
        "fields": interesting(values),
    }


def receipt_fact(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") if isinstance(record.get("values"), dict) else {}
    part = part_text(values)
    description = description_text(values)
    qty = first_number(values, "Quantity_Received", "Quantity", "Qty")
    unit = first_number(values, "PO_Item_Number_$_Unit_Price", "Unit_Price", "Purchase_Cost", "Unit_Cost")
    extended = first_number(values, "Extended_Purchase_Cost", "Extended_Cost", "Amount", "Extended_Price")
    if extended is None and qty is not None and unit is not None:
        extended = round(float(qty) * float(unit), 2)
    pol = values.get("PO_Item_Number")
    billed = invoiced_bill(values)
    return {
        "id": record.get("id"),
        "part": display_part(part, description),
        "description": description,
        "qty": qty,
        "unit": unit,
        "extended": extended,
        "po_line_id": pol.get("id") if isinstance(pol, dict) else None,
        "po_line": lookup_text(pol) if isinstance(pol, dict) else stamp_text(pol),
        "invoiced": billed["invoiced"],
        "quantity_invoiced": billed["quantity_invoiced"],
        "invoiced_bill": billed["bill"],
        "open": billed["invoiced"] not in (True, "true", "True") and not (billed["bill"] or {}).get("text"),
        "kit": is_kit(part, description, json.dumps(interesting(values), default=str)),
        "amount_213_93": extended == 213.93,
        "fields": interesting(values),
    }


def load_po(client: KimcoClient, po_id: Any) -> dict[str, Any]:
    purchase_rows = client.list_items("purchase_lines")
    receipt_rows = client.list_items("receipts")
    line_ids = [int(row["id"]) for row in purchase_rows if row.get("id") not in (None, "") and mentions_po(row, po_id)]
    lines = [po_line_fact(client.get_item("purchase_lines", line_id)) for line_id in line_ids]
    receipt_ids = []
    for row in receipt_rows:
        if row.get("id") in (None, ""):
            continue
        values = row.get("values") if isinstance(row.get("values"), dict) else {}
        po = values.get("PO_Number") or values.get("Purchase_Order")
        text = po.get("text") if isinstance(po, dict) else stamp_text(po)
        po_hit = (isinstance(po, dict) and po_id not in (None, "") and str(po.get("id")) == str(po_id)) or bool(
            re.search(rf"\b{PO_NUMBER}\b", str(text or ""))
        )
        if po_hit:
            receipt_ids.append(int(row["id"]))
    receipts = []
    for receipt_id in receipt_ids:
        fact = receipt_fact(client.get_item("receipts", receipt_id))
        po = (fact.get("fields") or {}).get("PO_Number") or {}
        text = po.get("text") if isinstance(po, dict) else stamp_text(po)
        po_hit = (isinstance(po, dict) and po_id not in (None, "") and str(po.get("id")) == str(po_id)) or bool(
            re.search(rf"\b{PO_NUMBER}\b", str(text or ""))
        )
        if po_hit:
            receipts.append(fact)
    kit_lines = [line for line in lines if line["kit"]]
    kit_receipts = [row for row in receipts if row["kit"]]
    amount_hits = [row for row in receipts if row["extended"] == 213.93]
    return {
        "po": PO_NUMBER,
        "po_id": po_id,
        "line_count": len(lines),
        "receipt_count": len(receipts),
        "lines": lines,
        "receipts": receipts,
        "kit_part": KIT_PART,
        "kit_price": 213.93,
        "kit_on_po": bool(kit_lines),
        "kit_on_receipt": bool(kit_receipts),
        "kit_lines": [{"id": line["id"], "part": line["part"], "description": line["description"]} for line in kit_lines],
        "kit_receipts": [
            {"id": row["id"], "part": row["part"], "description": row["description"], "extended": row["extended"]}
            for row in kit_receipts
        ],
        "receipts_at_213_93": [
            {"id": row["id"], "part": row["part"], "description": row["description"], "extended": row["extended"]}
            for row in amount_hits
        ],
    }


def audit_bills(client: KimcoClient) -> tuple[list[dict[str, str]], dict[str, Any], Any]:
    headers = []
    for bill_id in BILL_IDS:
        record = client.get_item("ap_invoices", bill_id)
        values = record.get("values") or {}
        po = values.get("Purchase_Order")
        headers.append(
            {
                "id": bill_id,
                "invoice": str(values.get("Invoice_Number") or "").strip(),
                "vendor": lookup_text(values.get("Vendor")),
                "po_id": lookup_id(po) if isinstance(po, dict) else None,
                "po": lookup_text(po) if isinstance(po, dict) else stamp_text(po),
            }
        )
    known = {row["invoice"] for row in headers if row["invoice"]}
    rows: list[dict[str, str]] = []
    sample_attachment: dict[str, Any] | None = None
    sample_bill = None
    for header in headers:
        bill_id = int(header["id"])
        own = header["invoice"]
        attachments = client.list_attachments(bill_id)
        parsed = []
        for item in attachments:
            content = attachment_bytes(client, bill_id, item)
            filename = attachment_name(item)
            attachment_id = str(item.get("id") or "")
            if sample_attachment is None and attachment_id:
                sample_attachment = item
                sample_bill = bill_id
            if not content or not looks_pdf(content):
                parsed.append(
                    {
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "page_count": "",
                        "by_page": "",
                        "numbers": [],
                        "readable": False,
                        "downloaded": bool(content),
                    }
                )
                continue
            pages = page_texts(content)
            by_page = []
            numbers: list[str] = []
            for index, text in enumerate(pages, start=1):
                found = numbers_on_page(text, known, own)
                for number in found:
                    add_number(numbers, number)
                by_page.append(f"{index}:{('|'.join(found) if found else '(none)')}")
            parsed.append(
                {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "page_count": str(len(pages)),
                    "by_page": "; ".join(by_page),
                    "numbers": numbers,
                    "readable": any(page.strip() for page in pages),
                    "downloaded": True,
                }
            )
        all_numbers = [number for item in parsed for number in item["numbers"]]
        own_present = any(same_invoice(number, own) for number in all_numbers)
        others = []
        for number in all_numbers:
            if not same_invoice(number, own):
                add_number(others, number)
        if not parsed:
            bill_flag = "no-attachment"
        elif any(not item["downloaded"] for item in parsed):
            bill_flag = "download-failed"
        elif parsed and not any(item["readable"] for item in parsed):
            bill_flag = "unreadable"
        else:
            flags = []
            if others:
                flags.append("other-invoice")
            if not own_present:
                flags.append("missing-own-invoice")
            bill_flag = "|".join(flags)
        if not parsed:
            rows.append(
                {
                    "bill id": str(bill_id),
                    "invoice #": own,
                    "vendor": header["vendor"],
                    "attachment id": "",
                    "filename": "",
                    "page count": "",
                    "invoice numbers by page": "",
                    "other invoice numbers": "",
                    "own invoice present": "no",
                    "flag": bill_flag,
                }
            )
            continue
        for item in parsed:
            item_others = [number for number in item["numbers"] if not same_invoice(number, own)]
            rows.append(
                {
                    "bill id": str(bill_id),
                    "invoice #": own,
                    "vendor": header["vendor"],
                    "attachment id": item["attachment_id"],
                    "filename": item["filename"],
                    "page count": item["page_count"],
                    "invoice numbers by page": item["by_page"],
                    "other invoice numbers": "|".join(item_others),
                    "own invoice present": "yes" if any(same_invoice(number, own) for number in item["numbers"]) else "no",
                    "flag": bill_flag,
                }
            )
        LOGGER.info("Audited %s attachments %s flag %s", bill_id, len(parsed), bill_flag or "ok")
    po_header = next(row for row in headers if row["id"] == 10480)
    return rows, po_header, (sample_bill, sample_attachment)


def attachment_api(client: KimcoClient, sample_bill: int | None, sample: dict[str, Any] | None) -> dict[str, Any]:
    record_url = client._record_url("ap_invoices", 10480)
    collection_url = client._record_url("ap_invoices", 10480, "attachments")
    probed = {
        "invoice_record_options": allow_header(client, record_url),
        "attachment_collection_options": allow_header(client, collection_url),
        "docs": (
            "README documents adding an attachment with POST .../{id}/attachments/upload, "
            "PUT the returned uploadUrl, then POST .../{id}/attachments. "
            "It does not document a replace or delete for an existing attachment file."
        ),
    }
    attachment_id = (sample or {}).get("id")
    if sample_bill and attachment_id not in (None, ""):
        item_url = client._record_url("ap_invoices", sample_bill, f"attachments/{attachment_id}")
        probed["attachment_item_options"] = allow_header(client, item_url)
        probed["attachment_item_bill"] = sample_bill
    allow = probed["attachment_item_options"]["allow"].upper() if "attachment_item_options" in probed else ""
    collection_allow = probed["attachment_collection_options"]["allow"].upper()
    probed["delete_allowed"] = "DELETE" in allow or "DELETE" in collection_allow
    probed["replace_allowed"] = any(verb in allow for verb in ("PUT", "PATCH")) or any(
        verb in collection_allow for verb in ("PUT", "PATCH")
    )
    return probed


def main() -> None:
    client = login()
    install_401_guard(client)
    rows, po_header, sample = audit_bills(client)
    po = load_po(client, po_header["po_id"])
    po["bill_10480_invoice"] = po_header["invoice"]
    po["bill_10480_po_text"] = po_header["po"]
    po["vendor_on_10480"] = po_header["vendor"]
    sample_bill, sample_attachment = sample
    po["attachment_api"] = attachment_api(client, sample_bill, sample_attachment)
    AUDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_CSV.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "bill id",
                "invoice #",
                "vendor",
                "attachment id",
                "filename",
                "page count",
                "invoice numbers by page",
                "other invoice numbers",
                "own invoice present",
                "flag",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    PO_JSON.write_text(json.dumps(po, indent=2, default=str))
    flagged = sorted({row["bill id"] for row in rows if row["flag"]})
    LOGGER.info(
        "Bills %s flagged %s kit_on_po %s kit_on_receipt %s",
        len(BILL_IDS),
        len(flagged),
        po["kit_on_po"],
        po["kit_on_receipt"],
    )


if __name__ == "__main__":
    main()
