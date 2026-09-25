"""Enter Earle M. Jorgensen invoices on live KIMCO. Never posts the bill.

Batch name: API Agent - 9/25/26 EMJ.
PO bills are type 3. Receipts are chosen by Quantity_Received.
Quantity_Remaining is ignored. Packing-slip gate is suspended.
No-PO invoices would be type 4. Every EMJ invoice in this window has a
customer PO on the PDF, so none take the miscellaneous path.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoError
from ap_clerk.rules import kimco_datetime, lookup_id, lookup_text

BATCH_NAME = "API Agent - 9/25/26 EMJ"
SHAWN_MENTION_ID = 104
PPV_LIMIT = 75.0
VENDOR_SAMPLE_ID = 9969
VENDOR_ID = 208
OUT_JSON = ROOT / "artifacts" / "emj-2026-09-25.json"
CHICAGO = ZoneInfo("America/Chicago")
WINDOW_START = datetime(2026, 9, 4, tzinfo=CHICAGO)
WINDOW_END = datetime(2026, 9, 26, tzinfo=CHICAGO)

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_INV_SPLIT = re.compile(r"(?=INVOICE NUMBER\s+[A-Z]\d{6,})")
_INV_NUM = re.compile(r"INVOICE NUMBER\s+([A-Z]\d{6,})")
_INV_DATE = re.compile(r"INVOICE DATE\s+(\d{2}-[A-Z]{3}-\d{4})")
_PO = re.compile(r"CUSTOMER PO\s+(\d{5})\b")
_TOTAL = re.compile(r"INVOICE TOTAL\s+\$\s*([\d,]+\.\d{2})")
_CREDIT_NUM = re.compile(r"CREDIT NUMBER\s+([A-Z]\d{6,})")
_CREDIT_TOTAL = re.compile(r"CREDIT TOTAL\s+\$\s*(-?[\d,]+\.\d{2})")
_ORIG = re.compile(r"ORIGINAL INVOICE\s+([A-Z]?\d{5,})")
_LINE = re.compile(
    r"(?P<pieces>\d+(?:\.\d+)?)\s+"
    r"(?P<piece_uom>PCs|PCS|TUBES|Tubes|Tube|TUBE|PC|Bars|Bar)\b\s+"
    r"(?P<shipped>[\d,]+\.\d+)\s+"
    r"(?P<price>\d*\.\d+)\s+"
    r"(?P<ext>[\d,]+\.\d{2})"
)
_PART = re.compile(r"Part #\s*(?P<part>.+?)(?:\s{2,}PO Line #\s*(?P<line>\d+)|\s*$)")
_CUT = re.compile(r"\((\d+(?:\.\d+)?)\"\)")
_UOM = re.compile(r"\b(IN|FT|EA|LB)\b")
_CHARGE = re.compile(
    r"\b(cutting|cut charge|fuel|freight|shipping|delivery|surcharge)\b",
    re.I,
)
_EMJ_HINT = re.compile(r"jorgensen|emjmetals|\bemj\b", re.I)
_PO_LINE = re.compile(r"PO(\d{5})-(\d+)")

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


def _parse_emj_date(value: str) -> date:
    day, month, year = value.split("-")
    return date(int(year), _MONTHS[month], int(day))


_ALLOY = re.compile(
    r"(?<![A-Z0-9])(1018|1026|4140|316L?|6061|A500|A513|A519|DOM)(?![0-9])",
    re.I,
)


def _size_set(text: str) -> set[float]:
    found: set[float] = set()
    blob = _ALLOY.sub(" ", text or "")
    for match in re.finditer(r"(\d+)\s*-\s*(\d+)\s*/\s*(\d+)", blob):
        whole = int(match.group(1)) + int(match.group(2)) / int(match.group(3))
        found.add(round(whole, 3))
    for raw in re.findall(r"\d*\.\d+|\d+", blob):
        token = raw
        if token.startswith("."):
            token = "0" + token
        try:
            value = round(float(token), 3)
        except ValueError:
            continue
        if 0.05 <= value <= 24:
            found.add(value)
    return found


def _sizes_overlap(left: set[float], right: set[float]) -> int:
    """Count invoice sizes that have a PO size within 0.01 (0.125 wall vs 0.13)."""
    count = 0
    for value in left:
        if any(abs(value - other) <= 0.01 for other in right):
            count += 1
    return count


def _alloys(text: str) -> set[str]:
    return {token.upper().replace("316L", "316") for token in _ALLOY.findall(text or "")}


def _compact_part(text: str) -> str:
    head = str(text or "").split(" - ", 1)[0]
    return re.sub(r"[^a-z0-9.]+", "", head.lower())


def parse_emj_layout(text: str) -> list[dict[str, Any]]:
    """EMJ invoice pages from pdftotext -layout. Credit memos are not invoices."""
    bills: list[dict[str, Any]] = []
    for chunk in _INV_SPLIT.split(text or ""):
        if "CREDIT NUMBER" in chunk and "INVOICE NUMBER" not in chunk:
            continue
        number = _INV_NUM.search(chunk)
        total_m = _TOTAL.search(chunk)
        date_m = _INV_DATE.search(chunk)
        if not number or not total_m or not date_m:
            continue
        if _CREDIT_NUM.search(chunk) and "INVOICE TOTAL" not in chunk:
            continue
        po_m = _PO.search(chunk)
        region_end = chunk.find("Thank you")
        if region_end < 0:
            region_end = chunk.find("TOTAL MATERIALS")
        region = chunk[:region_end] if region_end > 0 else chunk
        lines: list[dict[str, Any]] = []
        for match in _LINE.finditer(region):
            shipped = qty_num(match.group("shipped"))
            price = qty_num(match.group("price"))
            ext = money(match.group("ext"))
            if shipped is None or price is None or ext is None:
                continue
            after = region[match.end() : match.end() + 400]
            uoms = _UOM.findall(after)
            uom = uoms[0] if uoms else ""
            part_m = _PART.search(after)
            part = ""
            po_line = None
            if part_m:
                part = re.sub(r"\s+", " ", part_m.group("part")).strip(" -")
                if part_m.group("line"):
                    po_line = int(part_m.group("line"))
            line_start = region.rfind("\n", 0, match.start()) + 1
            prefix = region[line_start : match.start()].strip()
            before = region[max(0, line_start - 240) : line_start]
            desc_lines = []
            if prefix and "UNIT PRICE" not in prefix:
                desc_lines.append(re.sub(r"\s+", " ", prefix))
            for raw in before.splitlines()[-3:]:
                stripped = raw.strip()
                if not stripped or "ITEM DESCRIPTION" in stripped or "SOLD TO" in stripped:
                    continue
                if "UNIT PRICE" in stripped or "QTY DESC" in stripped:
                    continue
                desc_lines.append(re.sub(r"\s+", " ", stripped))
            tail = region[match.end() : match.end() + 280].splitlines()
            desc_more = []
            for raw in tail:
                stripped = raw.strip()
                if not stripped:
                    continue
                if stripped.lower().startswith("part #"):
                    break
                if "UNIT PRICE" in stripped or "QTY DESC" in stripped:
                    continue
                if re.fullmatch(r"(?:IN|FT|EA|LB)(?:\s+(?:IN|FT|EA|LB))*", stripped):
                    continue
                desc_more.append(re.sub(r"\s+", " ", stripped))
                if len(desc_more) >= 3:
                    break
            description = " ".join(desc_lines + desc_more)
            description = re.sub(r"\s+", " ", description).strip()
            description = re.sub(r"(?:\s+\b(?:IN|FT|EA|LB)\b){1,4}$", "", description).strip()
            cut_m = _CUT.search(description)
            lines.append(
                {
                    "pieces": qty_num(match.group("pieces")),
                    "piece_uom": match.group("piece_uom"),
                    "shipped": shipped,
                    "uom": uom,
                    "price": price,
                    "amount": ext,
                    "part": part,
                    "po_line": po_line,
                    "description": description,
                    "cut_inches": qty_num(cut_m.group(1)) if cut_m else None,
                }
            )
        if not lines:
            continue
        total = money(total_m.group(1))
        subtotal = round(sum(float(line["amount"]) for line in lines), 2)
        fees = _charges_in(region)
        fee_total = round(sum(float(fee["amount"]) for fee in fees), 2)
        if total is None or round(subtotal + fee_total - total, 2) != 0:
            continue
        bill_date = _parse_emj_date(date_m.group(1))
        bills.append(
            {
                "invoice_number": number.group(1),
                "po": po_m.group(1) if po_m else "",
                "date": bill_date,
                "due": bill_date + timedelta(days=30),
                "total": total,
                "subtotal": subtotal,
                "fees": fees,
                "lines": lines,
                "type": "parts" if po_m else "misc",
            }
        )
    return bills


def parse_emj_credits(text: str) -> list[dict[str, Any]]:
    credits = []
    for chunk in re.split(r"(?=CREDIT NUMBER\s+[A-Z]\d)", text or ""):
        number = _CREDIT_NUM.search(chunk)
        total_m = _CREDIT_TOTAL.search(chunk)
        if not number or not total_m:
            continue
        orig = _ORIG.search(chunk)
        credits.append(
            {
                "invoice_number": number.group(1),
                "amount": money(total_m.group(1)),
                "original_invoice": orig.group(1) if orig else "",
                "kind": "credit_memo",
            }
        )
    return credits


def _charges_in(region: str) -> list[dict[str, Any]]:
    fees = []
    for raw in (region or "").splitlines():
        stripped = raw.strip()
        if not stripped or not _CHARGE.search(stripped):
            continue
        if re.search(r"freight payment|prepaid", stripped, re.I):
            continue
        amounts = [money(item) for item in re.findall(r"[\d,]+\.\d{2}", stripped)]
        amounts = [item for item in amounts if item not in (None, 0)]
        if not amounts:
            continue
        name = re.sub(r"\s+\$?[\d,]+\.\d{2}\s*$", "", stripped)
        name = re.sub(r"\s{2,}", " ", name).strip(" :-") or "charge"
        fees.append({"name": name[:80], "amount": amounts[-1]})
    return fees


def layout_text(pdf_bytes: bytes) -> str:
    proc = subprocess.run(
        ["pdftotext", "-layout", "-", "-"],
        input=pdf_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("pdftotext failed")
    return proc.stdout.decode("utf-8", errors="replace")


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


def match_po_lines(invoice: dict[str, Any], po_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PO lines for this invoice. Same part on several lines stays together."""
    po = str(invoice.get("po") or "")
    lines = [row for row in po_lines if str(row.get("po") or "") == po]
    if not lines:
        return []
    inv_line = invoice["lines"][0]
    inv_blob = f"{inv_line.get('description') or ''} {inv_line.get('part') or ''}"
    inv_sizes = _size_set(inv_blob)
    inv_part = _compact_part(inv_line.get("part") or "")
    inv_alloys = _alloys(inv_blob)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for line in lines:
        blob = f"{line.get('part') or ''} {line.get('desc') or ''}"
        sizes = _size_set(blob)
        score = _sizes_overlap(inv_sizes, sizes)
        if inv_alloys & _alloys(blob):
            score += 4
        part = _compact_part(line.get("part") or "")
        if inv_part and part and (inv_part in part or part in inv_part):
            score += 8
        if inv_line.get("po_line") and int(line.get("line_no") or 0) == int(inv_line["po_line"]):
            score += 3
        ranked.append((score, line))
    best = max(score for score, _line in ranked)
    if best < 2:
        return []
    winners = [line for score, line in ranked if score == best]
    parts = {_compact_part(line.get("part") or "") for line in winners}
    parts.discard("")
    if not parts:
        return winners
    return [line for line in lines if _compact_part(line.get("part") or "") in parts]


def choose_receipts(
    invoice: dict[str, Any],
    po_lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select open receipts on the matched PO lines when the dollar gap is under $75.

    Quantity_Received is the quantity. A zero Quantity_Received is a missing
    receipt. A gap of $75 or more is not selected, so the receipt stays free
    to unreceive. Unit differences (feet invoiced, inches received) are a
    hold only when that dollar gap is at least $75.
    """
    po = str(invoice.get("po") or "")
    if not po:
        return {
            "action": "missing_po",
            "receipts": [],
            "lines": [],
            "problems": [{"kind": "missing_po", "line": "", "ordered": None, "invoiced": None, "received": None}],
            "merch_gap": None,
            "received_ext": 0.0,
        }
    matched = match_po_lines(invoice, po_lines)
    wanted = {str(line.get("line") or "") for line in matched}
    recs = [
        row
        for row in open_receipts(receipts)
        if str(row.get("po") or "") == po and str(row.get("line") or "") in wanted
    ]
    invoiced = qty_num((invoice["lines"][0] if invoice.get("lines") else {}).get("shipped"))
    ordered = round(sum(float(qty_num(line.get("ordered")) or 0) for line in matched), 4)
    received = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in recs), 4)
    detail = {
        "line": ", ".join(str(line.get("line") or "") for line in matched),
        "part": (invoice["lines"][0].get("part") if invoice.get("lines") else "") or "",
        "desc": (invoice["lines"][0].get("description") if invoice.get("lines") else "") or "",
        "ordered": ordered,
        "invoiced": invoiced,
        "invoiced_uom": (invoice["lines"][0].get("uom") if invoice.get("lines") else "") or "",
        "received": received,
        "receipts": [
            {"id": row.get("id"), "qty": qty_num(row.get("qty_received")), "price": row.get("unit"), "line": row.get("line")}
            for row in recs
        ],
    }
    if not matched or not recs:
        return {
            "action": "missing_receipt",
            "receipts": [],
            "lines": matched,
            "problems": [{**detail, "kind": "missing_receipt"}],
            "merch_gap": None,
            "received_ext": 0.0,
        }
    received_ext = receipt_extension(recs)
    gap = round(float(invoice["total"]) - received_ext, 2)
    if abs(gap) >= PPV_LIMIT:
        kind = "quantity_variance" if _unit_or_qty_disconnect(invoice, recs, matched) else "price_variance"
        return {
            "action": kind,
            "receipts": [],
            "lines": matched,
            "problems": [{**detail, "kind": kind, "gap": gap}],
            "merch_gap": gap,
            "received_ext": received_ext,
        }
    return {
        "action": "select",
        "receipts": recs,
        "lines": matched,
        "problems": [],
        "merch_gap": gap,
        "received_ext": received_ext,
    }


def _unit_or_qty_disconnect(
    invoice: dict[str, Any],
    receipts: list[dict[str, Any]],
    po_lines: list[dict[str, Any]],
) -> bool:
    """True when invoiced quantity does not equal Quantity_Received in the same unit."""
    line = invoice["lines"][0]
    shipped = qty_num(line.get("shipped"))
    uom = str(line.get("uom") or "").upper()
    received = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in receipts), 4)
    if shipped is None:
        return True
    if uom == "FT":
        shipped = round(shipped * 12.0, 4)
    elif uom == "EA":
        cut = qty_num(line.get("cut_inches"))
        pieces = qty_num(line.get("pieces"))
        if cut is not None and pieces is not None:
            shipped = round(pieces * cut, 4)
    ordered = round(sum(float(qty_num(row.get("ordered")) or 0) for row in po_lines), 4)
    if abs(received - shipped) <= 0.05:
        return False
    if ordered and abs(received - ordered) <= 0.05 and abs(shipped - ordered) > 0.05:
        return True
    return abs(received - shipped) > 0.05


def receipt_label(rows: list[dict[str, Any]]) -> str:
    bits = []
    for row in rows:
        qty = row.get("qty_received") if "qty_received" in row else row.get("qty")
        price = row.get("unit") if row.get("unit") not in (None, "") else row.get("price")
        bits.append(f"{row.get('id')} qty {qty_text(qty_num(qty))} @ {price_text(qty_num(price))}")
    return "; ".join(bits)


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
    lines: list[dict[str, Any]] | None = None,
) -> str:
    """Plain-English note. Success notes stay in the artifact and are not posted."""
    number = invoice["invoice_number"]
    total = float(invoice["total"])
    po = invoice.get("po") or "none"
    line = invoice["lines"][0]
    ordered = None
    if lines:
        ordered = round(sum(float(qty_num(row.get("ordered")) or 0) for row in lines), 4)
    shipped = qty_text(qty_num(line.get("shipped")))
    uom = line.get("uom") or "units"
    pieces = qty_text(qty_num(line.get("pieces")))
    piece_uom = line.get("piece_uom") or "pieces"
    part = line.get("part") or "the part"
    if status == "Success":
        label = receipt_label(receipt_rows or [])
        received_qty = 0.0
        for row in receipt_rows or []:
            raw_qty = row.get("qty_received") if "qty_received" in row else row.get("qty")
            received_qty = round(received_qty + float(qty_num(raw_qty) or 0), 4)
        received = qty_text(received_qty)
        line_names = ", ".join(str(row.get("line") or "") for row in (lines or []) if row.get("line"))
        if ppv not in (None, 0, 0.0):
            ppv_bit = (
                f"The receipt extension missed the PDF total by {dollar(gap if gap is not None else ppv)}, "
                f"which is under {dollar(PPV_LIMIT)}, so one signed Purchase Price Variance of {dollar(ppv)} "
                f"was posted. After that charge the gap is {dollar(0)}."
            )
        else:
            ppv_bit = (
                "The selected receipt lines equal the PDF total, so the gap is $0.00 and no "
                "Purchase Price Variance was posted."
            )
        text = (
            f"AP Clerk: Earle M. Jorgensen invoice {number} is entered as a parts bill on PO {po} "
            f"and is not posted. The PDF total is {dollar(total)}. "
            f"Amount entered is {dollar(amount_entered if amount_entered is not None else total)}. "
            f"The invoice line is {pieces} {piece_uom} of {part}, shipped {shipped} {uom}. "
            f"PO line {line_names or po} was ordered {qty_text(ordered)}. "
            f"Quantity_Received on the selected receipts is {received}. "
            f"Receipts selected: {label}. {ppv_bit} Nothing is waiting on purchasing."
        )
    else:
        problem = (problems or [{}])[0]
        received = qty_text(problem.get("received"))
        invoiced = qty_text(problem.get("invoiced"))
        ordered_txt = qty_text(problem.get("ordered"))
        line_name = problem.get("line") or po
        receipt_bit = ""
        if problem.get("receipts"):
            receipt_bit = " Receipts by Quantity_Received: " + receipt_label(
                [
                    {"id": row["id"], "qty_received": row["qty"], "unit": row["price"]}
                    for row in problem["receipts"]
                ]
            ) + "."
        entered = dollar(amount_entered if amount_entered is not None else 0)
        if action == "missing_receipt":
            why = (
                "This cannot be finished because that line has no selectable receipt. "
                "Quantity_Received is 0 or there is no open receipt on the matching part, "
                "so this is a missing receipt and not a price variance."
            )
            ask = (
                f"Shawn should receive the invoiced quantity of {part} on PO {po}. "
                f"After that receipt exists, AP will select it by Quantity_Received and finish "
                f"the bill if the header matches the PDF total of {dollar(total)} to the penny."
            )
        elif action == "price_variance":
            why = (
                f"The gap is {dollar(problem.get('gap'))}, which is {dollar(PPV_LIMIT)} or more, "
                "so no Purchase Price Variance was posted and the receipts were not selected. "
                "Selecting them would lock the receipt and block an unreceive."
            )
            ask = (
                "Shawn should unreceive the line, set the PO price to the invoice price, "
                "and re-receive the same quantity. After that, AP will select the receipts and finish the bill."
            )
        elif action == "quantity_variance":
            why = (
                f"Quantity received does not match quantity invoiced, and the dollar gap of "
                f"{dollar(problem.get('gap'))} is {dollar(PPV_LIMIT)} or more. "
                "That is a quantity or unit disconnect, not a price variance under the limit. "
                "No receipts were selected."
            )
            ask = (
                "Shawn should correct the receipt so Quantity_Received matches the invoiced "
                "quantity in the same unit, then tell AP to select that receipt and finish."
            )
        else:
            why = "This bill cannot be finished from the PDF and the open receipts."
            ask = "Shawn should review the PO and the receipt, then tell AP what to select."
        text = (
            f"AP Clerk: @Shawn McKibben Earle M. Jorgensen invoice {number} cannot be finished. "
            f"The PDF total is {dollar(total)}. Amount entered on receipt lines is {entered}. "
            f"PO {po} line {line_name} part {part} was ordered {ordered_txt}, invoiced {invoiced} {uom}, "
            f"and Quantity_Received is {received}.{receipt_bit} {why} {ask} "
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
    line_no = None
    if display:
        match = _PO_LINE.search(display)
        if match:
            po = po or match.group(1)
            line_no = int(match.group(2))
    if po not in wanted:
        return None
    return {
        "id": item.get("id"),
        "po": po,
        "po_id": lookup_id(values.get("Purchase_Order_Number")),
        "line": display,
        "line_no": line_no,
        "ordered": qty_num(values.get("Quantity")),
        "unit": qty_num(values.get("Unit_Price")),
        "part": lookup_text(values.get("Part_Number")) or "",
        "desc": str(values.get("Part_Description") or ""),
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
    return {
        "id": item.get("id"),
        "po": match.group(1),
        "line": line,
        "qty_received": qty_num(values.get("Quantity_Received")),
        "qty_remaining": qty_num(values.get("Quantity_Remaining")),
        "unit": qty_num(values.get("PO_Item_Number_$_Unit_Price")),
        "ap": ap_text,
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
        "type": values.get("Invoice_Type"),
        "qc": qc,
        "receipt_rows": receipt_rows,
        "comments": comments,
        "line_count": len(lists.get("APInvoiceLine") or []),
        "ppv_amounts": ppv_amounts,
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
        mention_saved = False
        needle = text.split("@Shawn McKibben", 1)[-1][:60].strip()
        for comment in live["comments"]:
            html = str(comment.get("html") or "").replace("&amp;", "&")
            tagged = 'data-mention-id="104"' in html and needle in html
            plain = text[:80] in html
            if tagged or plain:
                match = comment.get("id")
                mention_saved = tagged
                if tagged:
                    break
        last = {
            "status": "persisted" if status < 400 and match else f"put-{status}",
            "put": status,
            "error": error,
            "id": match,
            "mention": mention_saved,
        }
        if last["status"] == "persisted":
            return last
    return last


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
        "$select": "id,subject,from,receivedDateTime,hasAttachments,parentFolderId,categories",
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
    emj_messages = []
    for msg in messages:
        folder = folders.get(str(msg.get("parentFolderId") or ""), {"name": "unknown", "parent": None})
        key = f"{folder.get('parent') or ''}/{folder.get('name')}"
        by_folder[key] = by_folder.get(key, 0) + 1
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        addr = str(frm.get("address") or "")
        name = str(frm.get("name") or "")
        subject = str(msg.get("subject") or "")
        if not _EMJ_HINT.search(f"{addr} {name} {subject}"):
            continue
        emj_messages.append(
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
        "emj_messages": emj_messages,
    }


def collect_invoices(graph: Any, scan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    from ap_clerk.pdf_invoice import extract_pdf_text

    invoices: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pdfs: dict[str, bytes] = {}
    seen: dict[str, dict[str, Any]] = {}
    for msg in scan["emj_messages"]:
        subject = msg["subject"]
        if re.search(r"statement of account", subject, re.I):
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
        if not msg["has_attachments"]:
            skipped.append(
                {
                    "kind": "no_pdf",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "EMJ email has no PDF attachment, so it is not a new invoice.",
                }
            )
            continue
        attachments = graph.download_pdf_attachments(ALLOWED_MAILBOX, msg["id"])
        if len(attachments) != 1:
            skipped.append(
                {
                    "kind": "pdf_count",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "reason": f"Expected one PDF, found {len(attachments)}.",
                }
            )
            continue
        name, content = attachments[0]
        text = layout_text(content)
        for credit in parse_emj_credits(text):
            skipped.append(
                {
                    **credit,
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "message_id": msg["id"],
                    "reason": "Credit memo ignored. It is not a new invoice.",
                }
            )
        # Confirm pypdf can see the invoice number before we keep the bytes.
        path = Path("/tmp/emj-enter") / f"{msg['received_chicago']}-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        _ = extract_pdf_text(path)
        for bill in parse_emj_layout(text):
            number = bill["invoice_number"]
            bill["message_id"] = msg["id"]
            bill["subject"] = subject
            bill["email_received"] = msg["received_chicago"]
            bill["email_received_utc"] = msg["received"]
            bill["folder"] = msg["folder"]
            bill["pdf_name"] = name
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


def enter_live(*, write: bool = False) -> dict[str, Any]:
    from ap_clerk.auth import load_credentials, resolve_target
    from ap_clerk.cli import _find_or_create_batch, _optional_graph_client
    from ap_clerk.kimco import KimcoClient
    from ap_clerk.ppv_qc import pre_finish_totals_check
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
    print("Scanning mailbox", flush=True)
    scan = scan_mailbox(graph)
    print(
        f"Window messages {scan['messages_in_window']} folders {scan['folder_count']} "
        f"EMJ messages {len(scan['emj_messages'])}",
        flush=True,
    )
    invoices, skipped, pdfs = collect_invoices(graph, scan)
    print(f"Invoices {len(invoices)} skipped {len(skipped)}", flush=True)
    for bill in invoices:
        print(
            f"PDF {bill['invoice_number']} PO {bill['po'] or 'none'} total {bill['total']} "
            f"shipped {bill['lines'][0]['shipped']} {bill['lines'][0]['uom']}",
            flush=True,
        )
    wanted = {str(bill["po"]) for bill in invoices if bill.get("po")}
    print("Loading PO lines and receipts", flush=True)
    po_lines = load_po_lines(client, wanted)
    receipts = load_receipts(client, wanted)
    print(f"PO lines {len(po_lines)} receipts {len(receipts)}", flush=True)
    plans = []
    for bill in invoices:
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
                    "receipts": receipt_label(plan["receipts"]),
                    "lines": [row.get("line") for row in plan.get("lines") or []],
                }
            ),
            flush=True,
        )
    if not write:
        return {
            "dry": True,
            "scan": {"messages_in_window": scan["messages_in_window"], "by_folder": scan["by_folder"]},
            "skipped": skipped,
            "invoices": [
                {
                    "invoice_number": plan["invoice"]["invoice_number"],
                    "action": plan["action"],
                    "po": plan["invoice"]["po"],
                    "pdf_total": plan["invoice"]["total"],
                    "gap": plan.get("merch_gap"),
                    "receipts_selected": receipt_label(plan["receipts"]),
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
    if vendor_id != VENDOR_ID or not terms_id or not remit_id or not currency_id:
        raise SystemExit(f"EMJ sample {VENDOR_SAMPLE_ID} vendor/terms/remit changed: vendor {vendor_id}")
    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    batch_id = int(batch["id"])
    print(f"BATCH {batch_id} {BATCH_NAME} created={batch.get('created')}", flush=True)
    results: list[dict[str, Any]] = []
    for plan in plans:
        invoice = plan["invoice"]
        number = invoice["invoice_number"]
        print(f"CHECK {number}", flush=True)
        existing = find_invoice_ids(client, {number.upper()})
        if number.upper() in existing:
            live = snapshot_amounts(client, existing[number.upper()])
            results.append(_row(
                invoice,
                status="HOLD",
                reason=f"already in KIMCO {existing[number.upper()]}; skipped, not duplicated",
                kimco_id=existing[number.upper()],
                batch=live.get("batch_id"),
                receipts=receipt_label(live.get("receipt_rows") or []),
                note=(
                    f"AP Clerk: Earle M. Jorgensen invoice {number} is already in KIMCO as bill "
                    f"{existing[number.upper()]}. It was not entered again."
                ),
            ))
            print(f"SKIP {number} existing {existing[number.upper()]}", flush=True)
            continue
        if plan["action"] != "select":
            results.append(_enter_hold(
                client, graph, invoice, plan, batch_id, vendor_id, terms_id, remit_id, currency_id, po_lines, pdfs
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
                invoice,
                status="HOLD",
                reason=f"header create HTTP {created[2]}: {created[3]}",
                kimco_id=None,
                batch=batch_id,
                receipts="",
                note=f"AP Clerk: Earle M. Jorgensen invoice {number} header was not created.",
            ))
            continue
        created_id = int(created[0])
        pdf = page_pdf(pdfs[number], number)
        attach = client.try_official_attach(
            created_id, name=f"{number}.pdf", content_type="application/pdf", size=len(pdf), content=pdf
        )
        if attach != "attached":
            note = (
                f"AP Clerk: Earle M. Jorgensen invoice {number} header {created_id} was created "
                f"but the PDF attach status was {attach}. No receipts were selected. "
                f"The bill is not posted and was left on {BATCH_NAME}."
            )
            comment = write_comment(client, created_id, note, mention=False)
            results.append(_row(
                invoice, status="HOLD", reason=f"attach={attach}", kimco_id=created_id,
                batch=BATCH_NAME, receipts="", note=note, comments_1_id=comment.get("id"), attached=False,
            ))
            continue
        select_status = client.try_select_receipts(created_id, [int(row["id"]) for row in plan["receipts"]])
        pre = pre_finish_totals_check(client, created_id)
        live = snapshot_amounts(client, created_id)
        planned_ids = {int(row["id"]) for row in plan["receipts"]}
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
                f"AP Clerk: Earle M. Jorgensen invoice {number} was created as KIMCO {created_id} "
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
                amount_entered=live["amount"], ppv=ppv_amount, gap=plan.get("merch_gap"), lines=plan["lines"],
            )
            results.append(_row(
                invoice, status="Success", reason="totals match to the penny", kimco_id=created_id,
                batch=BATCH_NAME, receipts=receipt_label(live["receipt_rows"]), note=note,
                ppv=ppv_amount or 0.0, attached=True, gap=gap,
            ))
            print(f"SUCCESS {number} id={created_id} ppv={ppv_amount} gap={gap}", flush=True)
            continue
        if gap is not None and abs(float(gap)) >= PPV_LIMIT:
            client.try_deselect_receipts(created_id)
            plan = {
                **plan,
                "action": "price_variance",
                "problems": [{
                    "kind": "price_variance",
                    "line": invoice["po"],
                    "ordered": None,
                    "invoiced": invoice["lines"][0]["shipped"],
                    "received": None,
                    "receipts": [],
                    "gap": gap,
                }],
                "merch_gap": gap,
            }
            note = build_note(
                invoice, status="HOLD", action="price_variance", problems=plan["problems"],
                amount_entered=0, gap=gap, lines=plan.get("lines"),
            )
            comment = write_comment(client, created_id, note, mention=True)
            moved = apply_transfer_ap_batch_move(client, kimco_id=created_id) if comment.get("status") == "persisted" else {"status": "not-moved"}
            live = snapshot_amounts(client, created_id)
            on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
            results.append(_row(
                invoice, status="HOLD", reason="price_variance", kimco_id=created_id,
                batch=moved.get("batch_name") or BATCH_NAME, receipts="", note=note,
                comments_1_id=comment.get("id"), transfer_ap="yes" if on_transfer else "no",
                attached=True, gap=live["qc"].get("gap"),
            ))
            print(f"HOLD-PRICE {number} id={created_id}", flush=True)
            continue
        note = (
            f"AP Clerk: Earle M. Jorgensen invoice {number} was created as KIMCO {created_id} "
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
    payload = {
        "run": "emj-2026-09-25",
        "batch_name": BATCH_NAME,
        "batch_id": batch_id,
        "posted": False,
        "packing_slip_gate": "suspended",
        "quantity_field": "Quantity_Received",
        "mailbox_scan": {
            "messages_in_window": scan["messages_in_window"],
            "folder_count": scan["folder_count"],
            "by_folder": scan["by_folder"],
            "emj_messages": len(scan["emj_messages"]),
        },
        "skipped": skipped,
        "invoices": [_public_row(row) for row in results],
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2))
    print(f"WROTE {OUT_JSON}", flush=True)
    return payload


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
        "Purchase_Order": {"id": int(po_id)},
        "Comments": "API Agent",
    }
    return client.create("ap_invoices", payload)


def _enter_hold(client, graph, invoice, plan, batch_id, vendor_id, terms_id, remit_id, currency_id, po_lines, pdfs):
    po_id = next((row.get("po_id") for row in po_lines if str(row.get("po")) == str(invoice.get("po")) and row.get("po_id")), None)
    if not po_id:
        return _row(
            invoice, status="HOLD", reason="PO id missing", kimco_id=None, batch=batch_id, receipts="",
            note=f"AP Clerk: Earle M. Jorgensen invoice {invoice['invoice_number']} has no PO id in KIMCO.",
        )
    created = _create_header(
        client, invoice, batch_id, vendor_id, terms_id, remit_id, currency_id, int(po_id), invoice_type=3
    )
    if created[0] is None:
        return _row(
            invoice, status="HOLD", reason=f"header create HTTP {created[2]}", kimco_id=None,
            batch=batch_id, receipts="",
            note=f"AP Clerk: Earle M. Jorgensen invoice {invoice['invoice_number']} header was not created.",
        )
    created_id = int(created[0])
    pdf = page_pdf(pdfs[invoice["invoice_number"]], invoice["invoice_number"])
    attach = client.try_official_attach(
        created_id, name=f"{invoice['invoice_number']}.pdf", content_type="application/pdf",
        size=len(pdf), content=pdf,
    )
    note = build_note(
        invoice, status="HOLD", action=plan["action"], problems=plan.get("problems"),
        amount_entered=0, gap=plan.get("merch_gap"), lines=plan.get("lines"),
    )
    mention = plan["action"] in {"missing_receipt", "price_variance", "quantity_variance"}
    comment = write_comment(client, created_id, note, mention=mention)
    from ap_clerk.transfer_ap import apply_transfer_ap_batch_move

    moved = {"status": "not-moved"}
    if mention and comment.get("status") == "persisted":
        moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
    live = snapshot_amounts(client, created_id)
    on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
    print(f"HOLD {invoice['invoice_number']} id={created_id} {plan['action']} transfer={moved.get('status')}", flush=True)
    return _row(
        invoice, status="HOLD", reason=plan["action"], kimco_id=created_id,
        batch=moved.get("batch_name") or BATCH_NAME, receipts=receipt_label(live.get("receipt_rows") or []),
        note=note, comments_1_id=comment.get("id"), transfer_ap="yes" if on_transfer else "no",
        attached=attach == "attached", gap=live["qc"].get("gap"),
    )


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


def _finish_mail(graph: Any, results: list[dict[str, Any]]) -> None:
    by_message: dict[str, list[dict[str, Any]]] = {}
    for row in results:
        if row.get("message_id"):
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    payload = enter_live(write="--live" in args and "--write" in args)
    if payload.get("dry"):
        print(json.dumps({"dry": True, "count": len(payload["invoices"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
