"""Extract vendor-invoice fields from PDF text. No network I/O. Never logs PDF bytes."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from ap_clerk.rules import (
    FEE_KEYWORDS,
    extract_po_number,
    is_fee_or_surcharge,
    known_invoice_prefix,
    printed_invoice_number,
)

LOGGER = logging.getLogger("ap_clerk")

_INV_LABEL = re.compile(
    r"(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?\s*#)\s*[:.\s#]*([A-Z]{0,8}\d-?\d{3,}[A-Z0-9/_-]*)",
    flags=re.I,
)
_INV_PREFIXED = re.compile(r"\b(\d-\d{5,8})\b")
_PART_NUMBER = re.compile(r"\b(\d{3}-\d{4}-\d{3})\b")
_INV_FASTENAL = re.compile(r"\b(TXFT\d{5,})\b", flags=re.I)
_INV_GAS = re.compile(r"\b(00\d{8})\b")
_INV_EMJ = re.compile(r"\bINVOICE NUMBER\s+([A-Z]\d{6,})\b", flags=re.I)
_INV_PSI = re.compile(r"\b(PSI-\d{6,})\b", flags=re.I)
_INV_NTEX = re.compile(r"\b(\d{2}-\d{4})\b")
_INV_SV = re.compile(r"\b(SV\d{6,})\b", flags=re.I)
_INV_DASH_IN = re.compile(r"\b(\d{6,}-IN)\b", flags=re.I)
_INV_MCQUEARY = re.compile(r"\b(\d{2}-\d{5})\b")
_INV_TUBE = re.compile(r"\b(011\d{5})\b")
_INV_MSC_REAL = re.compile(r"Invoice Number\s+(\d{7,8})\b", flags=re.I)
_INV_GRM = re.compile(r"Invoice\s{2,}(\d{7,8})\b", flags=re.I)
_INV_PS_INV = re.compile(r"\b(PS-INV\d{5,})\b", flags=re.I)
_INV_JVT = re.compile(r"\b(JVT\s+SI-\d{4,})\b", flags=re.I)
_INV_TMC = re.compile(r"\b(TMC-\d{5,})\b", flags=re.I)
_INV_A1_STACKED = re.compile(
    r"Invoice:\s*(?:\n+\s*INVOICE DATE)?\s*\n+\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*\n+\s*(\d{5,})",
    flags=re.I,
)
_INV_COLON_NUM = re.compile(r"Invoice:\s*\n+\s*(\d{5,8})\b", flags=re.I)
_TOTAL_STACKED = re.compile(
    r"(?:^|\n)\s*TOTAL\b[:. \t]*\n(?:[ \t]*[A-Za-z].*\n){0,6}[ \t]*([\d,]+\.\d{2})",
    flags=re.I,
)
_AMOUNT_BALANCE = re.compile(
    r"(?:balance\s+due|please\s+pay\s+this\s+amount)\s*[:.]?\s*\$?\s*([\d,]+(?:\.\d{2}))",
    flags=re.I,
)
_PO_NONE = re.compile(r"purchase\s*order(?:\s*number)?\s*[:.\s#-]*none\b", flags=re.I)
_PO_LABEL = re.compile(
    r"(?:purchase\s*order(?:\s*number)?|customer\s*p\.?o\.?|your\s*p\.?o\.?|"
    r"cust(?:omer)?\.?\s*p\.?o\.?|p\.?o\.?\s*(?:number|#|no\.?))\s*[:.#\s-]*([A-Z]{0,4}\d{4,8})",
    flags=re.I,
)
_PO_BARE = re.compile(r"\bPO\s*[:.#]?\s*(\d{4,6})\b", flags=re.I)
_AMOUNT_LABEL = re.compile(
    r"(?:total\s+to\s+be\s+paid(?:\s+usd)?|invoice\s*total|amount\s*due|total\s*due|"
    r"total\s*amount\s*due|total\s*-\s*this\s*invoice|invoice\s*amount|"
    r"grand\s*total|total\s+order\s+amount|total\s+due\s*\(\s*usd\s*\)|"
    r"total\s*\$?\s*incl\.?\s*tax|total\s+this\s+invoice)\s*[:.\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    flags=re.I,
)
_AMOUNT_USD_DUE = re.compile(
    r"Total Due\s*(?:\(\s*USD\s*\))?\s*\$?\s*([\d,]+(?:\.\d{2}))",
    flags=re.I,
)
_AMOUNT_USD_PREFIX = re.compile(r"\bUSD\s+([\d,]+(?:\.\d{2}))", flags=re.I)
_AMOUNT_DUE_LABEL = re.compile(
    r"(?:amount due|total current charges|current charges due)\s*[:.\s]*(?:USD\s*)?([\d,]+(?:\.\d{2}))",
    flags=re.I,
)
_EXT_PRICE = re.compile(r"Ext(?:ended)?\s*Price.{0,120}?([\d,]+\.\d{2})", flags=re.I | re.S)
_AMOUNT_BEFORE = re.compile(
    r"\$?\s*([\d,]+(?:\.\d{2}))\s*(?:Invoice Total|Total Amount Due|Amount Due|AMOUNT DUE)",
    flags=re.I,
)
_CUSTOMER_ACCOUNTS = {"TXFT40601", "14748440", "02627782"}
_TOTAL_MONEY = re.compile(r"(?:^|\b)total(?:\s+\$|\s*[:.\s]*\$)\s*([\d,]+(?:\.\d{2})?)", flags=re.I)
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d{2}))")
_DATE_LABEL = re.compile(
    r"(?:invoice\s*date|date\s*of\s*invoice|inv(?:oice)?\s*date)\s*[:.\s]*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}-[A-Za-z]{3}-\d{2,4}|[A-Za-z]{3,9}-\d{1,2}-\d{2,4})",
    flags=re.I,
)
_DATE_ANY = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}-[A-Za-z]{3}-\d{2,4}|[A-Za-z]{3,9}-\d{1,2}-\d{2,4})\b"
)
_CHECK_STOP = re.compile(r"\bcheck\s*stop\b", flags=re.I)
_BAD_INVOICE_WORDS = {
    "WHEN",
    "TYPE",
    "DUE",
    "PLEASE",
    "TOTAL",
    "PAGE",
    "DATE",
    "NONE",
    "INVOICE",
    "NUMBER",
    "ORIGINAL",
    "STATEMENT",
}

PO_DOCUMENT_FILE_RE = re.compile(r"purchase[_ -]?order|packing[_ -]?list|packing[_ -]?slip", flags=re.I)
_PO_DOC_HEADING = re.compile(r"(?:^|\n)\s*PURCHASE\s+ORDER\b", flags=re.I)
_INVOICE_DOC_HINT = re.compile(r"\b(invoice\s*(number|no\.?|#|total)|amount\s+due|bill\s+to)\b", flags=re.I)


def is_purchase_order_document(*, text: str = "", filename: str = "") -> bool:
    """True for a PO/packing-list attachment that must not be entered as a vendor invoice."""
    name = filename or ""
    if PO_DOCUMENT_FILE_RE.search(name):
        return True
    blob = text or ""
    if _PO_DOC_HEADING.search(blob) and not _INVOICE_DOC_HINT.search(blob):
        return True
    if _PO_DOC_HEADING.search(blob) and re.search(r"\bship\s+to\b", blob, flags=re.I):
        if not re.search(r"\binvoice\s*(total|number|no\.?|#)\b", blob, flags=re.I):
            return True
    return False


DOMAIN_VENDORS = {
    "airproducts.com": "Air Products and Chemicals, Inc",
    "emjmetals.com": "Earle M. Jorgensen Co",
    "onealsteel.com": "O'Neal Steel - Dallas (GP)",
    "gasandsupply.com": "Gas and Supply North Texas, LLC",
    "fastenal.com": "Fastenal Company",
    "mcmaster.com": "McMaster-Carr Supply Company",
    "modernht.com": "Modern Heat Treat Inc",
    "ii-vi.com": "Coherent Corp.",
    "nsalloys.com": "National Specialty Alloys, Inc",
    "mscdirect.com": "MSC Industrial Supply",
    "metalsupermarkets.com": "Metal Supermarkets",
    "marmonkeystone.com": "Marmon/Keystone",
    "amada.com": "Amada America",
    "curbellplastics.com": "Curbell Plastics",
    "engieresources.com": "ENGIE Resources LLC",
    "wcicustomer.com": "Waste Connections Lone Star, Inc",
    "unifirstfirstaidandsafety.com": "UniFirst First Aid & Safety",
    "unifirst.com": "UniFirst Corporation",
    "ntexelectric.com": "NTEX Electric Inc.",
    "kloeckner.com": "Kloeckner Metals Corporation",
    "kloecknermetals.com": "Kloeckner Metals Corporation",
    "altparts.com": "Alternative Parts Inc",
    "grmdocument.com": "GRM Information Management Services",
    "grmdocumentmanagement.com": "GRM Information Management Services",
    "tubesupply.com": "Tube Supply",
    "crosslinktx.com": "Crosslink Powder Coating",
    "ryerson.com": "Joseph T. Ryerson & Son, Inc",
    "austinhardware.com": "Austin Hardware & Supply Inc.",
    "a1image.com": "A1 Image Office Systems",
    "gexpro.com": "Gexpro Services",
    "gexproservices.com": "Gexpro Services",
    "maynardnexsen.com": "Maynard Nexsen PC",
    "leecosteel.com": "Leeco Steel, LLC",
    "versalift.com": "Versalift National Parts Distribution Center",
    "aft-corp.com": "Automated Finishing Technology",
    "morgansteel.net": "Morgan Steel",
    "pctsupport.com": "PCT Support",
    "xcaliberind.com": "Xcaliber Industrial LLC",
    "aqpowder.com": "American Quality Powder Coating",
    "americanqualitypowder.com": "American Quality Powder Coating",
}

SUBJECT_VENDORS = (
    (re.compile(r"fastenal", re.I), "Fastenal Company"),
    (re.compile(r"mcmaster", re.I), "McMaster-Carr Supply Company"),
    (re.compile(r"o'?neal", re.I), "O'Neal Steel - Dallas (GP)"),
    (re.compile(r"earle m\.?\s*jorgensen|\bemj\b", re.I), "Earle M. Jorgensen Co"),
    (re.compile(r"air products", re.I), "Air Products and Chemicals, Inc"),
    (re.compile(r"gas\s*&?\s*supply", re.I), "Gas and Supply North Texas, LLC"),
    (re.compile(r"luxor", re.I), "Luxor Staffing, Inc."),
    (re.compile(r"national specialty alloys", re.I), "National Specialty Alloys, Inc"),
    (re.compile(r"modern heat treat", re.I), "Modern Heat Treat Inc"),
    (re.compile(r"coherent|ii-vi", re.I), "Coherent Corp."),
    (re.compile(r"telecom products", re.I), "Telecom Products Inc."),
    (re.compile(r"rmp industrial", re.I), "RMP Industrial Supply Inc"),
    (re.compile(r"tejas transportation", re.I), "Tejas Transportation"),
    (re.compile(r"telecom products", re.I), "Telecom Products Inc."),
    (re.compile(r"service experts", re.I), "Service Experts"),
    (re.compile(r"priority\s*1|priority1invoice", re.I), "Priority 1"),
    (re.compile(r"\bmsc\b|msc industrial", re.I), "MSC Industrial Supply"),
    (re.compile(r"metal supermarket", re.I), "Metal Supermarkets"),
    (re.compile(r"marmon|keystone", re.I), "Marmon/Keystone"),
    (re.compile(r"\bamada\b", re.I), "Amada America"),
    (re.compile(r"exotic metals", re.I), "Exotic Metals"),
    (re.compile(r"jp steel", re.I), "JP Steel"),
    (re.compile(r"curbell", re.I), "Curbell Plastics"),
    (re.compile(r"capital machine", re.I), "Capital Machine Technologies, Inc"),
    (re.compile(r"clear kut", re.I), "Clear Kut Engraving"),
    (re.compile(r"willbanks", re.I), "Willbanks Metals"),
    (re.compile(r"waste connections", re.I), "Waste Connections Lone Star, Inc"),
    (re.compile(r"\bengie\b", re.I), "ENGIE Resources LLC"),
    (re.compile(r"unifirst\s+first\s+aid|unifirstfirstaid|firstaidinquiry", re.I), "UniFirst First Aid & Safety"),
    (re.compile(r"unifirst", re.I), "UniFirst Corporation"),
    (re.compile(r"shoppa", re.I), "Shoppa's Material Handling"),
    (re.compile(r"eastern metal", re.I), "Eastern Metal Supply of Texas"),
    (re.compile(r"green valley compressor", re.I), "Green Valley Compressor LLC"),
    (re.compile(r"purvis", re.I), "Purvis Industries"),
    (re.compile(r"ntex", re.I), "NTEX Electric Inc."),
    (re.compile(r"kloeckner", re.I), "Kloeckner Metals Corporation"),
    (re.compile(r"american bearing", re.I), "American Bearing Company"),
    (re.compile(r"morgan steel", re.I), "Morgan Steel"),
    (re.compile(r"\bgrm\b", re.I), "GRM Information Management Services"),
    (re.compile(r"alternative parts|altparts", re.I), "Alternative Parts Inc"),
    (re.compile(r"tube supply|tubesupply", re.I), "Tube Supply"),
    (re.compile(r"lavanture", re.I), "Lavanture Products"),
    (re.compile(r"leeco", re.I), "Leeco Steel, LLC"),
    (re.compile(r"austin hardware", re.I), "Austin Hardware & Supply Inc."),
    (re.compile(r"a1[\s_]*image", re.I), "A1 Image Office Systems"),
    (re.compile(r"maynard\s+nexsen|nexsen", re.I), "Maynard Nexsen PC"),
    (re.compile(r"legacy wire", re.I), "Legacy Wire Products"),
    (re.compile(r"gexpro", re.I), "Gexpro Services"),
    (re.compile(r"beshert|triple-?s steel|steel warehouse", re.I), "Beshert Steel Processing"),
    (re.compile(r"precision fabrication", re.I), "Precision Fabrication Services"),
    (re.compile(r"versalift", re.I), "Versalift National Parts Distribution Center"),
    (re.compile(r"automated finishing|aft industries", re.I), "Automated Finishing Technology"),
    (re.compile(r"polymer products", re.I), "Polymer Products"),
    (re.compile(r"hapeco", re.I), "Hapeco, Inc"),
    (re.compile(r"morgan steel", re.I), "Morgan Steel"),
    (re.compile(r"pct\s+support|pctsupport", re.I), "PCT Support"),
    (re.compile(r"xcaliber", re.I), "Xcaliber Industrial LLC"),
    (re.compile(r"american\s+quality\s+powder|aqpc", re.I), "American Quality Powder Coating"),
    (re.compile(r"crosslink", re.I), "Crosslink Powder Coating"),
    (re.compile(r"ryerson", re.I), "Joseph T. Ryerson & Son, Inc"),
    (re.compile(r"mcqueary", re.I), "McQueary Industries"),
    (re.compile(r"hudson energy", re.I), "Hudson Energy"),
)


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
    text = value.strip().replace(",", "")
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%B %d %Y",
        "%b %d %Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%b-%d-%Y",
        "%b-%d-%y",
        "%B-%d-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
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


def _looks_like_date_token(token: str) -> bool:
    if parse_date_value(token):
        return True
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", token or ""):
        return True
    # AUG-03-2026 leftovers like 03-2026
    return bool(re.fullmatch(r"\d{1,2}-20\d{2}", token or ""))


def _usable_invoice_number(token: str | None) -> str | None:
    if not token:
        return None
    value = token.strip(" .:-#")
    if not value or value.upper() in _BAD_INVOICE_WORDS:
        return None
    if _looks_like_date_token(value):
        return None
    if re.fullmatch(r"20\d{2}", value):
        return None
    if re.fullmatch(r"[A-Za-z]+", value):
        return None
    if len(re.sub(r"\D", "", value)) < 3:
        return None
    if len(value) > 24:
        return None
    return value


def extract_po_numbers(text: str) -> list[str]:
    if _PO_NONE.search(text or ""):
        return []
    found: list[str] = []
    for match in _PO_LABEL.finditer(text or ""):
        raw = match.group(1)
        if raw.upper() in {"NONE", "NET"} or raw.upper().startswith("TXFT"):
            continue
        if re.fullmatch(r"C\d{5,8}", raw.upper()):
            continue
        number = extract_po_number(raw) or re.sub(r"\D", "", raw)
        if number and 4 <= len(number) <= 8 and not number.startswith("00"):
            found.append(number)
    for match in _PO_BARE.finditer(text or ""):
        found.append(match.group(1))
    modern = re.search(r"\b(\d{5}),\s*line\b", text or "", flags=re.I)
    if modern:
        found.append(modern.group(1))
    # Fastenal: Cust. No. / Cust. P.O. then TXFTxxxxx \n 58xxx
    fastenal = re.search(
        r"Cust(?:omer)?\.?\s*P\.?O\.?.{0,80}?TXFT\d+\s+(\d{5,6})",
        text or "",
        flags=re.I | re.S,
    )
    if fastenal:
        found.append(fastenal.group(1))
    # Fastenal column dump: customer number then PO on the next line
    stacked = re.search(r"\bTXFT\d{5,}\s+(\d{5,6})\b", text or "", flags=re.I)
    if stacked:
        found.append(stacked.group(1))
    your_po = re.findall(r"Your\s+PO\s+(\d{5,6})", text or "", flags=re.I)
    found.extend(your_po)
    # Live KIMCO POs are 57xxx–59xxx and often sit unlabeled on Tube Supply / Morgan PDFs.
    found.extend(re.findall(r"\b(5[7-9]\d{3})\b", text or ""))
    # Shoppas / UniFirst customer accounts like C109050 are not POs.
    cleaned: list[str] = []
    for number in _unique(found):
        if re.search(rf"\bC{re.escape(number)}\b", text or "", flags=re.I):
            continue
        # Vendor-internal 8-digit POs (Kloeckner 25576511) are not KIMCO POs.
        if len(number) >= 8:
            continue
        cleaned.append(number)
    kimco = [n for n in cleaned if re.fullmatch(r"5[7-9]\d{3}", n)]
    return kimco or cleaned


def extract_fees(text: str) -> list[dict[str, Any]]:
    fees: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or not is_fee_or_surcharge(stripped):
            continue
        amounts = [parse_money(m) for m in _MONEY.findall(stripped)]
        amounts = [a for a in amounts if a is not None and a < 100000]
        name = re.sub(r"\s+\$?[\d,]+\.\d{2}\s*$", "", stripped)
        name = re.sub(r"\s{2,}", " ", name).strip(" :-")
        if not name:
            name = next((k for k in FEE_KEYWORDS if k in stripped.lower()), "fee")
        fees.append({"name": name[:80], "amount": amounts[-1] if amounts else None})
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fee in fees:
        key = str(fee["name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(fee)
    return dedup[:8]


def extract_invoice_lines(text: str) -> list[dict[str, Any]]:
    """Part numbers and nearby qty/amount from PDF text. Used for Select Receipts."""
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        parts = _PART_NUMBER.findall(stripped)
        if not parts:
            continue
        amounts = [parse_money(m) for m in _MONEY.findall(stripped)]
        amounts = [a for a in amounts if a is not None and a < 100000]
        qty = None
        qty_match = re.search(r"\b(?:qty|quantity)\s*[:.]?\s*(\d+(?:\.\d+)?)\b", stripped, flags=re.I)
        if qty_match:
            qty = parse_money(qty_match.group(1))
        elif amounts and len(amounts) >= 2:
            qty = amounts[0]
        po_line = None
        line_match = re.search(r"\b(?:line|ln)\s*[:.#-]?\s*(\d{1,3})\b", stripped, flags=re.I)
        if line_match:
            po_line = int(line_match.group(1))
        wo_match = re.search(r"\bWO[:\s#-]*(\d{3,})\b", stripped, flags=re.I)
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            lines.append(
                {
                    "part": part,
                    "qty": qty,
                    "amount": amounts[-1] if amounts else None,
                    "po_line": po_line,
                    "wo": wo_match.group(1) if wo_match else None,
                    "label": stripped[:80],
                }
            )
    return lines[:40]


def vendor_from_context(*, subject: str = "", from_name: str = "", from_address: str = "", text: str = "") -> str:
    addr = (from_address or "").lower()
    if "firstaid" in addr or "first aid" in (from_name or "").lower() or "firstaid" in (subject or "").lower():
        return "UniFirst First Aid & Safety"
    if "@" in addr:
        domain = addr.split("@", 1)[1]
        if domain in DOMAIN_VENDORS:
            return DOMAIN_VENDORS[domain]
    blob = f"{subject}\n{from_name}\n{from_address}\n{text or ''}"
    for pattern, vendor in SUBJECT_VENDORS:
        if pattern.search(blob):
            return vendor
    for line in (text or "").splitlines():
        stripped = line.strip()
        if re.match(
            r"^(air products|fastenal|gas and supply|earle m|o'?neal|luxor|coherent|modern heat|national specialty|mcmaster|telecom products|rmp industrial|priority\s*1|msc industrial|metal supermarket|marmon|amada|exotic metals|jp steel|curbell|capital machine|clear kut|willbanks|waste connections|engie|unifirst|shoppa|eastern metal|green valley|purvis|ntex|kloeckner|american bearing|american quality powder|morgan steel|grm|alternative parts|tube supply|lavanture|crosslink|ryerson|mcqueary|hudson energy|leeco|austin hardware|a1 image|maynard nexsen|legacy wire|gexpro|beshert|precision fabrication|versalift|automated finishing|polymer products|hapeco|aft industries|pct support|xcaliber)",
            stripped,
            re.I,
        ):
            return stripped[:80]
    if from_name and "@" not in from_name:
        cleaned = re.sub(r"\s+", " ", from_name).strip()
        if re.search(r"\b(inc|llc|ltd|co|company|corp|supply|steel|products|staffing|alloys)\b", cleaned, re.I):
            return cleaned[:80]
    return (from_name or subject or "").strip()[:80]


def _invoice_from_filename(filename: str) -> str | None:
    name = filename or ""
    for pattern in (
        r"(TXFT\d{5,})",
        r"(PS-INV\d{5,})",
        r"(TMC-\d{5,})",
        r"\b(\d{2}-\d{4,5})\b",
        r"\b(\d-\d{5,8})\b",
        r"Invoice[-_ ]+(\d-\d{5,8}|\d{2}-\d{4,5}|\d{4,})",
        r"Inv[_-]?(\d-\d{5,8}|\d{5,})",
        r"[-_](\d-\d{5,8})\.pdf$",
        r"[-_](\d{5,})\.pdf$",
        r"inv[-_ ]+(\d{4,})",
        r"(00\d{8})",
        r"[-_]([A-Z]\d{7,})",
        r"(PSI-\d{6,})",
        r"(SV\d{6,})",
        r"(\d{6,}-IN)",
    ):
        match = re.search(pattern, name, flags=re.I)
        if match:
            return _usable_invoice_number(match.group(1))
    return None


def _invoice_from_subject(subject: str) -> str | None:
    for pattern in (
        r"Invoice\s*(?:Number|#|No\.?)?\s*[-:#]?\s*([A-Z]{0,8}\d{4,})",
        r"\b(TXFT\d{5,})\b",
        r"\b(00\d{8})\b",
        r"\binv(?:oice)?\s+(\d{4,})\b",
    ):
        match = re.search(pattern, subject or "", flags=re.I)
        if match:
            return _usable_invoice_number(match.group(1))
    return None


_ONEAL_INV = re.compile(r"\b(15\d{6})\b")
_ONEAL_PO = re.compile(r"Customer\s+PO#\s+(\d{5})", flags=re.I)
_ONEAL_TOTAL = re.compile(
    r"TOTAL ORDER AMOUNT.{0,500}?([\d,]+\.\d{2})\s*\n\s*\.00\s*\n\s*([\d,]+\.\d{2})",
    flags=re.I | re.S,
)


def _oneal_invoice_numbers(text: str) -> list[str]:
    found: list[str] = []
    for match in _ONEAL_INV.finditer(text or ""):
        number = match.group(1)
        if number not in found:
            found.append(number)
    return found


def _oneal_totals(text: str) -> list[float]:
    totals: list[float] = []
    for match in _ONEAL_TOTAL.finditer(text or ""):
        amount = parse_money(match.group(2))
        if amount not in (None, 0, 0.0):
            totals.append(amount)
    return totals


def expand_oneal_invoices(text: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Batched O'Neal PDFs can hold more than one 15xxxxxx invoice."""
    vendor = str(parsed.get("vendor") or "")
    if "oneal" not in vendor.lower() and "o'neal" not in vendor.lower():
        return [parsed]
    numbers = _oneal_invoice_numbers(text)
    totals = _oneal_totals(text)
    pos = _ONEAL_PO.findall(text or "")
    parsed = dict(parsed)
    if totals:
        parsed["amount"] = totals[0]
    if len(pos) == 1:
        parsed["po"] = pos[0]
        parsed["pos"] = pos
        parsed["multi_po"] = False
    if len(numbers) <= 1:
        return [parsed]
    bills: list[dict[str, Any]] = []
    for index, number in enumerate(numbers):
        bill = dict(parsed)
        bill["invoice_number"] = number
        if index < len(totals):
            bill["amount"] = totals[index]
        if index < len(pos) and len(pos) == len(numbers):
            bill["po"] = pos[index]
            bill["pos"] = [pos[index]]
            bill["multi_po"] = False
        elif len(pos) == 1:
            bill["po"] = pos[0]
            bill["pos"] = pos
            bill["multi_po"] = False
        else:
            bill["po"] = None
            bill["pos"] = pos
            bill["multi_po"] = len(pos) > 1
        bill.pop("siblings", None)
        bills.append(bill)
    return bills


def parse_invoice_text(
    text: str,
    *,
    subject: str = "",
    from_name: str = "",
    from_address: str = "",
    filename: str = "",
) -> dict[str, Any]:
    pdf_text = text or ""
    blob = "\n".join([subject, filename, pdf_text])
    sources = {"invoice_number": "", "date": "", "amount": "", "po": ""}
    vendor = vendor_from_context(subject=subject, from_name=from_name, from_address=from_address, text=pdf_text)
    invoice_number = None
    invoice_from_pdf = False
    if known_invoice_prefix(vendor):
        prefixed = _INV_PREFIXED.search(pdf_text)
        if prefixed:
            invoice_number = _usable_invoice_number(prefixed.group(1))
            invoice_from_pdf = bool(invoice_number)
    for rx in (_INV_EMJ, _INV_PS_INV, _INV_TMC, _INV_JVT, _INV_COLON_NUM, _INV_SV, _INV_DASH_IN, _INV_MSC_REAL, _INV_GRM, _INV_LABEL, _INV_GAS):
        if invoice_number:
            break
        match = rx.search(pdf_text)
        if match:
            invoice_number = _usable_invoice_number(match.group(1))
            if invoice_number and invoice_number.upper() not in _CUSTOMER_ACCOUNTS:
                invoice_from_pdf = True
                break
            invoice_number = None
    vendor_l = (vendor or "").lower()
    blob_l = blob.lower()
    if not invoice_number and ("mcqueary" in vendor_l or "mcqueary" in pdf_text.lower()):
        mcq = _INV_MCQUEARY.search(pdf_text)
        if mcq:
            invoice_number = _usable_invoice_number(mcq.group(1))
            invoice_from_pdf = bool(invoice_number)
    if ("gas and supply" in vendor_l or "gasandsupply" in blob_l):
        gas = _INV_GAS.search(pdf_text)
        if gas:
            invoice_number = _usable_invoice_number(gas.group(1))
            invoice_from_pdf = bool(invoice_number)
    if not invoice_number and ("ntex" in vendor_l or "ntex" in pdf_text.lower()):
        ntex = _INV_NTEX.search(pdf_text)
        if ntex:
            invoice_number = _usable_invoice_number(ntex.group(1))
            invoice_from_pdf = bool(invoice_number)
    if not invoice_number and ("tube supply" in vendor_l or "tubesupply" in pdf_text.lower()):
        tube = _INV_TUBE.search(pdf_text)
        if tube:
            invoice_number = _usable_invoice_number(tube.group(1))
            invoice_from_pdf = bool(invoice_number)
    if not invoice_number:
        msc_pair = re.search(
            r"Customer Number\s+Invoice Number\s+(\d{7,8})\s+(\d{7,8})",
            pdf_text,
            flags=re.I,
        )
        if msc_pair:
            first, second = msc_pair.group(1), msc_pair.group(2)
            pick = second if first.upper() in _CUSTOMER_ACCOUNTS else first
            if pick.upper() not in _CUSTOMER_ACCOUNTS:
                invoice_number = _usable_invoice_number(pick)
                invoice_from_pdf = bool(invoice_number)
    if not invoice_number:
        a1_stacked = _INV_A1_STACKED.search(pdf_text)
        if a1_stacked:
            invoice_number = _usable_invoice_number(a1_stacked.group(2))
            invoice_from_pdf = bool(invoice_number)
    if not invoice_number:
        psi = _INV_PSI.search(pdf_text)
        if psi:
            invoice_number = _usable_invoice_number(psi.group(1))
            invoice_from_pdf = bool(invoice_number)
    if not invoice_number:
        fastenal_hits = [
            tok.upper()
            for tok in _INV_FASTENAL.findall(pdf_text)
            if tok.upper() not in _CUSTOMER_ACCOUNTS
        ]
        if fastenal_hits:
            invoice_number = fastenal_hits[0]
            invoice_from_pdf = True
    filename_only = False
    subject_only = False
    if not invoice_number:
        filename_inv = _invoice_from_filename(filename)
        if filename_inv and filename_inv.upper() not in _CUSTOMER_ACCOUNTS:
            invoice_number = filename_inv
            filename_only = True
    if not invoice_number:
        subject_inv = _invoice_from_subject(subject)
        if subject_inv and subject_inv.upper() not in _CUSTOMER_ACCOUNTS and "account #" not in (subject or "").lower():
            invoice_number = subject_inv
            subject_only = True
    luxor = re.search(r"Invoice\s*#\s*\n\s*\d{1,2}/\d{1,2}/\d{2,4}\s+(\d{4,})", pdf_text, flags=re.I)
    if luxor and (not invoice_number or filename_only or subject_only):
        invoice_number = _usable_invoice_number(luxor.group(1))
        invoice_from_pdf = bool(invoice_number)
        filename_only = False
        subject_only = False
    if not invoice_number:
        plain = re.search(r"\bInvoice\s+(\d{5,8})\b", pdf_text, flags=re.I)
        if plain:
            invoice_number = _usable_invoice_number(plain.group(1))
            invoice_from_pdf = bool(invoice_number)
    # O'Neal invoice numbers look like 15452509 and appear twice (filename is often the date).
    oneal = re.findall(r"\b(15\d{6})\b", pdf_text)
    if oneal and (not invoice_number or _looks_like_date_token(invoice_number) or re.fullmatch(r"8?\d{6,7}", invoice_number or "") or filename_only):
        if "oneal" in blob.lower() or "o'neal" in blob.lower() or "o_neal" in (filename or "").lower():
            invoice_number = oneal[0]
            invoice_from_pdf = True
            filename_only = False
            subject_only = False
    if not invoice_number and "eastern metal" in (vendor or "").lower():
        ems = re.findall(r"\b(8\d{5})\b", pdf_text)
        if ems:
            invoice_number = _usable_invoice_number(ems[0])
            invoice_from_pdf = bool(invoice_number)
    printed = printed_invoice_number(invoice_number, vendor=vendor, text=pdf_text)
    if printed and printed != invoice_number and printed in pdf_text:
        invoice_from_pdf = True
        filename_only = False
        subject_only = False
    invoice_number = printed
    if invoice_from_pdf:
        sources["invoice_number"] = "pdf-prefix" if known_invoice_prefix(vendor) and invoice_number and "-" in invoice_number else "pdf"
    elif filename_only:
        sources["invoice_number"] = "filename"
    elif subject_only:
        sources["invoice_number"] = "subject"
    elif invoice_number:
        sources["invoice_number"] = "pdf"

    pos = extract_po_numbers(pdf_text)
    if pos:
        sources["po"] = "pdf"
    amount = None
    # Amount must come from vendor PDF text, never subject/filename (Gas 0040323616).
    due_label = _AMOUNT_DUE_LABEL.search(pdf_text)
    if due_label:
        amount = parse_money(due_label.group(1))
        if amount == 0:
            amount = None
    if amount is None and ("unifirst" in vendor_l or "unifirst" in pdf_text.lower()):
        usd_hits = [parse_money(m) for m in _AMOUNT_USD_PREFIX.findall(pdf_text)]
        usd_hits = [a for a in usd_hits if a not in (None, 0, 0.0) and a < 20000]
        if usd_hits:
            amount = usd_hits[0]
    bal = _AMOUNT_BALANCE.search(pdf_text)
    if amount is None and bal:
        amount = parse_money(bal.group(1))
        if amount == 0:
            amount = None
    stacked_total = re.search(r"Invoice Total:\s*\n(.{0,240})", pdf_text, flags=re.I | re.S)
    if amount is None and stacked_total:
        nums = [parse_money(m) for m in re.findall(r"([\d,]+(?:\.\d{2}))", stacked_total.group(1))]
        nums = [a for a in nums if a not in (None, 0, 0.0) and a < 100000]
        if nums:
            amount = max(nums)
    amt_match = None
    if amount is None:
        amt_match = _AMOUNT_BEFORE.search(pdf_text) or _AMOUNT_LABEL.search(pdf_text)
    if amt_match:
        amount = parse_money(amt_match.group(1))
        if amount == 0:
            amount = None
    if amount is None:
        totals = [parse_money(m) for m in _TOTAL_MONEY.findall(pdf_text)]
        totals = [a for a in totals if a not in (None, 0, 0.0)]
        if totals:
            amount = max(totals)
    if amount is None:
        usd_vals = []
        for left, right in re.findall(r"USD\s*([\d,]+(?:\.\d{2}))|([\d,]+(?:\.\d{2}))\s+USD", pdf_text, flags=re.I):
            usd_vals.append(parse_money(left or right))
        usd_vals = [a for a in usd_vals if a not in (None, 0, 0.0)]
        if usd_vals:
            best = max(usd_vals)
            if amount is None or best > amount:
                amount = best
    if amount is None:
        sub = re.search(r"\b(?:SUB-?TOTAL|AMOUNT DUE)\s*:?\s*([\d,]+(?:\.\d{2}))", pdf_text, flags=re.I)
        if sub:
            amount = parse_money(sub.group(1))
            if amount == 0:
                amount = None
    if amount is None:
        due = _AMOUNT_USD_DUE.search(pdf_text)
        if due:
            amount = parse_money(due.group(1))
    if amount is None:
        stacked_total_amt = _TOTAL_STACKED.search(pdf_text)
        if stacked_total_amt:
            amount = parse_money(stacked_total_amt.group(1))
            if amount == 0:
                amount = None
    if amount is None:
        ext_block = re.search(r"Ext(?:ended)?\s*Price(.{0,400})", pdf_text, flags=re.I | re.S)
        if ext_block:
            ext_nums = [parse_money(m) for m in re.findall(r"([\d,]+(?:\.\d{2}))", ext_block.group(1))]
            ext_nums = [a for a in ext_nums if a not in (None, 0, 0.0) and a < 100000]
            if ext_nums:
                amount = max(ext_nums)
    if amount is None:
        # Capital Machine prints a lone $1,067.50 on the last line.
        trailing = re.findall(r"\$([\d,]+(?:\.\d{2}))", pdf_text)
        trailing_amt = [parse_money(m) for m in trailing]
        trailing_amt = [a for a in trailing_amt if a not in (None, 0, 0.0) and a < 100000]
        if trailing_amt:
            amount = trailing_amt[-1]
    due_all = re.search(r"Total amount due:\s*\$?\s*([\d,]+(?:\.\d{2}))", pdf_text, flags=re.I)
    if due_all:
        amount = parse_money(due_all.group(1)) or amount
    if "oneal" in vendor_l or "o'neal" in vendor_l or "o_neal" in blob_l:
        oneal_totals = _oneal_totals(pdf_text)
        if oneal_totals:
            amount = oneal_totals[0]
    if amount is None:
        # UniFirst First Aid: Invoice Total: then Net / Tax / Total / Balance.
        block = re.search(r"Invoice Total:(.{0,240})", pdf_text, flags=re.I | re.S)
        if block:
            nums = [parse_money(m) for m in re.findall(r"([\d,]+(?:\.\d{2}))", block.group(1))]
            nums = [a for a in nums if a not in (None, 0, 0.0) and a < 100000]
            if nums:
                amount = max(nums)
    if amount not in (None, ""):
        sources["amount"] = "pdf"

    # Printed invoice date only. Never subject "Dated:" or the email received day.
    invoice_date = None
    date_match = _DATE_LABEL.search(pdf_text)
    if date_match:
        invoice_date = parse_date_value(date_match.group(1))
    if not invoice_date:
        loose = re.search(
            r"(?:^|\n)\s*date\s*[:.\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}-[A-Za-z]{3}-\d{2,4})",
            pdf_text,
            flags=re.I,
        )
        if loose:
            invoice_date = parse_date_value(loose.group(1))
    if not invoice_date:
        for raw in _DATE_ANY.findall(pdf_text):
            parsed = parse_date_value(raw)
            if not parsed or parsed < "2025-01-01":
                continue
            # MSC / Austin print Due Date before Invoice Date in the extracted text.
            around = pdf_text
            idx = around.lower().find(raw.lower())
            window = around[max(0, idx - 24) : idx] if idx >= 0 else ""
            if re.search(r"\bdue\s*date\b", window, flags=re.I):
                continue
            invoice_date = parsed
            break
    if invoice_date:
        sources["date"] = "pdf"

    check_stop = bool(_CHECK_STOP.search(blob))
    fees = extract_fees(pdf_text)
    lines = extract_invoice_lines(pdf_text)
    po = pos[0] if len(pos) == 1 else None
    po_doc = is_purchase_order_document(text=pdf_text, filename=filename)
    return {
        "vendor": vendor,
        "invoice_number": invoice_number or "",
        "date": invoice_date,
        "po": po,
        "pos": pos,
        "amount": amount,
        "fees": fees,
        "lines": lines,
        "check_stop": check_stop,
        "hold_reason": "CHECK STOP" if check_stop else ("parse-error" if po_doc else ""),
        "multi_po": len(pos) > 1,
        "text_chars": len(pdf_text),
        "field_sources": sources,
        "is_purchase_order_doc": po_doc,
        "parse_verified": bool(
            sources.get("invoice_number") in {"pdf", "pdf-prefix"}
            and sources.get("amount") == "pdf"
            and sources.get("date") == "pdf"
            and not po_doc
        ),
    }


def parse_invoice_pdf(
    path: Path,
    *,
    subject: str = "",
    from_name: str = "",
    from_address: str = "",
) -> dict[str, Any]:
    text = extract_pdf_text(path)
    parsed = parse_invoice_text(
        text,
        subject=subject,
        from_name=from_name,
        from_address=from_address,
        filename=path.name,
    )
    bills = expand_oneal_invoices(text, parsed)
    parsed = bills[0]
    if len(bills) > 1:
        parsed["siblings"] = bills[1:]
    parsed["pdf_path"] = str(path)
    parsed["pdf_text_empty"] = not (text or "").strip()
    return parsed
