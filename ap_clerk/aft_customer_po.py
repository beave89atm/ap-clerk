"""Customer P.O. No. field only. Other header numbers are not purchase orders.

AFT invoices print several numbers across the header. Treyce circled
Customer P.O. No. Receipts are selectable only when quantity and price
both match that invoice line to the penny.

NOTE-60: AFT Industries (vendor 1383) buys and receives in eachs. A printed
pound weight is not the commercial quantity. Automated Finishing Technology
(vendor 1329, API Vendor.id 331) is a different vendor.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from ap_clerk.rules import TREYCE_MENTION_ID, ap_clerk_edit_note, money

# Vendor number on the KIMCO vendor name (1383-AFT Industries). Not the API id.
AFT_INDUSTRIES_VENDOR = 1383
# Vendor number from Shawn 2026-10-01. API Vendor.id 331 is the same company
# in VENDOR_ID_ALIASES ("automated finishing"). Neither number is 1383.
AUTOMATED_FINISHING_VENDOR = 1329
AUTOMATED_FINISHING_API_VENDOR_ID = 331

_WEIGHT_UOM = re.compile(r"\b(?:lb|lbs|pound|pounds)\b", re.I)
_EACH_UOM = re.compile(r"\b(?:ea|each|eachs|pc|pcs|piece|pieces)\b", re.I)
_WEIGHT_QTY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:lb|lbs|pound|pounds)\b",
    re.I,
)

_LABEL_WORDS = {"no", "number", "num"}


def _token(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _is_po_digits(text: str) -> bool:
    return bool(re.fullmatch(r"\d{5,6}", str(text or "").strip()))


def _is_money_token(text: str) -> bool:
    raw = str(text or "").strip().replace("$", "").replace(",", "")
    return bool(re.fullmatch(r"\d+\.\d{2}", raw))


def _money_token(text: str) -> float | None:
    if not _is_money_token(text):
        return None
    return money(str(text).strip().replace("$", "").replace(",", ""))


def _qty_token(text: str) -> float | None:
    raw = str(text or "").strip().replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return None
    if _is_money_token(text):
        return None
    return float(raw)


def cluster_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group positioned words into visual lines. Y direction does not matter."""
    remaining = sorted(words, key=lambda word: (-float(word["y"]), float(word["x"])))
    lines: list[list[dict[str, Any]]] = []
    while remaining:
        anchor = float(remaining[0]["y"])
        same = [word for word in remaining if abs(float(word["y"]) - anchor) <= 2.5]
        remaining = [word for word in remaining if abs(float(word["y"]) - anchor) > 2.5]
        same.sort(key=lambda word: float(word["x"]))
        lines.append(same)
    return lines


def _label_span(line: list[dict[str, Any]]) -> tuple[float, float, int] | None:
    """X span of a Customer P.O. No. label, and the index of its last word."""
    tokens = [_token(word["text"]) for word in line]
    for index, token in enumerate(tokens):
        if token != "customer":
            continue
        rest = tokens[index + 1 : index + 4]
        if not rest:
            continue
        consumed = 0
        if rest[0] == "po" and len(rest) > 1 and rest[1] in _LABEL_WORDS:
            consumed = 3
        elif rest[0] in {"pono", "ponumber"}:
            consumed = 2
        if not consumed:
            continue
        end = index + consumed - 1
        return float(line[index]["x"]), float(line[end]["x"]), end
    return None


def _same_line_value(line: list[dict[str, Any]], label_end_x: float, label_end_index: int) -> str | None:
    for word in line[label_end_index + 1 :]:
        if float(word["x"]) + 1 < label_end_x:
            continue
        if float(word["x"]) - label_end_x > 140:
            break
        if _is_po_digits(word["text"]):
            return str(word["text"]).strip()
    return None


def _column_value(
    lines: list[list[dict[str, Any]]],
    line_index: int,
    x0: float,
    x1: float,
) -> str | None:
    anchor_y = float(lines[line_index][0]["y"])
    best_text: str | None = None
    best_dy: float | None = None
    for index, line in enumerate(lines):
        if index == line_index:
            continue
        for word in line:
            if not _is_po_digits(word["text"]):
                continue
            x_pos = float(word["x"])
            if x_pos < x0 - 12 or x_pos > x1 + 36:
                continue
            dy = abs(float(word["y"]) - anchor_y)
            if dy <= 2.5:
                continue
            if best_dy is None or dy < best_dy - 0.6:
                best_text = str(word["text"]).strip()
                best_dy = dy
            elif best_dy is not None and abs(dy - best_dy) <= 0.6 and str(word["text"]).strip() != best_text:
                return None
    return best_text


def customer_po_from_layout(text: str, *, invoice_number: str | None = None) -> str | None:
    """Customer P.O. No. from pdftotext -layout. Other columns on that row are ignored."""
    lines = str(text or "").splitlines()
    found: list[str] = []
    label_re = re.compile(r"CUSTOMER\s+P\.?\s*O\.?\s*NO\.?", re.I)
    for index, line in enumerate(lines):
        match = label_re.search(line)
        if not match:
            continue
        same = re.match(r"[\s:]*(\d{5,6})\b", line[match.end() :])
        if same:
            found.append(same.group(1))
            continue
        for follower in lines[index + 1 : index + 4]:
            if not follower.strip():
                continue
            for number in re.finditer(r"\d{5,6}", follower):
                if number.start() < match.start() - 2 or number.start() > match.end() + 2:
                    continue
                found.append(number.group())
            break
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        return None
    if invoice_number and unique[0] == str(invoice_number).strip():
        return None
    return unique[0]


def customer_po_from_words(
    words: list[dict[str, Any]],
    *,
    invoice_number: str | None = None,
) -> str | None:
    """Return the Customer P.O. No. value. Other header numbers are ignored."""
    lines = cluster_lines(words)
    found: list[str] = []
    for index, line in enumerate(lines):
        span = _label_span(line)
        if span is None:
            continue
        x0, x1, end_index = span
        value = _same_line_value(line, x1, end_index) or _column_value(lines, index, x0, x1)
        if value:
            found.append(value)
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        return None
    if invoice_number and unique[0] == str(invoice_number).strip():
        return None
    return unique[0]


def _line_text(line: list[dict[str, Any]]) -> str:
    return " ".join(str(word["text"]) for word in line)


def _parse_qty_price(line: list[dict[str, Any]]) -> dict[str, Any] | None:
    qty_words = []
    money_words = []
    for word in line:
        qty = _qty_token(word["text"])
        amount = _money_token(word["text"])
        if amount is not None:
            money_words.append(amount)
        elif qty is not None:
            qty_words.append(qty)
    if len(money_words) < 2 or not qty_words:
        return None
    unit = money_words[-2]
    ext = money_words[-1]
    matches = [qty for qty in qty_words if money(qty * unit) == ext]
    if len(matches) != 1:
        return None
    description = _line_text(line)
    return {"qty": matches[0], "unit_price": unit, "ext": ext, "description": description}


_SKIP_LINE = re.compile(
    r"\b(sub\s*total|total|sales\s*tax|tax|freight|shipping|amount\s*due|balance)\b",
    re.I,
)


def merchandise_lines(words: list[dict[str, Any]], total: float) -> list[dict[str, Any]]:
    """Item rows whose extensions add up to the invoice total. Empty when unproven."""
    rows: list[dict[str, Any]] = []
    for line in cluster_lines(words):
        if _SKIP_LINE.search(_line_text(line)):
            continue
        parsed = _parse_qty_price(line)
        if parsed is not None:
            rows.append(parsed)
    rows = _drop_rollup_rows(rows)
    if not rows:
        return []
    if money(sum(row["ext"] for row in rows)) != money(total):
        return []
    return rows


def _drop_rollup_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = list(rows)
    changed = True
    while changed and len(kept) > 1:
        changed = False
        for index, row in enumerate(kept):
            others = kept[:index] + kept[index + 1 :]
            if money(sum(item["ext"] for item in others)) == money(row["ext"]):
                kept = others
                changed = True
                break
    return kept


def qty_equal(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) < 1e-6
    except (TypeError, ValueError):
        return False


def penny_equal(left: Any, right: Any) -> bool:
    left_money = money(left)
    right_money = money(right)
    return left_money is not None and left_money == right_money


def _strict_receipt_match(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Quantity, unit price, and extension all match. Printed pounds stay pounds."""
    if not qty_equal(line.get("qty"), receipt.get("qty")):
        return False
    if not penny_equal(line.get("unit_price"), receipt.get("unit_price")):
        return False
    return penny_equal(line.get("ext"), _receipt_ext(receipt))


def receipt_matches_line(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Match quantity and price to the penny.

    A priced pound line stays on that quantity. When the commercial quantity
    is eachs, a printed weight does not count as that quantity.
    """
    if eachs_basis(line, receipt):
        return eachs_receipt_matches(line, receipt)
    return _strict_receipt_match(line, receipt)


def _blob(*parts: Any) -> str:
    return " ".join(str(part) for part in parts if part not in (None, ""))


def _vendor_code(value: Any) -> int | None:
    """Vendor number from an id or a '1383-Name' label. PO numbers are not codes."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{3,5}", text):
        return int(text)
    match = re.match(r"^(\d{3,5})\s*[-–]\s*\S", text)
    if match:
        return int(match.group(1))
    return None


def _vendor_name(value: Any) -> str:
    if isinstance(value, dict):
        return _blob(value.get("text"), value.get("name"), value.get("vendor"))
    return str(value or "")


def _vendor_blob(vendor_id: Any = None, name: Any = None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _blob(vendor_id, _vendor_name(name)).lower()).strip()


def is_automated_finishing_vendor(vendor_id: Any = None, name: Any = None) -> bool:
    """Automated Finishing Technology. Not AFT Industries."""
    code = _vendor_code(vendor_id)
    if code is None:
        code = _vendor_code(name)
    if code in {AUTOMATED_FINISHING_VENDOR, AUTOMATED_FINISHING_API_VENDOR_ID}:
        return True
    return "automated finishing" in _vendor_blob(vendor_id, name)


def is_aft_industries_vendor(vendor_id: Any = None, name: Any = None) -> bool:
    """AFT Industries, vendor 1383. A name that is only Automated Finishing is not."""
    if is_automated_finishing_vendor(vendor_id, name):
        return False
    code = _vendor_code(vendor_id)
    if code is None:
        code = _vendor_code(name)
    if code == AFT_INDUSTRIES_VENDOR:
        return True
    return "aft industries" in _vendor_blob(vendor_id, name)


def aft_vendor_mismatch(
    *,
    bill_vendor_id: Any = None,
    bill_vendor_name: Any = None,
    po_vendor_id: Any = None,
    po_vendor_name: Any = None,
) -> bool:
    """True when an AFT Industries bill is aimed at an Automated Finishing PO."""
    if not is_aft_industries_vendor(bill_vendor_id, bill_vendor_name):
        return False
    return is_automated_finishing_vendor(po_vendor_id, po_vendor_name) or not is_aft_industries_vendor(
        po_vendor_id, po_vendor_name
    )


def _line_blob(line: dict[str, Any]) -> str:
    return _blob(
        line.get("description"),
        line.get("text"),
        line.get("uom"),
        line.get("qty_uom"),
    )


def _line_uom(line: dict[str, Any]) -> str:
    return _blob(line.get("uom"), line.get("qty_uom"))


def _receipt_uom(receipt: dict[str, Any]) -> str:
    return _blob(receipt.get("uom"), receipt.get("qty_uom"), receipt.get("unit_uom"))


def printed_weight_lb(line: dict[str, Any]) -> float | None:
    """Pounds printed on the invoice line. Not the commercial each quantity."""
    for key in ("printed_weight_lb", "weight_lb", "weight_qty"):
        if line.get(key) not in (None, ""):
            try:
                return float(line[key])
            except (TypeError, ValueError):
                return None
    match = _WEIGHT_QTY.search(_line_blob(line))
    if match:
        return float(match.group(1))
    if _WEIGHT_UOM.search(_line_uom(line)) and line.get("qty") not in (None, ""):
        try:
            return float(line["qty"])
        except (TypeError, ValueError):
            return None
    return None


def _each_uom(text: str) -> bool:
    return bool(_EACH_UOM.search(text or ""))


def shawn_confirmed_eachs(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Shawn confirmed the commercial quantity is eachs. 'Done' is not that confirmation."""
    return bool(line.get("shawn_confirmed_eachs") or receipt.get("shawn_confirmed_eachs"))


def commercial_each_qty(line: dict[str, Any]) -> float | None:
    """Each quantity used for price. A printed pound quantity is not this number."""
    if line.get("each_qty") not in (None, ""):
        try:
            return float(line["each_qty"])
        except (TypeError, ValueError):
            return None
    if not _each_uom(_line_uom(line)) or line.get("qty") in (None, ""):
        return None
    try:
        qty = float(line["qty"])
    except (TypeError, ValueError):
        return None
    weight = printed_weight_lb(line)
    if weight is not None and qty_equal(qty, weight):
        return None
    return qty


def eachs_unit_price(ext: Any, each_qty: Any) -> float | None:
    """Unit price = invoice line dollars ÷ each qty, to 4 decimals."""
    extension = money(ext)
    try:
        qty = float(each_qty)
    except (TypeError, ValueError):
        return None
    if extension is None or qty == 0:
        return None
    return round(extension / qty, 4)


def _receipt_ext(receipt: dict[str, Any]) -> Any:
    if receipt.get("ext") is not None:
        return receipt.get("ext")
    if receipt.get("qty") in (None, "") or receipt.get("unit_price") in (None, ""):
        return None
    return money(float(receipt["qty"]) * float(receipt["unit_price"]))


def eachs_basis(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """True when eachs, not the printed weight, is the quantity to match."""
    each = commercial_each_qty(line)
    if each is None:
        return False
    confirmed = shawn_confirmed_eachs(line, receipt) or _each_uom(_receipt_uom(receipt)) or _each_uom(_line_uom(line))
    if not confirmed:
        return False
    weight = printed_weight_lb(line)
    if weight is None:
        return False
    return not qty_equal(weight, each)


def eachs_receipt_matches(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Receipt each qty and extension match. Unit price is extension ÷ each qty."""
    if not eachs_basis(line, receipt):
        return False
    each = commercial_each_qty(line)
    if each is None or not qty_equal(each, receipt.get("qty")):
        return False
    if not penny_equal(line.get("ext"), _receipt_ext(receipt)):
        return False
    derived = eachs_unit_price(line.get("ext"), each)
    unit = receipt.get("unit_price")
    if derived is None or unit in (None, ""):
        return False
    try:
        unit_f = float(unit)
    except (TypeError, ValueError):
        return False
    if abs(unit_f - derived) > 0.0002:
        return False
    return penny_equal(line.get("ext"), money(each * unit_f))


def weight_vs_eachs_conflict(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Invoice prints pounds and the receipt is eachs, and those quantities differ."""
    weight = printed_weight_lb(line)
    line_is_weight = weight is not None or bool(_WEIGHT_UOM.search(_line_blob(line)))
    receipt_is_each = _each_uom(_receipt_uom(receipt)) or shawn_confirmed_eachs(line, receipt)
    if not line_is_weight or not receipt_is_each:
        return False
    each = commercial_each_qty(line)
    if weight is not None and each is not None and not qty_equal(weight, each):
        return True
    if weight is not None and receipt.get("qty") not in (None, "") and not qty_equal(weight, receipt.get("qty")):
        return True
    return not qty_equal(line.get("qty"), receipt.get("qty"))


def dollars_match_uom_qty_hold(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """Dollars match after a re-receive, but pieces and pounds are still different.

    A Shawn 'Done' flag does not confirm eachs. Dollars alone do not Select.
    """
    if eachs_receipt_matches(line, receipt) or _strict_receipt_match(line, receipt):
        return False
    if not penny_equal(line.get("ext"), _receipt_ext(receipt)):
        return False
    if not weight_vs_eachs_conflict(line, receipt):
        return False
    each = commercial_each_qty(line)
    if each is not None and qty_equal(each, receipt.get("qty")):
        return False
    return True


def select_without_work_order_available() -> bool:
    """True only when Select Receipts already has a way to omit Work_Order.

    receipt_line_values_from_records copies Work_Order onto the AP line.
    There is no omit flag. Do not invent a live call that drops it.
    """
    from ap_clerk.kimco import receipt_line_values_from_records

    names = set(inspect.signature(receipt_line_values_from_records).parameters)
    return bool(names & {"omit_work_order", "include_work_order", "skip_work_order"})


def work_order_blocks_select(receipt: dict[str, Any], *, vendor_ok: bool) -> bool:
    """Wrong vendor: never Select. Correct vendor: Select without WO only if that path exists."""
    rejected = bool(receipt.get("work_order_rejected") or receipt.get("work_order_reject"))
    if not rejected:
        return False
    if not vendor_ok:
        return True
    return not select_without_work_order_available()


def assign_receipts(
    lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Select a receipt only when one open receipt matches that line and no other line."""
    if not lines or not receipts:
        return []
    hits = [[receipt for receipt in receipts if receipt_matches_line(line, receipt)] for line in lines]
    chosen: list[dict[str, Any]] = []
    used: set[Any] = set()
    for index, options in enumerate(hits):
        if len(options) != 1:
            continue
        receipt = options[0]
        receipt_id = receipt.get("id")
        if receipt_id in used:
            continue
        shared = False
        for other_index, other_options in enumerate(hits):
            if other_index == index:
                continue
            if any(item.get("id") == receipt_id for item in other_options):
                shared = True
                break
        if shared:
            continue
        chosen.append(receipt)
        used.add(receipt_id)
    return chosen


def payable_amount(snapshot: dict[str, Any]) -> tuple[float | None, str]:
    """Open payable on the bill. Skip a zero rollup when the header total is set."""
    order = (
        ("balance", "Invoice_Balance"),
        ("net", "Invoice_Net_Amount"),
        ("amount", "Invoice_Amount"),
        ("verification", "Invoice_Verification_Amount"),
    )
    for key, label in order:
        value = money(snapshot.get(key))
        if value not in (None, 0, 0.0):
            return value, label
    for key, label in order:
        value = money(snapshot.get(key))
        if value is not None:
            return value, label
    return None, ""


def _fmt_price(value: Any) -> str:
    number = round(float(value), 4)
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    if "." not in text:
        return f"{number:.2f}"
    decimals = text.split(".", 1)[1]
    if len(decimals) < 2:
        return f"{number:.2f}"
    return text


def build_uom_qty_hold_note(
    *,
    invoice: str,
    po: str,
    line: dict[str, Any],
    receipt: dict[str, Any],
) -> str:
    """UOM/qty hold. Tags Shawn. The bill stays in Transfer AP and is not posted."""
    weight = printed_weight_lb(line)
    weight_txt = _fmt_qty(weight) if weight is not None else _fmt_qty(line.get("qty"))
    plain = (
        f"AP Clerk: @Shawn McKibben UOM and quantity still do not match on invoice {invoice}. "
        f"Customer P.O. No. {po}. The invoice prints {weight_txt} lb and receipt {receipt.get('id')} "
        f"is qty {_fmt_qty(receipt.get('qty'))} {_receipt_uom(receipt) or 'pieces'}. "
        "Dollars match is not a quantity match. Receipts were not selected. "
        "The bill stays in Transfer AP and is not posted."
    )
    if re.search(r"\bSuccess\b", plain):
        raise ValueError("note invents Success")
    html = ap_clerk_edit_note(plain, action="receiving")
    if 'data-mention-id="104"' not in html:
        raise ValueError("UOM note is missing the Shawn mention")
    if re.search(r"\bSuccess\b", html):
        raise ValueError("note invents Success")
    return html


def build_eachs_select_note(
    *,
    po: str,
    invoice: str,
    selected: list[dict[str, Any]],
    payable: float,
) -> str:
    """After an eachs Select on Transfer AP, tag Treyce. Does not say Success."""
    bits = []
    for row in selected:
        each = row.get("qty")
        ext = _receipt_ext(row)
        unit = eachs_unit_price(ext, each)
        unit_txt = _fmt_price(unit if unit is not None else row.get("unit_price"))
        bits.append(f"receipt {row['id']} qty {_fmt_qty(each)} at {unit_txt}")
    plain = (
        f"AP Clerk: @Treyce Hodges Customer P.O. No. {po} was linked on invoice {invoice}. "
        f"Each quantity and dollars matched. Receipts were selected: {'; '.join(bits)}. "
        "Printed weight was not used as the quantity. "
        f"Payable total is ${payable:.2f}. "
        "The bill stays in Transfer AP and is not posted."
    )
    if re.search(r"shawn", plain, re.I) or re.search(r"\bSuccess\b", plain):
        raise ValueError("eachs note must tag Treyce only and must not say Success")
    html = ap_clerk_edit_note(plain, batch_id=375)
    if f'data-mention-id="{TREYCE_MENTION_ID}"' not in html:
        raise ValueError("note is missing the Treyce mention")
    if re.search(r"shawn", html, re.I) or re.search(r"\bSuccess\b", html):
        raise ValueError("eachs note must tag Treyce only and must not say Success")
    return html


def build_vendor_mismatch_note(
    *,
    po: str,
    invoice: str,
    payable: float,
    automated_finishing: bool,
) -> str:
    """Wrong PO vendor. Tag Treyce on Transfer AP. Do not Select."""
    if automated_finishing:
        which = (
            f"AFT Industries ({AFT_INDUSTRIES_VENDOR}) is not Automated Finishing Technology "
            f"({AUTOMATED_FINISHING_VENDOR}). "
        )
    else:
        which = f"Customer P.O. No. {po} is not vendor {AFT_INDUSTRIES_VENDOR}. "
    plain = (
        f"AP Clerk: @Treyce Hodges vendor_mismatch on invoice {invoice}. "
        f"{which}Customer P.O. No. {po} was not linked. "
        "Receipts were not selected. "
        f"Payable total is ${payable:.2f}. "
        "The bill stays in Transfer AP and is not posted."
    )
    if re.search(r"\bSuccess\b", plain):
        raise ValueError("note invents Success")
    html = ap_clerk_edit_note(plain, batch_id=375)
    if f'data-mention-id="{TREYCE_MENTION_ID}"' not in html:
        raise ValueError("note is missing the Treyce mention")
    if re.search(r"\bSuccess\b", html):
        raise ValueError("note invents Success")
    return html


def build_work_order_hold_note(*, po: str, invoice: str, receipt_ids: list[Any], payable: float) -> str:
    """Correct AFT receipt rejected Work_Order, and select-without-WO does not exist."""
    listed = ", ".join(str(item) for item in receipt_ids)
    plain = (
        f"AP Clerk: @Treyce Hodges invoice {invoice} Customer P.O. No. {po} "
        f"matched each quantity and dollars, but receipt {listed} rejected Work_Order. "
        "Select without Work_Order is not available. Receipts were not selected. "
        f"Payable total is ${payable:.2f}. "
        "The bill stays in Transfer AP and is not posted."
    )
    if re.search(r"\bSuccess\b", plain):
        raise ValueError("note invents Success")
    html = ap_clerk_edit_note(plain, batch_id=375)
    if f'data-mention-id="{TREYCE_MENTION_ID}"' not in html:
        raise ValueError("note is missing the Treyce mention")
    return html


def _decision(
    *,
    invoice: str,
    po: str,
    payable: float,
    selected: list[dict[str, Any]],
    link: bool,
    vendor_mismatch: bool,
    category: str,
    why: str,
    note: str,
    select: bool,
) -> dict[str, Any]:
    return {
        "invoice": invoice,
        "po": po,
        "posted": False,
        "batch": "Transfer AP",
        "result": "HOLD",
        "selected": selected,
        "receipts_selected": select,
        "select": select,
        "link": link,
        "vendor_mismatch": vendor_mismatch,
        "category": category,
        "why": why,
        "note": note,
        "payable": payable,
    }


def decide_aft_industries(
    *,
    invoice: str,
    po: str,
    lines: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    payable: float,
    bill_vendor_id: Any = None,
    bill_vendor_name: Any = None,
    po_vendor_id: Any = None,
    po_vendor_name: Any = None,
) -> dict[str, Any]:
    """Select only an AFT Industries eachs match. Never Success. Never posts.

    No live KIMCO call. Work_Order rejection does not invent a select-without-WO payload.
    """
    bill_ok = is_aft_industries_vendor(bill_vendor_id, bill_vendor_name)
    po_ok = is_aft_industries_vendor(po_vendor_id, po_vendor_name)
    if not bill_ok:
        why = (
            f"Invoice {invoice} is not AFT Industries ({AFT_INDUSTRIES_VENDOR}). "
            "Receipts were not selected. The bill stays in Transfer AP and is not posted."
        )
        plain = (
            f"AP Clerk: @Treyce Hodges {why} Payable total is ${payable:.2f}."
        )
        return _decision(
            invoice=invoice,
            po=po,
            payable=payable,
            selected=[],
            link=False,
            vendor_mismatch=False,
            category="",
            why=why,
            note=ap_clerk_edit_note(plain, batch_id=375),
            select=False,
        )
    wrong_auto = bill_ok and is_automated_finishing_vendor(po_vendor_id, po_vendor_name)
    if wrong_auto or (bill_ok and not po_ok):
        if wrong_auto:
            why = (
                "category=vendor_mismatch; owner=AP / vendor master. "
                f"AFT Industries ({AFT_INDUSTRIES_VENDOR}) invoice {invoice} cannot use "
                f"Customer P.O. No. {po}. That PO is Automated Finishing Technology "
                f"({AUTOMATED_FINISHING_VENDOR}). Receipts were not selected."
            )
        else:
            why = (
                "category=vendor_mismatch; owner=AP / vendor master. "
                f"Customer P.O. No. {po} is not an AFT Industries ({AFT_INDUSTRIES_VENDOR}) "
                f"purchase order. Invoice {invoice}. Receipts were not selected."
            )
        return _decision(
            invoice=invoice,
            po=po,
            payable=payable,
            selected=[],
            link=False,
            vendor_mismatch=True,
            category="vendor_mismatch",
            why=why,
            note=build_vendor_mismatch_note(
                po=po,
                invoice=invoice,
                payable=payable,
                automated_finishing=wrong_auto,
            ),
            select=False,
        )

    chosen = assign_receipts(lines, receipts)
    blocked = [row for row in chosen if work_order_blocks_select(row, vendor_ok=po_ok)]
    if blocked:
        ids = [row.get("id") for row in blocked]
        why = (
            f"Work_Order was rejected on AFT Industries receipt {ids}. "
            "Select without Work_Order is not an existing API. "
            "Receipts were not selected. The bill stays in Transfer AP and is not posted."
        )
        return _decision(
            invoice=invoice,
            po=po,
            payable=payable,
            selected=[],
            link=po_ok,
            vendor_mismatch=False,
            category="other",
            why=why,
            note=build_work_order_hold_note(po=po, invoice=invoice, receipt_ids=ids, payable=payable),
            select=False,
        )

    if chosen and len(chosen) == len(lines):
        by_eachs = all(any(eachs_receipt_matches(line, row) for line in lines) for row in chosen)
        if by_eachs:
            why = (
                f"Each quantity and dollars match on invoice {invoice}. "
                "Printed weight was not the quantity. Receipts were selected. "
                "The bill stays in Transfer AP and is not posted."
            )
            note = build_eachs_select_note(po=po, invoice=invoice, selected=chosen, payable=payable)
        else:
            why = (
                f"Quantity and price matched invoice {invoice} to the penny. "
                "Receipts were selected. The bill stays in Transfer AP and is not posted."
            )
            note = build_treyce_note(
                po=po,
                invoice=invoice,
                linked=po_ok,
                selected=chosen,
                payable=payable,
                match_reason="selected",
            )
        return _decision(
            invoice=invoice,
            po=po,
            payable=payable,
            selected=chosen,
            link=po_ok,
            vendor_mismatch=False,
            category="",
            why=why,
            note=note,
            select=True,
        )

    conflicts = [
        (line, receipt)
        for line in lines
        for receipt in receipts
        if dollars_match_uom_qty_hold(line, receipt)
    ]
    if conflicts:
        line, receipt = conflicts[0]
        weight = printed_weight_lb(line)
        why = (
            "category=quantity_variance; owner=Shawn McKibben. "
            f"UOM/qty: invoice {invoice} prints {weight} lb and receipt {receipt.get('id')} "
            f"qty {receipt.get('qty')} is still pieces, not pounds. "
            "Dollars match. Receipts were not selected. "
            "The bill stays in Transfer AP and is not posted."
        )
        return _decision(
            invoice=invoice,
            po=po,
            payable=payable,
            selected=[],
            link=False,
            vendor_mismatch=False,
            category="quantity_variance",
            why=why,
            note=build_uom_qty_hold_note(invoice=invoice, po=po, line=line, receipt=receipt),
            select=False,
        )

    why = (
        f"Receipts were not selected because quantity and price did not match "
        f"invoice {invoice} to the penny."
    )
    return _decision(
        invoice=invoice,
        po=po,
        payable=payable,
        selected=[],
        link=False,
        vendor_mismatch=False,
        category="quantity_variance",
        why=why,
        note=build_treyce_note(
            po=po,
            invoice=invoice,
            linked=False,
            selected=[],
            payable=payable,
            match_reason="matched-none",
        ),
        select=False,
    )


def build_treyce_note(
    *,
    po: str,
    invoice: str,
    linked: bool,
    selected: list[dict[str, Any]],
    payable: float,
    match_reason: str,
) -> str:
    """One Comments_1 note. Tags Treyce. Does not mention Shawn."""
    if linked:
        link = f"Customer P.O. No. {po} was linked on invoice {invoice}."
    else:
        link = f"Customer P.O. No. {po} was not linked on invoice {invoice}."
    if selected:
        bits = "; ".join(
            f"receipt {row['id']} qty {_fmt_qty(row['qty'])} at {float(row['unit_price']):.2f}"
            for row in selected
        )
        receipts = f"Receipts were selected: {bits}."
    elif match_reason == "matched-none":
        receipts = (
            "Receipts were not selected because quantity and price did not match "
            "the invoice to the penny."
        )
    else:
        receipts = "Receipts were not selected."
    plain = (
        f"AP Clerk: @Treyce Hodges {link} {receipts} "
        f"Payable total is ${payable:.2f}. "
        "The bill stays in Transfer AP and is not posted."
    )
    if re.search(r"shawn", plain, re.I):
        raise ValueError("note mentions Shawn")
    html = ap_clerk_edit_note(plain, batch_id=375)
    if f'data-mention-id="{TREYCE_MENTION_ID}"' not in html:
        raise ValueError("note is missing the Treyce mention")
    if re.search(r"shawn", html, re.I):
        raise ValueError("note mentions Shawn")
    return html


def _fmt_qty(value: Any) -> str:
    number = float(value)
    if abs(number - round(number)) < 1e-6:
        return str(int(round(number)))
    return f"{number:g}"
