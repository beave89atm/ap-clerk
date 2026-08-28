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
    """Manual Outlook flag column. Yes only after Success so Kyle can flag it."""
    return "Yes" if (result or "").strip() == "Success" else "No"


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
    r"\bdelivery\s+receipt\b)",
    flags=re.I,
)
STATEMENT_RE = re.compile(r"\b(account\s+)?statement\b", flags=re.I)
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
