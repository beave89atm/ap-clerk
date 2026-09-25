"""Enter five Fastenal invoices on live KIMCO. Never posts the bill.

Batch name: API Agent - 9/25/26 Fastenal.
PO bills are type 3. Receipts are chosen by Quantity_Received.
Quantity_Remaining is ignored. Packing-slip gate is suspended.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoError
from ap_clerk.rules import kimco_datetime, lookup_id, lookup_text

BATCH_NAME = "API Agent - 9/25/26 Fastenal"
SHAWN_MENTION_ID = 104
PPV_LIMIT = 75.0
VENDOR_SAMPLE_ID = 9924
OUT_JSON = ROOT / "artifacts" / "fastenal-five-2026-09-25.json"

# Source emails in the 2026-09-04..2026-09-25 accountspayable@ window.
SOURCE_MESSAGES = {
    "sep04": "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOQsGYkAAA=",
    "sep19": "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOdIDLDAAA=",
    "sep17": "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAObq9T0AAA=",
}
TARGET_NUMBERS = (
    "TXFT4100349",
    "TXFT4100376",
    "TXFT4100503",
    "TXFT4100537",
    "TXFT499930",
)

_TAIL = re.compile(
    r"(?P<control>\S+)\s+(?P<part>\d{4,}(?:-\d+)?)\s+"
    r"(?P<price>[\d,]+\.\d{4})\s+(?P<amount>[\d,]+\.\d{2})(?:\s+\S+)?\s*$"
)
_HEAD4 = re.compile(
    r"^(?P<line>\d+)\s+(?P<ordered>\d+)\s+(?P<shipped>\d+)\s+(?P<back>\d+)\s+(?P<desc>.+)$"
)
_HEAD3 = re.compile(r"^(?P<ordered>\d+)\s+(?P<shipped>\d+)\s+(?P<back>\d+)\s+(?P<desc>.+)$")
_FASTENAL_NO = re.compile(r"Fastenal\s*#?\s*(\d{4,}(?:-\d+)?)", re.I)
_PO_LINE = re.compile(r"PO(\d{5})-(\d+)")


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def qty_num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


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


def price_text(price: float | None) -> str:
    if price is None:
        return "?"
    text = f"{price:.4f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.00"


def _mdy(value: str) -> date:
    month, day, year = value.split("/")
    return date(int(year), int(month), int(day))


def fastenal_number(*blobs: str | None) -> str:
    for blob in blobs:
        match = _FASTENAL_NO.search(blob or "")
        if match:
            return match.group(1)
    return ""


def parse_fastenal_page(page: str) -> dict[str, Any] | None:
    """One Fastenal invoice page. Prices on the PDF are per hundred."""
    text = page or ""
    inv = re.search(r"Invoice No\.\s*(TXFT\d+)", text) or re.search(
        r"\d{2}/\d{2}/\d{4}\s+(TXFT\d+)", text
    )
    if not inv:
        return None
    total_m = re.search(r"Invoice Total\s*([\d,]+\.\d{2})", text) or re.search(
        r"Invoice Total\s*\n\s*([\d,]+\.\d{2})", text
    )
    date_m = re.search(r"Invoice Date\s*(\d{2}/\d{2}/\d{4})", text) or re.search(
        r"(\d{2}/\d{2}/\d{4})\s+TXFT\d+", text
    )
    due_m = re.search(r"\(NET\s*30\)\s*(\d{2}/\d{2}/\d{4})", text)
    po = None
    for line in text.splitlines():
        token = line.strip()
        if re.fullmatch(r"\d{5}", token):
            po = token
            break
    if not total_m or not date_m or not po:
        return None
    start = text.find("Hundred")
    end = text.find("Received By")
    region = text[start:end] if start >= 0 and end > start else text
    lines: list[dict[str, Any]] = []
    for raw in region.splitlines():
        raw = re.sub(r"[ \t]+", " ", raw).strip()
        tail = _TAIL.search(raw)
        if not tail:
            continue
        front = raw[: tail.start()].strip()
        head = _HEAD4.match(front) or _HEAD3.match(front)
        if not head:
            continue
        price_c = money(tail.group("price"))
        amount = money(tail.group("amount"))
        if price_c is None or amount is None:
            continue
        lines.append(
            {
                "ordered": int(head.group("ordered")),
                "shipped": int(head.group("shipped")),
                "desc": head.group("desc").strip(),
                "part": tail.group("part"),
                "price_per_hundred": price_c,
                "price_each": round(price_c / 100.0, 4),
                "amount": amount,
            }
        )
    if not lines:
        return None
    subtotal = round(sum(line["amount"] for line in lines), 2)
    total = money(total_m.group(1))
    ship_m = re.search(r"Shipping\s*&\s*Handling\s+([\d,]+\.\d{2})", text)
    if ship_m:
        shipping = money(ship_m.group(1))
    else:
        shipping = round((total or 0) - subtotal, 2)
    if total is None or shipping is None or round(subtotal + shipping - total, 2) != 0:
        return None
    return {
        "invoice_number": inv.group(1),
        "po": po,
        "date": _mdy(date_m.group(1)),
        "due": _mdy(due_m.group(1)) if due_m else None,
        "total": total,
        "subtotal": subtotal,
        "shipping": shipping,
        "lines": lines,
        "type": "parts",
    }


def parse_fastenal_text(text: str) -> list[dict[str, Any]]:
    bills = []
    for page in (text or "").split("\f"):
        if not page.strip():
            continue
        bill = parse_fastenal_page(page)
        if bill:
            bills.append(bill)
    return bills


def invoiced_qty_for_part(invoice: dict[str, Any], part: str) -> float:
    if not part:
        return 0.0
    total = 0.0
    for line in invoice.get("lines") or []:
        if str(line.get("part") or "") == part:
            total += float(line.get("shipped") or 0)
    return round(total, 4)


def open_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unbilled receipts. Quantity_Remaining is never a reason to drop a row."""
    chosen = []
    for row in receipts:
        if row.get("invoiced") is True:
            continue
        if str(row.get("ap") or "").strip():
            continue
        if qty_num(row.get("qty_received")) is None:
            continue
        chosen.append(row)
    return chosen


def choose_receipts(
    invoice: dict[str, Any],
    po_lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select every open receipt on the PO when Quantity_Received covers the order.

    A line with Quantity_Received 0 is missing_receipt even when
    Quantity_Remaining is also 0. Partial receives are summed. Nothing is
    selected when any line is short, missing, or the merchandise gap is
    $75 or more.
    """
    po = str(invoice.get("po") or "")
    lines = [row for row in po_lines if str(row.get("po") or "") == po]
    recs = [row for row in open_receipts(receipts) if str(row.get("po") or "") == po]
    by_line: dict[str, list[dict[str, Any]]] = {}
    for rec in recs:
        by_line.setdefault(str(rec.get("line") or ""), []).append(rec)
    problems: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    received_ext = 0.0
    for line in lines:
        group = by_line.get(str(line.get("line") or ""), [])
        got = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in group), 4)
        ordered = qty_num(line.get("ordered")) or 0.0
        part = str(line.get("fastenal") or "")
        invoiced = invoiced_qty_for_part(invoice, part)
        detail = {
            "line": line.get("line"),
            "part": part,
            "desc": line.get("desc") or "",
            "ordered": ordered,
            "invoiced": invoiced,
            "received": got,
            "receipts": [
                {
                    "id": row.get("id"),
                    "qty": qty_num(row.get("qty_received")),
                    "price": row.get("unit"),
                }
                for row in group
            ],
        }
        if got == 0:
            problems.append({**detail, "kind": "missing_receipt"})
            continue
        if abs(got - ordered) > 0.001:
            problems.append({**detail, "kind": "quantity_variance"})
            continue
        for row in group:
            qty = float(qty_num(row.get("qty_received")) or 0)
            unit = float(row.get("unit") or 0)
            received_ext = round(received_ext + round(qty * unit, 2), 2)
            selected.append(row)
    invoiced_total = round(sum(float(line.get("shipped") or 0) for line in invoice.get("lines") or []), 4)
    ordered_total = round(sum(float(qty_num(line.get("ordered")) or 0) for line in lines), 4)
    if abs(invoiced_total - ordered_total) > 0.001 and lines:
        problems.append(
            {
                "kind": "quantity_variance",
                "line": po,
                "part": "",
                "desc": "",
                "ordered": ordered_total,
                "invoiced": invoiced_total,
                "received": round(sum(float(qty_num(row.get("qty_received")) or 0) for row in recs), 4),
                "receipts": [],
            }
        )
    merch_gap = round(float(invoice.get("subtotal") or 0) - received_ext, 2)
    if problems:
        kinds = {row["kind"] for row in problems}
        if kinds == {"missing_receipt"}:
            action = "missing_receipt"
        elif "quantity_variance" in kinds:
            action = "quantity_variance"
        else:
            action = "missing_receipt"
        return {
            "action": action,
            "receipts": [],
            "problems": problems,
            "merch_gap": None,
            "received_ext": 0.0,
        }
    if abs(merch_gap) >= PPV_LIMIT:
        return {
            "action": "price_variance",
            "receipts": [],
            "problems": [
                {
                    "kind": "price_variance",
                    "line": po,
                    "part": "",
                    "desc": "",
                    "ordered": ordered_total,
                    "invoiced": invoiced_total,
                    "received": ordered_total,
                    "receipts": [
                        {
                            "id": row.get("id"),
                            "qty": qty_num(row.get("qty_received")),
                            "price": row.get("unit"),
                        }
                        for row in selected
                    ],
                    "gap": merch_gap,
                }
            ],
            "merch_gap": merch_gap,
            "received_ext": received_ext,
        }
    return {
        "action": "select",
        "receipts": selected,
        "problems": [],
        "merch_gap": merch_gap,
        "received_ext": received_ext,
    }


def receipt_label(rows: list[dict[str, Any]]) -> str:
    bits = []
    for row in rows:
        bits.append(
            f"{row.get('id')} qty {qty_text(qty_num(row.get('qty_received') if 'qty_received' in row else row.get('qty')))} "
            f"@ {price_text(float(row['unit']) if row.get('unit') not in (None, '') else float(row.get('price') or 0))}"
        )
    return "; ".join(bits)


def _problem_sentence(invoice: dict[str, Any], problem: dict[str, Any]) -> str:
    ordered = qty_text(problem.get("ordered"))
    invoiced = qty_text(problem.get("invoiced"))
    received = qty_text(problem.get("received"))
    part = problem.get("part") or "the part"
    desc = str(problem.get("desc") or "").strip()
    desc_bit = f" ({desc})" if desc else ""
    line = problem.get("line") or invoice.get("po")
    receipts = problem.get("receipts") or []
    receipt_bit = ""
    if receipts:
        receipt_bit = " Receipts by Quantity_Received: " + receipt_label(
            [
                {"id": row["id"], "qty_received": row["qty"], "unit": row["price"]}
                for row in receipts
            ]
        ) + "."
    return (
        f"PO {invoice.get('po')} line {line} Fastenal part {part}{desc_bit} "
        f"was ordered {ordered}, invoiced {invoiced}, and Quantity_Received is {received}."
        f"{receipt_bit}"
    )


def build_note(
    invoice: dict[str, Any],
    *,
    status: str,
    action: str,
    problems: list[dict[str, Any]] | None = None,
    receipt_rows: list[dict[str, Any]] | None = None,
    amount_entered: float | None = None,
    ppv: float | None = None,
    gap: float | None = None,
) -> str:
    """Plain-English Comments_1 / sheet note. No category= shorthand and no HTML."""
    number = invoice["invoice_number"]
    total = float(invoice["total"])
    sub = float(invoice["subtotal"])
    ship = float(invoice["shipping"])
    po = invoice["po"]
    if status == "Success":
        label = receipt_label(receipt_rows or [])
        if ppv not in (None, 0, 0.0):
            ppv_bit = (
                f"Receipt lines plus shipping missed the PDF total by {dollar(ppv)}, "
                f"which is under {dollar(PPV_LIMIT)}, so one signed Purchase Price Variance "
                f"of {dollar(ppv)} was posted. After that charge the gap is {dollar(0)}."
            )
        else:
            ppv_bit = (
                "Selected lines plus the Shipping & Handling fee equal the PDF total, "
                "so the gap is $0.00 and no Purchase Price Variance was posted."
            )
        text = (
            f"AP Clerk: Fastenal invoice {number} is entered as a parts bill on PO {po} "
            f"and is not posted. The PDF total is {dollar(total)}, made up of merchandise "
            f"{dollar(sub)} and Shipping & Handling {dollar(ship)}. "
            f"Amount entered is {dollar(amount_entered if amount_entered is not None else total)}. "
            f"Open receipts were selected by Quantity_Received, and that quantity matches "
            f"the quantity invoiced and the quantity ordered on every PO line. "
            f"Quantity_Remaining was not used. Receipts selected: {label}. "
            f"PDF prices are per hundred and the PO price is per each; the extended amounts "
            f"are what were compared. Shipping & Handling was posted as Fees and surcharges, "
            f"not as freight and not as a price variance. {ppv_bit} "
            f"Nothing is waiting on purchasing. Treyce can post the bill."
        )
    else:
        detail = " ".join(_problem_sentence(invoice, row) for row in (problems or []))
        if action == "missing_receipt":
            why = (
                "This cannot be finished because those lines have no selectable receipt. "
                "Quantity_Received is 0, so this is a missing receipt and not a price variance."
            )
            ask = (
                f"Shawn should receive the invoiced quantity of each part above on PO {po}. "
                f"After those receipts exist, AP will select them by Quantity_Received, "
                f"post Shipping & Handling {dollar(ship)} as Fees and surcharges, and finish "
                f"the bill if the header matches the PDF total of {dollar(total)} to the penny."
            )
        elif action == "price_variance":
            why = (
                f"The merchandise gap is {dollar(gap)} which is {dollar(PPV_LIMIT)} or more, "
                "so no Purchase Price Variance was posted and the receipts were not selected. "
                "Selecting them would lock the receipt and block an unreceive."
            )
            ask = (
                "Shawn should unreceive the line, set the PO price to the invoice each-price "
                "(PDF price per hundred divided by 100), and re-receive the same quantity. "
                "After that, AP will select the receipts and finish the bill."
            )
        else:
            why = (
                "Quantity received does not match quantity invoiced or quantity ordered, "
                "and the difference is outside what a Purchase Price Variance can absorb. "
                "No receipts were selected."
            )
            ask = (
                "Shawn should correct the receipt quantity so Quantity_Received equals the "
                "invoiced quantity on that PO line, then tell AP to select those receipts and finish."
            )
        entered = dollar(amount_entered if amount_entered is not None else 0)
        text = (
            f"AP Clerk: @Shawn McKibben Fastenal invoice {number} cannot be finished. "
            f"The PDF total is {dollar(total)} (merchandise {dollar(sub)} plus "
            f"Shipping & Handling {dollar(ship)}). Amount entered on receipt lines is {entered}. "
            f"{detail} {why} Quantity_Remaining was not used. {ask} "
            f"The bill was moved to Transfer AP and is not posted."
        )
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if "category=" in text or "owner=" in text or "<" in text or ">" in text:
        raise RuntimeError("note must be plain text")
    return re.sub(r"\s+", " ", text).strip()


def comment_values(kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    values: dict[str, Any] = {
        "HtmlValue": text,
        "Entity": {"id": 203},
        "ObjectId": int(kimco_id),
        "FormId": 218,
    }
    if mention:
        values["Mention"] = {"id": SHAWN_MENTION_ID}
    return values


def _po_token(value: Any) -> str:
    text = lookup_text(value) or str(value or "")
    match = re.search(r"PO(\d{5})", text)
    return match.group(1) if match else ""


def po_line_from_record(item: dict[str, Any]) -> dict[str, Any] | None:
    values = item.get("values") or {}
    po = _po_token(values.get("Purchase_Order_Number"))
    display = str(values.get("Display_Name") or "")
    if not po and display:
        match = _PO_LINE.search(display)
        po = match.group(1) if match else ""
    if po not in {row for row in ("58835", "59054", "59055", "59179", "59190")}:
        return None
    desc = str(values.get("Part_Description") or "")
    part_text = lookup_text(values.get("Part_Number")) or ""
    return {
        "id": item.get("id"),
        "po": po,
        "po_id": lookup_id(values.get("Purchase_Order_Number")),
        "line": display,
        "ordered": qty_num(values.get("Quantity")),
        "unit": values.get("Unit_Price"),
        "fastenal": fastenal_number(desc, part_text),
        "desc": desc or part_text,
    }


def receipt_from_record(item: dict[str, Any]) -> dict[str, Any] | None:
    values = item.get("values") or {}
    line = lookup_text(values.get("PO_Item_Number")) or ""
    match = _PO_LINE.search(line)
    if not match:
        return None
    ap = values.get("AP_Invoice_Number")
    ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "")
    return {
        "id": item.get("id"),
        "po": match.group(1),
        "line": line,
        "qty_received": qty_num(values.get("Quantity_Received")),
        "qty_remaining": qty_num(values.get("Quantity_Remaining")),
        "unit": values.get("PO_Item_Number_$_Unit_Price"),
        "ap": "" if ap_text in {"None", "null"} else ap_text,
        "invoiced": values.get("Invoiced"),
    }


def find_invoice_ids(client: Any, numbers: set[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    offset = 0
    total = None
    url = client._url("ap_invoices")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"invoice list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            number = str((item.get("values") or {}).get("Invoice_Number") or "").strip().upper()
            if number in numbers and item.get("id") not in (None, ""):
                found[number] = int(item["id"])
        if not items:
            break
        offset += len(items)
    return found


def load_po_lines(client: Any) -> list[dict[str, Any]]:
    rows = []
    offset = 0
    total = None
    url = client._url("purchase_lines")
    wanted = ("58835", "59054", "59055", "59179", "59190")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"purchase line list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            blob = json.dumps(item.get("values") or {})
            if not any(po in blob for po in wanted):
                continue
            row = po_line_from_record(item)
            if row:
                rows.append(row)
        if not items:
            break
        offset += len(items)
    return rows


def load_receipts(client: Any) -> list[dict[str, Any]]:
    ids: list[int] = []
    offset = 0
    total = None
    url = client._url("receipts")
    wanted = ("58835", "59054", "59055", "59179", "59190")
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise RuntimeError(f"receipt list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            part = lookup_text((item.get("values") or {}).get("PO_Item_Number")) or ""
            if any(po in part for po in wanted):
                ids.append(int(item["id"]))
        if not items:
            break
        offset += len(items)
    rows = []
    for rid in ids:
        rec = receipt_from_record(client.get_item("receipts", rid))
        if rec:
            rows.append(rec)
    return rows


def page_pdf(pdf_bytes: bytes, invoice_number: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    found = False
    for page in reader.pages:
        if invoice_number in (page.extract_text() or ""):
            writer.add_page(page)
            found = True
            break
    if not found:
        raise RuntimeError(f"{invoice_number} page not found in the source PDF")
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def snapshot_amounts(client: Any, kimco_id: int) -> dict[str, Any]:
    from ap_clerk.kimco import header_penny_ppv_from_record
    from ap_clerk.rules import ppv_qc_gap

    record = client.get_item("ap_invoices", kimco_id)
    values = record.get("values") or {}
    decision = header_penny_ppv_from_record(record)
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
                "qty_received": qty_num(lv.get("Quantity")),
                "unit": lv.get("Unit_Price"),
            }
        )
    charge_amounts = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        amount = money((charge.get("values") or {}).get("Amount"))
        if amount is not None:
            charge_amounts.append(amount)
    qc = ppv_qc_gap(
        invoice_amount=values.get("Invoice_Amount"),
        verification_amount=values.get("Invoice_Verification_Amount"),
        line_amounts=line_amounts,
        charge_amounts=charge_amounts,
    )
    comments = []
    for comment in lists.get("Comments_1") or []:
        comments.append(
            {
                "id": comment.get("id"),
                "html": (comment.get("values") or {}).get("HtmlValue") or "",
            }
        )
    return {
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "posted": values.get("Posted"),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "type": values.get("Invoice_Type"),
        "decision": decision,
        "qc": qc,
        "receipt_rows": receipt_rows,
        "comments": comments,
        "line_count": len(lists.get("APInvoiceLine") or []),
    }


def write_comment(client: Any, kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    attempts = [comment_values(kimco_id, text, mention=mention)]
    if mention:
        attempts.append(comment_values(kimco_id, text, mention=False))
    last: dict[str, Any] = {"status": "blocked"}
    for values in attempts:
        payload = {
            "state": "Modified",
            "id": int(kimco_id),
            "lists": {"Comments_1": [{"state": "Added", "values": values}]},
        }
        try:
            _body, status, error = client.update("ap_invoices", kimco_id, payload)
        except KimcoError as exc:
            last = {"status": "blocked", "error": str(exc)[:200]}
            continue
        live = snapshot_amounts(client, kimco_id)
        match = None
        for comment in live["comments"]:
            if text[:80] in str(comment.get("html") or ""):
                match = comment.get("id")
        last = {
            "status": "persisted" if status < 400 and match else f"put-{status}",
            "put": status,
            "error": error,
            "id": match,
            "mention": "Mention" in values,
        }
        if last["status"] == "persisted":
            return last
    return last


def enter_live(*, write: bool = True) -> dict[str, Any]:
    from ap_clerk.auth import load_credentials, resolve_target
    from ap_clerk.cli import _find_or_create_batch, _optional_graph_client
    from ap_clerk.kimco import KimcoClient, post_header_penny_ppv
    from ap_clerk.transfer_ap import apply_transfer_ap_batch_move

    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph credentials missing")
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error)
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    sample = client.get_item("ap_invoices", VENDOR_SAMPLE_ID)
    sample_values = sample.get("values") or {}
    vendor_id = lookup_id(sample_values.get("Vendor"))
    terms_id = lookup_id(sample_values.get("Terms_Code"))
    remit_id = lookup_id(sample_values.get("Remit_To_Address"))
    currency_id = lookup_id(sample_values.get("Currency"))
    if vendor_id != 66 or not terms_id or not remit_id or not currency_id:
        raise SystemExit(
            f"Fastenal sample {VENDOR_SAMPLE_ID} vendor/terms/remit changed: "
            f"vendor {vendor_id} terms {terms_id} remit {remit_id}"
        )

    from ap_clerk.pdf_invoice import extract_pdf_text

    downloads: dict[str, bytes] = {}
    parsed: list[dict[str, Any]] = []
    for label, mid in SOURCE_MESSAGES.items():
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, mid)
        if len(pdfs) != 1:
            raise SystemExit(f"{label} expected 1 PDF, got {len(pdfs)}")
        name, content = pdfs[0]
        downloads[label] = content
        path = Path("/tmp/fastenal-enter") / f"{label}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        text = extract_pdf_text(path)
        for bill in parse_fastenal_text(text):
            if bill["invoice_number"] not in TARGET_NUMBERS:
                continue
            bill["source"] = label
            bill["message_id"] = mid
            bill["pdf_name"] = name
            parsed.append(bill)
    by_number = {bill["invoice_number"]: bill for bill in parsed}
    missing = [number for number in TARGET_NUMBERS if number not in by_number]
    if missing:
        raise SystemExit(f"PDF parse missed {missing}")

    print("Loading PO lines and receipts", flush=True)
    po_lines = load_po_lines(client)
    receipts = load_receipts(client)
    print(f"PO lines {len(po_lines)} receipts {len(receipts)}", flush=True)
    if not write:
        for number in TARGET_NUMBERS:
            invoice = by_number[number]
            plan = choose_receipts(invoice, po_lines, receipts)
            print(
                json.dumps(
                    {
                        "invoice": number,
                        "po": invoice["po"],
                        "total": invoice["total"],
                        "shipping": invoice["shipping"],
                        "action": plan["action"],
                        "merch_gap": plan.get("merch_gap"),
                        "receipts": receipt_label(plan["receipts"]),
                        "problems": plan["problems"],
                        "fastenal": [
                            {"line": row["line"], "part": row["fastenal"], "ordered": row["ordered"]}
                            for row in po_lines
                            if row["po"] == invoice["po"]
                        ],
                    },
                    default=str,
                ),
                flush=True,
            )
        return {"dry": True}
    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    batch_id = int(batch["id"])
    print(f"BATCH {batch_id} {BATCH_NAME} created={batch.get('created')}", flush=True)

    results: list[dict[str, Any]] = []
    for number in TARGET_NUMBERS:
        invoice = by_number[number]
        print(f"CHECK {number}", flush=True)
        existing = find_invoice_ids(client, {number})
        if number in existing:
            live = snapshot_amounts(client, existing[number])
            results.append(
                {
                    "invoice_number": number,
                    "status": "HOLD",
                    "reason": f"already in KIMCO {existing[number]}; skipped, not duplicated",
                    "type": "parts",
                    "pdf_total": invoice["total"],
                    "kimco_bill_id": existing[number],
                    "batch": live.get("batch_id"),
                    "transfer_ap": "no",
                    "receipts_selected": receipt_label(live.get("receipt_rows") or []),
                    "ppv": None,
                    "comments_1_id": None,
                    "email_moved": "no",
                    "note": (
                        f"AP Clerk: Fastenal invoice {number} is already in KIMCO as bill "
                        f"{existing[number]}. It was not entered again."
                    ),
                    "message_id": invoice["message_id"],
                    "source": invoice["source"],
                    "attached": False,
                }
            )
            print(f"SKIP {number} existing {existing[number]}", flush=True)
            continue
        po_id = next(
            (row.get("po_id") for row in po_lines if str(row.get("po")) == invoice["po"] and row.get("po_id")),
            None,
        )
        if not po_id:
            raise SystemExit(f"PO {invoice['po']} id missing")
        po_receipts = [row for row in receipts if str(row.get("po")) == invoice["po"]]
        confirmed = []
        for row in po_receipts:
            confirmed_row = receipt_from_record(client.get_item("receipts", int(row["id"])))
            if confirmed_row:
                confirmed.append(confirmed_row)
        plan = choose_receipts(invoice, po_lines, confirmed)
        print(
            f"PLAN {number} {plan['action']} receipts={len(plan['receipts'])} "
            f"merch_gap={plan.get('merch_gap')}",
            flush=True,
        )
        payload = {
            "AP_Invoice_Batch": {"id": batch_id},
            "Vendor": {"id": vendor_id},
            "Invoice_Number": number,
            "Invoice_Type": 3,
            "Invoice_Date": kimco_datetime(invoice["date"]),
            "Invoice_Verification_Amount": float(invoice["total"]),
            "Invoice_Due_Date": kimco_datetime(invoice["due"]),
            "Terms_Code": {"id": terms_id},
            "Currency": {"id": currency_id},
            "Remit_To_Address": {"id": remit_id},
            "Transaction_Date": kimco_datetime(invoice["date"]),
            "Purchase_Order": {"id": int(po_id)},
            "Comments": "API Agent",
        }
        created_id, _body, status, error = client.create("ap_invoices", payload)
        if created_id is None:
            results.append(
                {
                    "invoice_number": number,
                    "status": "HOLD",
                    "reason": f"header create HTTP {status}: {error}",
                    "type": "parts",
                    "pdf_total": invoice["total"],
                    "kimco_bill_id": None,
                    "batch": batch_id,
                    "transfer_ap": "no",
                    "receipts_selected": "",
                    "ppv": None,
                    "comments_1_id": None,
                    "email_moved": "no",
                    "note": f"AP Clerk: Fastenal invoice {number} header was not created.",
                    "message_id": invoice["message_id"],
                    "source": invoice["source"],
                    "attached": False,
                }
            )
            continue
        pdf = page_pdf(downloads[invoice["source"]], number)
        attach = client.try_official_attach(
            created_id,
            name=f"{number}.pdf",
            content_type="application/pdf",
            size=len(pdf),
            content=pdf,
        )
        ppv_amount = None
        gap = None
        receipt_rows: list[dict[str, Any]] = []
        if plan["action"] == "select" and attach == "attached":
            select_status = client.try_select_receipts(
                created_id, [int(row["id"]) for row in plan["receipts"]]
            )
            fee_status = client.try_post_fees(
                created_id,
                [{"name": "Shipping & Handling", "amount": invoice["shipping"]}],
            )
            ppv_post = post_header_penny_ppv(client, created_id)
            # Re-read after the variance charge. This is the post-finish check.
            live = snapshot_amounts(client, created_id)
            qc = live["qc"]
            gap = qc.get("gap")
            ppv_amount = ppv_post.get("ppv") if ppv_post.get("ppv_status") == "posted" else 0.0
            receipt_rows = live["receipt_rows"]
            planned_ids = {int(row["id"]) for row in plan["receipts"]}
            live_ids = {int(row["id"]) for row in receipt_rows if row.get("id") not in (None, "")}
            if planned_ids != live_ids:
                client.try_deselect_receipts(created_id)
                note = (
                    f"AP Clerk: Fastenal invoice {number} was created as KIMCO {created_id} "
                    f"but the receipts on the bill do not match the Quantity_Received plan. "
                    f"PDF total {dollar(invoice['total'])}. Planned {sorted(planned_ids)}. "
                    f"On the bill {sorted(live_ids)}. Those lines were deselected. "
                    f"The bill is not posted and was left on {BATCH_NAME}."
                )
                comment = write_comment(client, created_id, note, mention=False)
                results.append(
                    {
                        "invoice_number": number,
                        "status": "HOLD",
                        "reason": "selected receipts do not match the Quantity_Received plan",
                        "type": "parts",
                        "pdf_total": invoice["total"],
                        "kimco_bill_id": created_id,
                        "batch": BATCH_NAME,
                        "batch_id": batch_id,
                        "transfer_ap": "no",
                        "receipts_selected": "",
                        "ppv": ppv_amount,
                        "comments_1_id": comment.get("id"),
                        "email_moved": "no",
                        "note": note,
                        "message_id": invoice["message_id"],
                        "source": invoice["source"],
                        "attached": True,
                        "gap": gap,
                    }
                )
                print(f"HOLD-RECEIPTS {number} id={created_id}", flush=True)
                continue
            success = (
                select_status == "selected"
                and fee_status == "posted"
                and qc.get("success_allowed") is True
                and gap in (0, 0.0)
                and qc.get("rollup_gap") in (None, 0, 0.0)
                and live["posted"] in (None, "", False)
                and live["verification"] == invoice["total"]
                and attach == "attached"
            )
            if success:
                note = build_note(
                    invoice,
                    status="Success",
                    action="select",
                    receipt_rows=receipt_rows,
                    amount_entered=live["amount"],
                    ppv=ppv_amount,
                    gap=0.0,
                )
                comment = write_comment(client, created_id, note, mention=False)
                results.append(
                    {
                        "invoice_number": number,
                        "status": "Success" if comment.get("id") else "HOLD",
                        "reason": "totals match to the penny"
                        if comment.get("id")
                        else "entered but Comments_1 did not persist",
                        "type": "parts",
                        "pdf_total": invoice["total"],
                        "kimco_bill_id": created_id,
                        "batch": BATCH_NAME,
                        "batch_id": batch_id,
                        "transfer_ap": "no",
                        "receipts_selected": receipt_label(receipt_rows),
                        "ppv": ppv_amount or 0.0,
                        "comments_1_id": comment.get("id"),
                        "email_moved": "no",
                        "note": note,
                        "message_id": invoice["message_id"],
                        "source": invoice["source"],
                        "attached": True,
                        "select_status": select_status,
                        "fee_status": fee_status,
                        "gap": gap,
                    }
                )
                print(f"SUCCESS {number} id={created_id} ppv={ppv_amount} gap={gap}", flush=True)
                continue
            if gap is not None and abs(float(gap)) >= PPV_LIMIT:
                client.try_deselect_receipts(created_id)
                plan = {
                    "action": "price_variance",
                    "problems": [
                        {
                            "kind": "price_variance",
                            "line": invoice["po"],
                            "part": "",
                            "desc": "",
                            "ordered": None,
                            "invoiced": None,
                            "received": None,
                            "receipts": [],
                            "gap": gap,
                        }
                    ],
                    "merch_gap": gap,
                }
            else:
                note = (
                    f"AP Clerk: Fastenal invoice {number} was created as KIMCO {created_id} "
                    f"but was not finished. PDF total {dollar(invoice['total'])}. "
                    f"Select Receipts status was {select_status}. Fee status was {fee_status}. "
                    f"Live gap is {gap}. The bill is not posted and was left on {BATCH_NAME}."
                )
                comment = write_comment(client, created_id, note, mention=False)
                results.append(
                    {
                        "invoice_number": number,
                        "status": "HOLD",
                        "reason": f"select={select_status} fee={fee_status} gap={gap}",
                        "type": "parts",
                        "pdf_total": invoice["total"],
                        "kimco_bill_id": created_id,
                        "batch": BATCH_NAME,
                        "batch_id": batch_id,
                        "transfer_ap": "no",
                        "receipts_selected": receipt_label(receipt_rows),
                        "ppv": ppv_amount,
                        "comments_1_id": comment.get("id"),
                        "email_moved": "no",
                        "note": note,
                        "message_id": invoice["message_id"],
                        "source": invoice["source"],
                        "attached": True,
                        "gap": gap,
                    }
                )
                print(f"HOLD-OPEN {number} id={created_id} gap={gap}", flush=True)
                continue
        if plan["action"] == "select":
            note = (
                f"AP Clerk: Fastenal invoice {number} header {created_id} was created "
                f"but the PDF attach status was {attach}. No receipts were selected. "
                f"The bill is not posted and was left on {BATCH_NAME}."
            )
            comment = write_comment(client, created_id, note, mention=False)
            results.append(
                {
                    "invoice_number": number,
                    "status": "HOLD",
                    "reason": f"attach={attach}",
                    "type": "parts",
                    "pdf_total": invoice["total"],
                    "kimco_bill_id": created_id,
                    "batch": BATCH_NAME,
                    "batch_id": batch_id,
                    "transfer_ap": "no",
                    "receipts_selected": "",
                    "ppv": None,
                    "comments_1_id": comment.get("id"),
                    "email_moved": "no",
                    "note": note,
                    "message_id": invoice["message_id"],
                    "source": invoice["source"],
                    "attached": False,
                }
            )
            continue
        live = snapshot_amounts(client, created_id)
        entered_lines = 0 if not live.get("line_count") else live["amount"]
        note = build_note(
            invoice,
            status="HOLD",
            action=plan["action"],
            problems=plan.get("problems") or [],
            amount_entered=entered_lines or 0,
            gap=plan.get("merch_gap"),
        )
        comment = write_comment(client, created_id, note, mention=True)
        moved = {"status": "not-moved"}
        if comment.get("status") == "persisted":
            moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
            live = snapshot_amounts(client, created_id)
        on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
        results.append(
            {
                "invoice_number": number,
                "status": "HOLD",
                "reason": plan["action"],
                "type": "parts",
                "pdf_total": invoice["total"],
                "kimco_bill_id": created_id,
                "batch": moved.get("batch_name") or BATCH_NAME,
                "batch_id": live.get("batch_id"),
                "transfer_ap": "yes" if on_transfer else "no",
                "receipts_selected": receipt_label(live.get("receipt_rows") or []),
                "ppv": None,
                "comments_1_id": comment.get("id"),
                "email_moved": "no",
                "note": note,
                "message_id": invoice["message_id"],
                "source": invoice["source"],
                "attached": attach == "attached",
                "transfer_status": moved.get("status"),
                "gap": live["qc"].get("gap"),
            }
        )
        print(
            f"HOLD {number} id={created_id} {plan['action']} transfer={moved.get('status')}",
            flush=True,
        )

    _finish_mail(graph, results)
    payload = {
        "run": "fastenal-five-2026-09-25",
        "batch_name": BATCH_NAME,
        "batch_id": batch_id,
        "posted": False,
        "packing_slip_gate": "suspended",
        "quantity_field": "Quantity_Received",
        "invoices": [
            {
                "invoice_number": row["invoice_number"],
                "status": row["status"],
                "reason": row["reason"],
                "type": row["type"],
                "pdf_total": row["pdf_total"],
                "kimco_bill_id": row["kimco_bill_id"],
                "batch": row["batch"],
                "transfer_ap": row["transfer_ap"],
                "receipts_selected": row["receipts_selected"],
                "ppv": row["ppv"],
                "comments_1_id": row["comments_1_id"],
                "email_moved": row["email_moved"],
                "note": row["note"],
            }
            for row in results
        ],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2))
    print(f"WROTE {OUT_JSON}", flush=True)
    return payload


def _finish_mail(graph: Any, results: list[dict[str, Any]]) -> None:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        by_source.setdefault(row["source"], []).append(row)
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(folder.get("id") or "")
    for source, rows in by_source.items():
        created = [row for row in rows if row.get("attached") and row.get("kimco_bill_id")]
        if not created:
            continue
        if all(row["status"] == "Success" for row in rows):
            flag = graph.flag_matched
        else:
            flag = graph.flag_issues
        message_id = rows[0]["message_id"]
        flag(ALLOWED_MAILBOX, message_id)
        moved = graph.move_message(ALLOWED_MAILBOX, message_id, fort_id)
        new_id = str(moved.get("new_id") or message_id)
        after = graph.get_message(
            ALLOWED_MAILBOX,
            new_id,
            select="id,subject,parentFolderId,categories",
        )
        in_folder = str(after.get("parentFolderId") or "") == fort_id
        for row in rows:
            row["email_moved"] = "yes" if in_folder else "no"
            row["email_folder"] = folder.get("displayName") if in_folder else ""
            row["email_categories"] = after.get("categories")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--dry" in args:
        enter_live(write=False)
        return 0
    if "--live" not in args:
        print("Refusing to write. Re-run with --live.", flush=True)
        return 2
    enter_live(write=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
