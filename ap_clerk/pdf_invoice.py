"""Extract vendor-invoice fields from PDF text. No network I/O. Never logs PDF bytes."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from ap_clerk.rules import FEE_KEYWORDS, extract_po_number, is_fee_or_surcharge

LOGGER = logging.getLogger("ap_clerk")

_INV_LABEL = re.compile(
    r"(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?\s*#|inv\s+no\.?)\s*[:.\s#-]*([A-Z0-9][A-Z0-9/_-]{2,})",
    flags=re.I,
)
_INV_BARE = re.compile(r"\b(?:invoice)\s+([A-Z]{1,6}-?\d{4,}|\d{5,})\b", flags=re.I)
_PO_LABEL = re.compile(
    r"(?:purchase\s*order|customer\s*p\.?o\.?|your\s*p\.?o\.?|p\.?o\.?\s*(?:number|no\.?|#)?)\s*[:.\s#-]*([A-Z]{0,6}\d{4,8})",
    flags=re.I,
)
_AMOUNT_LABEL = re.compile(
    r"(?:invoice\s*total|amount\s*due|total\s*due|balance\s*due|amount\s*of\s*invoice|"
    r"total\s*invoice|invoice\s*amount|total\s*amount\s*due|amount\s*to\s*pay|"
    r"please\s*pay|grand\s*total)\s*[:.\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    flags=re.I,
)
_TOTAL_FALLBACK = re.compile(r"\b(?:total)\s*[:.\s]*\$\s*([\d,]+(?:\.\d{2})?)", flags=re.I)
_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
_DATE_LABEL = re.compile(
    r"(?:invoice\s*date|date\s*of\s*invoice|inv(?:oice)?\s*date)\s*[:.\s]*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})",
    flags=re.I,
)
_DATE_ANY = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b"
)
_CHECK_STOP = re.compile(r"\bcheck\s*stop\b", flags=re.I)
_FROM_LINE = re.compile(r"^(?:from|bill\s*from|sold\s*by|remit(?:\s*to)?)\s*[:\-]\s*(.+)$", flags=re.I)
_MULTI_PO_SPLIT = re.compile(r"[;,/]|and")


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not kill the invoice
            pages.append("")
    return "\n".join(pages)


def parse_money(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace("$", "").strip()
    if not text:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def parse_date_value(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text.replace(",", ""), fmt.replace(",", "")).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def extract_po_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _PO_LABEL.finditer(text or ""):
        raw = match.group(1)
        number = extract_po_number(raw) or re.sub(r"\D", "", raw)
        if number and 4 <= len(number) <= 8:
            found.append(number)
    for match in re.finditer(r"\bPO\s*#?\s*(\d{4,6})\b", text or "", flags=re.I):
        found.append(match.group(1))
    return _unique(found)


def extract_fees(text: str) -> list[dict[str, Any]]:
    fees: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or not is_fee_or_surcharge(stripped):
            continue
        amounts = [parse_money(m) for m in _MONEY.findall(stripped)]
        amounts = [a for a in amounts if a is not None]
        name = re.sub(r"\s+\$?[\d,]+\.\d{2}\s*$", "", stripped)
        name = re.sub(r"\s{2,}", " ", name).strip(" :-")
        if not name:
            name = next((k for k in FEE_KEYWORDS if k in stripped.lower()), "fee")
        fees.append({"name": name[:80], "amount": amounts[-1] if amounts else None})
    # Dedup by lowercase name
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fee in fees:
        key = str(fee["name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(fee)
    return dedup[:8]


def _vendor_from_text(text: str, *, from_name: str = "", subject: str = "") -> str:
    if from_name and "@" not in from_name:
        cleaned = re.sub(r"\s+", " ", from_name).strip()
        if cleaned and "microsoft" not in cleaned.lower() and "mailer" not in cleaned.lower():
            return cleaned
    for line in (text or "").splitlines()[:20]:
        match = _FROM_LINE.match(line.strip())
        if match:
            return match.group(1).strip()[:80]
    # First substantial letterhead-looking line
    for line in (text or "").splitlines()[:12]:
        stripped = line.strip()
        if len(stripped) < 4 or len(stripped) > 80:
            continue
        if re.search(r"invoice|page\s+\d|statement|remit to|bill to", stripped, flags=re.I):
            continue
        if re.search(r"[A-Za-z]{3,}", stripped) and not re.match(r"^\d", stripped):
            return stripped[:80]
    if from_name:
        return re.sub(r"\s+", " ", from_name).strip()[:80]
    return (subject or "").strip()[:80]


def parse_invoice_text(
    text: str,
    *,
    subject: str = "",
    from_name: str = "",
    filename: str = "",
) -> dict[str, Any]:
    blob = "\n".join([subject, filename, text or ""])
    invoice_number = ""
    match = _INV_LABEL.search(blob)
    if match:
        invoice_number = match.group(1).strip(" .:-")
    if not invoice_number:
        match = _INV_BARE.search(blob)
        if match:
            invoice_number = match.group(1).strip()
    if not invoice_number:
        file_match = re.search(r"([A-Z0-9][A-Z0-9/_-]{4,})", filename or "", flags=re.I)
        if file_match and not re.search(r"invoice|statement|attachment", file_match.group(1), flags=re.I):
            invoice_number = file_match.group(1)

    pos = extract_po_numbers(blob)
    amount = None
    amt_match = _AMOUNT_LABEL.search(text or "") or _AMOUNT_LABEL.search(blob)
    if amt_match:
        amount = parse_money(amt_match.group(1))
    if amount is None:
        totals = [parse_money(m) for m in _TOTAL_FALLBACK.findall(text or "")]
        totals = [a for a in totals if a is not None]
        if totals:
            amount = max(totals)

    invoice_date = None
    date_match = _DATE_LABEL.search(blob)
    if date_match:
        invoice_date = parse_date_value(date_match.group(1))
    if not invoice_date:
        for raw in _DATE_ANY.findall(blob):
            parsed = parse_date_value(raw)
            if parsed:
                invoice_date = parsed
                break

    check_stop = bool(_CHECK_STOP.search(blob))
    vendor = _vendor_from_text(text, from_name=from_name, subject=subject)
    fees = extract_fees(text)
    po: str | None
    if len(pos) == 1:
        po = pos[0]
    elif len(pos) > 1:
        po = None
    else:
        po = None

    return {
        "vendor": vendor,
        "invoice_number": invoice_number,
        "date": invoice_date,
        "po": po,
        "pos": pos,
        "amount": amount,
        "fees": fees,
        "check_stop": check_stop,
        "hold_reason": "CHECK STOP" if check_stop else "",
        "multi_po": len(pos) > 1,
        "text_chars": len(text or ""),
    }


def parse_invoice_pdf(
    path: Path,
    *,
    subject: str = "",
    from_name: str = "",
) -> dict[str, Any]:
    text = extract_pdf_text(path)
    parsed = parse_invoice_text(text, subject=subject, from_name=from_name, filename=path.name)
    parsed["pdf_path"] = str(path)
    parsed["pdf_text_empty"] = not (text or "").strip()
    return parsed
