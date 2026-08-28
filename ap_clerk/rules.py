"""AP clerk policy helpers. No network I/O."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
COMMENTS = "API TEST prototype only do not pay."
LIVE_COMMENTS = "API Agent"
CURRENCY_USD_ID = 3
INVOICE_TYPE_PO = 3
# Existing prototype no-PO vendor bills (UniFirst, Shoppa, Hudson, Luxor, GRM, etc.) use type 4.
INVOICE_TYPE_NO_PO = 4
FORBIDDEN_BATCH_IDS = {669}
FORBIDDEN_BATCH_NAMES = {"Mark Brown 8/4/26"}
FORBIDDEN_INVOICE_IDS = set(range(9474, 9479)) | set(range(9481, 9500))
def flag_in_outlook_for(result: str | None) -> str:
    """Outlook category column. Yes for Success (Entered in AI) and HOLD/Fail (AI HOLD)."""
    return "Yes" if (result or "").strip() in {"Success", "HOLD", "Fail"} else "No"


def comments_for(target: str) -> str:
    """Live bills are real; do not stamp the prototype 'do not pay' comment."""
    if (target or "").strip().lower() == "live":
        return LIVE_COMMENTS
    return COMMENTS


HOLD_ONLY_REASONS = {
    "check stop",
    "statement",
    "pod",
    "payment letter",
    "dup",
    "duplicate",
    "not-a-bill",
    "not a bill",
    "price does not match",
}

# Kyle 2026-08-28: PPV is signed Additional Charge Purchase Price Variance.
# Post only when |line variance| <= 10% of invoice total AND |bill PPV| <= $100.
PPV_MAX_PCT_OF_INVOICE = 0.10
PPV_MAX_ABS_ON_BILL = 100.00
PRICE_DOES_NOT_MATCH = "price does not match"
SHAWN_MCKIBBEN = "@Shawn McKibben"
PRICE_MISMATCH_PO_COMMENT = (
    "@Shawn McKibben price does not match. Purchasing must unreceive, change the PO price, "
    "and re-receive. Do not alter receipt unit price in GI (breaks WO cost, material cost, "
    "and PO clearing)."
)

# Treyce 2026-08-28: when name match fails, these vendors are known live ids.
# Do not Fail "vendor missing" when the PO has a vendor.
VENDOR_ID_ALIASES = {
    "national specialty alloys": 1386,
    "coherent": 1410,
    "ii vi": 1410,
}

# Printed invoice-number prefixes. Learn from the PDF first; apply only for
# these known vendor patterns. Do not invent a prefix for other vendors.
VENDOR_INVOICE_PREFIXES = {
    "modern heat treat": "8-",
}
FEE_KEYWORDS = (
    "shop supplies",
    "packaging",
    "recovery",
    "fuel",
    "energy surcharge",
    "freight",
    "shipping",
    "handling",
    "surcharge",
    "supply fee",
    "admin fee",
    "account maintenance",
    "check processing",
    "garment",
    "rental",
    "tdsp",
    "ercot",
    "market securitization",
    "settlement",
    "taxes/fees",
    "pass-through",
)

_SUFFIXES = {
    "inc",
    "llc",
    "ltd",
    "lp",
    "co",
    "company",
    "corp",
    "corporation",
    "services",
    "service",
    "of",
    "the",
    "gp",
    "dallas",
    "texas",
    "tx",
    "ft",
    "fort",
    "worth",
    "north",
}


def chicago_today(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=CHICAGO)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CHICAGO)
    return current.astimezone(CHICAGO).date()


def batch_name_for(day: date | None = None) -> str:
    """Exact KIMCO batch name: API Agent - M/D/YY in America/Chicago."""
    day = day or chicago_today()
    yy = day.strftime("%y")
    return f"API Agent - {day.month}/{day.day}/{yy}"


def format_ppv(amount: float | None) -> str:
    """Excel PPV column. Signed; negative is allowed. Zero / missing is none."""
    if amount is None:
        return "none"
    value = round(float(amount), 2)
    if value == 0:
        return "none"
    return f"{value:.2f}"


def known_vendor_id(name: str | None) -> int | None:
    """National Specialty Alloys → 1386; Coherent Corp. → 1410."""
    norm = normalize_name(name)
    if not norm:
        return None
    if norm in VENDOR_ID_ALIASES:
        return VENDOR_ID_ALIASES[norm]
    for key, vendor_id in VENDOR_ID_ALIASES.items():
        if key in norm or norm in key or names_match(name, key):
            return vendor_id
    return None


def known_invoice_prefix(vendor: str | None) -> str | None:
    """Modern Heat Treat prints 8-220804, not 220804. Other vendors: do not invent."""
    norm = normalize_name(vendor)
    if not norm:
        return None
    for key, prefix in VENDOR_INVOICE_PREFIXES.items():
        if key in norm or names_match(vendor, key):
            return prefix
    return None


def printed_invoice_number(
    number: str | None,
    *,
    vendor: str | None = None,
    text: str = "",
) -> str:
    """Keep the number as printed. Prefer PDF form; apply known vendor prefix only."""
    raw = (number or "").strip()
    blob = text or ""
    if raw:
        prefixed = re.search(rf"\b(\d-{re.escape(raw)})\b", blob)
        if prefixed:
            return prefixed.group(1)
        already = re.search(rf"\b({re.escape(raw)})\b", blob)
        if already and "-" in raw:
            return raw
    hits = re.findall(r"\b(\d-\d{5,8})\b", blob)
    if hits:
        if not raw:
            return hits[0]
        digits = re.sub(r"\D", "", raw)
        for hit in hits:
            if re.sub(r"\D", "", hit).endswith(digits) or digits.endswith(re.sub(r"\D", "", hit)[1:]):
                return hit
        return hits[0]
    prefix = known_invoice_prefix(vendor)
    if prefix and raw and "-" not in raw and re.fullmatch(r"\d{5,8}", raw):
        return f"{prefix}{raw}"
    return raw


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def decide_ppv(
    *,
    invoice_line_amount: float,
    po_line_amount: float,
    invoice_total: float,
    ppv_already_on_bill: float = 0.0,
    po_unit_price: float | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Kyle 2026-08-28 PPV rule. Fees never go through this helper (caller filters)."""
    if is_fee_or_surcharge(label):
        return {
            "action": "fee",
            "ppv": 0.0,
            "hold": False,
            "reason": "Fees and surcharges, never PPV",
            "po_comment": "",
        }
    unit = money(po_unit_price)
    if unit is not None and unit == 0:
        return {
            "action": "hold",
            "ppv": 0.0,
            "hold": True,
            "reason": (
                f"HOLD: {PRICE_DOES_NOT_MATCH} ($0 PO unit price; Modern Heat pattern). "
                "Not PPV. Purchasing must unreceive, change the PO price, and re-receive. "
                f"Comment the PO line for {SHAWN_MCKIBBEN}. Do not alter receipt unit price in GI."
            ),
            "po_comment": PRICE_MISMATCH_PO_COMMENT,
        }
    invoice_amt = money(invoice_line_amount) or 0.0
    po_amt = money(po_line_amount) or 0.0
    variance = round(invoice_amt - po_amt, 2)
    if variance == 0:
        return {
            "action": "match",
            "ppv": 0.0,
            "hold": False,
            "reason": "Invoice line matches PO line",
            "po_comment": "",
        }
    total = money(invoice_total) or 0.0
    pct_limit = round(abs(total) * PPV_MAX_PCT_OF_INVOICE, 2)
    abs_var = abs(variance)
    next_bill_ppv = round((money(ppv_already_on_bill) or 0.0) + variance, 2)
    over_pct = total > 0 and abs_var > pct_limit
    over_abs = abs(next_bill_ppv) > PPV_MAX_ABS_ON_BILL or abs_var > PPV_MAX_ABS_ON_BILL
    if over_pct or over_abs:
        why_bits = []
        if over_pct:
            pct = (abs_var / total) * 100 if total else 0
            why_bits.append(f"{pct:.1f}% of invoice total")
        if over_abs:
            why_bits.append(f"${abs_var:.2f} exceeds ${PPV_MAX_ABS_ON_BILL:.0f}")
        return {
            "action": "hold",
            "ppv": 0.0,
            "hold": True,
            "reason": (
                f"HOLD: {PRICE_DOES_NOT_MATCH} ({', '.join(why_bits)}). "
                "Do not post PPV. Purchasing must unreceive, change the PO price, and re-receive. "
                f"Comment the PO line for {SHAWN_MCKIBBEN}. Do not alter receipt unit price in GI."
            ),
            "po_comment": PRICE_MISMATCH_PO_COMMENT,
        }
    return {
        "action": "ppv",
        "ppv": variance,
        "hold": False,
        "reason": (
            f"Additional Charge Purchase Price Variance {variance:.2f} "
            f"(signed; |var| {abs_var:.2f} is {((abs_var / total) * 100) if total else 0:.1f}% "
            f"of invoice total and bill PPV {next_bill_ppv:.2f} is under ${PPV_MAX_ABS_ON_BILL:.0f})"
        ),
        "po_comment": "",
    }


def evaluate_bill_price_variance(
    invoice_lines: list[dict[str, Any]] | None,
    po_lines: list[dict[str, Any]] | None,
    *,
    invoice_total: float | None,
) -> dict[str, Any]:
    """Compare merchandise invoice lines to PO lines. Fees stay out of PPV."""
    result: dict[str, Any] = {
        "hold": False,
        "ppv_total": 0.0,
        "items": [],
        "why": "",
        "po_comment": "",
    }
    inv_lines = [dict(line) for line in (invoice_lines or []) if line]
    po = [dict(line) for line in (po_lines or []) if line]
    if not inv_lines or invoice_total is None:
        return result
    used_po: set[int] = set()
    running = 0.0
    holds: list[str] = []
    comments: list[str] = []
    for inv in inv_lines:
        label = str(inv.get("label") or inv.get("name") or inv.get("part") or "")
        if is_fee_or_surcharge(label) or inv.get("fee"):
            result["items"].append({"action": "fee", "ppv": 0.0, "label": label})
            continue
        po_match = _match_po_line(inv, po, used_po)
        if po_match is None:
            continue
        used_po.add(id(po_match))
        inv_amt = money(inv.get("amount") if inv.get("amount") is not None else inv.get("line_amount"))
        po_amt = money(
            po_match.get("amount")
            if po_match.get("amount") is not None
            else po_match.get("line_amount")
        )
        if inv_amt is None or po_amt is None:
            unit = money(po_match.get("unit_price"))
            qty = money(po_match.get("qty") or po_match.get("quantity"))
            if po_amt is None and unit is not None and qty is not None:
                po_amt = round(unit * qty, 2)
            inv_unit = money(inv.get("unit_price"))
            inv_qty = money(inv.get("qty") or inv.get("quantity"))
            if inv_amt is None and inv_unit is not None and inv_qty is not None:
                inv_amt = round(inv_unit * inv_qty, 2)
        if inv_amt is None or po_amt is None:
            continue
        decision = decide_ppv(
            invoice_line_amount=inv_amt,
            po_line_amount=po_amt,
            invoice_total=float(invoice_total),
            ppv_already_on_bill=running,
            po_unit_price=po_match.get("unit_price"),
            label=label,
        )
        result["items"].append({**decision, "label": label, "invoice_amount": inv_amt, "po_amount": po_amt})
        if decision["hold"]:
            result["hold"] = True
            holds.append(decision["reason"])
            if decision.get("po_comment"):
                comments.append(decision["po_comment"])
            continue
        if decision["action"] == "ppv":
            running = round(running + float(decision["ppv"]), 2)
    result["ppv_total"] = running
    result["why"] = " ".join(holds)
    result["po_comment"] = comments[0] if comments else ""
    return result


def _match_po_line(
    invoice_line: dict[str, Any],
    po_lines: list[dict[str, Any]],
    used: set[int],
) -> dict[str, Any] | None:
    """Prefer part + PO/WO line. Never the first leftover qty that happens to fit."""
    scored: list[tuple[int, dict[str, Any]]] = []
    for po_line in po_lines:
        if id(po_line) in used:
            continue
        score = _line_match_score(invoice_line, po_line)
        if score:
            scored.append((score, po_line))
    if not scored:
        if len(po_lines) == 1 and id(po_lines[0]) not in used:
            return po_lines[0]
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best, second = scored[0][0], scored[1][0] if len(scored) > 1 else 0
    if second and best < second + 10 and best < 50:
        return None
    return scored[0][1]


def normalize_part(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _same_part(left: Any, right: Any) -> bool:
    a, b = normalize_part(left), normalize_part(right)
    return bool(a and b and a == b)


def _same_line_no(left: Any, right: Any) -> bool:
    if left in (None, "") or right in (None, ""):
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _same_qty(left: Any, right: Any) -> bool:
    a, b = money(left), money(right)
    return a is not None and b is not None and a == b


def _line_match_score(invoice_line: dict[str, Any], other: dict[str, Any]) -> int:
    """Part and PO/WO line beat qty. Qty-only is not enough to pick a receipt."""
    score = 0
    if _same_part(invoice_line.get("part") or invoice_line.get("item"), other.get("part") or other.get("item")):
        score += 100
    if _same_line_no(invoice_line.get("po_line") or invoice_line.get("line"), other.get("po_line") or other.get("line") or other.get("line_no")):
        score += 50
    wo_left = invoice_line.get("wo") or invoice_line.get("work_order")
    wo_right = other.get("wo") or other.get("work_order")
    if wo_left and wo_right and normalize_part(wo_left) == normalize_part(wo_right):
        score += 40
    if _same_qty(invoice_line.get("qty") or invoice_line.get("quantity"), other.get("qty") or other.get("quantity")):
        score += 10
    return score


def receipt_field(item: dict[str, Any], *names: str) -> Any:
    values = item.get("values") if isinstance(item.get("values"), dict) else item
    for name in names:
        if values.get(name) not in (None, ""):
            raw = values.get(name)
            if isinstance(raw, dict):
                return raw.get("text") or raw.get("id")
            return raw
    return None


def normalize_receipt(item: dict[str, Any]) -> dict[str, Any]:
    """Slip / part / qty / PO line from a KIMCO receipt row. Field names vary."""
    values = item.get("values") if isinstance(item.get("values"), dict) else item
    slip = receipt_field(
        item,
        "Receipt",
        "Packing_Slip",
        "Packing_Slip_Number",
        "Slip",
        "Name",
        "Receiver",
    )
    part = receipt_field(item, "PO_Item_Number", "Item_Number", "Item", "Part", "Part_Number")
    qty = receipt_field(item, "Quantity_Received", "Quantity", "Qty", "qty")
    po_line = receipt_field(item, "Purchase_Line_Number", "PO_Line", "Line_Number", "Line")
    po_number = receipt_field(item, "Purchase_Order_Number", "Purchase_Order", "PO")
    wo = receipt_field(item, "Work_Order", "WO", "Work_Order_Number")
    return {
        "id": item.get("id"),
        "slip": str(slip or "").strip(),
        "part": str(part or "").strip(),
        "qty": money(qty),
        "po_line": po_line,
        "line": po_line,
        "line_no": po_line,
        "po": extract_po_number(str(po_number or "")) or str(po_number or "").strip(),
        "wo": str(wo or "").strip(),
        "name": str(receipt_field(item, "Name") or ""),
        "raw": values,
    }


def slip_matches_invoice(slip: str | None, invoice_number: str | None) -> bool:
    if not slip or not invoice_number:
        return False
    a = invoice_number_key(str(slip))
    b = invoice_number_key(str(invoice_number))
    if not a or not b:
        return False
    return a == b or a in b or b in a


def match_receipts(
    *,
    invoice_number: str | None,
    invoice_lines: list[dict[str, Any]] | None,
    receipts: list[dict[str, Any]],
    po_number: str | None = None,
) -> dict[str, Any]:
    """Select Receipts: part + PO/WO line, then slip # = invoice #. Not first qty.

    Search order before HOLD-no-receipts: slip #, part, qty, PO line.
    Fastenal TXFT499356 is findable via slip # = invoice #.
    Modern Heat 220804 must take lines 6–7 (parts 625-5200-002 / 400-5200-001),
    not lines 1–3 that merely have a fitting qty.
    """
    cleaned: list[dict[str, Any]] = []
    for row in receipts:
        if row.get("part") is not None or row.get("slip") or row.get("po_line") is not None or row.get("qty") is not None:
            if "values" not in row:
                cleaned.append(dict(row))
                continue
        cleaned.append(normalize_receipt(row))
    normalized = cleaned
    lines = [dict(line) for line in (invoice_lines or []) if line and not is_fee_or_surcharge(str(line.get("label") or line.get("name") or ""))]

    matched: list[dict[str, Any]] = []
    used: set[int] = set()
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    def _receipt_score(inv_line: dict[str, Any], receipt: dict[str, Any]) -> int:
        score = _line_match_score(inv_line, receipt)
        if slip_matches_invoice(str(receipt.get("slip") or receipt.get("name") or ""), invoice_number):
            score += 80
        if po_number and str(receipt.get("po") or "") == str(po_number):
            score += 15
        return score

    for inv_line in lines:
        scored: list[tuple[int, dict[str, Any]]] = []
        for receipt in normalized:
            if id(receipt) in used:
                continue
            score = _receipt_score(inv_line, receipt)
            # Qty-only (10) is not a selection. Need part, PO/WO line, or slip.
            if score >= 50:
                scored.append((score, receipt))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if not scored:
            unmatched.append(inv_line)
            continue
        if len(scored) > 1 and scored[0][0] < scored[1][0] + 10:
            ambiguous.append({"line": inv_line, "candidates": [r for _s, r in scored[:3]]})
            continue
        pick = scored[0][1]
        used.add(id(pick))
        matched.append({"line": inv_line, "receipt": pick, "score": scored[0][0]})

    slip_hits = [
        r
        for r in normalized
        if id(r) not in used and slip_matches_invoice(str(r.get("slip") or r.get("name") or ""), invoice_number)
    ]
    if not lines and slip_hits:
        matched.append({"line": {"invoice_number": invoice_number}, "receipt": slip_hits[0], "score": 80})
        used.add(id(slip_hits[0]))

    found = bool(matched) or bool(slip_hits)
    if not found and not lines:
        # Last pass: slip / part / qty / PO line against the invoice number and PO.
        for receipt in normalized:
            if slip_matches_invoice(str(receipt.get("slip") or receipt.get("name") or ""), invoice_number):
                matched.append({"line": {"invoice_number": invoice_number}, "receipt": receipt, "score": 80})
                found = True
                break
            if po_number and str(receipt.get("po") or "") == str(po_number) and receipt.get("part"):
                # A PO+part hit without an invoice line is a candidate, not a qty guess.
                matched.append({"line": {"po": po_number}, "receipt": receipt, "score": 65})
                found = True
                break

    hold_no_receipts = not found and not ambiguous
    return {
        "matched": matched,
        "unmatched_lines": unmatched,
        "ambiguous": ambiguous,
        "hold_no_receipts": hold_no_receipts,
        "found": found,
        "why": (
            "HOLD: no receipts after slip # / part / qty / PO line search."
            if hold_no_receipts
            else (
                f"Select Receipts: {len(matched)} receipt(s) matched by part/PO-WO/slip "
                f"(not first qty)."
                + (f" {len(ambiguous)} ambiguous; will not guess." if ambiguous else "")
            )
        ),
    }


def format_fees(fees: list[dict[str, Any]] | None) -> str:
    if not fees:
        return "none"
    parts = []
    for fee in fees:
        name = str(fee.get("name") or "").strip()
        amount = fee.get("amount")
        if amount is None or amount == "":
            parts.append(name or "fee")
        else:
            parts.append(f"{name} {float(amount):.2f}")
    return "; ".join(parts) if parts else "none"


def is_fee_or_surcharge(label: str) -> bool:
    text = (label or "").strip().lower()
    return any(key in text for key in FEE_KEYWORDS)


def normalize_name(value: str | None) -> str:
    text = (value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _SUFFIXES and not t.isdigit()]
    return " ".join(tokens)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value)


def names_match(left: str | None, right: str | None) -> bool:
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True
    compact_a, compact_b = _compact(a), _compact(b)
    if compact_a and compact_b and (compact_a == compact_b or compact_a in compact_b or compact_b in compact_a):
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return False
    overlap = a_tokens & b_tokens
    shorter = min(len(a_tokens), len(b_tokens))
    return len(overlap) >= max(2, shorter - 1) or (
        len(overlap) >= 1 and (a.split()[0] == b.split()[0]) and shorter <= 2
    )


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def due_date_from_terms(invoice_day: date, terms_text: str | None) -> date:
    """Net-N due date. '1/2% 10 - Net 30' is still Net 30 (optional 0.5% in 10 days)."""
    text = (terms_text or "").replace(" ", "")
    match = re.search(r"N(?:et)?(\d+)", text, flags=re.I)
    days = int(match.group(1)) if match else 30
    return invoice_day + timedelta(days=days)


def kimco_datetime(day: date) -> str:
    return f"{day.isoformat()}T00:00:00"


def extract_po_number(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"PO\s*(\d+)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{5,})\b", text)
    return match.group(1) if match else None


def invoice_number_key(value: str | None) -> str:
    return (value or "").strip().upper()


def invoice_type_for(po: Any) -> int:
    """PO bills use type 3. Existing prototype no-PO bills use type 4, not 3."""
    if po is None or str(po).strip() in {"", "None", "null"}:
        return INVOICE_TYPE_NO_PO
    return INVOICE_TYPE_PO


NOT_A_BILL_SUBJECT_RE = re.compile(
    r"(\bcheck\s*stop\b|\bproof\s+of\s+delivery\b|\bpacking\s+(list|slip)\b|"
    r"\bremittance\s+advice\b|\bpayment\s+confirmation\b|\bpayment\s+received\b|"
    r"\bthank\s+you\s+for\s+your\s+payment\b|\bwire\s+confirmation\b|"
    r"\bdelivery\s+receipt\b|\bpast\s+due\b|\bcollection\s+notice\b)",
    flags=re.I,
)
STATEMENT_RE = re.compile(r"\b(account\s+)?statement\b|\bpast\s+due\b|\bcollection\s+notice\b", flags=re.I)
INVOICE_HINT_RE = re.compile(r"\b(invoice|inv[#\s.-]|bill\b)", flags=re.I)
POD_NAME_RE = re.compile(r"(^|[^a-z])pod([^a-z]|$)|proof.of.delivery", flags=re.I)


def classify_mail(*, subject: str = "", attachment_names: list[str] | None = None, preview: str = "") -> str:
    """Return 'invoice', 'check_stop', 'statement', 'pod', 'payment', or 'not-a-bill'."""
    names = " ".join(attachment_names or [])
    blob = f"{subject}\n{names}\n{preview}"
    if re.search(r"\bcheck\s*stop\b", blob, flags=re.I):
        return "check_stop"
    if re.search(r"\b(payment\s+confirmation|payment\s+received|thank\s+you\s+for\s+your\s+payment|wire\s+confirmation)\b", blob, flags=re.I):
        return "payment"
    if POD_NAME_RE.search(blob) or re.search(r"\bproof\s+of\s+delivery\b|\bpacking\s+(list|slip)\b|\bdelivery\s+receipt\b", blob, flags=re.I):
        if INVOICE_HINT_RE.search(subject) and not POD_NAME_RE.search(subject) and not POD_NAME_RE.search(names):
            return "invoice"
        return "pod"
    if STATEMENT_RE.search(blob) and not INVOICE_HINT_RE.search(subject) and not INVOICE_HINT_RE.search(names):
        return "statement"
    if NOT_A_BILL_SUBJECT_RE.search(blob) and not INVOICE_HINT_RE.search(subject):
        return "not-a-bill"
    return "invoice"


def should_create_header(inv: dict[str, Any]) -> tuple[bool, str]:
    """Real vendor bills get a header even with no PO. HOLD is not for missing PO alone."""
    if inv.get("check_stop") or str(inv.get("hold_reason") or "").strip().upper() == "CHECK STOP":
        return False, "CHECK STOP"
    reason = str(inv.get("hold_reason") or "").strip()
    reason_key = reason.lower()
    if reason_key in HOLD_ONLY_REASONS:
        return False, reason or reason_key
    if inv.get("action") == "hold" and reason_key and reason_key not in {"no-po", "no po", "nopo"}:
        return False, reason
    return True, ""


def vendor_match_score(fixture_vendor: str | None, sample_text: str | None) -> int:
    """Prefer the most specific vendor name so UniFirst Corp != UniFirst First Aid."""
    if not names_match(fixture_vendor, sample_text):
        return 0
    a_tokens = set(normalize_name(fixture_vendor).split())
    b_tokens = set(normalize_name(sample_text).split())
    if not a_tokens or not b_tokens:
        return 0
    overlap = len(a_tokens & b_tokens)
    exact = 8 if a_tokens == b_tokens else 0
    return overlap * 10 + exact


def lookup_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def lookup_id(value: Any) -> int | None:
    if isinstance(value, dict) and value.get("id") is not None:
        return int(value["id"])
    return None
