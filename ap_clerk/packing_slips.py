"""NOTE-49: signed packing slips from receiving@ for Success.

accountspayable@ is the only invoice mailbox. receiving@ is Sharp MFP
scans of signed packing slips — GET-only; never enter invoices from it.

A scan PDF may contain more than one slip, and a slip may be more than
one page. Do not treat 1 page = 1 slip or 1 PDF = 1 invoice. Keep
consecutive pages of the same slip together as one logical slip.

KIMCO list_attachments is filename-only (no doc-type). Classify header
files with invoice vs packing-slip name/body heuristics.
"""

from __future__ import annotations

import re
from typing import Any

from ap_clerk.pdf_invoice import (
    ATTACHMENT_INVOICE,
    ATTACHMENT_PACKING_SLIP,
    ATTACHMENT_POD,
    ATTACHMENT_RECEIPT_SCAN,
    classify_attachment,
)
from ap_clerk.receiving_probe import RECEIVING_MAILBOX
from ap_clerk.rules import (
    extract_po_number,
    invoice_number_key,
    names_match,
)

SHARP_MFP_FROM = "scans@sharp-mfp.com"
SHARP_MFP_SUBJECT_RE = re.compile(
    r"scanned\s+image\s+from\s+kannon\s+manufacturing",
    flags=re.I,
)
SHARP_MFP_FILE_RE = re.compile(
    r"kannon\s+manufacturing_\d{8}_\d{6}(?:\.pdf)?",
    flags=re.I,
)

PAGE_OF_RE = re.compile(
    r"(?:page|pg\.?)\s*(\d+)\s*(?:of|/)\s*(\d+)",
    flags=re.I,
)
SLIP_NO_RE = re.compile(
    r"(?:packing\s+slip|delivery\s+ticket|receiver|receipt)\s*"
    r"(?:no\.?|number|#)\s*[:.]?\s*([A-Z0-9][-A-Z0-9/.]+)",
    flags=re.I,
)
INVOICE_ON_SLIP_RE = re.compile(
    r"(?:invoice\s*(?:no\.?|number|#)\s*[:.]?\s*|ps-inv)"
    r"(PS-INV)?([A-Z0-9][-A-Z0-9/.]{3,})",
    flags=re.I,
)
DATE_RE = re.compile(
    r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b"
)
PACKING_HEADING_RE = re.compile(
    r"\b(packing\s+slip|delivery\s+receipt|proof\s+of\s+delivery|"
    r"receiving\s+report|signed\s+packing)\b",
    flags=re.I,
)
SIGNED_RE = re.compile(
    r"\b(customer\s+signature|received\s+by|signed|signature|x\s*_+)\b",
    flags=re.I,
)
INVOICE_FILE_HINT_RE = re.compile(
    r"sales\s*invoice|ps-inv|\binvoice[-_ .]|inv[_-]|txft\d",
    flags=re.I,
)

PACKING_SLIP_KINDS = frozenset(
    {
        ATTACHMENT_PACKING_SLIP,
        ATTACHMENT_POD,
        ATTACHMENT_RECEIPT_SCAN,
        "signed_packing_slip",
        "receiving_scan",
    }
)


def normalize_mailbox(mailbox: str | None) -> str:
    return (mailbox or "").strip().lower()


def receiving_is_not_invoice_mailbox(mailbox: str | None) -> bool:
    """True when this mailbox must not be used to enter vendor invoices."""
    return normalize_mailbox(mailbox) == RECEIVING_MAILBOX


def refuse_enter_from_receiving(mailbox: str | None = None) -> str:
    return (
        "receiving@ is packing slips / signed receives only. "
        "Do not enter invoices from receiving@. "
        "Invoice mailbox is accountspayable@kannonmfg.com."
    )


def is_sharp_mfp_scan(
    *,
    from_addr: str = "",
    subject: str = "",
    filename: str = "",
) -> bool:
    """Sharp MFP scans land in receiving@ as Kannon Manufacturing_YYYYMMDD_HHMMSS.pdf."""
    addr = (from_addr or "").strip().lower()
    if addr == SHARP_MFP_FROM or addr.endswith("@sharp-mfp.com"):
        return True
    if subject and SHARP_MFP_SUBJECT_RE.search(subject):
        return True
    if filename and SHARP_MFP_FILE_RE.search(filename):
        return True
    return False


def is_receiving_scan(
    *,
    mailbox: str = "",
    from_addr: str = "",
    subject: str = "",
    filename: str = "",
) -> bool:
    if receiving_is_not_invoice_mailbox(mailbox):
        return True
    return is_sharp_mfp_scan(from_addr=from_addr, subject=subject, filename=filename)


def header_attachment_kind(*, filename: str = "", text: str = "") -> str:
    """Filename/body heuristic. KIMCO attachments have no doc-type field."""
    name = filename or ""
    if is_sharp_mfp_scan(filename=name):
        return ATTACHMENT_PACKING_SLIP
    kind = classify_attachment(filename=name, text=text)
    if kind in PACKING_SLIP_KINDS:
        return ATTACHMENT_PACKING_SLIP
    if kind == ATTACHMENT_INVOICE or INVOICE_FILE_HINT_RE.search(name):
        return ATTACHMENT_INVOICE
    return kind


def _attachment_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item or "")
    for key in ("filename", "name", "FileName", "file_name", "text", "Title"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _attachment_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("body") or "")
    return ""


def header_has_vendor_invoice_pdf(attachments: list[Any] | None) -> bool:
    for item in attachments or []:
        if header_attachment_kind(filename=_attachment_name(item), text=_attachment_text(item)) == ATTACHMENT_INVOICE:
            return True
    return False


def header_has_signed_packing_slip(attachments: list[Any] | None) -> bool:
    for item in attachments or []:
        if header_attachment_kind(filename=_attachment_name(item), text=_attachment_text(item)) in PACKING_SLIP_KINDS:
            return True
    return False


def packing_slip_attached_for_invoice(
    inv: dict[str, Any] | None,
    header_attachments: list[Any] | None = None,
) -> bool:
    """True when the header already has a matched signed packing slip."""
    data = inv or {}
    flagged = data.get("packing_slip_attached")
    if flagged is True:
        return True
    if flagged is False:
        return False
    if data.get("matched_packing_slip"):
        return True
    names = header_attachments
    if names is None:
        names = data.get("header_attachments") or data.get("attachments") or []
    return header_has_signed_packing_slip(names)


def _norm_key(value: str | None) -> str:
    return invoice_number_key(value).replace(" ", "")


def _norm_po(value: str | None) -> str:
    if not value:
        return ""
    extracted = extract_po_number(str(value)) or str(value).strip()
    return re.sub(r"\D", "", extracted)


def _norm_date(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        if digits.startswith("20"):
            return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        return f"{digits[4:8]}-{digits[0:2]}-{digits[2:4]}"
    return text[:10]


def extract_page_keys(text: str, *, page_index: int) -> dict[str, Any]:
    blob = text or ""
    page_of = None
    match = PAGE_OF_RE.search(blob)
    if match:
        page_of = (int(match.group(1)), int(match.group(2)))
    slip_match = SLIP_NO_RE.search(blob)
    inv_match = INVOICE_ON_SLIP_RE.search(blob)
    invoice_number = None
    if inv_match:
        prefix = (inv_match.group(1) or "").upper()
        rest = (inv_match.group(2) or "").strip(".-")
        invoice_number = f"{prefix}{rest}" if prefix else rest
    date_match = DATE_RE.search(blob)
    return {
        "page_index": page_index,
        "text": blob,
        "po": extract_po_number(blob),
        "invoice_number": invoice_number,
        "slip_number": slip_match.group(1).strip(".-") if slip_match else None,
        "vendor": None,
        "date": _norm_date(date_match.group(1)) if date_match else None,
        "page_of": page_of,
        "has_heading": bool(PACKING_HEADING_RE.search(blob)),
        "signed": bool(SIGNED_RE.search(blob)),
        "is_packing_slip": bool(PACKING_HEADING_RE.search(blob) or SIGNED_RE.search(blob)),
    }


def _shares_identity(current: dict[str, Any], page: dict[str, Any]) -> bool:
    for key, norm in (
        ("slip_number", _norm_key),
        ("invoice_number", _norm_key),
        ("po", _norm_po),
    ):
        left = norm(current.get(key))
        right = norm(page.get(key))
        if left and right and left == right:
            return True
    return False


def _conflicts_identity(current: dict[str, Any], page: dict[str, Any]) -> bool:
    for key, norm in (
        ("slip_number", _norm_key),
        ("invoice_number", _norm_key),
        ("po", _norm_po),
    ):
        left = norm(current.get(key))
        right = norm(page.get(key))
        if left and right and left != right:
            return True
    return False


def _fill_identity(current: dict[str, Any], page: dict[str, Any]) -> None:
    for key in ("po", "invoice_number", "slip_number", "vendor", "date"):
        if not current.get(key) and page.get(key):
            current[key] = page[key]
    current["signed"] = bool(current.get("signed") or page.get("signed"))
    if page.get("page_of"):
        current["page_of"] = page["page_of"]
        current["last_page_x"] = page["page_of"][0]


def _start_slip(page: dict[str, Any], *, source_filename: str = "") -> dict[str, Any]:
    page_of = page.get("page_of")
    return {
        "pages": [int(page["page_index"])],
        "po": page.get("po"),
        "invoice_number": page.get("invoice_number"),
        "slip_number": page.get("slip_number"),
        "vendor": page.get("vendor"),
        "date": page.get("date"),
        "signed": bool(page.get("signed")),
        "page_of": page_of,
        "last_page_x": page_of[0] if page_of else None,
        "source_filename": source_filename,
        "source_mailbox": RECEIVING_MAILBOX,
    }


def continues_same_slip(current: dict[str, Any], page: dict[str, Any]) -> bool:
    """True when this page is more of the current slip — never 1 page = 1 slip."""
    if _conflicts_identity(current, page):
        return False
    cur_of = current.get("page_of")
    page_of = page.get("page_of")
    last_x = current.get("last_page_x")
    if cur_of and page_of and last_x is not None:
        cur_total = cur_of[1]
        page_x, page_total = page_of
        if page_total == cur_total and page_x == int(last_x) + 1:
            return True
        if page_x == 1 and (int(last_x) >= cur_total or page_total != cur_total):
            return False
    if (
        page.get("has_heading")
        and page_of
        and page_of[0] == 1
        and not _shares_identity(current, page)
        and current.get("pages")
    ):
        if last_x is not None and cur_of and int(last_x) >= cur_of[1]:
            return False
        if page.get("slip_number") or page.get("po") or page.get("invoice_number"):
            return False
    return True


def group_consecutive_slip_pages(
    pages: list[dict[str, Any]],
    *,
    source_filename: str = "",
) -> list[dict[str, Any]]:
    """Group consecutive pages of the same slip. Do not treat 1 page = 1 slip."""
    slips: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in pages:
        page = dict(raw)
        page.setdefault("page_index", len(current["pages"]) + 1 if current else 1)
        if current is None:
            current = _start_slip(page, source_filename=source_filename)
            continue
        if continues_same_slip(current, page):
            current["pages"].append(int(page["page_index"]))
            _fill_identity(current, page)
        else:
            slips.append(current)
            current = _start_slip(page, source_filename=source_filename)
    if current:
        slips.append(current)
    return slips


def pages_from_texts(
    texts: list[str],
    *,
    source_filename: str = "",
) -> list[dict[str, Any]]:
    return [extract_page_keys(text, page_index=idx) for idx, text in enumerate(texts, start=1)]


def logical_slips_from_scan(
    texts: list[str],
    *,
    source_filename: str = "",
    from_addr: str = "",
    subject: str = "",
) -> list[dict[str, Any]]:
    """Split a receiving@ scan into logical slips (multi-page slips stay together)."""
    pages = pages_from_texts(texts, source_filename=source_filename)
    if is_sharp_mfp_scan(from_addr=from_addr, subject=subject, filename=source_filename):
        for page in pages:
            page["is_packing_slip"] = True
    return group_consecutive_slip_pages(pages, source_filename=source_filename)


def slip_match_score(
    slip: dict[str, Any],
    *,
    po: str | None = None,
    invoice_number: str | None = None,
    vendor: str | None = None,
    invoice_date: str | None = None,
) -> int:
    score = 0
    if po and slip.get("po") and _norm_po(po) == _norm_po(str(slip.get("po"))):
        score += 100
    if (
        invoice_number
        and slip.get("invoice_number")
        and _norm_key(invoice_number) == _norm_key(str(slip.get("invoice_number")))
    ):
        score += 80
    if vendor and slip.get("vendor") and names_match(vendor, str(slip.get("vendor"))):
        score += 20
    if invoice_date and slip.get("date") and _norm_date(invoice_date) == _norm_date(str(slip.get("date"))):
        score += 10
    return score


def match_logical_slips_to_invoice(
    slips: list[dict[str, Any]],
    *,
    po: str | None = None,
    invoice_number: str | None = None,
    vendor: str | None = None,
    invoice_date: str | None = None,
) -> dict[str, Any]:
    """Match one AP header to logical slip(s). No unique match → HOLD.

    A multi-slip PDF is not 1 invoice. Attach the matching slip's consecutive
    pages (or the whole PDF when every slip in the scan covers this bill).
    """
    scored: list[tuple[int, dict[str, Any]]] = []
    for slip in slips:
        score = slip_match_score(
            slip,
            po=po,
            invoice_number=invoice_number,
            vendor=vendor,
            invoice_date=invoice_date,
        )
        if score:
            scored.append((score, slip))
    strong = [(score, slip) for score, slip in scored if score >= 80]
    if not strong:
        unique_weak = [(score, slip) for score, slip in scored if score >= 30]
        if len(unique_weak) == 1:
            strong = unique_weak
        elif unique_weak:
            return {
                "status": "ambiguous",
                "hold": True,
                "matched": [slip for _score, slip in unique_weak],
                "attach_whole_pdf": False,
                "pages": [],
                "why": (
                    "receiving@ scan has more than one packing slip that weakly "
                    "matches this bill (vendor/date). Do not guess. HOLD."
                ),
            }
        else:
            return {
                "status": "no_match",
                "hold": True,
                "matched": [],
                "attach_whole_pdf": False,
                "pages": [],
                "why": (
                    "No receiving@ signed packing slip matched this bill by PO / "
                    "invoice # / vendor / dates. Success needs ≥1 signed packing "
                    "slip plus the vendor invoice PDF. HOLD."
                ),
            }
    matched = [slip for _score, slip in strong]
    pages: list[int] = []
    for slip in matched:
        pages.extend(int(p) for p in (slip.get("pages") or []))
    whole = len(matched) == len(slips) and bool(slips)
    return {
        "status": "matched",
        "hold": False,
        "matched": matched,
        "attach_whole_pdf": whole,
        "pages": pages,
        "why": "",
    }


def slip_attachment_label(slip: dict[str, Any]) -> str:
    pages = slip.get("pages") or []
    if len(pages) == 1:
        page_bit = f"page {pages[0]}"
    elif pages:
        page_bit = f"pages {pages[0]}-{pages[-1]}"
    else:
        page_bit = "pages unknown"
    ident = slip.get("slip_number") or slip.get("po") or slip.get("invoice_number") or "slip"
    return f"packing-slip-{ident}-{page_bit}.pdf"
