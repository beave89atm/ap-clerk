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
    """Yes only when a process category is applied.

    Success → Entered in AI.
    Header+PDF entered but unfinished (price/qty HOLD, Incomplete) → Entered with issues.
    Real bill unprocessable without a header → AI HOLD.
    Skipped / Noise → Yes (`AI Skipped`). Never AI HOLD for noise.
    """
    return "Yes" if (result or "").strip() in {
        "Success",
        "Incomplete",
        "HOLD",
        "Fail",
        "Skipped",
        "Noise",
    } else "No"


def comments_for(target: str) -> str:
    """Live bills are real; do not stamp the prototype 'do not pay' comment."""
    if (target or "").strip().lower() == "live":
        return LIVE_COMMENTS
    return COMMENTS


# Mailbox noise: sheet-note Skipped + Outlook AI Skipped (never AI HOLD). Consumes the 10-email touch cap.
NOISE_REASONS = {
    "check stop",
    "check_stop",
    "statement",
    "pod",
    "payment",
    "payment letter",
    "dup",
    "duplicate",
    "not-a-bill",
    "not a bill",
    "internal",
    "internal mail",
    "unreadable-or-not-a-bill",
    "no-attachment",
    "no-pdf",
}
# Real bill HOLDs that could not finish. These consume the email cap (as do noise skips).
# price / qty HOLDs still create a header + attach PDF (Entered with issues).
# parse-error / auto-pay / pdf-behind-link do not create a header (AI HOLD).
BILL_HOLD_REASONS = {
    "price does not match",
    "parse-error",
    "auto-pay",
    "auto pay",
    "pdf-behind-link",
    "qty-does-not-match",
    "qty does not match",
}
# HOLD reasons that still get a KIMCO header + vendor PDF (Treyce 2026-09-10).
CREATE_HEADER_ON_HOLD = {
    "price does not match",
    "qty-does-not-match",
    "qty does not match",
}
HOLD_ONLY_REASONS = NOISE_REASONS | BILL_HOLD_REASONS
GAS_AND_SUPPLY_MISC_ITEM = "Shop Supplies - G&S"


def is_noise_reason(reason: str | None) -> bool:
    """True for bill-vs-noise skips (statement, CHECK STOP, POD, payment, dup, not-a-bill)."""
    key = (reason or "").strip().lower()
    if not key:
        return False
    collapsed = key.replace("_", " ")
    return key in NOISE_REASONS or collapsed in {item.replace("_", " ") for item in NOISE_REASONS}

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
    # Confirmed 2026-08-31 via GET of existing live invoices (API Vendor.id, not invented).
    "priority 1": 145,
    "priority1": 145,
    "msc industrial": 128,
    "metal supermarkets": 121,
    "marmon": 115,
    "keystone": 115,
    "jp steel": 100,
    "amada": 18,
    "exotic metals": 346,
    "curbell": 353,
    # Confirmed 2026-09-01 via GET of existing live invoices / PO_Vendor (API Vendor.id).
    "capital machine": 45,
    "willbanks": 202,
    "shoppa": 159,
    "eastern metal": 64,
    "unifirst first aid": 209,
    "green valley": 405,
    "luxor": 112,
    "rmp industrial": 322,
    "purvis": 333,
    "gas and supply": 71,
    "clear kut": 345,
    "tpi": 183,
    "telecom": 183,
    # Confirmed 2026-09-02 via GET of existing live invoices (API Vendor.id, not invented).
    "ntex": 134,
    "grm": 78,
    "kloeckner": 106,
    "morgan steel": 304,
    "american bearing": 20,
    "crosslink": 278,
    "ryerson": 152,
    "mcqueary": 119,
    "hudson energy": 88,
    "lavanture": 295,
    "alternative parts": 215,
    "altparts": 215,
    "alt parts": 215,
    "tube supply": 341,
    # Confirmed 2026-09-04 via GET of existing live invoices (API Vendor.id).
    "leeco": 109,
    "austin hardware": 34,
    "austinhardware": 34,
    "a1 image": 8,
    "a1image": 8,
    "maynard": 116,
    "nexsen": 116,
    "gexpro": 73,
    "legacy wire": 292,
    "beshert": 37,
    # Confirmed 2026-09-07 via GET of existing live invoices (API Vendor.id).
    "unifirst corporation": 189,
    "precision fabrication": 144,
    "versalift": 178,
    "automated finishing": 331,
    "aft corp": 331,
    "aftcorp": 331,
    "polymer products": 358,
    "hapeco": 384,
    "mcmaster": 117,
    "air products": 13,
    "earle": 208,
    "emj": 208,
    # Confirmed 2026-09-08 via GET of existing live invoices (API Vendor.id).
    "oneal": 137,
    "o neal": 137,
    "pct support": 140,
    "pctsupport": 140,
    "xcaliber": 339,
    "morgansteel": 304,
    # Confirmed 2026-09-08 via GET of live invoice 9496 (Vendor.id, not invented).
    "orthman": 434,
    "orthman conveying": 434,
}

# Existing live invoices used only for remit/terms copy. Confirmed by GET.
KNOWN_VENDOR_SAMPLE_INVOICES = {
    434: 9496,  # Orthman 701599
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
    "misc",
    "miscellaneous",
    "supplies",
    "misc charge",
    "additional charge",
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

# Generic tokens must not be enough for a vendor name match (MSC ≠ RMP).
# Distinctive tokens (msc vs rmp) must agree. Kept out of normalize_name so
# alias keys like "msc industrial" still substring-match the full name.
GENERIC_VENDOR_TOKENS = frozenset(
    {
        "industrial",
        "supply",
        "steel",
        "metal",
        "metals",
        "company",
        "corp",
        "inc",
        "llc",
        "co",
        "services",
        "service",
        "products",
        "product",
        "manufacturing",
        "and",
        "industries",
        "industry",
    }
)


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
    """National Specialty Alloys → 1386; Coherent Corp. → 1410.

    Longest alias wins so UniFirst First Aid (209) is not collapsed to UniFirst (189).
    """
    raw = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    if raw in VENDOR_ID_ALIASES:
        return VENDOR_ID_ALIASES[raw]
    norm = normalize_name(name)
    if not norm:
        return None
    if norm in VENDOR_ID_ALIASES:
        return VENDOR_ID_ALIASES[norm]
    scored: list[tuple[int, int]] = []
    for key, vendor_id in VENDOR_ID_ALIASES.items():
        # Alias must appear in the vendor name. Do not match "unifirst" ⊂ "unifirst first aid".
        if (
            key == norm
            or key in norm.split()
            or (len(key.split()) >= 2 and key in norm)
            or (len(key) >= 6 and key in norm)
        ):
            scored.append((len(key), vendor_id))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


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
        # Techni-Tool style: keep the printed suffix (S1387370.001 not S1387370).
        if "." not in raw:
            suffixed = re.search(rf"\b({re.escape(raw)}\.\d{{3}})\b", blob)
            if suffixed:
                return suffixed.group(1)
        prefixed = re.search(rf"\b(\d-{re.escape(raw)})\b", blob)
        if prefixed:
            return prefixed.group(1)
        already = re.search(rf"\b({re.escape(raw)})\b", blob)
        if already and ("-" in raw or "." in raw):
            return raw
    hits = re.findall(r"\b(\d-\d{5,8})\b", blob)
    if hits and known_invoice_prefix(vendor):
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


_DESC_STOP = {
    "the",
    "and",
    "of",
    "for",
    "a",
    "an",
    "to",
    "in",
    "on",
    "with",
    "from",
    "inc",
    "llc",
    "qty",
    "ea",
    "each",
    "pcs",
    "pc",
    "item",
}


def description_tokens(value: Any) -> set[str]:
    """Tokens for O'Neal-style part matching (PIPE A500 SCH 40 ↔ P-1.00 SCH 40-A500)."""
    text = re.sub(r"[^A-Z0-9.]+", " ", str(value or "").upper())
    raw = [tok for tok in text.split() if tok and tok.lower() not in _DESC_STOP]
    tokens: set[str] = set()
    for tok in raw:
        compact = tok.replace(".", "")
        if len(compact) >= 2:
            tokens.add(compact)
    for index, tok in enumerate(raw):
        nxt = raw[index + 1] if index + 1 < len(raw) else ""
        if tok in {"SCH", "SCHEDULE"} and nxt:
            tokens.add("SCH" + nxt.replace(".", ""))
    return tokens


def description_match_score(left: Any, right: Any) -> int:
    """Score overlapping description/part tokens. Qty-only is not enough."""
    overlap = description_tokens(left) & description_tokens(right)
    if not overlap:
        return 0
    score = 0
    for tok in overlap:
        if re.search(r"[A-Z]\d|\d[A-Z]", tok) or len(tok) >= 5:
            score += 25
        elif len(tok) >= 3:
            score += 15
        else:
            score += 5
    return min(score, 80)


def _line_description(line: dict[str, Any]) -> str:
    return str(
        line.get("description")
        or line.get("label")
        or line.get("name")
        or line.get("part")
        or line.get("item")
        or ""
    )


def _line_match_score(invoice_line: dict[str, Any], other: dict[str, Any]) -> int:
    """Part, description, and PO/WO line beat qty. Qty-only is not a pick."""
    score = 0
    if _same_part(invoice_line.get("part") or invoice_line.get("item"), other.get("part") or other.get("item")):
        score += 100
    score += description_match_score(_line_description(invoice_line), _line_description(other))
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
    description = receipt_field(
        item,
        "Description",
        "Item_Description",
        "PO_Item_Description",
        "Part_Description",
        "Name",
    )
    qty = receipt_field(item, "Quantity_Received", "Quantity", "Qty", "qty")
    po_line = receipt_field(item, "Purchase_Line_Number", "PO_Line", "Line_Number", "Line")
    po_number = receipt_field(item, "Purchase_Order_Number", "Purchase_Order", "PO")
    wo = receipt_field(item, "Work_Order", "WO", "Work_Order_Number")
    unit_price = receipt_field(
        item,
        "PO_Item_Number_$_Unit_Price",
        "Unit_Price",
        "Unit_Cost",
        "Purchase_Cost",
        "Price",
        "unit_price",
        "cost",
    )
    amount = receipt_field(
        item,
        "Amount",
        "Line_Amount",
        "Extended_Price",
        "Extended_Cost",
        "Purchase_Amount",
        "amount",
    )
    qty_n = money(qty)
    unit_n = money(unit_price)
    amount_n = money(amount)
    if amount_n is None and qty_n is not None and unit_n is not None:
        amount_n = round(qty_n * unit_n, 2)
    return {
        "id": item.get("id"),
        "slip": str(slip or "").strip(),
        "part": str(part or "").strip(),
        "description": str(description or "").strip(),
        "label": str(description or part or "").strip(),
        "qty": qty_n,
        "unit_price": unit_n,
        "amount": amount_n,
        "cost": amount_n,
        "po_line": po_line,
        "line": po_line,
        "line_no": po_line,
        "po": extract_po_number(str(po_number or ""))
        or extract_po_number(str(receipt_field(item, "Name") or ""))
        or str(po_number or "").strip(),
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


# Two-cent rounding: merchandise amount vs receipt extended cost.
COST_ALIGN_TOLERANCE = 0.02


def receipt_cost(receipt: dict[str, Any] | None) -> float | None:
    """Extended receipt cost: amount, else qty × unit price."""
    if not isinstance(receipt, dict):
        return None
    amount = money(
        receipt.get("amount")
        if receipt.get("amount") is not None
        else receipt.get("cost")
        if receipt.get("cost") is not None
        else receipt.get("line_amount")
        if receipt.get("line_amount") is not None
        else receipt.get("extended")
    )
    if amount is not None:
        return amount
    qty = money(receipt.get("qty") if receipt.get("qty") is not None else receipt.get("quantity"))
    unit = money(
        receipt.get("unit_price")
        if receipt.get("unit_price") is not None
        else receipt.get("unit_cost")
        if receipt.get("unit_cost") is not None
        else receipt.get("purchase_cost")
    )
    if qty is not None and unit is not None:
        return round(qty * unit, 2)
    return None


def costs_align(left: Any, right: Any, *, tolerance: float = COST_ALIGN_TOLERANCE) -> bool:
    a, b = money(left), money(right)
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance


def merchandise_qty(invoice_lines: list[dict[str, Any]] | None) -> float | None:
    """Sum merchandise (non-fee) line quantities. None if no qty evidence."""
    total = 0.0
    found = False
    for line in invoice_lines or []:
        if not line:
            continue
        if is_fee_or_surcharge(_line_description(line)) or line.get("fee"):
            continue
        qty = money(line.get("qty") if line.get("qty") is not None else line.get("quantity"))
        if qty is None:
            continue
        total = round(total + qty, 2)
        found = True
    return total if found else None


def invoice_qty_evidence(
    invoice_lines: list[dict[str, Any]] | None = None,
    invoice_qty: Any = None,
) -> float | None:
    """Invoice merchandise qty: explicit invoice-level qty, else sum of lines."""
    explicit = money(invoice_qty)
    if explicit is not None:
        return explicit
    return merchandise_qty(invoice_lines)


def _receipt_aligns(
    receipt: dict[str, Any],
    invoice_qty: float | None,
    invoice_amount: float | None,
    *,
    allow_missing: bool = True,
    qty_already_ok: bool = False,
) -> tuple[bool, str]:
    """True when receipt qty/cost do not conflict with invoice evidence."""
    if invoice_qty is not None and not qty_already_ok:
        rq = money(receipt.get("qty") if receipt.get("qty") is not None else receipt.get("quantity"))
        if rq is not None and not _same_qty(rq, invoice_qty):
            return False, f"receipt qty {rq} ≠ invoice qty {invoice_qty}"
        if rq is None and not allow_missing:
            return False, "receipt qty missing"
    if invoice_amount is not None:
        rc = receipt_cost(receipt)
        if rc is not None and not costs_align(rc, invoice_amount):
            return False, f"receipt cost {rc:.2f} ≠ invoice merchandise {invoice_amount:.2f}"
        if rc is None and not allow_missing:
            return False, "receipt cost missing"
    return True, ""


def pick_receipts_by_qty_cost(
    candidates: list[dict[str, Any]],
    *,
    invoice_qty: float | None = None,
    invoice_amount: float | None = None,
    invoice_number: str | None = None,
) -> dict[str, Any]:
    """Choose open PO receipts by invoice qty, then merchandise cost.

    Prefer exact qty match (35 vs 36 → 35). If remaining candidates still
    differ in cost, require cost alignment within rounding or HOLD ambiguous.
    Never first-open / second-open-on-po Success when multiple open receipts
    differ in qty/cost.
    """
    empty = {"picked": [], "ambiguous": None, "how": "", "why": ""}
    if not candidates:
        return empty

    slip_hits = [
        r
        for r in candidates
        if slip_matches_invoice(str(r.get("slip") or r.get("name") or ""), invoice_number)
    ]
    if len(slip_hits) == 1:
        ok, reason = _receipt_aligns(slip_hits[0], invoice_qty, invoice_amount)
        if ok:
            return {
                "picked": slip_hits,
                "ambiguous": None,
                "how": "slip # = invoice # (qty/cost checked)",
                "why": "",
            }
        if invoice_qty is not None or invoice_amount is not None:
            return {
                "picked": [],
                "ambiguous": {"candidates": slip_hits, "why": reason},
                "how": "",
                "why": reason,
            }

    if len(candidates) == 1:
        ok, reason = _receipt_aligns(candidates[0], invoice_qty, invoice_amount, allow_missing=True)
        if ok:
            return {
                "picked": candidates,
                "ambiguous": None,
                "how": "single open receipt on PO",
                "why": "",
            }
        if invoice_qty is not None or invoice_amount is not None:
            return {
                "picked": [],
                "ambiguous": {"candidates": candidates, "why": reason},
                "how": "",
                "why": reason,
            }
        return {
            "picked": candidates,
            "ambiguous": None,
            "how": "single open receipt on PO",
            "why": "",
        }

    qtys = {money(r.get("qty") if r.get("qty") is not None else r.get("quantity")) for r in candidates}
    costs = {receipt_cost(r) for r in candidates}
    differ = len(qtys - {None}) > 1 or len(costs - {None}) > 1

    if invoice_qty is not None:
        qty_hits = [
            r
            for r in candidates
            if _same_qty(r.get("qty") if r.get("qty") is not None else r.get("quantity"), invoice_qty)
        ]
        if len(qty_hits) == 1:
            ok, reason = _receipt_aligns(
                qty_hits[0], invoice_qty, invoice_amount, qty_already_ok=True
            )
            if ok:
                return {
                    "picked": qty_hits,
                    "ambiguous": None,
                    "how": f"invoice qty {invoice_qty:g}",
                    "why": "",
                }
            return {
                "picked": [],
                "ambiguous": {"candidates": qty_hits, "why": reason},
                "how": "",
                "why": reason,
            }
        if len(qty_hits) > 1:
            if invoice_amount is not None:
                cost_hits = [r for r in qty_hits if costs_align(receipt_cost(r), invoice_amount)]
                if len(cost_hits) == 1:
                    return {
                        "picked": cost_hits,
                        "ambiguous": None,
                        "how": f"invoice qty {invoice_qty:g} and merchandise cost",
                        "why": "",
                    }
            why = (
                f"multiple open receipts qty {invoice_qty:g}; "
                "cost does not uniquely align. HOLD ambiguous (will not guess)."
            )
            return {
                "picked": [],
                "ambiguous": {"candidates": qty_hits, "why": why},
                "how": "",
                "why": why,
            }
        why = (
            f"no open receipt qty matches invoice qty {invoice_qty:g} "
            "(will not take first-open / second-open-on-po)."
        )
        return {
            "picked": [],
            "ambiguous": {"candidates": candidates, "why": why},
            "how": "",
            "why": why,
        }

    if invoice_amount is not None:
        cost_hits = [r for r in candidates if costs_align(receipt_cost(r), invoice_amount)]
        if len(cost_hits) == 1:
            return {
                "picked": cost_hits,
                "ambiguous": None,
                "how": "invoice merchandise cost",
                "why": "",
            }
        why = (
            "multiple open receipts on the PO; merchandise cost does not uniquely align. "
            "HOLD ambiguous (will not guess first-open / second-open-on-po)."
        )
        return {
            "picked": [],
            "ambiguous": {"candidates": candidates, "why": why},
            "how": "",
            "why": why,
        }

    if differ:
        why = (
            "multiple open receipts on the PO differ in qty/cost and invoice "
            "qty/cost evidence is missing. HOLD ambiguous (will not guess "
            "first-open / second-open-on-po)."
        )
        return {
            "picked": [],
            "ambiguous": {"candidates": candidates, "why": why},
            "how": "",
            "why": why,
        }
    return {
        "picked": [candidates[0]],
        "ambiguous": None,
        "how": "open receipts on PO agree in qty/cost",
        "why": "",
    }


def match_receipts(
    *,
    invoice_number: str | None,
    invoice_lines: list[dict[str, Any]] | None,
    receipts: list[dict[str, Any]],
    po_number: str | None = None,
    invoice_qty: Any = None,
    invoice_amount: Any = None,
) -> dict[str, Any]:
    """Select Receipts: part + PO/WO line, then slip # = invoice #.

    When invoice lines are empty, verify qty and merchandise cost against
    open receipts on the PO. Prefer exact qty (35 vs 36 → 35). Never Success
    via first-open / second-open-on-po when multiple open receipts differ in
    qty/cost. Fastenal TXFT499356 is findable via slip # = invoice #.
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
    qty_ev = invoice_qty_evidence(lines, invoice_qty)
    amount_ev = money(invoice_amount)
    if amount_ev is None:
        summed = 0.0
        found_amt = False
        for line in lines:
            amt = money(line.get("amount") if line.get("amount") is not None else line.get("line_amount"))
            if amt is None:
                continue
            summed = round(summed + amt, 2)
            found_amt = True
        if found_amt:
            amount_ev = summed

    matched: list[dict[str, Any]] = []
    used: set[int] = set()
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    hows: list[str] = []

    def _receipt_score(inv_line: dict[str, Any], receipt: dict[str, Any]) -> int:
        score = _line_match_score(inv_line, receipt)
        if slip_matches_invoice(str(receipt.get("slip") or receipt.get("name") or ""), invoice_number):
            score += 80
        if po_number and str(receipt.get("po") or "") == str(po_number):
            score += 15
        return score

    def _open_on_po() -> list[dict[str, Any]]:
        open_rows: list[dict[str, Any]] = []
        for receipt in normalized:
            if id(receipt) in used:
                continue
            receipt_po = str(receipt.get("po") or "") or extract_po_number(str(receipt.get("name") or ""))
            if po_number and receipt_po == str(po_number):
                open_rows.append(receipt)
        return open_rows

    def _apply_qty_cost_pick(
        candidates: list[dict[str, Any]],
        *,
        score: int,
        pass_name: str,
        line: dict[str, Any] | None = None,
    ) -> bool:
        pick = pick_receipts_by_qty_cost(
            candidates,
            invoice_qty=qty_ev,
            invoice_amount=amount_ev,
            invoice_number=invoice_number,
        )
        if pick.get("ambiguous") and not pick.get("picked"):
            ambiguous.append(pick["ambiguous"])
            return False
        for receipt in pick.get("picked") or []:
            if id(receipt) in used:
                continue
            used.add(id(receipt))
            matched.append(
                {
                    "line": line or ({"invoice_number": invoice_number} if invoice_number else {"po": po_number}),
                    "receipt": receipt,
                    "score": score,
                    "pass": pass_name,
                    "how": pick.get("how") or pass_name,
                }
            )
            if pick.get("how"):
                hows.append(str(pick["how"]))
        return bool(pick.get("picked"))

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
            # Same part/score: break ties with invoice qty, then cost.
            tied = [r for s, r in scored if s >= scored[0][0] - 5]
            if len(tied) > 1 and (qty_ev is not None or amount_ev is not None):
                if _apply_qty_cost_pick(tied, score=scored[0][0], pass_name="qty-cost-tiebreak", line=inv_line):
                    hows.append("part/PO-WO + invoice qty/cost")
                    continue
            ambiguous.append({"line": inv_line, "candidates": [r for _s, r in scored[:3]]})
            continue
        pick = scored[0][1]
        used.add(id(pick))
        matched.append({"line": inv_line, "receipt": pick, "score": scored[0][0], "how": "part/PO-WO/slip"})
        hows.append("part/PO-WO/slip")

    slip_hits = [
        r
        for r in normalized
        if id(r) not in used and slip_matches_invoice(str(r.get("slip") or r.get("name") or ""), invoice_number)
    ]
    if not lines and slip_hits:
        _apply_qty_cost_pick(slip_hits, score=80, pass_name="slip")

    found = bool(matched)
    if not found and not lines and not ambiguous:
        # Empty invoice lines (Fastenal TXFT4100079): never first-open on PO.
        pool = _open_on_po() if po_number else list(normalized)
        if pool:
            _apply_qty_cost_pick(pool, score=65, pass_name="qty-cost-on-po")
            found = bool(matched)

    # Synthesized / named-for-PO receipts (McMaster PO58808, Ryerson PO58789).
    # Never a single PO-receipt Success when the invoice has multiple merchandise lines
    # (EMJ Z250725432: two lines, runner selected one).
    if not found and po_number and not ambiguous and len(lines) <= 1:
        pool = _open_on_po()
        if pool:
            _apply_qty_cost_pick(pool, score=65, pass_name="named-po")
            found = bool(matched)

    # Second pass before HOLD-no-receipts (Capital 26167 / Fastenal TXFT499356):
    # slip # = invoice #, part, qty, PO line, then qty/cost among open receipts.
    second_pass = False
    if not found and not ambiguous and po_number:
        open_on_po = _open_on_po()
        if _apply_qty_cost_pick(
            [r for r in open_on_po if slip_matches_invoice(str(r.get("slip") or r.get("name") or ""), invoice_number)],
            score=80,
            pass_name="second-slip",
        ):
            found = True
            second_pass = True
        if not found:
            for inv_line in lines:
                scored = []
                for receipt in open_on_po:
                    if id(receipt) in used:
                        continue
                    score = _receipt_score(inv_line, receipt)
                    if score >= 50:
                        scored.append((score, receipt))
                scored.sort(key=lambda pair: pair[0], reverse=True)
                if scored:
                    pick = scored[0][1]
                    used.add(id(pick))
                    matched.append({"line": inv_line, "receipt": pick, "score": scored[0][0], "pass": "second-part", "how": "second-part"})
                    hows.append("part/PO-WO/slip")
                    found = True
                    second_pass = True
        if not found and open_on_po and len(lines) <= 1:
            # Capital 26167: a single open receipt on the PO is enough.
            # Multiple invoice lines must each match; do not stop after one.
            if _apply_qty_cost_pick(open_on_po, score=55, pass_name="second-open-on-po"):
                found = True
                second_pass = True

    found = bool(matched)
    hold_no_receipts = not found and not ambiguous
    unique_hows: list[str] = []
    for how in hows:
        if how and how not in unique_hows:
            unique_hows.append(how)
    if hold_no_receipts:
        why = "HOLD: no receipts after second pass (slip # / part / qty / PO line / open receipts on PO)."
        if unmatched:
            why += (
                f" Unmatched invoice line(s): {format_unmatched_lines(unmatched)}. "
                "Select Receipts for each invoice line; do not stop after one."
            )
    elif ambiguous and not matched:
        why = (
            f"HOLD: {len(ambiguous)} ambiguous open receipt set(s) on the PO "
            "(qty/cost differ; will not guess first-open / second-open-on-po)."
        )
        if ambiguous[0].get("why"):
            why = f"HOLD: {ambiguous[0]['why']}"
    else:
        how_txt = ", ".join(unique_hows) if unique_hows else "part/PO-WO/slip"
        verified = any("qty" in h or "cost" in h or "merchandise" in h for h in unique_hows)
        why = f"Select Receipts: {len(matched)} receipt(s) matched by {how_txt}"
        if verified:
            why += " (invoice qty/cost verified; not first open receipt on the PO)."
        elif "part/PO-WO/slip" in unique_hows or "second-part" in unique_hows:
            why += " (part/PO-WO/slip; not first leftover qty)."
        else:
            why += "."
        if second_pass:
            why += " Second pass used open receipts on the PO (qty/cost checked)."
        if ambiguous:
            why += f" {len(ambiguous)} ambiguous; will not guess."
        if unmatched:
            why += (
                f" Unmatched invoice line(s): {format_unmatched_lines(unmatched)}. "
                "Select Receipts for each invoice line; do not stop after one."
            )
    return {
        "matched": matched,
        "unmatched_lines": unmatched,
        "ambiguous": ambiguous,
        "hold_no_receipts": hold_no_receipts,
        "found": found,
        "second_pass": second_pass,
        "why": why,
    }


def format_fees(fees: list[dict[str, Any]] | None) -> str:
    if not fees:
        return "none"
    parts = []
    for fee in fees:
        name = str(fee.get("name") or "").strip()
        amount = fee.get("amount")
        if amount is None or amount == "":
            continue
        parts.append(f"{name} {float(amount):.2f}")
    return "; ".join(parts) if parts else "none"


def invoice_line_label(line: dict[str, Any] | None) -> str:
    if not isinstance(line, dict):
        return "line"
    return str(
        line.get("label")
        or line.get("description")
        or line.get("part")
        or (f"line {line.get('po_line')}" if line.get("po_line") not in (None, "") else "")
        or "line"
    ).strip()[:80] or "line"


def format_unmatched_lines(lines: list[dict[str, Any]] | None) -> str:
    labels = [invoice_line_label(line) for line in (lines or []) if line]
    return "; ".join(labels) if labels else ""


_NOT_A_FEE = re.compile(
    r"shipping\s*date|ship(?:ping)?\s*date|prepaid|pre-?paid|shipped\s+via",
    flags=re.I,
)


def is_fee_or_surcharge(label: str) -> bool:
    text = (label or "").strip().lower()
    if not text:
        return False
    if _NOT_A_FEE.search(text):
        return False
    return any(key in text for key in FEE_KEYWORDS)


def normalize_name(value: str | None) -> str:
    text = (value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _SUFFIXES and not t.isdigit()]
    return " ".join(tokens)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value)


def distinctive_vendor_tokens(value: str | None) -> list[str]:
    """Tokens that identify a vendor after suffixes and generic industry words.

    MSC Industrial Supply → [msc]. RMP INDUSTRIAL SUPPLY → [rmp].
    Earle M. Jorgensen → [earle, jorgensen]. Single-letter leftovers are dropped.
    """
    tokens = []
    for token in normalize_name(value).split():
        if token in GENERIC_VENDOR_TOKENS or len(token) <= 1:
            continue
        tokens.append(token)
    return tokens


def _shorter_has_distinctive(left: str, right: str) -> bool:
    shorter = left if len(left) <= len(right) else right
    return bool(distinctive_vendor_tokens(shorter))


def names_match(left: str | None, right: str | None) -> bool:
    """True when two vendor strings are the same company.

    Generic overlap ({industrial, supply}) is not a match. Distinctive tokens
    must agree — first significant token or shared unique tokens after stopwords.
    MSC Industrial Supply must not match RMP INDUSTRIAL SUPPLY.
    """
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if (a in b or b in a) and _shorter_has_distinctive(a, b):
        return True
    compact_a, compact_b = _compact(a), _compact(b)
    if compact_a and compact_b and (compact_a == compact_b or compact_a in compact_b or compact_b in compact_a):
        if _shorter_has_distinctive(a, b):
            return True
    a_dist = distinctive_vendor_tokens(left)
    b_dist = distinctive_vendor_tokens(right)
    if not a_dist or not b_dist:
        return False
    if a_dist[0] == b_dist[0]:
        return True
    return bool(set(a_dist) & set(b_dist))


def vendors_strictly_match(
    parsed: str | None,
    posted_name: str | None = None,
    posted_id: int | None = None,
) -> bool:
    """True when the posted KIMCO vendor is the parsed vendor or a known alias.

    Alias ids are API Vendor.id (MSC=128, RMP=322). A lookup-id like 1320-RMP
    is not an alias for MSC even if names once fuzzy-matched.
    """
    if not (parsed or "").strip():
        return False
    parsed_alias = known_vendor_id(parsed)
    posted_alias = known_vendor_id(posted_name)
    if parsed_alias is not None and posted_id is not None and int(parsed_alias) == int(posted_id):
        return True
    if parsed_alias is not None and posted_alias is not None and int(parsed_alias) == int(posted_alias):
        return True
    if posted_id is not None and posted_alias is not None and int(posted_id) != int(posted_alias):
        # Posted lookup-id ≠ API Vendor.id. Name/alias must still agree.
        if parsed_alias is not None and int(parsed_alias) == int(posted_alias):
            return True
    if not names_match(parsed, posted_name):
        return False
    if parsed_alias is not None and posted_alias is not None and int(parsed_alias) != int(posted_alias):
        return False
    return True


def posted_vendor_fields(record: Any) -> tuple[str, int | None]:
    """Vendor text and lookup/id from a KIMCO invoice GET body."""
    values = (record or {}).get("values") or {}
    vendor = values.get("Vendor") or values.get("Vendor_$_Display_Name")
    return lookup_text(vendor), lookup_id(vendor)


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
    """PO bills use type 3. Existing prototype no-PO bills use type 4, not 3.

    Never return Type 4 when a PO number is present (Purvis 32625214 / 58926).
    """
    if po is None or str(po).strip() in {"", "None", "null"}:
        return INVOICE_TYPE_NO_PO
    return INVOICE_TYPE_PO


def misc_purchase_item_for(vendor: str | None) -> str | None:
    """Gas & Supply Misc invoices use Shop Supplies - G&S, not CHECK STOP."""
    norm = normalize_name(vendor)
    if "gas and supply" in (norm or "") or "gasandsupply" in (norm or "").replace(" ", ""):
        return GAS_AND_SUPPLY_MISC_ITEM
    return None


def qty_discrepancy(
    invoice_lines: list[dict[str, Any]] | None,
    other_lines: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """HOLD when invoice qty ≠ matched PO/receipt qty (Capital 26764: 2 vs 2.5)."""
    result: dict[str, Any] = {"hold": False, "why": "", "items": []}
    inv_lines = [dict(line) for line in (invoice_lines or []) if line]
    others = [dict(line) for line in (other_lines or []) if line]
    if not inv_lines or not others:
        return result
    used: set[int] = set()
    mismatches: list[str] = []
    for inv in inv_lines:
        if is_fee_or_surcharge(_line_description(inv)) or inv.get("fee"):
            continue
        matched = _match_po_line(inv, others, used)
        if matched is None:
            continue
        used.add(id(matched))
        inv_qty = money(inv.get("qty") if inv.get("qty") is not None else inv.get("quantity"))
        other_qty = money(matched.get("qty") if matched.get("qty") is not None else matched.get("quantity"))
        if inv_qty is None or other_qty is None:
            continue
        if inv_qty != other_qty:
            label = _line_description(inv) or str(inv.get("part") or "")
            mismatches.append(f"{label or 'line'} invoice qty {inv_qty} vs PO/receipt qty {other_qty}")
            result["items"].append({"label": label, "invoice_qty": inv_qty, "other_qty": other_qty})
    if mismatches:
        result["hold"] = True
        result["why"] = (
            "invoice qty does not match PO/receipt qty ("
            + "; ".join(mismatches)
            + "). HOLD so a buyer can comment/tag. Do not claim Success."
        )
    return result


AUTO_PAY_RE = re.compile(
    r"toyota\s+commercial\s+finance|\bauto[\s-]?pay\b|\bautopay\b",
    flags=re.I,
)
MELODY_CHANNELL_RE = re.compile(r"melody\s+channell", flags=re.I)
GAS_AND_SUPPLY_RE = re.compile(r"gas\s*(?:and|&)\s*supply|gasandsupply", flags=re.I)


def is_auto_pay(*, vendor: str = "", subject: str = "", preview: str = "", text: str = "") -> bool:
    """Toyota Commercial Finance / auto-pay: HOLD, do not enter in ERP."""
    blob = f"{vendor}\n{subject}\n{preview}\n{text}"
    return bool(AUTO_PAY_RE.search(blob))


def is_gas_and_supply(value: str | None) -> bool:
    return bool(GAS_AND_SUPPLY_RE.search(value or ""))


def is_melody_channell(value: str | None) -> bool:
    return bool(MELODY_CHANNELL_RE.search(value or ""))


NOT_A_BILL_SUBJECT_RE = re.compile(
    r"(\bcheck\s*stop\b|\bproof\s+of\s+delivery\b|\bpacking\s+(list|slip)\b|"
    r"\bremittance\s+advice\b|\bpayment\s+confirmation\b|\bpayment\s+received\b|"
    r"\bthank\s+you\s+for\s+your\s+payment\b|\bwire\s+confirmation\b|"
    r"\bdelivery\s+receipt\b|\bpast\s+due\b|\bcollection\s+notice\b)",
    flags=re.I,
)
STATEMENT_RE = re.compile(
    r"\b(account\s+)?statement\b|\bpast\s+due\b|\bcollection\s+notice\b|\baccount\s*status\b",
    flags=re.I,
)
INVOICE_HINT_RE = re.compile(r"\b(invoice|inv[#\s.-]|bill\b)", flags=re.I)
POD_NAME_RE = re.compile(r"(^|[^a-z])pod([^a-z]|$)|proof.of.delivery", flags=re.I)
# Vendor invoices with a PDF must enter even when the subject is short (AQPC, Melody).
KNOWN_BILL_VENDOR_RE = re.compile(
    r"american\s+quality\s+powder|aqpc|quality\s+powder\s+coating|"
    r"melody\s+channell|"
    r"\b3p\b|rachel\s+bailey|"
    r"eastern\s+metal",
    flags=re.I,
)
THREE_P_RE = re.compile(r"\b3p\b|rachel\s+bailey", flags=re.I)
EASTERN_METAL_RE = re.compile(r"eastern\s+metal", flags=re.I)
_PO_HASH_LIST = re.compile(r"\bPO\s*#\s*([0-9,\s]+)", flags=re.I)
_INV_HASH = re.compile(r"\bINV(?:OICE)?\s*#?\s*[:.]?\s*(\d{5,8})\b", flags=re.I)
# AQPC often links a PDF instead of attaching it.
LINK_DOWNLOAD_VENDOR_RE = re.compile(
    r"american\s+quality\s+powder|aqpc|quality\s+powder\s+coating|aqpowder",
    flags=re.I,
)
INTERNAL_MAIL_RE = re.compile(
    r"\b(internal\s+only|do\s+not\s+process|ap\s+clerk\s+test\s+mail)\b",
    flags=re.I,
)


def classify_mail(*, subject: str = "", attachment_names: list[str] | None = None, preview: str = "") -> str:
    """Return 'invoice', 'check_stop', 'statement', 'pod', 'payment', 'internal', 'auto-pay', or 'not-a-bill'."""
    names = " ".join(attachment_names or [])
    blob = f"{subject}\n{names}\n{preview}"
    if is_auto_pay(subject=subject, preview=preview):
        return "auto-pay"
    # Gas & Supply: subject CHECK STOP is not enough — inbox must read the PDF
    # (0040367887 had 5 Misc invoices). Other CHECK STOP subjects stay noise.
    if re.search(r"\bcheck\s*stop\b", blob, flags=re.I):
        if is_gas_and_supply(blob):
            return "invoice"
        return "check_stop"
    if INTERNAL_MAIL_RE.search(blob):
        return "internal"
    if KNOWN_BILL_VENDOR_RE.search(blob) or MELODY_CHANNELL_RE.search(blob) or THREE_P_RE.search(blob):
        if re.search(r"\b(payment\s+confirmation|payment\s+received|thank\s+you\s+for\s+your\s+payment)\b", blob, flags=re.I):
            return "payment"
        return "invoice"
    if never_skip_vendor_invoice(
        subject=subject, from_name="", preview=preview, attachment_names=attachment_names
    ):
        if re.search(
            r"\b(payment\s+confirmation|payment\s+received|thank\s+you\s+for\s+your\s+payment|wire\s+confirmation)\b",
            subject,
            flags=re.I,
        ) and not INVOICE_HINT_RE.search(subject):
            return "payment"
        return "invoice"
    if INVOICE_HINT_RE.search(subject):
        if re.search(r"\b(payment\s+confirmation|payment\s+received|thank\s+you\s+for\s+your\s+payment|wire\s+confirmation)\b", subject, flags=re.I):
            return "payment"
        return "invoice"
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


def extract_subject_pos(subject: str) -> list[str]:
    """PO # 58766, 58767, 58844 from a 3P / Rachel Bailey subject."""
    match = _PO_HASH_LIST.search(subject or "")
    if not match:
        return []
    found = [n for n in re.findall(r"\d{5,6}", match.group(1))]
    return list(dict.fromkeys(found))


def extract_subject_invoice_number(subject: str) -> str | None:
    match = _INV_HASH.search(subject or "")
    if match:
        return match.group(1)
    loose = re.search(r"\binvoice\b[^0-9]{0,12}(\d{4,8})\b", subject or "", flags=re.I)
    if loose:
        return loose.group(1)
    return None


def format_unmatched_pos(pos: list[str] | None) -> str:
    return ", ".join(str(p) for p in (pos or []) if p)


def format_selected_receipts(matched: list[dict[str, Any]] | None) -> str:
    """Sheet notation: receipt id on each PO (3P multi-PO Select Receipts)."""
    bits: list[str] = []
    seen: set[str] = set()
    for hit in matched or []:
        rec = hit.get("receipt") if isinstance(hit, dict) else None
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id")
        if rid in (None, ""):
            continue
        po = rec.get("po")
        label = f"{rid} on PO {po}" if po not in (None, "") else str(rid)
        if label in seen:
            continue
        seen.add(label)
        bits.append(label)
    return ", ".join(bits)


def is_known_kimco_vendor(*parts: str) -> bool:
    """True when sender/subject/text matches a listed KIMCO vendor alias."""
    blob = " ".join(str(p or "") for p in parts).strip()
    if not blob:
        return False
    if known_vendor_id(blob) is not None:
        return True
    norm = normalize_name(blob)
    if not norm:
        return False
    for key in VENDOR_ID_ALIASES:
        if len(key) >= 4 and key in norm:
            return True
    return False


def never_skip_vendor_invoice(
    *,
    subject: str = "",
    from_name: str = "",
    preview: str = "",
    attachment_names: list[str] | None = None,
) -> bool:
    """KIMCO / known-bill vendor + invoice evidence is never not-a-bill/Skipped.

    Kyle: if a supplier is listed in KIMCO and provides an invoice (PDF,
    link-PDF, or Invoice/INV subject), enter or HOLD with a real Why.
    AI Skipped is only for true non-vendor noise.
    """
    names = list(attachment_names or [])
    blob = f"{from_name}\n{subject}\n{preview}\n{' '.join(names)}"
    if re.search(
        r"\b(payment\s+confirmation|payment\s+received|thank\s+you\s+for\s+your\s+payment|wire\s+confirmation)\b",
        blob,
        flags=re.I,
    ) and not INVOICE_HINT_RE.search(subject):
        return False
    has_invoice = bool(
        INVOICE_HINT_RE.search(subject)
        or INVOICE_HINT_RE.search(preview)
        or extract_subject_invoice_number(subject)
        or any(str(n or "").lower().endswith(".pdf") for n in names)
        or LINK_DOWNLOAD_VENDOR_RE.search(blob)
    )
    if not has_invoice:
        return False
    return bool(
        is_known_kimco_vendor(from_name, subject, preview)
        or KNOWN_BILL_VENDOR_RE.search(blob)
        or THREE_P_RE.search(blob)
        or EASTERN_METAL_RE.search(blob)
        or MELODY_CHANNELL_RE.search(blob)
        or LINK_DOWNLOAD_VENDOR_RE.search(blob)
        or INVOICE_HINT_RE.search(subject)
    )


def should_create_header(inv: dict[str, Any]) -> tuple[bool, str]:
    """Real vendor bills get a header even with no PO. HOLD is not for missing PO alone.

    Price-does-not-match and qty HOLD still create header + attach PDF.
    Auto-pay / Toyota and pdf-behind-link do not enter in ERP.
    """
    if is_auto_pay(
        vendor=str(inv.get("vendor") or ""),
        subject=str(inv.get("subject") or ""),
        preview=str(inv.get("bodyPreview") or inv.get("preview") or ""),
        text=str(inv.get("text") or ""),
    ) or str(inv.get("hold_reason") or "").strip().lower() in {"auto-pay", "auto pay"}:
        return False, "auto-pay"
    if inv.get("check_stop") or str(inv.get("hold_reason") or "").strip().upper() == "CHECK STOP":
        return False, "CHECK STOP"
    reason = str(inv.get("hold_reason") or "").strip()
    reason_key = reason.lower()
    if reason_key in CREATE_HEADER_ON_HOLD:
        return True, ""
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
