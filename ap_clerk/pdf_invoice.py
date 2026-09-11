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
    r"(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?\s*#)\s*[:.\s#]*([A-Z]{0,8}\d-?\d{3,}(?:\.\d{3})?[A-Z0-9/_-]*)",
    flags=re.I,
)
_INV_TECHNI = re.compile(r"\b(S\d{6,}\.\d{3})\b")
_INV_INSIGHT = re.compile(
    r"(?:invoice\s*(?:number|no\.?|#)|inv(?:oice)?\s*#?)\s*[:.\s]*\n?\s*(\d{4,5})\b",
    flags=re.I,
)
_INV_STACKED_SHORT = re.compile(
    r"(?:^|\n)\s*(?:INVOICE(?:\s*(?:NUMBER|NO\.?|#))?)\s*\n\s*(\d{4,8})\b",
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
_INV_LS = re.compile(r"\b(LS-\d{4,})\b", flags=re.I)
_INV_BILL_HASH = re.compile(r"\bBill\s*#\s*(\d{5,})\b", flags=re.I)
_INV_STACKED = re.compile(r"(?:^|\n)\s*INVOICE\s*\n\s*(\d{6,8})\b", flags=re.I)
_INV_UNIFIRST = re.compile(
    r"Invoice\s*#:\s*(?:\n[^\n]{0,80}){0,16}\n\s*(28\d{8}|\d{10})\b",
    flags=re.I,
)
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
    "orthmanconveying.com": "Orthman Conveying Systems",
    "spectrumvoip.com": "SpectrumVoIP",
    "xcaliberind.com": "Xcaliber Industrial LLC",
    "aqpowder.com": "American Quality Powder Coating",
    "americanqualitypowder.com": "American Quality Powder Coating",
    "insightcontrollerservices.com": "Insight Controller Services",
    "techni-tool.com": "Techni-Tool",
    "technitool.com": "Techni-Tool",
    "toyota.com": "Toyota Commercial Finance",
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
    (re.compile(r"orthman", re.I), "Orthman Conveying Systems"),
    (re.compile(r"spectrumvoip|spectrum\s*voip", re.I), "SpectrumVoIP"),
    (re.compile(r"xcaliber", re.I), "Xcaliber Industrial LLC"),
    (re.compile(r"american\s+quality\s+powder|aqpc", re.I), "American Quality Powder Coating"),
    (re.compile(r"insight\s+controller", re.I), "Insight Controller Services"),
    (re.compile(r"techni[\s-]?tool", re.I), "Techni-Tool"),
    (re.compile(r"toyota\s+commercial\s+finance|toyota\s+financial", re.I), "Toyota Commercial Finance"),
    (re.compile(r"melody\s+channell", re.I), "Melody Channell"),
    (re.compile(r"crosslink", re.I), "Crosslink Powder Coating"),
    (re.compile(r"ryerson", re.I), "Joseph T. Ryerson & Son, Inc"),
    (re.compile(r"mcqueary", re.I), "McQueary Industries"),
    (re.compile(r"hudson energy", re.I), "Hudson Energy"),
    (re.compile(r"nova\s+alloys", re.I), "Nova Alloys"),
)


def extract_pdf_text(path: Path) -> str:
    """Read PDF text. If pypdf gets nothing, OCR/retry. Never invent no-pdf-on-vm."""
    text = _extract_pypdf_text(path)
    if (text or "").strip():
        return text
    ocr = _ocr_pdf_text(path)
    if (ocr or "").strip():
        LOGGER.info("OCR/retry extracted %s chars from %s", len(ocr), path.name)
        return ocr
    return text or ""


def _extract_pypdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception:  # noqa: BLE001 - unreadable PDF still exists on disk
        return ""
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not kill the invoice
            pages.append("")
    return "\n".join(pages)


def _ocr_pdf_text(path: Path) -> str:
    """Best-effort OCR/retry when pypdf extracted no text. Tools optional. No network."""
    import subprocess

    commands = (
        ["pdftotext", "-layout", str(path), "-"],
        ["pdftotext", str(path), "-"],
    )
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (result.stdout or "").strip()
        if text:
            return text
    return ""


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
    # UniFirst "SZ Prem Charge 58002" is a garment code, not a KIMCO PO.
    blob = text or ""
    if "unifirst" not in blob.lower():
        found.extend(re.findall(r"\b(5[7-9]\d{3})\b", blob))
    else:
        for hit in re.finditer(r"\b(5[7-9]\d{3})\b", blob):
            window = blob[max(0, hit.start() - 24) : hit.start()]
            if re.search(r"prem(?:ium)?\s*charge|sz\s*prem", window, flags=re.I):
                continue
            found.append(hit.group(1))
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
        desc_only = bool(not parts and re.search(r"\b(?:SCH(?:EDULE)?\s*\d+|A500|PIPE)\b", stripped, flags=re.I))
        if not parts and not desc_only:
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
                    "description": stripped[:120],
                }
            )
        # O'Neal / mill descriptions without XXX-XXXX-XXX part numbers.
        if desc_only:
            key = re.sub(r"\s+", " ", stripped.upper())[:48]
            if key not in seen:
                seen.add(key)
                if qty is None:
                    bare_qty = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:EA|PC|PCS|FT|LF)?\b", stripped, flags=re.I)
                    if bare_qty:
                        qty = parse_money(bare_qty.group(1))
                lines.append(
                    {
                        "part": "",
                        "qty": qty,
                        "amount": amounts[-1] if amounts else None,
                        "po_line": po_line,
                        "wo": wo_match.group(1) if wo_match else None,
                        "label": stripped[:80],
                        "description": stripped[:120],
                    }
                )
    return lines[:40]


_COMPANY_LEGAL_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9&.'\s]{1,50}?),\s*(INC\.?|LLC|L\.L\.C\.|LTD\.?|CORP\.?|CO\.?)\b",
    flags=re.I,
)
_COMPANY_BEFORE_INVOICE_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9&.'\s,]{2,70}?)\s*[-–—|:]\s*(?:Invoice|Inv\.?|Inv\b)",
    flags=re.I,
)
_COMPANY_WORD_RE = re.compile(
    r"\b(inc|llc|ltd|co|company|corp|supply|steel|products|staffing|alloys|industries|services|metals)\b",
    flags=re.I,
)


def _looks_like_person_name(name: str) -> bool:
    """True for a From display name like Erica Barrett — not a company."""
    cleaned = re.sub(r"\s+", " ", name or "").strip()
    if not cleaned or "@" in cleaned:
        return False
    if _COMPANY_WORD_RE.search(cleaned) or _COMPANY_LEGAL_RE.search(cleaned):
        return False
    parts = [p for p in re.split(r"\s+", cleaned) if p]
    if not (2 <= len(parts) <= 3):
        return False
    return all(part[0].isalpha() and part[0].isupper() for part in parts)


def _title_company(name: str) -> str:
    small = {"inc", "inc.", "llc", "ltd", "ltd.", "corp", "co", "co.", "l.l.c."}
    out: list[str] = []
    for tok in re.split(r"(\s+|,)", name.strip()):
        if not tok or tok.isspace() or tok == ",":
            out.append(tok)
            continue
        key = tok.lower()
        if key in small:
            out.append(tok[0].upper() + tok[1:].lower())
        elif tok.isupper():
            out.append(tok.title())
        else:
            out.append(tok)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def company_from_subject_or_text(*, subject: str = "", text: str = "") -> str:
    """Company printed in the subject or PDF — never a From person's name."""
    for blob in (subject or "", text or ""):
        legal = _COMPANY_LEGAL_RE.search(blob)
        if legal:
            return _title_company(f"{legal.group(1).strip()}, {legal.group(2).strip()}")
    headed = _COMPANY_BEFORE_INVOICE_RE.search(subject or "")
    if headed:
        raw = headed.group(1).strip(" -–—|:,")
        if raw and not _looks_like_person_name(raw):
            return _title_company(raw)
    return ""


def _vendor_from_pdf_text(text: str) -> str:
    """Company printed on the vendor PDF. Subject/From are hints only."""
    if not (text or "").strip():
        return ""
    for pattern, vendor in SUBJECT_VENDORS:
        if pattern.search(text):
            return vendor
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(
            r"^(air products|fastenal|gas and supply|earle m|o'?neal|luxor|coherent|modern heat|national specialty|mcmaster|telecom products|rmp industrial|priority\s*1|msc industrial|metal supermarket|marmon|amada|exotic metals|jp steel|curbell|capital machine|clear kut|willbanks|waste connections|engie|unifirst|shoppa|eastern metal|green valley|purvis|ntex|kloeckner|american bearing|american quality powder|morgan steel|grm|alternative parts|tube supply|lavanture|crosslink|ryerson|mcqueary|hudson energy|leeco|austin hardware|a1 image|maynard nexsen|legacy wire|gexpro|beshert|precision fabrication|versalift|automated finishing|polymer products|hapeco|aft industries|pct support|orthman|spectrumvoip|xcaliber|insight controller|techni|toyota commercial|melody channell|nova alloys)",
            stripped,
            re.I,
        ):
            return stripped[:80]
    return company_from_subject_or_text(subject="", text=text)


def vendor_from_context(*, subject: str = "", from_name: str = "", from_address: str = "", text: str = "") -> str:
    # PDF is the version of the truth for vendor when the invoice text names a company.
    pdf_vendor = _vendor_from_pdf_text(text)
    if pdf_vendor:
        return pdf_vendor
    addr = (from_address or "").lower()
    if "firstaid" in addr or "first aid" in (from_name or "").lower() or "firstaid" in (subject or "").lower():
        return "UniFirst First Aid & Safety"
    if "@" in addr:
        domain = addr.split("@", 1)[1]
        if domain in DOMAIN_VENDORS:
            return DOMAIN_VENDORS[domain]
    blob = f"{subject}\n{from_name}\n{from_address}"
    for pattern, vendor in SUBJECT_VENDORS:
        if pattern.search(blob):
            return vendor
    company = company_from_subject_or_text(subject=subject, text="")
    if company:
        return company[:80]
    if from_name and "@" not in from_name and not _looks_like_person_name(from_name):
        cleaned = re.sub(r"\s+", " ", from_name).strip()
        if _COMPANY_WORD_RE.search(cleaned):
            return cleaned[:80]
    # Kyle 8/18 Nova 258145: never prefer Erica Barrett over a company in subject/PDF.
    if _looks_like_person_name(from_name):
        return (company or subject.split("-")[0] or subject or "").strip()[:80]
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
        r"Invoice0+(\d{5,})",
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


_FASTENAL_BLOCK = re.compile(
    r"Cust\.?\s*P\.?O\.?.{0,80}?TXFT\d+\s+(\d{5,6}).{0,400}?Invoice No\.\s+(TXFT\d{5,}).{0,120}?Invoice Total\s+([\d,]+\.\d{2})",
    flags=re.I | re.S,
)


def expand_fastenal_invoices(text: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """One Fastenal email PDF can hold more than one TXFT invoice (each with its own PO)."""
    vendor = str(parsed.get("vendor") or "")
    if "fastenal" not in vendor.lower():
        return [parsed]
    blocks = list(_FASTENAL_BLOCK.finditer(text or ""))
    if len(blocks) <= 1:
        return [parsed]
    bills: list[dict[str, Any]] = []
    for match in blocks:
        po, number, total = match.group(1), match.group(2).upper(), parse_money(match.group(3))
        bill = dict(parsed)
        bill["invoice_number"] = number
        bill["po"] = po
        bill["pos"] = [po]
        bill["multi_po"] = False
        if total not in (None, 0, 0.0):
            bill["amount"] = total
        sources = dict(bill.get("field_sources") or {})
        sources["invoice_number"] = "pdf"
        sources["po"] = "pdf"
        if total not in (None, 0, 0.0):
            sources["amount"] = "pdf"
        bill["field_sources"] = sources
        bill.pop("siblings", None)
        bills.append(bill)
    return bills or [parsed]


def expand_gas_misc_invoices(text: str, parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Gas & Supply PDFs can hold several Misc 00xxxxxxxx invoices (0040367887 notes: 5).

    When amounts cannot be split per number, mark gas_misc_ambiguous so the
    enter path HOLDs instead of inventing Type 4 amounts.
    """
    vendor = str(parsed.get("vendor") or "")
    if "gas and supply" not in vendor.lower() and "gasandsupply" not in vendor.lower():
        return [parsed]
    numbers = []
    for hit in _INV_GAS.findall(text or ""):
        token = _usable_invoice_number(hit)
        if token and token not in numbers:
            numbers.append(token)
    if len(numbers) <= 1:
        return [parsed]
    bills: list[dict[str, Any]] = []
    for number in numbers:
        bill = dict(parsed)
        bill["invoice_number"] = number
        bill["po"] = None
        bill["pos"] = []
        bill["multi_po"] = False
        bill["gas_misc"] = True
        bill["misc_item"] = "Shop Supplies - G&S"
        sources = dict(bill.get("field_sources") or {})
        sources["invoice_number"] = "pdf"
        bill["field_sources"] = sources
        bill.pop("siblings", None)
        bills.append(bill)
    # One shared total cannot be trusted as each Misc invoice amount.
    if parsed.get("amount") not in (None, "") and len(bills) > 1:
        for bill in bills:
            bill["gas_misc_ambiguous"] = True
    return bills or [parsed]


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
    for rx in (
        _INV_TECHNI,
        _INV_EMJ,
        _INV_PS_INV,
        _INV_LS,
        _INV_TMC,
        _INV_JVT,
        _INV_UNIFIRST,
        _INV_COLON_NUM,
        _INV_SV,
        _INV_DASH_IN,
        _INV_MSC_REAL,
        _INV_GRM,
        _INV_LABEL,
        _INV_BILL_HASH,
        _INV_GAS,
    ):
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
        stacked_nums = [_usable_invoice_number(n) for n in _INV_STACKED.findall(pdf_text)]
        stacked_nums = [n for n in stacked_nums if n]
        if stacked_nums:
            # RMP prints a form id then the real invoice under a second INVOICE heading.
            invoice_number = stacked_nums[-1]
            invoice_from_pdf = True
    if not invoice_number:
        short_stacked = [_usable_invoice_number(n) for n in _INV_STACKED_SHORT.findall(pdf_text)]
        short_stacked = [n for n in short_stacked if n]
        if short_stacked:
            invoice_number = short_stacked[-1]
            invoice_from_pdf = True
    if not invoice_number and (
        "insight" in vendor_l
        or "insight" in pdf_text.lower()
        or "melody" in vendor_l
        or "melody channell" in pdf_text.lower()
        or re.search(r"\binvoice\s*(?:number|no\.?|#)\s*[:.\s]*\d{4}\b", pdf_text, flags=re.I)
    ):
        insight = _INV_INSIGHT.search(pdf_text)
        if insight:
            invoice_number = _usable_invoice_number(insight.group(1))
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
    # Filename/subject # that also appears in PDF text is PDF-confirmed
    # (Crosslink invoice-27943.pdf; Nova subject 258145).
    if (
        invoice_number
        and pdf_text
        and re.search(rf"\b{re.escape(str(invoice_number))}\b", pdf_text, flags=re.I)
    ):
        invoice_from_pdf = True
        filename_only = False
        subject_only = False
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

    # CHECK STOP on the subject/filename is not enough when the PDF has invoice pages
    # (Gas & Supply 0040367887: 5 Misc invoices). Real notices have no invoice to enter.
    po_doc = is_purchase_order_document(text=pdf_text, filename=filename)
    check_stop_in_pdf = bool(_CHECK_STOP.search(pdf_text))
    check_stop_in_subject = bool(_CHECK_STOP.search(f"{subject}\n{filename}"))
    has_invoice_pages = bool(
        invoice_from_pdf and invoice_number and amount not in (None, "") and not po_doc
    )
    if has_invoice_pages:
        check_stop = False
    else:
        check_stop = check_stop_in_pdf or (check_stop_in_subject and not invoice_from_pdf)
    fees = extract_fees(pdf_text)
    lines = extract_invoice_lines(pdf_text)
    po = pos[0] if len(pos) == 1 else None
    pdf_text_empty = not (pdf_text or "").strip()
    if invoice_number and pdf_text and invoice_number in pdf_text:
        sources["invoice_number"] = (
            "pdf-prefix" if known_invoice_prefix(vendor) and "-" in invoice_number else "pdf"
        )
        filename_only = False
        subject_only = False
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
        "pdf_text_empty": pdf_text_empty,
        "pdf_unavailable": pdf_text_empty,
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
    if len(bills) <= 1:
        bills = expand_fastenal_invoices(text, parsed)
    if len(bills) <= 1:
        bills = expand_gas_misc_invoices(text, parsed)
    parsed = bills[0]
    if len(bills) > 1:
        parsed["siblings"] = bills[1:]
    parsed["pdf_path"] = str(path)
    parsed["pdf_on_disk"] = path.is_file()
    parsed["pdf_text_empty"] = not (text or "").strip()
    # File on disk is never "unavailable" — empty extract means OCR/retry, not no-pdf-on-vm.
    parsed["pdf_unavailable"] = not path.is_file()
    return parsed
