"""Customer P.O. No. field only. Other header numbers are not purchase orders.

AFT invoices print several numbers across the header. Treyce circled
Customer P.O. No. Receipts are selectable only when quantity and price
both match that invoice line to the penny.
"""

from __future__ import annotations

import re
from typing import Any

from ap_clerk.rules import TREYCE_MENTION_ID, ap_clerk_edit_note, money

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


def receipt_matches_line(line: dict[str, Any], receipt: dict[str, Any]) -> bool:
    """True only when quantity and both unit price and extension match to the penny."""
    if not qty_equal(line.get("qty"), receipt.get("qty")):
        return False
    if not penny_equal(line.get("unit_price"), receipt.get("unit_price")):
        return False
    receipt_ext = receipt.get("ext")
    if receipt_ext is None:
        receipt_ext = money(float(receipt["qty"]) * float(receipt["unit_price"]))
    return penny_equal(line.get("ext"), receipt_ext)


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
