"""AP clerk policy helpers. No network I/O."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
COMMENTS = "API TEST prototype only do not pay."
CURRENCY_USD_ID = 3
INVOICE_TYPE_PO = 3
FORBIDDEN_BATCH_IDS = {669}
FORBIDDEN_BATCH_NAMES = {"Mark Brown 8/4/26"}
FORBIDDEN_INVOICE_IDS = {9474, 9475, 9476, 9477, 9478}
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


def lookup_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def lookup_id(value: Any) -> int | None:
    if isinstance(value, dict) and value.get("id") is not None:
        return int(value["id"])
    return None
