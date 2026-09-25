"""Enter Crosslink Powder Coating invoices on live KIMCO. Never posts the bill.

Batch name: API Agent - 9/25/26 Crosslink.
PO bills are type 3. Receipts are chosen by Quantity_Received.
Quantity_Remaining is ignored. Packing-slip gate is suspended.
Powder coat is outside processing, so receipts may sit on a work-order
PO line. Select Receipts copies that Work_Order id onto the AP line.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoError
from ap_clerk.pdf_invoice import extract_crosslink_bill
from ap_clerk.rules import kimco_datetime, lookup_id, lookup_text

BATCH_NAME = "API Agent - 9/25/26 Crosslink"
VENDOR_SAMPLE_ID = 10101
VENDOR_ID = 278
PPV_LIMIT = 75.0
OUT_JSON = ROOT / "artifacts" / "crosslink-2026-09-25.json"
CHICAGO = ZoneInfo("America/Chicago")
WINDOW_START = datetime(2026, 9, 4, tzinfo=CHICAGO)
WINDOW_END = datetime(2026, 9, 26, tzinfo=CHICAGO)

_HINT = re.compile(r"crosslink", re.I)
_STATEMENT = re.compile(r"statement of account|account statement|^\s*statement\s*$", re.I | re.M)
_REMINDER = re.compile(r"friendly payment reminder|past due", re.I)
_CREDIT = re.compile(r"credit memo|credit number|credit no\b", re.I)
_SH_NUM = re.compile(r"Invoice no\.:\s*SH:(\d+)", re.I)
_SH_PO = re.compile(r"P\.O\. Number:\s*(\d{5})")
_SH_DATE = re.compile(r"Invoice date:\s*(\d{2}/\d{2}/\d{4})", re.I)
_SH_DUE = re.compile(r"Due date:\s*(\d{2}/\d{2}/\d{4})", re.I)
_SH_TOTAL = re.compile(r"\bTotal\s+\$\s*([\d,]+\.\d{2})")
_SH_ROW = re.compile(r"(?m)^\s*\d+\.\s+(?P<part>.+?)\s{2,}(?P<rest>.+)$")
_SH_MONEY = re.compile(
    r"(?P<qty>[\d,]+\.?\d*)\s+\$(?P<rate>[\d,]+\.\d{2})\s+\$(?P<amt>[\d,]+\.\d{2})\s*$"
)
_TOTAL = re.compile(r"TOTAL:\s*\$\s*([\d,]+\.\d{2})", re.S)
_PART = re.compile(r"(\d{6,8}-\d+)")
_PO_LINE = re.compile(r"PO(\d{5})-(\d+)")
_FEE_NAME = "Packaging/Shop Supplies Recovery"

SHAWN_MENTION_HTML = (
    '<span data-mention-id="104" data-mention-name="Shawn McKibben" '
    'data-mention-email="Shawn.McKibben@kannonmfg.com" '
    'class="prosemirror-mention-node">@Shawn McKibben</span>'
)


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(",", "").replace("$", "")), 2)
    except (TypeError, ValueError):
        return None


def qty_num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(",", "")), 4)
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


def _parse_date(value: str) -> date:
    month, day, year = value.split("/")
    year_n = int(year)
    if year_n < 100:
        year_n += 2000
    return date(year_n, int(month), int(day))


def _part_token(text: str) -> str:
    match = _PART.search(text or "")
    if match:
        return match.group(1)
    if re.search(r"customer\s+touchup", text or "", re.I):
        return "Customer Touchup"
    return ""


def layout_pages(pdf_bytes: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for index in range(len(reader.pages)):
        proc = subprocess.run(
            ["pdftotext", "-layout", "-f", str(index + 1), "-l", str(index + 1), "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("pdftotext failed")
        pages.append(proc.stdout.decode("utf-8", errors="replace"))
    return pages


def _invoice_number_on_page(text: str) -> str:
    sh = _SH_NUM.search(text or "")
    if sh:
        return sh.group(1)
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"INVOICE\s*#", line, re.I):
            continue
        for nxt in lines[index : index + 8]:
            match = re.fullmatch(r"\s*(\d{5})\s*", nxt)
            if match:
                return match.group(1)
    return ""


def _date_near(text: str, label: str) -> date | None:
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if not re.search(label, line, re.I):
            continue
        same = re.search(r"(\d{2}/\d{2}/\d{4})", line)
        if same:
            return _parse_date(same.group(1))
        for nxt in lines[index : index + 6]:
            match = re.search(r"(\d{2}/\d{2}/\d{4})", nxt)
            if match:
                return _parse_date(match.group(1))
    return None


def _last_total(text: str) -> float | None:
    found = [money(raw) for raw in _TOTAL.findall(text or "")]
    found = [value for value in found if value is not None]
    if found:
        return found[-1]
    sh = _SH_TOTAL.search(text or "")
    return money(sh.group(1)) if sh else None


def parse_sh_layout(text: str) -> dict[str, Any] | None:
    number = _SH_NUM.search(text or "")
    total = _last_total(text or "")
    bill_date = _date_near(text or "", r"Invoice date:")
    if not number or total is None or bill_date is None:
        return None
    due = _date_near(text or "", r"Due date:") or (bill_date + timedelta(days=30))
    po_m = _SH_PO.search(text or "")
    lines: list[dict[str, Any]] = []
    fees: list[dict[str, Any]] = []
    for match in _SH_ROW.finditer(text or ""):
        part = re.sub(r"\s+", " ", match.group("part")).strip()
        money_m = _SH_MONEY.search(match.group("rest"))
        if not money_m:
            continue
        qty = qty_num(money_m.group("qty"))
        unit = money(money_m.group("rate"))
        amount = money(money_m.group("amt"))
        if qty is None or unit is None or amount is None:
            continue
        if re.search(r"packaging|shop supplies", part, re.I):
            fees.append({"name": _FEE_NAME, "amount": amount, "fee": True})
            continue
        lines.append(
            {
                "part": _part_token(part) or part,
                "qty": qty,
                "unit_price": unit,
                "amount": amount,
                "po": po_m.group(1) if po_m else "",
            }
        )
    if not lines:
        return None
    subtotal = round(sum(float(line["amount"]) for line in lines), 2)
    fee_total = round(sum(float(fee["amount"]) for fee in fees), 2)
    if round(subtotal + fee_total - total, 2) != 0:
        return None
    return {
        "invoice_number": number.group(1),
        "printed_number": f"SH:{number.group(1)}",
        "po": po_m.group(1) if po_m else "",
        "date": bill_date,
        "due": due,
        "total": total,
        "subtotal": subtotal,
        "fees": fees,
        "lines": lines,
        "type": "parts" if po_m else "misc",
    }


def parse_generator_layout(text: str) -> dict[str, Any] | None:
    number = _invoice_number_on_page(text or "")
    total = _last_total(text or "")
    bill_date = _date_near(text or "", r"\bDATE\b")
    if not number or total is None or bill_date is None:
        return None
    raw_lines, fees = extract_crosslink_bill(text or "")
    lines = []
    for row in raw_lines:
        if row.get("qty") is None or row.get("amount") is None:
            continue
        lines.append(
            {
                "part": row.get("part") or "",
                "qty": qty_num(row.get("qty")),
                "unit_price": money(row.get("unit_price")),
                "amount": money(row.get("amount")),
                "po": str(row.get("po") or ""),
            }
        )
    if not lines:
        return None
    subtotal = round(sum(float(line["amount"]) for line in lines), 2)
    fee_total = round(sum(float(fee["amount"]) for fee in fees), 2)
    if round(subtotal + fee_total - total, 2) != 0:
        return None
    po = ""
    for line in lines:
        head = re.match(r"(\d{5})", str(line.get("po") or ""))
        if head:
            po = head.group(1)
            break
    return {
        "invoice_number": number,
        "printed_number": number,
        "po": po,
        "date": bill_date,
        "due": bill_date + timedelta(days=30),
        "total": total,
        "subtotal": subtotal,
        "fees": fees,
        "lines": lines,
        "type": "parts" if po else "misc",
    }


def parse_crosslink_layout(text: str) -> dict[str, Any] | None:
    if _SH_NUM.search(text or ""):
        return parse_sh_layout(text)
    return parse_generator_layout(text)


def parse_crosslink_pages(pages: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One bill per invoice number. A later page with only the total stays with it."""
    groups: list[dict[str, Any]] = []
    credits: list[dict[str, Any]] = []
    for page in pages:
        if _STATEMENT.search(page) and not _invoice_number_on_page(page) and not _SH_NUM.search(page):
            continue
        number = _invoice_number_on_page(page)
        if _CREDIT.search(page) and not number:
            credits.append(
                {
                    "kind": "credit_memo",
                    "invoice_number": "",
                    "amount": _last_total(page),
                    "reason": "Credit memo ignored. It is not a new invoice.",
                }
            )
            continue
        if not number:
            if groups:
                groups[-1]["pages"].append(page)
            continue
        if groups and groups[-1]["number"] == number:
            groups[-1]["pages"].append(page)
        else:
            groups.append({"number": number, "pages": [page]})
    bills = []
    for group in groups:
        bill = parse_crosslink_layout("\n".join(group["pages"]))
        if bill:
            bill["page_indexes"] = []
            bills.append(bill)
    return bills, credits


def receipt_extension(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        qty = qty_num(row.get("qty_received"))
        price = qty_num(row.get("unit"))
        if qty is None or price is None:
            continue
        total = round(total + round(qty * price, 2), 2)
    return total


def open_receipts(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unbilled receipts. Quantity_Remaining is never a reason to drop a row."""
    chosen = []
    for row in receipts:
        if row.get("invoiced") is True:
            continue
        ap = str(row.get("ap") or "").strip()
        if ap and ap not in {"None", "null"}:
            continue
        if qty_num(row.get("qty_received")) in (None, 0, 0.0):
            continue
        chosen.append(row)
    return chosen


def _subset_for_qty(rows: list[dict[str, Any]], wanted: float) -> list[dict[str, Any]] | None:
    """Receipts whose Quantity_Received sums to the invoice qty. Same unit cost."""
    if not rows:
        return None
    units = {qty_num(row.get("unit")) for row in rows}
    if len(units) != 1:
        return None
    indexed = list(enumerate(rows))
    for size in range(1, len(indexed) + 1):
        for combo in combinations(indexed, size):
            total = round(sum(float(qty_num(row.get("qty_received")) or 0) for _, row in combo), 4)
            if abs(total - wanted) <= 0.001:
                return [row for _, row in combo]
    return None


def choose_receipts(
    invoice: dict[str, Any],
    po_lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match each powder-coat line to open receipts on that part.

    Quantity_Received is the quantity. A zero Quantity_Received, or no open
    receipt on the part, is a missing receipt. A dollar gap of $75 or more
    is not selected. A quantity mismatch is a hold only when that gap is
    at least $75.
    """
    po = str(invoice.get("po") or "")
    fee_total = round(sum(float(fee.get("amount") or 0) for fee in invoice.get("fees") or []), 2)
    if not po:
        return {
            "action": "missing_po",
            "receipts": [],
            "considered": [],
            "lines": [],
            "line_details": [],
            "problems": [{"kind": "missing_po", "line": "", "part": "", "ordered": None, "invoiced": None, "received": None}],
            "merch_gap": None,
            "received_ext": 0.0,
            "fee_total": fee_total,
            "qty_difference": False,
        }
    details = []
    chosen: list[dict[str, Any]] = []
    matched_lines: list[dict[str, Any]] = []
    missing = False
    qty_difference = False
    po_rows = [row for row in po_lines if str(row.get("po") or "") == po]
    open_rows = [row for row in open_receipts(receipts) if str(row.get("po") or "") == po]
    for inv_line in invoice.get("lines") or []:
        part = str(inv_line.get("part") or "")
        candidates = [row for row in po_rows if str(row.get("part") or "") == part]
        if not candidates and part == "Customer Touchup":
            candidates = [row for row in po_rows if "touchup" in str(row.get("wo") or "").lower()]
        names = {str(row.get("line") or "") for row in candidates}
        recs = [row for row in open_rows if str(row.get("line") or "") in names]
        invoiced = qty_num(inv_line.get("qty"))
        ordered = round(sum(float(qty_num(row.get("ordered")) or 0) for row in candidates), 4)
        received = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in recs), 4)
        take = recs
        if invoiced is not None and recs:
            exact = _subset_for_qty(recs, invoiced)
            if exact:
                take = exact
                received = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in take), 4)
        if invoiced is not None and abs(received - invoiced) > 0.001:
            qty_difference = True
        detail = {
            "part": part,
            "line": ", ".join(str(row.get("line") or "") for row in candidates),
            "ordered": ordered,
            "invoiced": invoiced,
            "invoice_unit": money(inv_line.get("unit_price")),
            "invoice_amount": money(inv_line.get("amount")),
            "po_unit": qty_num(candidates[0].get("unit")) if candidates else None,
            "received": received if recs else 0,
            "receipts": take,
            "po_lines": candidates,
        }
        details.append(detail)
        if not candidates or not recs:
            missing = True
            continue
        matched_lines.extend(candidates)
        chosen.extend(take)
    received_ext = receipt_extension(chosen)
    gap = None if missing else round(float(invoice["total"]) - received_ext - fee_total, 2)
    if missing:
        action = "missing_receipt"
        selected: list[dict[str, Any]] = []
    elif gap is not None and abs(gap) >= PPV_LIMIT:
        action = "quantity_variance" if qty_difference else "price_variance"
        selected = []
    else:
        action = "select"
        selected = chosen
    return {
        "action": action,
        "receipts": selected,
        "considered": chosen,
        "lines": matched_lines,
        "line_details": details,
        "problems": details,
        "merch_gap": gap,
        "received_ext": received_ext,
        "fee_total": fee_total,
        "qty_difference": qty_difference,
    }


def receipt_label(rows: list[dict[str, Any]]) -> str:
    bits = []
    for row in rows:
        qty = row.get("qty_received") if "qty_received" in row else row.get("qty")
        price = row.get("unit") if row.get("unit") not in (None, "") else row.get("price")
        bits.append(f"{row.get('id')} qty {qty_text(qty_num(qty))} @ {price_text(qty_num(price))}")
    return "; ".join(bits)


def _line_sentence(detail: dict[str, Any]) -> str:
    part = detail.get("part") or "the part"
    line = detail.get("line") or "the PO line"
    ordered = qty_text(detail.get("ordered"))
    invoiced = qty_text(detail.get("invoiced"))
    received = qty_text(detail.get("received"))
    po_unit = dollar(detail.get("po_unit"))
    inv_unit = dollar(detail.get("invoice_unit"))
    inv_amt = dollar(detail.get("invoice_amount"))
    recs = detail.get("receipts") or []
    if recs:
        rec_bit = " Receipts by Quantity_Received: " + receipt_label(recs) + "."
    else:
        rec_bit = " There is no open receipt on that part."
    return (
        f"PO line {line} part {part} was ordered {ordered} at {po_unit} each. "
        f"The invoice bills {invoiced} at {inv_unit} each, which is {inv_amt}. "
        f"Quantity_Received is {received}.{rec_bit}"
    )


def build_note(
    invoice: dict[str, Any],
    *,
    status: str,
    action: str,
    receipt_rows: list[dict[str, Any]] | None = None,
    amount_entered: float | None = None,
    ppv: float | None = None,
    gap: float | None = None,
    line_details: list[dict[str, Any]] | None = None,
    fee_total: float | None = None,
    received_ext: float | None = None,
) -> str:
    """Plain-English note. Success notes stay in the artifact and are not posted."""
    number = invoice["invoice_number"]
    total = float(invoice["total"])
    po = invoice.get("po") or "none"
    fees = fee_total
    if fees is None:
        fees = round(sum(float(fee.get("amount") or 0) for fee in invoice.get("fees") or []), 2)
    if status == "Success":
        label = receipt_label(receipt_rows or [])
        if ppv not in (None, 0, 0.0):
            ppv_bit = (
                f"The receipt lines plus the packaging fee missed the PDF total by {dollar(gap if gap is not None else ppv)}, "
                f"which is under {dollar(PPV_LIMIT)}, so one signed Purchase Price Variance of {dollar(ppv)} "
                f"was posted. After that charge the gap is {dollar(0)}."
            )
        else:
            ppv_bit = (
                "The selected receipt lines and the packaging fee equal the PDF total, so the gap is $0.00 "
                "and no Purchase Price Variance was posted."
            )
        fee_bit = (
            f"The packaging and shop supplies recovery fee is {dollar(fees)}. "
            if fees
            else "The PDF has no packaging fee. "
        )
        text = (
            f"AP Clerk: Crosslink Powder Coating invoice {number} is entered as an outside-processing "
            f"bill on PO {po} and is not posted. The PDF total is {dollar(total)}. "
            f"Amount entered is {dollar(amount_entered if amount_entered is not None else total)}. "
            f"{fee_bit}Receipts selected: {label}. {ppv_bit} Nothing is waiting on purchasing."
        )
    else:
        sentences = " ".join(_line_sentence(detail) for detail in (line_details or []))
        fee_bit = (
            f"The PDF also has a packaging and shop supplies recovery fee of {dollar(fees)}. "
            if fees
            else "The PDF has no packaging fee. "
        )
        entered = dollar(amount_entered if amount_entered is not None else 0)
        if action == "missing_receipt":
            why = (
                "This cannot be finished because a line has no selectable receipt. "
                "Quantity_Received is 0 or there is no open receipt on the matching part, "
                "so this is a missing receipt and not a price variance. "
                "The work-order and outside-service lines on the PO were checked."
            )
            ask = (
                f"Shawn should receive the invoiced quantity on PO {po}. "
                f"After that receipt exists, AP will select it by Quantity_Received, "
                f"copy the work order id from the receipt onto the bill line, and finish "
                f"the bill if the header matches the PDF total of {dollar(total)} to the penny."
            )
        elif action == "price_variance":
            why = (
                f"Invoice total minus the receipt lines and that fee is {dollar(gap)}, "
                f"which is {dollar(PPV_LIMIT)} or more, so no Purchase Price Variance was posted "
                "and the receipts were not selected. Selecting them would lock the receipt and block an unreceive. "
                "The quantity matches, so this is a price difference, not a missing receipt."
            )
            ask = (
                "Shawn should unreceive the line, set the PO price to the invoice price, "
                "and re-receive the same quantity. After that, AP will select the receipts and finish the bill."
            )
        elif action == "quantity_variance":
            why = (
                f"Quantity received does not match quantity invoiced, and the dollar gap of "
                f"{dollar(gap)} is {dollar(PPV_LIMIT)} or more. "
                "That is a quantity or unit disconnect, not a price variance under the limit. "
                "No receipts were selected."
            )
            ask = (
                "Shawn should correct the receipt so Quantity_Received matches the invoiced "
                "quantity, then tell AP to select that receipt and finish."
            )
        else:
            why = "This bill cannot be finished from the PDF and the open receipts."
            ask = "Shawn should review the PO and the receipt, then tell AP what to select."
        ext_bit = ""
        if received_ext is not None and action != "missing_receipt":
            ext_bit = f"Those receipt lines extend to {dollar(received_ext)}. "
        text = (
            f"AP Clerk: @Shawn McKibben Crosslink Powder Coating invoice {number} cannot be finished. "
            f"The PDF total is {dollar(total)}. Amount entered on receipt lines is {entered}. "
            f"PO {po}. {sentences} {ext_bit}{fee_bit}{why} {ask} "
            f"The bill was moved to Transfer AP and is not posted."
        )
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if "category=" in text or "owner=" in text or "<" in text or ">" in text:
        raise RuntimeError("note must be plain text")
    return re.sub(r"\s+", " ", text).strip()


def html_with_shawn_mention(text: str) -> str:
    if "@Shawn McKibben" not in text:
        raise RuntimeError("hold note must name @Shawn McKibben")
    body = text.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    return f"<p>{body}</p>"


def comment_values(kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    return {
        "HtmlValue": html_with_shawn_mention(text) if mention else text,
        "Entity": {"id": 203},
        "ObjectId": int(kimco_id),
        "FormId": 218,
    }


def _po_token(value: Any) -> str:
    text = lookup_text(value) or str(value or "")
    match = re.search(r"PO(\d{5})", text)
    return match.group(1) if match else ""


def po_line_from_record(item: dict[str, Any], wanted: set[str]) -> dict[str, Any] | None:
    values = item.get("values") or {}
    po = _po_token(values.get("Purchase_Order_Number"))
    display = str(values.get("Display_Name") or "")
    if display:
        match = _PO_LINE.search(display)
        if match:
            po = po or match.group(1)
    if po not in wanted:
        return None
    wo = lookup_text(values.get("Work_Order_Number")) or ""
    desc = str(values.get("PO_Item_Description") or values.get("Part_Description") or "")
    return {
        "id": item.get("id"),
        "po": po,
        "po_id": lookup_id(values.get("Purchase_Order_Number")),
        "line": display,
        "ordered": qty_num(values.get("Quantity")),
        "unit": qty_num(values.get("Unit_Price")),
        "part": _part_token(f"{wo} {desc}"),
        "wo": wo,
        "desc": desc,
    }


def receipt_from_record(item: dict[str, Any], wanted: set[str]) -> dict[str, Any] | None:
    values = item.get("values") or {}
    line = lookup_text(values.get("PO_Item_Number")) or ""
    match = _PO_LINE.search(line)
    if not match or match.group(1) not in wanted:
        return None
    ap = values.get("AP_Invoice_Number")
    ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "")
    if ap_text in {"None", "null"}:
        ap_text = ""
    wo = values.get("Work_Order_Issue") or values.get("Work_Order")
    wo_text = lookup_text(wo) if isinstance(wo, dict) else str(wo or "")
    return {
        "id": item.get("id"),
        "po": match.group(1),
        "line": line,
        "qty_received": qty_num(values.get("Quantity_Received")),
        "qty_remaining": qty_num(values.get("Quantity_Remaining")),
        "unit": qty_num(values.get("PO_Item_Number_$_Unit_Price")),
        "ap": ap_text,
        "invoiced": values.get("Invoiced"),
        "part": _part_token(wo_text),
        "wo": wo_text,
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
    kept: dict[str, int] = {}
    for number, kimco_id in found.items():
        record = client.get_item("ap_invoices", kimco_id)
        values = record.get("values") or {}
        vendor_id = lookup_id(values.get("Vendor"))
        vendor = lookup_text(values.get("Vendor")) or ""
        po = lookup_text(values.get("Purchase_Order")) or ""
        if vendor_id == VENDOR_ID or "crosslink" in vendor.lower() or "crosslink" in po.lower():
            if values.get("Void") is True:
                continue
            kept[number] = kimco_id
    return kept


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
            blob = json.dumps(item.get("values") or {})
            if not any(po in blob for po in wanted):
                continue
            row = po_line_from_record(item, wanted)
            if row:
                rows.append(row)
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
            part = lookup_text((item.get("values") or {}).get("PO_Item_Number")) or ""
            if any(po in part for po in wanted):
                ids.append(int(item["id"]))
        if not items:
            break
        offset += len(items)
    rows = []
    for rid in ids:
        rec = receipt_from_record(client.get_item("receipts", rid), wanted)
        if rec:
            rows.append(rec)
    return rows


def slice_pdf(pdf_bytes: bytes, indexes: list[int]) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    for index in indexes:
        writer.add_page(reader.pages[index])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def snapshot_amounts(client: Any, kimco_id: int) -> dict[str, Any]:
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
                "qty_received": qty_num(lv.get("Quantity")),
                "unit": lv.get("Unit_Price"),
                "line": lookup_text(lv.get("Purchase_Order_Line")),
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
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch_name": lookup_text(values.get("AP_Invoice_Batch")),
        "type": values.get("Invoice_Type"),
        "qc": qc,
        "receipt_rows": receipt_rows,
        "comments": comments,
        "line_count": len(lists.get("APInvoiceLine") or []),
        "ppv_amounts": ppv_amounts,
    }


def write_comment(client: Any, kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {"Comments_1": [{"state": "Added", "values": comment_values(kimco_id, text, mention=mention)}]},
    }
    try:
        _body, status, error = client.update("ap_invoices", kimco_id, payload)
    except KimcoError as exc:
        return {"status": "blocked", "error": str(exc)[:200], "mention": False, "id": None}
    live = snapshot_amounts(client, kimco_id)
    match = None
    mention_saved = False
    needle = text.split("@Shawn McKibben", 1)[-1][:60].strip()
    for comment in live["comments"]:
        html = str(comment.get("html") or "").replace("&amp;", "&")
        tagged = 'data-mention-id="104"' in html and needle[:40] in html
        if tagged or (not mention and text[:80] in html):
            match = comment.get("id")
            mention_saved = tagged
            if tagged or not mention:
                break
    return {
        "status": "persisted" if status < 400 and match else f"put-{status}",
        "put": status,
        "error": error,
        "id": match,
        "mention": mention_saved,
    }


def _utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chicago_day(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(CHICAGO).date().isoformat()


def scan_mailbox(graph: Any) -> dict[str, Any]:
    folders: dict[str, dict[str, Any]] = {}

    def walk(url: str, parent_name: str | None) -> None:
        first = True
        while url:
            kwargs: dict[str, Any] = {}
            if first:
                kwargs["params"] = {"$select": "id,displayName,parentFolderId,childFolderCount", "$top": 100}
                first = False
            response = graph.request("GET", url, **kwargs)
            if response.status_code != 200:
                raise RuntimeError(f"folder list HTTP {response.status_code}")
            payload = response.json() or {}
            for item in payload.get("value") or []:
                fid = str(item.get("id") or "")
                name = str(item.get("displayName") or "")
                folders[fid] = {"name": name, "parent": parent_name}
                if int(item.get("childFolderCount") or 0) > 0 and fid:
                    walk(graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{fid}/childFolders"), name)
            url = payload.get("@odata.nextLink")

    walk(graph._user_url(ALLOWED_MAILBOX, "mailFolders"), None)
    filt = f"receivedDateTime ge {_utc(WINDOW_START)} and receivedDateTime lt {_utc(WINDOW_END)}"
    params = {
        "$select": "id,subject,from,receivedDateTime,hasAttachments,parentFolderId,categories,bodyPreview",
        "$filter": filt,
        "$orderby": "receivedDateTime asc",
        "$top": 100,
    }
    messages: list[dict[str, Any]] = []
    url: str | None = graph._messages_url(ALLOWED_MAILBOX)
    first = True
    while url:
        response = graph.request("GET", url, **({"params": params} if first else {}))
        first = False
        if response.status_code != 200:
            raise RuntimeError(f"message list HTTP {response.status_code}")
        payload = response.json() or {}
        messages.extend(payload.get("value") or [])
        url = payload.get("@odata.nextLink")
    by_folder: dict[str, int] = {}
    hits = []
    for msg in messages:
        folder = folders.get(str(msg.get("parentFolderId") or ""), {"name": "unknown", "parent": None})
        key = f"{folder.get('parent') or ''}/{folder.get('name')}"
        by_folder[key] = by_folder.get(key, 0) + 1
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        addr = str(frm.get("address") or "")
        name = str(frm.get("name") or "")
        subject = str(msg.get("subject") or "")
        preview = str(msg.get("bodyPreview") or "")
        if not _HINT.search(f"{addr} {name} {subject} {preview}"):
            continue
        hits.append(
            {
                "id": msg.get("id"),
                "subject": subject,
                "from": addr,
                "from_name": name,
                "received": msg.get("receivedDateTime"),
                "received_chicago": _chicago_day(str(msg.get("receivedDateTime") or "")),
                "folder": folder.get("name"),
                "parent": folder.get("parent"),
                "has_attachments": bool(msg.get("hasAttachments")),
            }
        )
    return {
        "folder_count": len(folders),
        "messages_in_window": len(messages),
        "by_folder": by_folder,
        "crosslink_messages": hits,
    }


def collect_invoices(graph: Any, scan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    invoices: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pdfs: dict[str, bytes] = {}
    seen: dict[str, dict[str, Any]] = {}
    for msg in scan["crosslink_messages"]:
        subject = msg["subject"]
        if _STATEMENT.search(subject) or _REMINDER.search(subject) and not msg["has_attachments"]:
            kind = "statement" if _STATEMENT.search(subject) else "reminder"
            skipped.append(
                {
                    "kind": kind,
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "Account statement is not an invoice." if kind == "statement" else "Payment reminder is not a new invoice.",
                }
            )
            continue
        if not msg["has_attachments"]:
            skipped.append(
                {
                    "kind": "no_pdf",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "Crosslink email has no PDF attachment, so it is not a new invoice.",
                }
            )
            continue
        attachments = graph.download_pdf_attachments(ALLOWED_MAILBOX, msg["id"])
        if not attachments:
            skipped.append(
                {
                    "kind": "no_pdf",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "reason": "Crosslink email has no PDF attachment, so it is not a new invoice.",
                }
            )
            continue
        for name, content in attachments:
            pages = layout_pages(content)
            joined = "\n".join(pages)
            if _STATEMENT.search(joined) and not _invoice_number_on_page(joined) and not _SH_NUM.search(joined):
                skipped.append(
                    {
                        "kind": "statement",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "reason": "Account statement is not an invoice.",
                    }
                )
                continue
            page_bills, credits = parse_crosslink_pages(pages)
            for credit in credits:
                skipped.append(
                    {
                        **credit,
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "message_id": msg["id"],
                    }
                )
            if not page_bills and not credits:
                skipped.append(
                    {
                        "kind": "unparsed",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "reason": "PDF pages did not contain a Crosslink invoice whose lines add up to the total.",
                    }
                )
                continue
            # Page indexes: continuation pages follow the invoice that owns them.
            cursor = 0
            owned: dict[str, list[int]] = {}
            current = None
            for index, page in enumerate(pages):
                number = _invoice_number_on_page(page)
                if number:
                    current = number
                    owned.setdefault(number, []).append(index)
                elif current:
                    owned.setdefault(current, []).append(index)
                cursor = index
            del cursor
            for bill in page_bills:
                number = bill["invoice_number"]
                bill["message_id"] = msg["id"]
                bill["subject"] = subject
                bill["email_received"] = msg["received_chicago"]
                bill["email_received_utc"] = msg["received"]
                bill["folder"] = msg["folder"]
                bill["pdf_name"] = name
                bill["page_count"] = len(pages)
                bill["page_indexes"] = owned.get(number) or list(range(len(pages)))
                prior = seen.get(number)
                if prior:
                    dates = list(prior.get("email_received_dates") or [prior["email_received"]])
                    if bill["email_received"] not in dates:
                        dates.append(bill["email_received"])
                    prior["email_received_dates"] = dates
                    prior["resends"] = prior.get("resends", 0) + 1
                    continue
                bill["email_received_dates"] = [bill["email_received"]]
                bill["resends"] = 0
                seen[number] = bill
                invoices.append(bill)
                pdfs[number] = content
    invoices.sort(key=lambda row: (row["date"], row["invoice_number"]))
    return invoices, skipped, pdfs


def _row(invoice, **kwargs) -> dict[str, Any]:
    return {
        "invoice_number": invoice["invoice_number"],
        "status": kwargs["status"],
        "reason": kwargs["reason"],
        "type": "parts" if invoice.get("po") else "misc",
        "pdf_total": invoice["total"],
        "kimco_bill_id": kwargs.get("kimco_id"),
        "batch": kwargs.get("batch"),
        "transfer_ap": kwargs.get("transfer_ap", "no"),
        "receipts_selected": kwargs.get("receipts") or "",
        "ppv": kwargs.get("ppv"),
        "comments_1_id": kwargs.get("comments_1_id"),
        "email_moved": "no",
        "note": kwargs.get("note") or "",
        "email_received": invoice.get("email_received_dates") or [invoice.get("email_received")],
        "email_received_utc": invoice.get("email_received_utc"),
        "message_id": invoice.get("message_id"),
        "attached": kwargs.get("attached", False),
        "po": invoice.get("po"),
        "gap": kwargs.get("gap"),
    }


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
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
        "email_received": row["email_received"],
    }


def _create_header(client, invoice, batch_id, vendor_id, terms_id, remit_id, currency_id, po_id, invoice_type):
    payload = {
        "AP_Invoice_Batch": {"id": batch_id},
        "Vendor": {"id": vendor_id},
        "Invoice_Number": invoice["invoice_number"],
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(invoice["date"]),
        "Invoice_Verification_Amount": float(invoice["total"]),
        "Invoice_Due_Date": kimco_datetime(invoice["due"]),
        "Terms_Code": {"id": terms_id},
        "Currency": {"id": currency_id},
        "Remit_To_Address": {"id": remit_id},
        "Transaction_Date": kimco_datetime(invoice["date"]),
        "Comments": "API Agent",
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    return client.create("ap_invoices", payload)


def _enter_hold(client, invoice, plan, batch_id, vendor_id, terms_id, remit_id, currency_id, po_lines, pdfs):
    po_id = next((row.get("po_id") for row in po_lines if str(row.get("po")) == str(invoice.get("po")) and row.get("po_id")), None)
    if invoice.get("po") and not po_id:
        return _row(
            invoice, status="HOLD", reason="PO id missing", kimco_id=None, batch=BATCH_NAME, receipts="",
            note=f"AP Clerk: Crosslink Powder Coating invoice {invoice['invoice_number']} has no PO id in KIMCO.",
        )
    created = _create_header(
        client, invoice, batch_id, vendor_id, terms_id, remit_id, currency_id, int(po_id) if po_id else None,
        invoice_type=3 if invoice.get("po") else 4,
    )
    if created[0] is None:
        return _row(
            invoice, status="HOLD", reason=f"header create HTTP {created[2]}", kimco_id=None,
            batch=BATCH_NAME, receipts="",
            note=f"AP Clerk: Crosslink Powder Coating invoice {invoice['invoice_number']} header was not created.",
        )
    created_id = int(created[0])
    pdf = slice_pdf(pdfs[invoice["invoice_number"]], invoice.get("page_indexes") or [0])
    attach = client.try_official_attach(
        created_id, name=f"{invoice['invoice_number']}.pdf", content_type="application/pdf",
        size=len(pdf), content=pdf,
    )
    note = build_note(
        invoice, status="HOLD", action=plan["action"],
        amount_entered=0, gap=plan.get("merch_gap"), line_details=plan.get("line_details"),
        fee_total=plan.get("fee_total"), received_ext=plan.get("received_ext"),
    )
    mention = plan["action"] in {"missing_receipt", "price_variance", "quantity_variance"}
    comment = write_comment(client, created_id, note, mention=mention)
    from ap_clerk.transfer_ap import apply_transfer_ap_batch_move

    moved = {"status": "not-moved"}
    if mention and comment.get("status") == "persisted" and comment.get("mention") is True:
        moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
    live = snapshot_amounts(client, created_id)
    on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
    print(
        f"HOLD {invoice['invoice_number']} id={created_id} {plan['action']} "
        f"transfer={moved.get('status')} mention={comment.get('mention')} comment={comment.get('id')}",
        flush=True,
    )
    return _row(
        invoice, status="HOLD", reason=plan["action"], kimco_id=created_id,
        batch=moved.get("batch_name") or BATCH_NAME, receipts="",
        note=note, comments_1_id=comment.get("id"), transfer_ap="yes" if on_transfer else "no",
        attached=attach == "attached", gap=live["qc"].get("gap"), ppv=0.0,
    )


def _select_receipts(client, kimco_id: int, receipt_ids: list[int]) -> str:
    """Select by receipt id. try_select_receipts copies Work_Order from the receipt."""
    status = client.try_select_receipts(kimco_id, receipt_ids)
    if status == "selected":
        return status
    live = snapshot_amounts(client, kimco_id)
    have = {int(row["id"]) for row in live["receipt_rows"] if row.get("id") not in (None, "")}
    pending = [rid for rid in receipt_ids if rid not in have]
    if not pending:
        return "selected"
    for rid in pending:
        one = client.try_select_receipts(kimco_id, [rid])
        if one != "selected":
            return one
    return "selected"


def _finish_mail(graph: Any, results: list[dict[str, Any]]) -> None:
    by_message: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        if row.get("message_id") and row.get("status") != "already":
            by_message.setdefault(row["message_id"], []).append(row)
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(folder.get("id") or "")
    for message_id, rows in by_message.items():
        created = [row for row in rows if row.get("attached") and row.get("kimco_bill_id")]
        if not created:
            continue
        flag = graph.flag_matched if all(row["status"] == "Success" for row in rows) else graph.flag_issues
        flag(ALLOWED_MAILBOX, message_id)
        moved = graph.move_message(ALLOWED_MAILBOX, message_id, fort_id)
        new_id = str(moved.get("new_id") or message_id)
        after = graph.get_message(ALLOWED_MAILBOX, new_id, select="id,subject,parentFolderId,categories")
        in_folder = str(after.get("parentFolderId") or "") == fort_id
        for row in rows:
            row["email_moved"] = "yes" if in_folder else "no"
            row["message_id"] = new_id


def already_record(client: Any, number: str, kimco_id: int, invoice: dict[str, Any] | None = None) -> dict[str, Any]:
    live = snapshot_amounts(client, kimco_id)
    record = client.get_item("ap_invoices", kimco_id)
    values = record.get("values") or {}
    row = {
        "invoice_number": number,
        "kimco_bill_id": kimco_id,
        "posted": bool(values.get("Posted")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")) or None,
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "invoice_amount": live.get("amount"),
        "verification_amount": live.get("verification"),
        "left_alone": True,
    }
    if invoice:
        row["email_received"] = invoice.get("email_received_dates") or [invoice.get("email_received")]
        row["pdf_total"] = invoice.get("total")
        row["po"] = invoice.get("po")
    return row


def enter_live(*, write: bool = False) -> dict[str, Any]:
    from ap_clerk.auth import load_credentials, resolve_target
    from ap_clerk.cli import _find_or_create_batch, _optional_graph_client
    from ap_clerk.kimco import KimcoClient
    from ap_clerk.ppv_qc import pre_finish_totals_check, scan_ids

    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph credentials missing")
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error)
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    print("Scanning mailbox", flush=True)
    scan = scan_mailbox(graph)
    print(
        f"Window messages {scan['messages_in_window']} folders {scan['folder_count']} "
        f"Crosslink messages {len(scan['crosslink_messages'])}",
        flush=True,
    )
    invoices, skipped, pdfs = collect_invoices(graph, scan)
    print(f"Invoices {len(invoices)} skipped {len(skipped)}", flush=True)
    for bill in invoices:
        print(
            f"PDF {bill['invoice_number']} PO {bill['po'] or 'none'} total {bill['total']} "
            f"lines {len(bill['lines'])} fees {len(bill['fees'])} received {bill['email_received']}",
            flush=True,
        )
    numbers = {bill["invoice_number"].upper() for bill in invoices}
    print("Checking KIMCO invoice numbers", flush=True)
    existing = find_invoice_ids(client, numbers)
    print(f"Already in KIMCO {sorted(existing)}", flush=True)
    plans = []
    already = []
    pending = []
    for bill in invoices:
        if bill["invoice_number"].upper() in existing:
            kimco_id = existing[bill["invoice_number"].upper()]
            already.append(already_record(client, bill["invoice_number"], kimco_id, bill))
            print(f"ALREADY {bill['invoice_number']} {kimco_id}", flush=True)
            continue
        pending.append(bill)
    wanted = {str(bill["po"]) for bill in pending if bill.get("po")}
    print("Loading PO lines and receipts", flush=True)
    po_lines = load_po_lines(client, wanted) if wanted else []
    receipts = load_receipts(client, wanted) if wanted else []
    print(f"PO lines {len(po_lines)} receipts {len(receipts)}", flush=True)
    for bill in pending:
        fresh = []
        for row in receipts:
            if str(row.get("po")) != str(bill.get("po")):
                continue
            confirmed = receipt_from_record(client.get_item("receipts", int(row["id"])), wanted)
            if confirmed:
                fresh.append(confirmed)
        plan = choose_receipts(bill, po_lines, fresh)
        plan["invoice"] = bill
        plans.append(plan)
        print(
            json.dumps(
                {
                    "invoice": bill["invoice_number"],
                    "po": bill["po"],
                    "total": bill["total"],
                    "action": plan["action"],
                    "gap": plan.get("merch_gap"),
                    "qty_difference": plan.get("qty_difference"),
                    "receipts": receipt_label(plan["receipts"]),
                    "considered": receipt_label(plan["considered"]),
                    "lines": [row.get("line") for row in plan.get("lines") or []],
                }
            ),
            flush=True,
        )
    if not write:
        return {
            "dry": True,
            "scan": {
                "messages_in_window": scan["messages_in_window"],
                "folder_count": scan["folder_count"],
                "by_folder": scan["by_folder"],
                "crosslink_messages": len(scan["crosslink_messages"]),
            },
            "skipped": skipped,
            "already_in_kimco": already,
            "invoices": [
                {
                    "invoice_number": plan["invoice"]["invoice_number"],
                    "action": plan["action"],
                    "po": plan["invoice"]["po"],
                    "pdf_total": plan["invoice"]["total"],
                    "gap": plan.get("merch_gap"),
                    "qty_difference": plan.get("qty_difference"),
                    "receipts_selected": receipt_label(plan["receipts"]),
                    "considered": receipt_label(plan["considered"]),
                    "email_received": plan["invoice"]["email_received"],
                    "note": build_note(
                        plan["invoice"],
                        status="HOLD" if plan["action"] != "select" else "Success",
                        action=plan["action"],
                        receipt_rows=plan["receipts"],
                        amount_entered=plan["invoice"]["total"] if plan["action"] == "select" else 0,
                        ppv=plan.get("merch_gap") if plan["action"] == "select" else 0,
                        gap=plan.get("merch_gap"),
                        line_details=plan.get("line_details"),
                        fee_total=plan.get("fee_total"),
                        received_ext=plan.get("received_ext"),
                    ),
                }
                for plan in plans
            ],
        }

    sample = client.get_item("ap_invoices", VENDOR_SAMPLE_ID)
    sample_values = sample.get("values") or {}
    vendor_id = lookup_id(sample_values.get("Vendor"))
    terms_id = lookup_id(sample_values.get("Terms_Code"))
    remit_id = lookup_id(sample_values.get("Remit_To_Address"))
    currency_id = lookup_id(sample_values.get("Currency"))
    vendor_name = lookup_text(sample_values.get("Vendor")) or ""
    if vendor_id != VENDOR_ID or "CROSSLINK" not in vendor_name.upper() or not terms_id or not remit_id or not currency_id:
        raise SystemExit(f"Crosslink sample {VENDOR_SAMPLE_ID} vendor/terms/remit changed: vendor {vendor_id} {vendor_name}")
    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    batch_id = int(batch["id"])
    print(f"BATCH {batch_id} {BATCH_NAME} created={batch.get('created')}", flush=True)
    results: list[dict[str, Any]] = []
    for plan in plans:
        invoice = plan["invoice"]
        number = invoice["invoice_number"]
        print(f"CHECK {number}", flush=True)
        live_existing = find_invoice_ids(client, {number.upper()})
        if number.upper() in live_existing:
            kimco_id = live_existing[number.upper()]
            already.append(already_record(client, number, kimco_id, invoice))
            print(f"SKIP {number} existing {kimco_id}", flush=True)
            continue
        if plan["action"] != "select":
            results.append(_enter_hold(
                client, invoice, plan, batch_id, vendor_id, terms_id, remit_id, currency_id, po_lines, pdfs
            ))
            continue
        po_id = next((row.get("po_id") for row in plan["lines"] if row.get("po_id")), None)
        if not po_id:
            raise SystemExit(f"PO {invoice['po']} id missing")
        created = _create_header(
            client, invoice, batch_id, vendor_id, terms_id, remit_id, currency_id, int(po_id), invoice_type=3
        )
        if created[0] is None:
            results.append(_row(
                invoice, status="HOLD", reason=f"header create HTTP {created[2]}: {created[3]}",
                kimco_id=None, batch=BATCH_NAME, receipts="",
                note=f"AP Clerk: Crosslink Powder Coating invoice {number} header was not created.",
            ))
            continue
        created_id = int(created[0])
        pdf = slice_pdf(pdfs[number], invoice.get("page_indexes") or [0])
        attach = client.try_official_attach(
            created_id, name=f"{number}.pdf", content_type="application/pdf", size=len(pdf), content=pdf
        )
        if attach != "attached":
            note = (
                f"AP Clerk: Crosslink Powder Coating invoice {number} header {created_id} was created "
                f"but the PDF attach status was {attach}. No receipts were selected. "
                f"The bill is not posted and was left on {BATCH_NAME}."
            )
            comment = write_comment(client, created_id, note, mention=False)
            results.append(_row(
                invoice, status="HOLD", reason=f"attach={attach}", kimco_id=created_id,
                batch=BATCH_NAME, receipts="", note=note, comments_1_id=comment.get("id"), attached=False,
            ))
            continue
        if invoice.get("fees"):
            client.try_post_fees(created_id, invoice["fees"])
        receipt_ids = [int(row["id"]) for row in plan["receipts"]]
        select_status = _select_receipts(client, created_id, receipt_ids)
        pre = pre_finish_totals_check(client, created_id)
        live = snapshot_amounts(client, created_id)
        planned_ids = set(receipt_ids)
        live_ids = {int(row["id"]) for row in live["receipt_rows"] if row.get("id") not in (None, "")}
        qc = live["qc"]
        gap = qc.get("gap")
        ppv_amount = float(pre.get("ppv_posted") or 0.0)
        if not ppv_amount and live.get("ppv_amounts"):
            ppv_amount = round(sum(live["ppv_amounts"]), 2)
        if planned_ids != live_ids or select_status != "selected":
            if live_ids:
                client.try_deselect_receipts(created_id)
            note = (
                f"AP Clerk: Crosslink Powder Coating invoice {number} was created as KIMCO {created_id} "
                f"but the receipts on the bill do not match the Quantity_Received plan. "
                f"PDF total {dollar(invoice['total'])}. Select status was {select_status}. "
                f"Planned {sorted(planned_ids)}. On the bill {sorted(live_ids)}. "
                f"Those lines were deselected. The bill is not posted and was left on {BATCH_NAME}."
            )
            comment = write_comment(client, created_id, note, mention=False)
            results.append(_row(
                invoice, status="HOLD", reason="selected receipts do not match the Quantity_Received plan",
                kimco_id=created_id, batch=BATCH_NAME, receipts="", note=note,
                comments_1_id=comment.get("id"), ppv=ppv_amount, attached=True, gap=gap,
            ))
            print(f"HOLD-RECEIPTS {number} id={created_id}", flush=True)
            continue
        success = (
            pre.get("ok") is True
            and qc.get("success_allowed") is True
            and gap in (0, 0.0)
            and qc.get("rollup_gap") in (None, 0, 0.0)
            and live["posted"] in (None, "", False)
            and live["verification"] == invoice["total"]
        )
        if success:
            note = build_note(
                invoice, status="Success", action="select", receipt_rows=live["receipt_rows"],
                amount_entered=live["amount"], ppv=ppv_amount, gap=plan.get("merch_gap"),
                line_details=plan.get("line_details"), fee_total=plan.get("fee_total"),
            )
            results.append(_row(
                invoice, status="Success", reason="totals match to the penny", kimco_id=created_id,
                batch=BATCH_NAME, receipts=receipt_label(live["receipt_rows"]), note=note,
                ppv=ppv_amount or 0.0, attached=True, gap=gap,
            ))
            print(f"SUCCESS {number} id={created_id} ppv={ppv_amount} gap={gap}", flush=True)
            continue
        note = (
            f"AP Clerk: Crosslink Powder Coating invoice {number} was created as KIMCO {created_id} "
            f"but was not finished. PDF total {dollar(invoice['total'])}. "
            f"Select Receipts status was {select_status}. Live gap is {gap}. "
            f"The bill is not posted and was left on {BATCH_NAME}."
        )
        comment = write_comment(client, created_id, note, mention=False)
        results.append(_row(
            invoice, status="HOLD", reason=f"select={select_status} gap={gap}", kimco_id=created_id,
            batch=BATCH_NAME, receipts=receipt_label(live["receipt_rows"]), note=note,
            comments_1_id=comment.get("id"), ppv=ppv_amount, attached=True, gap=gap,
        ))
        print(f"HOLD-OPEN {number} id={created_id} gap={gap}", flush=True)

    _finish_mail(graph, results)
    created_ids = [int(row["kimco_bill_id"]) for row in results if row.get("kimco_bill_id")]
    qc_report = scan_ids(client, created_ids) if created_ids else {
        "read_only": True, "ids": [], "scanned": 0, "nonzero_gap_count": 0, "gaps": [], "bills": [],
    }
    payload = {
        "run": "crosslink-2026-09-25",
        "batch_name": BATCH_NAME,
        "batch_id": batch_id,
        "posted": False,
        "packing_slip_gate": "suspended",
        "quantity_field": "Quantity_Received",
        "mailbox_scan": {
            "messages_in_window": scan["messages_in_window"],
            "folder_count": scan["folder_count"],
            "by_folder": scan["by_folder"],
            "crosslink_messages": len(scan["crosslink_messages"]),
        },
        "skipped": skipped,
        "already_in_kimco": already,
        "invoices": [_public_row(row) for row in results],
        "ppv_qc": {
            "read_only": True,
            "scanned": qc_report.get("scanned"),
            "nonzero_gap_count": qc_report.get("nonzero_gap_count"),
            "gaps": qc_report.get("gaps"),
            "bills": qc_report.get("bills"),
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"WROTE {OUT_JSON}", flush=True)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    payload = enter_live(write="--live" in args and "--write" in args)
    if payload.get("dry"):
        print(json.dumps({
            "dry": True,
            "count": len(payload["invoices"]),
            "already": len(payload["already_in_kimco"]),
            "skipped": len(payload["skipped"]),
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
