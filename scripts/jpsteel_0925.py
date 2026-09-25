"""Enter JP Steel invoices on live KIMCO. Never posts the bill.

Batch name: API Agent - 9/25/26 JP Steel.
PO bills are type 3. Receipts are chosen by Quantity_Received.
Quantity_Remaining is ignored. Packing-slip gate is suspended.
No-PO invoices are type 4. Every JP Steel invoice in this window has a PO.
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

BATCH_NAME = "API Agent - 9/25/26 JP Steel"
VENDOR_SAMPLE_ID = 10107
VENDOR_ID = 100
PPV_LIMIT = 75.0
OUT_JSON = ROOT / "artifacts" / "jpsteel-2026-09-25.json"
CHICAGO = ZoneInfo("America/Chicago")
WINDOW_START = datetime(2026, 9, 4, tzinfo=CHICAGO)
WINDOW_END = datetime(2026, 9, 26, tzinfo=CHICAGO)
QTY_TOLERANCE = 0.15

_HINT = re.compile(r"jpsteel|jp[\s-]*steel", re.I)
_INV_NO = re.compile(r"Invoice No:\s*(\d{5,6})")
_PO = re.compile(r"Customer P\.O\.#:\s*(\d{5})")
_INV_DATE = re.compile(r"Invoice Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})")
_TOTAL = re.compile(r"(?<!Sub)Total\s+\$\s*([\d,]+\.\d{2})")
_CREDIT = re.compile(r"credit memo|credit number|credit no\b", re.I)
_STATEMENT = re.compile(r"statement of account|account statement", re.I)
_CHARGE = re.compile(r"\b(cutting|cut charge|fuel|freight|shipping|delivery|surcharge)\b", re.I)
_LINE = re.compile(
    r"^\s*(?P<so>\d+)\s+"
    r"(?:(?P<measure>[\d,]+\.\d+)\s*(?P<mark>['\"])\s+)?"
    r"(?P<pcs>\d+)\s+P\s+"
    r"(?P<rest>.+?)\s+"
    r"(?:(?P<len_ft>[\d,.]+)'\s+)?"
    r"(?P<len_in>[\d,.]+)\"\s+"
    r"(?P<weight>[\d,]+\.\d+)\s+"
    r"\$(?P<price>[\d,]+\.\d+)\s+"
    r"(?P<um>[EFI])\s+"
    r"\$(?P<ext>[\d,]+\.\d{2})",
    re.M,
)
_PART = re.compile(r"\b(\d{4,6}-\d)\b")
_PO_LINE = re.compile(r"PO(\d{5})-(\d+)")
_ALLOY = re.compile(
    r"(?<![A-Z0-9])(1018|1020|1026|4130|4140|316L?|6061|A36|A500|A513|A519|DOM|NQT)(?![0-9])",
    re.I,
)

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


def _size_set(text: str) -> set[float]:
    found: set[float] = set()
    blob = _ALLOY.sub(" ", text or "")
    # 1/8 inch is 0.125, not the sizes 1 and 8.
    blob = re.sub(
        r"(\d+)\s*/\s*(\d+)",
        lambda match: str(round(int(match.group(1)) / int(match.group(2)), 3)),
        blob,
    )
    for raw in re.findall(r"\d*\.\d+|\d+", blob):
        token = "0" + raw if raw.startswith(".") else raw
        try:
            value = round(float(token), 3)
        except ValueError:
            continue
        if 0.05 <= value <= 24:
            found.add(value)
    return found


def _sizes_overlap(left: set[float], right: set[float]) -> int:
    count = 0
    for value in left:
        # 0.120 wall versus 0.130 on the PO is the same size. Float makes 0.01 fail.
        if any(abs(value - other) <= 0.015 for other in right):
            count += 1
    return count


def _alloys(text: str) -> set[str]:
    return {token.upper().replace("316L", "316") for token in _ALLOY.findall(text or "")}


def _compact_part(text: str) -> str:
    head = str(text or "").split(" - ", 1)[0]
    return re.sub(r"[^a-z0-9.]+", "", head.lower())


def _part_token(text: str) -> str:
    match = _PART.search(text or "")
    return match.group(1) if match else ""


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


def _continuations(text: str, end: int) -> str:
    rest = text[end:]
    bits = []
    for raw in rest.splitlines()[1:8]:
        stripped = raw.strip()
        if not stripped:
            if bits:
                break
            continue
        lower = stripped.lower()
        if lower.startswith("tag#") or "invoice totals" in lower or "bol no" in lower or lower.startswith("total"):
            break
        if re.match(r"^\d+\s", stripped) or stripped.startswith("SO "):
            break
        bits.append(re.sub(r"\s+", " ", stripped))
    return " ".join(bits)


def _charges_in(text: str) -> list[dict[str, Any]]:
    fees = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if not stripped or not _CHARGE.search(stripped):
            continue
        if _LINE.search(stripped):
            continue
        if re.search(r"subtotal|invoice total|rusty|messages:|ship via", stripped, re.I):
            continue
        amounts = [money(item) for item in re.findall(r"[\d,]+\.\d{2}", stripped)]
        amounts = [item for item in amounts if item not in (None, 0)]
        if not amounts:
            continue
        name = re.sub(r"\s+\$?[\d,]+\.\d{2}\s*$", "", stripped)
        name = re.sub(r"\s{2,}", " ", name).strip(" :-") or "charge"
        fees.append({"name": name[:80], "amount": amounts[-1]})
    return fees


def parse_jpsteel_pages(pages: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One bill per invoice number. Later pages of the same invoice stay with it."""
    groups: list[dict[str, Any]] = []
    credits: list[dict[str, Any]] = []
    for page in pages:
        if _STATEMENT.search(page) and not _INV_NO.search(page):
            continue
        number = _INV_NO.search(page)
        if _CREDIT.search(page) and not number:
            total = _TOTAL.search(page)
            credits.append(
                {
                    "kind": "credit_memo",
                    "invoice_number": "",
                    "amount": money(total.group(1)) if total else None,
                    "reason": "Credit memo ignored. It is not a new invoice.",
                }
            )
            continue
        if not number:
            if groups:
                groups[-1]["pages"].append(page)
            continue
        token = number.group(1)
        if groups and groups[-1]["number"] == token:
            groups[-1]["pages"].append(page)
        else:
            groups.append({"number": token, "pages": [page]})
    bills = []
    for group in groups:
        bill = parse_jpsteel_layout("\n".join(group["pages"]))
        if bill:
            bills.append(bill)
    return bills, credits


def parse_jpsteel_layout(text: str) -> dict[str, Any] | None:
    number = _INV_NO.search(text or "")
    total_m = None
    for total_m in _TOTAL.finditer(text or ""):
        pass
    date_m = _INV_DATE.search(text or "")
    if not number or not total_m or not date_m:
        return None
    if _CREDIT.search(text or "") and "Invoice No:" not in (text or ""):
        return None
    lines = []
    for match in _LINE.finditer(text or ""):
        price = money(match.group("price"))
        ext = money(match.group("ext"))
        pieces = qty_num(match.group("pcs"))
        if price is None or ext is None or pieces is None:
            continue
        rest = re.sub(r"\s+", " ", match.group("rest")).strip(" -")
        more = _continuations(text, match.end())
        description = re.sub(r"\s+", " ", f"{rest} {more}").strip(" -")
        measure = qty_num(match.group("measure")) if match.group("measure") else None
        lines.append(
            {
                "so_line": int(match.group("so")),
                "pieces": pieces,
                "measure": measure,
                "measure_mark": match.group("mark") or "",
                "cut_inches": qty_num(match.group("len_in")),
                "length_feet": qty_num(match.group("len_ft")),
                "um": match.group("um"),
                "price": price,
                "amount": ext,
                "description": description,
                "part": _part_token(description),
            }
        )
    if not lines:
        return None
    total = money(total_m.group(1))
    fees = _charges_in(text or "")
    subtotal = round(sum(float(line["amount"]) for line in lines), 2)
    fee_total = round(sum(float(fee["amount"]) for fee in fees), 2)
    if total is None or round(subtotal + fee_total - total, 2) != 0:
        return None
    bill_date = _parse_date(date_m.group(1))
    po_m = _PO.search(text or "")
    return {
        "invoice_number": number.group(1),
        "po": po_m.group(1) if po_m else "",
        "date": bill_date,
        "due": bill_date + timedelta(days=60),
        "total": total,
        "subtotal": subtotal,
        "fees": fees,
        "lines": lines,
        "type": "parts" if po_m else "misc",
    }


def parse_jpsteel_credits(text: str) -> list[dict[str, Any]]:
    credits = []
    if _CREDIT.search(text or "") and not _INV_NO.search(text or ""):
        total = _TOTAL.search(text or "")
        credits.append(
            {
                "kind": "credit_memo",
                "amount": money(total.group(1)) if total else None,
                "reason": "Credit memo ignored. It is not a new invoice.",
            }
        )
    return credits


def quantity_targets(line: dict[str, Any]) -> list[float]:
    """Quantities that equal this invoice line in pieces or in inches."""
    targets: list[float] = []
    pieces = qty_num(line.get("pieces"))
    cut = qty_num(line.get("cut_inches"))
    measure = qty_num(line.get("measure"))
    um = str(line.get("um") or "")
    if um == "E" and pieces is not None:
        targets.append(pieces)
    if um == "I" and measure is not None:
        targets.append(measure)
    if um == "F":
        if pieces is not None and cut is not None:
            targets.append(round(pieces * cut, 4))
        elif measure is not None:
            targets.append(round(measure * 12.0, 4))
    if um == "E" and pieces is not None and cut is not None:
        targets.append(round(pieces * cut, 4))
    unique: list[float] = []
    for value in targets:
        if not any(abs(value - prior) <= 0.001 for prior in unique):
            unique.append(value)
    return unique


def quantity_phrase(line: dict[str, Any]) -> str:
    um = str(line.get("um") or "")
    pieces = qty_text(qty_num(line.get("pieces")))
    desc = line.get("description") or "the part"
    if um == "E":
        cut = qty_num(line.get("cut_inches"))
        cut_bit = f", cut {qty_text(cut)} inches" if cut is not None else ""
        return f"{pieces} pieces of {desc}{cut_bit} at {dollar(line.get('price'))} each"
    if um == "F":
        feet = qty_text(qty_num(line.get("measure")))
        inches = None
        targets = quantity_targets(line)
        if targets:
            inches = targets[0]
        inch_bit = f", which is {qty_text(inches)} inches" if inches is not None else ""
        return f"{feet} feet of {desc}{inch_bit} at {dollar(line.get('price'))} per foot"
    inches = qty_text(qty_num(line.get("measure")))
    return f"{inches} inches of {desc} at {dollar(line.get('price'))} per inch"


def _score_po_line(inv_line: dict[str, Any], po_line: dict[str, Any]) -> int:
    inv_blob = f"{inv_line.get('description') or ''} {inv_line.get('part') or ''}"
    po_blob = f"{po_line.get('part') or ''} {po_line.get('desc') or ''}"
    inv_sizes = _size_set(inv_blob)
    po_sizes = _size_set(po_blob)
    overlap = _sizes_overlap(inv_sizes, po_sizes)
    score = overlap * 2
    if inv_sizes and overlap == len(inv_sizes):
        score += 4
    if len(inv_sizes) >= 2:
        extras = 0
        for value in po_sizes:
            if not any(abs(value - other) <= 0.015 for other in inv_sizes):
                extras += 1
        score -= extras
    if _alloys(inv_blob) & _alloys(po_blob):
        score += 3
    inv_part = _part_token(inv_blob)
    po_part = _part_token(po_blob)
    if inv_part and po_part and inv_part == po_part:
        score += 10
    return score


def match_po_lines(
    inv_line: dict[str, Any],
    po_lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not po_lines:
        return []
    ranked = [(_score_po_line(inv_line, line), line) for line in po_lines]
    best = max(score for score, _line in ranked)
    if best < 4:
        return []
    winners = [line for score, line in ranked if score == best]
    parts = {_compact_part(line.get("part") or "") for line in winners}
    parts.discard("")
    if len(parts) > 1 and receipts is not None:
        targets = quantity_targets(inv_line)
        best_part = ""
        best_dist = None
        for part in parts:
            group = [line for line in po_lines if _compact_part(line.get("part") or "") == part]
            wanted = {str(line.get("line") or "") for line in group}
            got = round(
                sum(
                    float(qty_num(row.get("qty_received")) or 0)
                    for row in receipts
                    if str(row.get("line") or "") in wanted
                ),
                4,
            )
            if not targets or got == 0:
                dist = 10**9
            else:
                dist = min(abs(got - target) for target in targets)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_part = part
        if best_part and best_dist is not None and best_dist < 10**9:
            parts = {best_part}
    if len(parts) == 1:
        return [line for line in po_lines if _compact_part(line.get("part") or "") in parts]
    return winners


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


def receipt_extension(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        qty = qty_num(row.get("qty_received"))
        price = qty_num(row.get("unit"))
        if qty is None or price is None:
            continue
        total = round(total + round(qty * price, 2), 2)
    return total


def _aligned(line: dict[str, Any], received: float) -> bool:
    return any(abs(received - target) <= QTY_TOLERANCE for target in quantity_targets(line))


def choose_receipts(
    invoice: dict[str, Any],
    po_lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select open receipts on the matched PO lines when the dollar gap is under $75.

    Quantity_Received is the quantity. A gap of $75 or more is not selected.
    A quantity or unit disconnect is a hold only when that dollar gap is at least $75.
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
            "qty_difference": False,
        }
    po_only = [row for row in po_lines if str(row.get("po") or "") == po]
    pool = [row for row in open_receipts(receipts) if str(row.get("po") or "") == po]
    selected: list[dict[str, Any]] = []
    used: set[int] = set()
    problems: list[dict[str, Any]] = []
    matched_lines: list[dict[str, Any]] = []
    qty_difference = False
    qty_details: list[dict[str, Any]] = []
    for inv_line in invoice.get("lines") or []:
        matched = match_po_lines(inv_line, po_only, pool)
        wanted = {str(line.get("line") or "") for line in matched}
        recs = [
            row
            for row in pool
            if str(row.get("line") or "") in wanted and int(row.get("id") or 0) not in used
        ]
        ordered = round(sum(float(qty_num(line.get("ordered")) or 0) for line in matched), 4)
        received = round(sum(float(qty_num(row.get("qty_received")) or 0) for row in recs), 4)
        targets = quantity_targets(inv_line)
        detail = {
            "line": ", ".join(str(line.get("line") or "") for line in matched),
            "part": inv_line.get("description") or "",
            "ordered": ordered,
            "invoiced": targets[0] if targets else qty_num(inv_line.get("pieces")),
            "invoiced_phrase": quantity_phrase(inv_line),
            "received": received,
            "receipts": [
                {"id": row.get("id"), "qty": qty_num(row.get("qty_received")), "price": row.get("unit"), "line": row.get("line")}
                for row in recs
            ],
        }
        if not matched or not recs:
            problems.append({**detail, "kind": "missing_receipt"})
            matched_lines.extend(matched)
            continue
        if not _aligned(inv_line, received):
            qty_difference = True
            qty_details.append(
                {
                    "part": inv_line.get("description") or "",
                    "invoiced": targets[0] if targets else None,
                    "received": received,
                }
            )
        for row in recs:
            used.add(int(row["id"]))
            selected.append(row)
        matched_lines.extend(matched)
    received_ext = receipt_extension(selected)
    fee_total = round(sum(float(fee.get("amount") or 0) for fee in invoice.get("fees") or []), 2)
    gap = round(float(invoice["total"]) - received_ext - fee_total, 2)
    if problems:
        return {
            "action": "missing_receipt",
            "receipts": [],
            "lines": matched_lines,
            "problems": problems,
            "merch_gap": None,
            "received_ext": 0.0,
            "qty_difference": False,
        }
    if abs(gap) >= PPV_LIMIT:
        kind = "quantity_variance" if qty_difference else "price_variance"
        problem = {
            "kind": kind,
            "line": ", ".join(str(line.get("line") or "") for line in matched_lines),
            "part": (invoice["lines"][0].get("description") if invoice.get("lines") else "") or "",
            "ordered": round(sum(float(qty_num(line.get("ordered")) or 0) for line in matched_lines), 4),
            "invoiced": None,
            "invoiced_phrase": "; ".join(quantity_phrase(line) for line in invoice.get("lines") or []),
            "received": round(sum(float(qty_num(row.get("qty_received")) or 0) for row in selected), 4),
            "receipts": [
                {"id": row.get("id"), "qty": qty_num(row.get("qty_received")), "price": row.get("unit"), "line": row.get("line")}
                for row in selected
            ],
            "gap": gap,
        }
        return {
            "action": kind,
            "receipts": [],
            "lines": matched_lines,
            "problems": [problem],
            "merch_gap": gap,
            "received_ext": received_ext,
            "qty_difference": qty_difference,
            "qty_details": qty_details,
        }
    chosen_names = {str(row.get("line") or "") for row in selected}
    matched_lines = [line for line in matched_lines if str(line.get("line") or "") in chosen_names]
    return {
        "action": "select",
        "receipts": selected,
        "lines": matched_lines,
        "problems": [],
        "merch_gap": gap,
        "received_ext": received_ext,
        "qty_difference": qty_difference,
        "qty_details": qty_details,
    }


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
    qty_difference: bool = False,
    qty_details: list[dict[str, Any]] | None = None,
) -> str:
    """Plain-English note. Success notes stay in the artifact and are not posted."""
    number = invoice["invoice_number"]
    total = float(invoice["total"])
    po = invoice.get("po") or "none"
    phrases = "; ".join(quantity_phrase(line) for line in invoice.get("lines") or [])
    line_names = ", ".join(str(row.get("line") or "") for row in (lines or []) if row.get("line"))
    ordered = None
    if lines:
        ordered = round(sum(float(qty_num(row.get("ordered")) or 0) for row in lines), 4)
    if status == "Success":
        label = receipt_label(receipt_rows or [])
        received_qty = 0.0
        for row in receipt_rows or []:
            raw_qty = row.get("qty_received") if "qty_received" in row else row.get("qty")
            received_qty = round(received_qty + float(qty_num(raw_qty) or 0), 4)
        if ppv not in (None, 0, 0.0):
            if qty_difference:
                bits = []
                for detail in qty_details or []:
                    bits.append(
                        f"{detail.get('part') or 'The line'} was invoiced {qty_text(detail.get('invoiced'))} "
                        f"and Quantity_Received is {qty_text(detail.get('received'))}."
                    )
                named = " ".join(bits) or "The invoiced quantity does not equal Quantity_Received."
                why_ppv = f"That Purchase Price Variance is a quantity difference. {named}"
            else:
                why_ppv = (
                    "That variance is a unit-price difference between the invoice and the PO. "
                    "The quantity invoiced matches Quantity_Received."
                )
            ppv_bit = (
                f"The receipt extension missed the PDF total by {dollar(gap if gap is not None else ppv)}, "
                f"which is under {dollar(PPV_LIMIT)}, so one signed Purchase Price Variance of {dollar(ppv)} "
                f"was posted. {why_ppv} After that charge the gap is {dollar(0)}."
            )
        else:
            ppv_bit = (
                "The selected receipt lines equal the PDF total, so the gap is $0.00 and no "
                "Purchase Price Variance was posted."
            )
        text = (
            f"AP Clerk: JP Steel invoice {number} is entered as a parts bill on PO {po} "
            f"and is not posted. The PDF total is {dollar(total)}. "
            f"Amount entered is {dollar(amount_entered if amount_entered is not None else total)}. "
            f"The invoice lines are {phrases}. "
            f"PO line {line_names or po} was ordered {qty_text(ordered)}. "
            f"Quantity_Received on the selected receipts is {qty_text(received_qty)}. "
            f"Receipts selected: {label}. {ppv_bit} Nothing is waiting on purchasing."
        )
    else:
        problem = (problems or [{}])[0]
        receipt_bit = ""
        if problem.get("receipts"):
            receipt_bit = " Receipts by Quantity_Received, not selected: " + receipt_label(
                [
                    {"id": row["id"], "qty_received": row["qty"], "unit": row["price"]}
                    for row in problem["receipts"]
                ]
            ) + "."
        entered = dollar(amount_entered if amount_entered is not None else 0)
        invoiced_phrase = problem.get("invoiced_phrase") or phrases
        if action == "missing_receipt":
            why = (
                "This cannot be finished because that line has no selectable receipt. "
                "Quantity_Received is 0 or there is no open receipt on the matching part, "
                "so this is a missing receipt and not a price variance."
            )
            ask = (
                f"Shawn should receive the invoiced quantity on PO {po}. "
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
            f"AP Clerk: @Shawn McKibben JP Steel invoice {number} cannot be finished. "
            f"The PDF total is {dollar(total)}. Amount entered on receipt lines is {entered}. "
            f"The invoice lines are {invoiced_phrase}. "
            f"PO {po} line {problem.get('line') or line_names or po} was ordered {qty_text(problem.get('ordered'))}. "
            f"Quantity_Received is {qty_text(problem.get('received'))}.{receipt_bit} {why} {ask} "
            f"The bill was moved to Transfer AP and is not posted."
        )
    text = re.sub(r"\s+", " ", text).strip()
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if "category=" in text or "owner=" in text or "<" in text or ">" in text:
        raise RuntimeError("note must be plain text")
    return text


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


def pages_for_invoice(pdf_bytes: bytes, invoice_number: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    found = False
    for page in reader.pages:
        if invoice_number in (page.extract_text() or ""):
            writer.add_page(page)
            found = True
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
        "batch_name": lookup_text(values.get("AP_Invoice_Batch")),
        "type": values.get("Invoice_Type"),
        "qc": qc,
        "receipt_rows": receipt_rows,
        "comments": comments,
        "line_count": len(lists.get("APInvoiceLine") or []),
        "ppv_amounts": ppv_amounts,
    }


def write_comment(client: Any, kimco_id: int, text: str, *, mention: bool) -> dict[str, Any]:
    attempts = [comment_values(kimco_id, text, mention=mention)]
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
    hits = []
    for msg in messages:
        folder = folders.get(str(msg.get("parentFolderId") or ""), {"name": "unknown", "parent": None})
        key = f"{folder.get('parent') or ''}/{folder.get('name')}"
        by_folder[key] = by_folder.get(key, 0) + 1
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        addr = str(frm.get("address") or "")
        name = str(frm.get("name") or "")
        subject = str(msg.get("subject") or "")
        if not _HINT.search(f"{addr} {name} {subject}"):
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
        "jp_messages": hits,
    }


def collect_invoices(graph: Any, scan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    invoices: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pdfs: dict[str, bytes] = {}
    seen: dict[str, dict[str, Any]] = {}
    for msg in scan["jp_messages"]:
        subject = msg["subject"]
        if _STATEMENT.search(subject):
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
                    "reason": "JP Steel email has no PDF attachment, so it is not a new invoice.",
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
        pages = layout_pages(content)
        page_bills, credits = parse_jpsteel_pages(pages)
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
        if not page_bills:
            skipped.append(
                {
                    "kind": "unparsed",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "reason": "PDF pages did not contain a JP Steel invoice whose lines add up to the total.",
                }
            )
            continue
        for bill in page_bills:
            number = bill["invoice_number"]
            bill["message_id"] = msg["id"]
            bill["subject"] = subject
            bill["email_received"] = msg["received_chicago"]
            bill["email_received_utc"] = msg["received"]
            bill["folder"] = msg["folder"]
            bill["pdf_name"] = name
            bill["page_count"] = len(pages)
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
            invoice, status="HOLD", reason="PO id missing", kimco_id=None, batch=batch_id, receipts="",
            note=f"AP Clerk: JP Steel invoice {invoice['invoice_number']} has no PO id in KIMCO.",
        )
    created = _create_header(
        client, invoice, batch_id, vendor_id, terms_id, remit_id, currency_id, int(po_id) if po_id else None,
        invoice_type=3 if invoice.get("po") else 4,
    )
    if created[0] is None:
        return _row(
            invoice, status="HOLD", reason=f"header create HTTP {created[2]}", kimco_id=None,
            batch=batch_id, receipts="",
            note=f"AP Clerk: JP Steel invoice {invoice['invoice_number']} header was not created.",
        )
    created_id = int(created[0])
    pdf = pages_for_invoice(pdfs[invoice["invoice_number"]], invoice["invoice_number"])
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
    print(f"HOLD {invoice['invoice_number']} id={created_id} {plan['action']} transfer={moved.get('status')} mention={comment.get('mention')}", flush=True)
    return _row(
        invoice, status="HOLD", reason=plan["action"], kimco_id=created_id,
        batch=moved.get("batch_name") or BATCH_NAME, receipts="",
        note=note, comments_1_id=comment.get("id"), transfer_ap="yes" if on_transfer else "no",
        attached=attach == "attached", gap=live["qc"].get("gap"),
    )


def _select_receipts(client, kimco_id: int, receipt_ids: list[int]) -> str:
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


def already_record(client: Any, number: str, kimco_id: int) -> dict[str, Any]:
    live = snapshot_amounts(client, kimco_id)
    record = client.get_item("ap_invoices", kimco_id)
    values = record.get("values") or {}
    return {
        "invoice_number": number,
        "kimco_bill_id": kimco_id,
        "posted": bool(values.get("Posted")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")) or None,
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "invoice_amount": live.get("amount"),
        "verification_amount": live.get("verification"),
    }


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
        f"JP Steel messages {len(scan['jp_messages'])}",
        flush=True,
    )
    invoices, skipped, pdfs = collect_invoices(graph, scan)
    print(f"Invoices {len(invoices)} skipped {len(skipped)}", flush=True)
    for bill in invoices:
        print(
            f"PDF {bill['invoice_number']} PO {bill['po'] or 'none'} total {bill['total']} "
            f"lines {len(bill['lines'])} received {bill['email_received']}",
            flush=True,
        )
    numbers = {bill["invoice_number"].upper() for bill in invoices}
    print("Checking KIMCO invoice numbers", flush=True)
    existing = find_invoice_ids(client, numbers)
    print(f"Already in KIMCO {sorted(existing)}", flush=True)
    wanted = {str(bill["po"]) for bill in invoices if bill.get("po")}
    print("Loading PO lines and receipts", flush=True)
    po_lines = load_po_lines(client, wanted)
    receipts = load_receipts(client, wanted)
    print(f"PO lines {len(po_lines)} receipts {len(receipts)}", flush=True)
    plans = []
    already = []
    for bill in invoices:
        if bill["invoice_number"].upper() in existing:
            kimco_id = existing[bill["invoice_number"].upper()]
            already.append(already_record(client, bill["invoice_number"], kimco_id))
            print(f"ALREADY {bill['invoice_number']} {kimco_id}", flush=True)
            continue
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
                "jp_messages": len(scan["jp_messages"]),
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
                    "email_received": plan["invoice"]["email_received"],
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
    if vendor_id != VENDOR_ID or "JP STEEL" not in vendor_name.upper() or not terms_id or not remit_id or not currency_id:
        raise SystemExit(f"JP Steel sample {VENDOR_SAMPLE_ID} vendor/terms/remit changed: vendor {vendor_id} {vendor_name}")
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
            already.append(already_record(client, number, kimco_id))
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
                kimco_id=None, batch=batch_id, receipts="",
                note=f"AP Clerk: JP Steel invoice {number} header was not created.",
            ))
            continue
        created_id = int(created[0])
        pdf = pages_for_invoice(pdfs[number], number)
        attach = client.try_official_attach(
            created_id, name=f"{number}.pdf", content_type="application/pdf", size=len(pdf), content=pdf
        )
        if attach != "attached":
            note = (
                f"AP Clerk: JP Steel invoice {number} header {created_id} was created "
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
                f"AP Clerk: JP Steel invoice {number} was created as KIMCO {created_id} "
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
                lines=plan["lines"], qty_difference=bool(plan.get("qty_difference")),
                qty_details=plan.get("qty_details"),
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
            hold_plan = {
                **plan,
                "action": "quantity_variance" if plan.get("qty_difference") else "price_variance",
                "problems": [{
                    "kind": "quantity_variance" if plan.get("qty_difference") else "price_variance",
                    "line": invoice["po"],
                    "ordered": None,
                    "invoiced": None,
                    "invoiced_phrase": "; ".join(quantity_phrase(line) for line in invoice["lines"]),
                    "received": None,
                    "receipts": [],
                    "gap": gap,
                }],
            }
            note = build_note(
                invoice, status="HOLD", action=hold_plan["action"], problems=hold_plan["problems"],
                amount_entered=0, gap=gap, lines=plan.get("lines"),
            )
            comment = write_comment(client, created_id, note, mention=True)
            moved = apply_transfer_ap_batch_move(client, kimco_id=created_id) if comment.get("status") == "persisted" else {"status": "not-moved"}
            live = snapshot_amounts(client, created_id)
            on_transfer = moved.get("status") in {"moved", "already-on-transfer-ap"}
            results.append(_row(
                invoice, status="HOLD", reason=hold_plan["action"], kimco_id=created_id,
                batch=moved.get("batch_name") or BATCH_NAME, receipts="", note=note,
                comments_1_id=comment.get("id"), transfer_ap="yes" if on_transfer else "no",
                attached=True, gap=live["qc"].get("gap"),
            ))
            print(f"HOLD-PRICE {number} id={created_id}", flush=True)
            continue
        note = (
            f"AP Clerk: JP Steel invoice {number} was created as KIMCO {created_id} "
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
        "run": "jpsteel-2026-09-25",
        "batch_name": BATCH_NAME,
        "batch_id": batch_id,
        "posted": False,
        "packing_slip_gate": "suspended",
        "quantity_field": "Quantity_Received",
        "mailbox_scan": {
            "messages_in_window": scan["messages_in_window"],
            "folder_count": scan["folder_count"],
            "by_folder": scan["by_folder"],
            "jp_messages": len(scan["jp_messages"]),
        },
        "skipped": skipped,
        "already_in_kimco": already,
        "invoices": [_public_row(row) for row in results],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"WROTE {OUT_JSON}", flush=True)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    payload = enter_live(write="--live" in args and "--write" in args)
    if payload.get("dry"):
        print(json.dumps({"dry": True, "count": len(payload["invoices"]), "already": len(payload["already_in_kimco"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
