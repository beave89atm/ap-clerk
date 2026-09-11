"""QUALITY V1.2 hard gates. Success means a finished bill, not a header create.

Treyce 2026-09-10 notes on the 8/16 dry-10 sheet. No network I/O. Never logs secrets.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ap_clerk.rules import (
    CREATE_HEADER_ON_HOLD,
    extract_po_number,
    invoice_type_for,
    is_auto_pay,
    is_fee_or_surcharge,
    known_vendor_id,
    misc_purchase_item_for,
    money,
    names_match,
    normalize_part,
    qty_discrepancy,
    format_unmatched_lines,
    format_unmatched_pos,
    vendor_match_score,
    vendors_strictly_match,
)

RESULT_SUCCESS = "Success"
RESULT_INCOMPLETE = "Incomplete"
RESULT_HOLD = "HOLD"
RESULT_FAIL = "Fail"
# Bill-attempt outcomes. The run cap is mailbox messages touched (Kyle 2026-09-11),
# not N bill attempts. Noise is RESULT_SKIPPED and still consumes the email cap.
RESULT_VALUES = (RESULT_SUCCESS, RESULT_INCOMPLETE, RESULT_HOLD, RESULT_FAIL)
RESULT_SKIPPED = "Skipped"
RESULT_NOISE_ALIASES = frozenset({RESULT_SKIPPED, "Noise"})

GATE_PREFLIGHT = "preflight-parse"
GATE_PO = "po"
GATE_RECEIPT = "receipt"
GATE_FINISH = "finish"
GATE_BILL_VS_NOISE = "bill-vs-noise"
GATE_PRICE = "price-does-not-match"
GATE_QTY = "qty-does-not-match"
GATE_AUTO_PAY = "auto-pay"
GATE_PDF_LINK = "pdf-behind-link"
GATE_VENDOR = "vendor-mismatch"
GATE_ALREADY_ENTERED = "already-entered"

ATTACH_OK = frozenset({"attached"})
PDF_FIELD_SOURCES = frozenset({"pdf", "pdf-prefix"})


def attach_succeeded(status: str | None) -> bool:
    return (status or "").strip().lower() in ATTACH_OK


def receipts_required(*, po: Any = None, multi_po: bool = False) -> bool:
    """Select Receipts is required when a PO exists (including multi-PO)."""
    if multi_po:
        return True
    return po is not None and str(po).strip() not in {"", "None", "null"}


def fee_total(fees: list[dict[str, Any]] | None) -> float:
    total = 0.0
    for fee in fees or []:
        amount = money(fee.get("amount"))
        if amount is not None:
            total = round(total + amount, 2)
    return total


def merchandise_amount(invoice_total: Any, fees: list[dict[str, Any]] | None) -> float | None:
    """Invoice total minus fees/surcharges. Freight must not become PPV."""
    total = money(invoice_total)
    if total is None:
        return None
    return round(total - fee_total(fees), 2)


def drop_fee_disguised_as_ppv(
    ppv_total: float | None,
    fees: list[dict[str, Any]] | None,
) -> float:
    """Never double-count freight as both Fees and PPV in Excel."""
    value = money(ppv_total) or 0.0
    if value == 0:
        return 0.0
    for fee in fees or []:
        amount = money(fee.get("amount"))
        if amount is None:
            continue
        if abs(value) == abs(amount) or abs(abs(value) - abs(amount)) < 0.005:
            return 0.0
    return value


def is_bill_attempt_result(result: str | None) -> bool:
    """True for Success / Incomplete / HOLD / Fail. Not the run cap."""
    return (result or "").strip() in RESULT_VALUES


def is_noise_result(result: str | None) -> bool:
    return (result or "").strip() in RESULT_NOISE_ALIASES


def counts_toward_email_cap(result: str | None) -> bool:
    """Kyle 2026-09-11: any processed outcome consumes the 10-email touch cap."""
    value = (result or "").strip()
    return value in RESULT_VALUES or value in RESULT_NOISE_ALIASES


def why_hold(gate: str, detail: str) -> str:
    clean = (detail or "").strip()
    if gate == GATE_PREFLIGHT:
        prefix = "HOLD parse-error (preflight-parse)"
        return f"{prefix}: {clean}" if clean else f"{prefix}."
    return f"HOLD ({gate}): {clean}" if clean else f"HOLD ({gate})."


def why_skipped(gate: str, detail: str) -> str:
    """Sheet Why for walked-past noise. Not a bill HOLD."""
    clean = (detail or "").strip()
    return f"Skipped ({gate}): {clean}" if clean else f"Skipped ({gate})."


def why_incomplete(detail: str) -> str:
    clean = (detail or "").strip()
    return f"Incomplete (finish): {clean}" if clean else "Incomplete (finish)."


def why_fail(detail: str) -> str:
    clean = (detail or "").strip()
    if clean.lower().startswith("fail"):
        return clean
    return f"Fail: {clean}" if clean else "Fail."


def vendor_confirmation_gate(
    *,
    parsed_vendor: str | None = None,
    posted_name: str | None = None,
    posted_id: int | None = None,
    forced_mismatch: bool = False,
) -> tuple[bool, str]:
    """GET-after-create: posted KIMCO vendor must be the parsed vendor (or alias).

    Missing posted fields (test mocks / remit-only GET) do not fail — there is
    nothing to contradict. A posted RMP on a parsed MSC is never Success.
    Do not void the header.
    """
    posted_label = (posted_name or "").strip() or (
        f"id {posted_id}" if posted_id is not None else ""
    )
    parsed_label = (parsed_vendor or "").strip() or "unknown"
    if forced_mismatch or (
        (posted_label or posted_id is not None)
        and not vendors_strictly_match(parsed_vendor, posted_name, posted_id)
    ):
        detail = (
            f"parsed {parsed_label}, posted {posted_label or 'unknown'}. "
            "Do not void. Treyce must correct the vendor."
        )
        return False, why_hold(GATE_VENDOR, detail)
    return True, ""


def pdf_file_present(inv: dict[str, Any] | None) -> bool:
    """True when the vendor PDF is on disk. Empty extract ≠ missing file (Nova 258145)."""
    data = inv or {}
    if data.get("pdf_on_disk"):
        return True
    path = str(data.get("pdf_path") or "").strip()
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def attach_presence_status(inv: dict[str, Any] | None) -> str:
    """Sheet Attach status from disk presence. Never no-pdf-on-vm when the file exists."""
    return "pdf-on-vm" if pdf_file_present(inv) else "no-pdf-on-vm"


def invoice_number_source(inv: dict[str, Any] | None) -> str:
    """Where this invoice # was tagged: pdf / pdf-prefix / subject / filename / unknown."""
    data = inv or {}
    sources = data.get("field_sources") or {}
    tagged = str(sources.get("invoice_number") or "").strip().lower()
    if tagged:
        return tagged
    number = str(data.get("invoice_number") or "").strip()
    if not number:
        return "unknown"
    needle = number.lower()
    filename = str(data.get("filename") or "") or Path(str(data.get("pdf_path") or "")).name
    if needle and needle in filename.lower():
        return "filename"
    if needle and needle in str(data.get("subject") or "").lower():
        return "subject"
    if pdf_file_present(data):
        return "pdf"
    return "unknown"


def pdf_extract_or_ocr_failed(inv: dict[str, Any] | None) -> bool:
    """True when a file is on disk but extract+OCR produced no usable PDF fields."""
    data = inv or {}
    if not pdf_file_present(data):
        return False
    if data.get("ocr_failed") or data.get("pdf_extract_failed"):
        return True
    sources = data.get("field_sources") or {}
    if any(str(sources.get(key) or "") in PDF_FIELD_SOURCES for key in ("invoice_number", "date", "amount", "po")):
        return False
    return bool(data.get("pdf_text_empty"))


def this_invoice_facts(inv: dict[str, Any] | None) -> dict[str, Any]:
    """Facts Why must state for THIS bill — not a prior case."""
    data = inv or {}
    filename = str(data.get("filename") or "").strip() or Path(str(data.get("pdf_path") or "")).name
    on_disk = pdf_file_present(data)
    return {
        "vendor": str(data.get("vendor") or "").strip() or "unknown vendor",
        "invoice_number": str(data.get("invoice_number") or "").strip() or "unknown",
        "number_source": invoice_number_source(data),
        "pdf_on_disk": on_disk,
        "filename": filename,
        "pdf_path": str(data.get("pdf_path") or "").strip(),
    }


def why_has_foreign_history(why: str, inv: dict[str, Any] | None) -> list[str]:
    """Historical case names in Why that are not this invoice's vendor/subject/filename/#."""
    data = inv or {}
    own = " ".join(
        str(data.get(key) or "")
        for key in ("vendor", "subject", "filename", "invoice_number", "from_name", "pdf_path")
    ).lower()
    why_l = (why or "").lower()
    hits: list[str] = []
    for token, own_key in (
        ("mcqueary", "mcqueary"),
        ("msc", "msc"),
        ("insight", "insight"),
        ("rob brown", "rob brown"),
        ("erica barrett", "erica"),
    ):
        if token in why_l and own_key not in own:
            hits.append(token)
    return hits


def why_preflight_this_invoice(inv: dict[str, Any] | None, kind: str) -> str:
    """HOLD detail that is true for THIS invoice: vendor, #, source, PDF presence, next action.

    Never append (MSC/McQueary) or any other historical case name unless this bill
    is that vendor. Never say no-pdf-on-vm when the file is on disk.
    """
    facts = this_invoice_facts(inv)
    vendor = facts["vendor"]
    number = facts["invoice_number"]
    source = facts["number_source"]
    filename = facts["filename"]
    on_disk = facts["pdf_on_disk"]
    if on_disk:
        pdf_bit = f"PDF on disk ({filename})" if filename else "PDF on disk"
    elif filename:
        pdf_bit = f"PDF path missing on VM (filename {filename})"
    else:
        pdf_bit = "PDF path missing on VM"
    head = f"{vendor} invoice #{number} sourced from {source}; {pdf_bit}."
    if kind == "pdf_missing":
        next_action = (
            "Next: obtain the vendor PDF and read invoice #, date, and amount from PDF text."
        )
        if on_disk:
            next_action = (
                "Next: read invoice #, date, and amount from the vendor PDF already on disk."
            )
        return f"{head} {next_action}"
    if kind == "ocr_failed":
        return (
            f"{head} Extract+OCR failed after retry. "
            "Next: retry OCR or attach a readable vendor PDF. "
            "Do not invent fields from subject/filename."
        )
    if kind == "number_missing":
        return f"{head} Invoice # missing from PDF text. Next: read the invoice # from the vendor PDF."
    if kind == "number_not_from_pdf":
        return (
            f"{head} Invoice # is from {source}, not vendor PDF text. "
            "Next: obtain the vendor PDF and take invoice # from PDF text, not subject/filename alone."
        )
    if kind == "amount_unverified":
        return (
            f"{head} PDF total could not be verified. "
            "Next: read Amount Due from the vendor PDF. Do not use the email subject amount."
        )
    if kind == "date_missing":
        return (
            f"{head} Invoice date missing from PDF text. "
            "Next: read the invoice date from the vendor PDF, not the email received date."
        )
    if kind == "po_not_from_pdf":
        return (
            f"{head} Printed PO was not taken from vendor PDF text. "
            "Next: read the PO from the vendor PDF."
        )
    if kind == "po_document":
        name = filename or "attachment"
        return (
            f"{head} Attachment {name} is a purchase order, not an invoice. "
            "Next: skip; do not create a header."
        )
    if kind == "gas_ambiguous":
        item = misc_purchase_item_for(str((inv or {}).get("vendor") or "")) or "Shop Supplies - G&S"
        return (
            f"{head} PDF has multiple Misc invoices but amounts could not be split. "
            f"When entering, use Invoice_Type 4 and miscellaneous purchase item {item}. "
            "Next: HOLD; do not invent per-invoice amounts."
        )
    return f"{head} Next: Treyce must review this parse HOLD."


def _preflight_hold(inv: dict[str, Any] | None, kind: str) -> str:
    detail = why_preflight_this_invoice(inv, kind)
    if pdf_file_present(inv):
        detail = detail.replace("no-pdf-on-vm", "PDF on disk")
    return why_hold(GATE_PREFLIGHT, detail)


def invoice_number_matches_subject_or_filename(inv: dict[str, Any] | None) -> bool:
    """True when the invoice # also appears on the subject, filename, or pdf_path name.

    8/18: Nova 258145 (subject) and Crosslink 27943 (invoice-27943.pdf).
    """
    data = inv or {}
    number = str(data.get("invoice_number") or "").strip()
    if not number:
        return False
    haystacks = (
        str(data.get("subject") or ""),
        str(data.get("filename") or ""),
        Path(str(data.get("pdf_path") or "")).name,
    )
    needle = number.lower()
    return any(needle in part.lower() for part in haystacks if part)


def preflight_parse_gate(inv: dict[str, Any]) -> tuple[bool, str]:
    """PDF is the version of the truth. Subject/filename are hints only.

    Do not HOLD preflight-parse because the # was also on the subject or
    filename, or because field_sources tagged subject/filename, when a PDF
    is on disk. Create the header, attach the PDF, and continue finish gates.

    HOLD parse / no-pdf only when the PDF is truly missing, or extract+OCR
    of that PDF failed. Why always describes THIS invoice.
    """
    if is_auto_pay(
        vendor=str(inv.get("vendor") or ""),
        subject=str(inv.get("subject") or ""),
        preview=str(inv.get("bodyPreview") or inv.get("preview") or ""),
        text=str(inv.get("text") or ""),
    ) or str(inv.get("hold_reason") or "").strip().lower() in {"auto-pay", "auto pay"}:
        return False, why_hold(GATE_AUTO_PAY, "Toyota Commercial Finance / auto-pay. Do not enter in ERP.")
    if str(inv.get("hold_reason") or "").strip().lower() == GATE_PDF_LINK or inv.get("pdf_behind_link"):
        vendor = str(inv.get("vendor") or "vendor")
        number = str(inv.get("invoice_number") or "").strip() or "unknown"
        host = str(inv.get("pdf_link_host") or "")
        host_bit = f" host {host}" if host else ""
        return False, why_hold(
            GATE_PDF_LINK,
            f"{vendor} invoice #{number} PDF is behind a download link{host_bit} "
            "(auth wall or failed unauthenticated GET). Not a silent not-a-bill.",
        )
    if inv.get("check_stop") or str(inv.get("hold_reason") or "").strip().upper() == "CHECK STOP":
        # Real notices are mailbox noise (Skipped), not a fake parse-error HOLD.
        return True, ""
    if inv.get("gas_misc_ambiguous"):
        return False, _preflight_hold(inv, "gas_ambiguous")
    if inv.get("is_purchase_order_doc") or inv.get("is_po_document"):
        return False, _preflight_hold(inv, "po_document")
    number = str(inv.get("invoice_number") or "").strip()
    amount = inv.get("amount")
    invoice_date = inv.get("date")
    sources = inv.get("field_sources") or {}
    has_sources = bool(sources)
    on_disk = pdf_file_present(inv)
    number_src = str(sources.get("invoice_number") or "")
    amount_src = str(sources.get("amount") or "")
    date_src = str(sources.get("date") or "")
    po_src = str(sources.get("po") or "")
    number_from_pdf = number_src in PDF_FIELD_SOURCES
    pdf_missing = (not on_disk) and bool(
        inv.get("pdf_unavailable") or inv.get("pdf_text_empty") or (has_sources and not number_from_pdf)
    )

    # Truly missing PDF: HOLD. Why names this vendor / # / source — not a prior case.
    if pdf_missing:
        return False, _preflight_hold(inv, "pdf_missing")

    # File exists but extract+OCR failed: HOLD. Do not invent from subject/filename.
    if pdf_extract_or_ocr_failed(inv):
        return False, _preflight_hold(inv, "ocr_failed")

    if has_sources:
        # PDF on disk + subject/filename # is a hint, not a parse HOLD
        # (Nova 258145 subject; Crosslink 27943 filename).
        if not number:
            return False, _preflight_hold(inv, "number_missing")
        if not number_from_pdf and not on_disk:
            return False, _preflight_hold(inv, "number_not_from_pdf")
        if amount in (None, "") or (amount_src and amount_src not in PDF_FIELD_SOURCES):
            return False, _preflight_hold(inv, "amount_unverified")
        if not invoice_date or (date_src and date_src not in PDF_FIELD_SOURCES):
            return False, _preflight_hold(inv, "date_missing")
        if inv.get("po") and po_src and po_src not in PDF_FIELD_SOURCES and not on_disk:
            return False, _preflight_hold(inv, "po_not_from_pdf")
        return True, ""

    # Fixtures without provenance still cannot invent a total or number.
    if not number:
        return False, _preflight_hold(inv, "number_missing")
    if amount in (None, ""):
        return False, _preflight_hold(inv, "amount_unverified")
    if not invoice_date:
        return False, _preflight_hold(inv, "date_missing")
    return True, ""


def fees_required(fees: list[dict[str, Any]] | None) -> bool:
    """True when parsed fees have amounts that must be posted on the bill."""
    for fee in fees or []:
        if not isinstance(fee, dict):
            continue
        if money(fee.get("amount")) is not None:
            return True
    return False


def finish_gate(
    *,
    header_created: bool,
    attach_status: str | None,
    po: Any = None,
    multi_po: bool = False,
    receipts_selected: bool = False,
    kimco_id: Any = None,
    selfcheck: dict[str, Any] | None = None,
    fees: list[dict[str, Any]] | None = None,
    fees_posted: bool = False,
) -> tuple[str, str]:
    """Success only if header + (Select Receipts when PO) + PDF attached
    + Additional Charge Fees posted when fees were parsed
    AND the Treyce-load self-check passes (she would not need to rework).

    Header-only with blocked-405 attach, receipts not selected, or fees
    noted on the sheet but not posted is Incomplete.
    Incomplete is not Success and must not be Entered in AI.
    """
    if not header_created:
        return RESULT_HOLD, why_hold(GATE_FINISH, "header was not created. Treyce cannot finish this bill.")
    attached = attach_succeeded(attach_status)
    need_receipts = receipts_required(po=po, multi_po=multi_po)
    receipts_ok = (not need_receipts) or bool(receipts_selected)
    need_fees = fees_required(fees)
    fees_ok = (not need_fees) or bool(fees_posted)
    if not (attached and receipts_ok and fees_ok):
        bits = [f"header created (id {kimco_id})." if kimco_id not in (None, "") else "header created."]
        if need_receipts and not receipts_selected:
            bits.append(
                "Select Receipts not posted on the invoice record "
                "(check Can Edit Items / Inline on the list). "
                "Do not type Add Item. Treyce would still select receipts."
            )
        if need_fees and not fees_posted:
            bits.append(
                "Additional Charge Fees and surcharges / F-Fees & Surcharges "
                "not posted on the bill (sheet Fees column is not enough). "
                "Treyce would still post the fees."
            )
        if not attached:
            bits.append(
                f"PDF attach={attach_status or 'missing'}. "
                "Success requires the vendor PDF on the header. Treyce would still attach it."
            )
        bits.append("Not Entered in AI.")
        return RESULT_INCOMPLETE, why_incomplete(" ".join(bits))
    if selfcheck:
        ok, why = treyce_finish_selfcheck(selfcheck)
        if not ok:
            return RESULT_HOLD, why
    return RESULT_SUCCESS, ""


def success_is_legal(
    *,
    header_created: bool,
    attach_status: str | None,
    po: Any = None,
    multi_po: bool = False,
    receipts_selected: bool = False,
) -> bool:
    result, _why = finish_gate(
        header_created=header_created,
        attach_status=attach_status,
        po=po,
        multi_po=multi_po,
        receipts_selected=receipts_selected,
    )
    return result == RESULT_SUCCESS


def find_live_po(
    po_index: dict[str, dict[str, Any]],
    *,
    printed_po: str | None,
    vendor: str | None,
    parts: list[str] | None = None,
    wo: str | None = None,
) -> dict[str, Any]:
    """Resolve a live PO id. Printed PO first; else vendor + part/WO.

    Willbanks/RMP: do not fall through to Misc Type 4 when a PO is findable.
    """
    empty = {"info": None, "number": printed_po, "how": "missing"}
    if printed_po:
        info = po_index.get(str(printed_po))
        if info:
            return {"info": info, "number": str(printed_po), "how": "printed"}
    inv_parts = {normalize_part(p) for p in (parts or []) if p}
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for number, info in po_index.items():
        vendor_text = str(info.get("vendor_text") or info.get("text") or "")
        vendor_id = info.get("vendor_id")
        alias = known_vendor_id(vendor)
        vendor_ok = (
            names_match(vendor, vendor_text)
            or bool(vendor_match_score(vendor, vendor_text))
            or (alias is not None and vendor_id is not None and int(alias) == int(vendor_id))
        )
        if not vendor_ok:
            continue
        score = 10
        info_parts = {normalize_part(line.get("part")) for line in (info.get("lines") or []) if line.get("part")}
        overlap = inv_parts & info_parts - {""}
        if overlap:
            score += 50 * len(overlap)
        if wo:
            if any(normalize_part(line.get("wo")) == normalize_part(wo) for line in (info.get("lines") or [])):
                score += 40
        if score >= 60 or (score >= 10 and inv_parts and overlap):
            scored.append((score, str(number), info))
        elif score >= 10 and not inv_parts and printed_po and str(number) == str(printed_po):
            scored.append((score, str(number), info))
    if not scored:
        return empty
    scored.sort(key=lambda row: row[0], reverse=True)
    if len(scored) > 1 and scored[0][0] < scored[1][0] + 10:
        return {"info": None, "number": printed_po, "how": "ambiguous"}
    return {"info": scored[0][2], "number": scored[0][1], "how": "vendor+part"}


def po_gate_decision(
    *,
    printed_pos: list[str],
    multi_po: bool,
    resolved: dict[str, Any] | None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Return (ok_to_create, why, po_info).

    Multi-PO: header PO blank is OK. Single printed/findable PO must be set.
    Printed PO that is not findable on live is HOLD (not Misc Type 4).
    """
    if multi_po or len(printed_pos) > 1:
        return True, "Multi-PO: header Purchase Order left blank; Select Receipts per PO required.", None
    if resolved and resolved.get("info"):
        return True, "", resolved["info"]
    if printed_pos:
        return False, why_hold(
            GATE_PO,
            f"PO {printed_pos[0]} is on the invoice but not findable on live by vendor + part/WO. "
            "Will not enter as Misc Type 4 (Willbanks 209663/209664; RMP 1470159).",
        ), None
    return True, "", None


def classify_noise_reason(klass: str | None) -> str | None:
    """Statements, payments, CHECK STOP, internal mail, PODs stay HOLD."""
    key = (klass or "").strip().lower().replace("_", "-")
    if key in {"statement", "pod", "payment", "check-stop", "check stop", "internal", "not-a-bill"}:
        return key
    return None


def qty_gate(
    invoice_lines: list[dict[str, Any]] | None,
    other_lines: list[dict[str, Any]] | None,
) -> tuple[bool, str]:
    """HOLD when invoice qty ≠ matched receipt/PO qty. Not Success."""
    found = qty_discrepancy(invoice_lines, other_lines)
    if found.get("hold"):
        return False, why_hold(GATE_QTY, str(found.get("why") or "invoice qty does not match."))
    return True, ""


def header_created_with_issues(*, result: str | None, kimco_id: Any = None, attach_status: str | None = None) -> bool:
    """True when a header exists but the bill cannot be finished.

    Outlook: Entered with issues (price-does-not-match, qty HOLD, Incomplete).
    """
    outcome = (result or "").strip()
    if outcome not in {RESULT_HOLD, RESULT_INCOMPLETE}:
        return False
    if kimco_id in (None, ""):
        return False
    return True


def outlook_process_for(*, result: str | None, kimco_id: Any = None) -> str:
    """entered-in-ai | entered-with-issues | ai-hold | none."""
    outcome = (result or "").strip()
    if outcome in RESULT_NOISE_ALIASES:
        return "none"
    if outcome == RESULT_SUCCESS:
        return "entered-in-ai"
    if header_created_with_issues(result=outcome, kimco_id=kimco_id):
        return "entered-with-issues"
    if outcome in {RESULT_HOLD, RESULT_FAIL, RESULT_INCOMPLETE}:
        return "ai-hold"
    return "none"


def never_type_4_when_po(po: Any, invoice_type: int | None = None) -> bool:
    """Purvis-like: a printed/findable PO must be Invoice_Type 3, never blank Type 4."""
    expected = invoice_type_for(po)
    if expected != 3:
        return True
    if invoice_type is None:
        return True
    return int(invoice_type) == 3


def creates_header_on_hold(reason: str | None) -> bool:
    key = (reason or "").strip().lower()
    return key in CREATE_HEADER_ON_HOLD


def treyce_finish_selfcheck(check: dict[str, Any]) -> tuple[bool, str]:
    """Fix-before-complete. Success only if Treyce would not rework the bill.

    Checklist (QUALITY.md / quality_v12.TREYCE_FINISH_CHECKLIST):
    invoice # from PDF (suffixes), PO not blank Type 4, receipt by part/description,
    qty match, fees not PPV, PPV within Kyle's rule, PDF attached, Select Receipts.
    """
    failures: list[str] = []
    sources = check.get("field_sources") or {}
    number = str(check.get("invoice_number") or "").strip()
    number_src = str(sources.get("invoice_number") or "")
    if check.get("require_pdf_number", True) and sources:
        number_ok = bool(number) and (
            number_src in PDF_FIELD_SOURCES or pdf_file_present(check)
        )
        if not number_ok:
            failures.append(
                "Invoice # is not from vendor PDF text (Insight 1809 / MSC 5157357 / Techni-Tool suffix). "
                "Fix: read the PDF; do not use filename/subject."
            )
        pdf_text = str(check.get("pdf_text") or "")
        if number and "." not in number and pdf_text:
            suffixed = re.search(rf"\b({re.escape(number)}\.\d{{3}})\b", pdf_text)
            if suffixed:
                failures.append(
                    f"Invoice # on PDF is {suffixed.group(1)}, not bare {number}. "
                    "Fix: keep the Techni-Tool-style suffix on the header."
                )
    printed_pos = [str(p) for p in (check.get("printed_pos") or []) if p]
    invoice_type = check.get("invoice_type")
    po = check.get("po")
    multi_po = bool(check.get("multi_po"))
    if printed_pos and not multi_po:
        if po in (None, "", "None", "null") or (invoice_type is not None and int(invoice_type) == 4):
            failures.append(
                f"PO {printed_pos[0]} is on the PDF but header would be blank Type 4 (Purvis 32625214). "
                "Fix: set Purchase Order and Invoice_Type 3; Select Receipts."
            )
    if check.get("qty_hold"):
        failures.append(
            "Invoice qty ≠ PO/receipt qty (Capital 26764). "
            "Fix: HOLD so a buyer can comment/tag. Do not claim Success."
        )
    if check.get("price_hold"):
        failures.append(
            "Price does not match (over 10% of invoice total or over $100, or $0 PO unit). "
            "Fix: HOLD, header+PDF, comment @Shawn McKibben. Do not post PPV."
        )
    if check.get("fees_posted_as_ppv"):
        failures.append(
            "A supply/fee/surcharge was coded as PPV (Techni-Tool $46.20). "
            "Fix: Additional Charge Fees and surcharges, never PPV."
        )
    ppv_over = check.get("ppv_over_rule")
    if ppv_over:
        failures.append(
            "PPV exceeds Kyle’s ≤10% of invoice total and ≤$100 rule. "
            "Fix: price-does-not-match HOLD, not Success."
        )
    receipt_qty_only = check.get("receipt_qty_only_match")
    if receipt_qty_only:
        failures.append(
            "Receipt/PO line was chosen by first leftover qty, not part/description (O'Neal SCH 40 A500). "
            "Fix: rematch by description/part. Do not finish."
        )
    if check.get("require_attach", False) and not attach_succeeded(str(check.get("attach_status") or "")):
        failures.append(
            "Vendor PDF is not attached on the header. "
            "Fix: attach the PDF before Success. Treyce would still attach it."
        )
    if check.get("require_select_receipts", False) and not check.get("receipts_selected"):
        failures.append(
            "Select Receipts was not posted on a PO-path bill. "
            "Fix: post Select Receipts. Treyce would still select receipts."
        )
    if check.get("receipt_qty_mismatch"):
        failures.append(
            "Selected receipt qty does not match invoice qty (Fastenal TXFT4100079 36 vs 35). "
            "Fix: rematch by invoice qty/cost. Do not claim Success."
        )
    if check.get("fees_required") and not check.get("fees_posted"):
        failures.append(
            "Parsed fees were not posted as Additional Charge Fees and surcharges "
            "(Fastenal TXFT4100079 Shipping & Handling). "
            "Sheet Fees column is not enough. Fix: post F-Fees & Surcharges before Success."
        )
    unmatched_lines = check.get("unmatched_invoice_lines") or []
    if unmatched_lines:
        labels = format_unmatched_lines(unmatched_lines)
        failures.append(
            f"Unmatched invoice line(s): {labels or 'unspecified'} "
            "(EMJ Z250725432). Fix: Select Receipts for each merchandise line. "
            "Do not stop after one. Never silent Success."
        )
    unmatched_pos = [str(p) for p in (check.get("unmatched_pos") or []) if p]
    if unmatched_pos:
        failures.append(
            f"Unmatched PO(s): {format_unmatched_pos(unmatched_pos)} "
            "(3P multi-PO). Fix: Select Receipts per PO. Never silent Success."
        )
    vendor_ok, vendor_why = vendor_confirmation_gate(
        parsed_vendor=check.get("parsed_vendor"),
        posted_name=check.get("posted_vendor"),
        posted_id=check.get("posted_vendor_id"),
        forced_mismatch=bool(check.get("vendor_mismatch")),
    )
    if not vendor_ok:
        failures.append(vendor_why)
    if not failures:
        return True, ""
    return False, why_hold(
        GATE_FINISH,
        "Treyce would still rework this bill. " + " ".join(failures) + " Never Success.",
    )


def selfcheck_payload(
    inv: dict[str, Any],
    *,
    invoice_type: int | None = None,
    po: Any = None,
    multi_po: bool = False,
    price: dict[str, Any] | None = None,
    qty_hold: bool = False,
    receipt_result: dict[str, Any] | None = None,
    fees_posted: bool = False,
    receipt_qty_mismatch: bool = False,
) -> dict[str, Any]:
    """Build the finish self-check dict from a processed invoice."""
    printed = [str(p) for p in (inv.get("pos") or ([inv.get("po")] if inv.get("po") else [])) if p]
    fees_as_ppv = False
    ppv_over = False
    if price:
        for item in price.get("items") or []:
            if item.get("action") == "ppv" and (
                is_fee_or_surcharge(str(item.get("label") or "")) or item.get("fee")
            ):
                fees_as_ppv = True
        if price.get("hold"):
            ppv_over = True
    qty_only = False
    for hit in (receipt_result or {}).get("matched") or []:
        score = int(hit.get("score") or 0)
        if 0 < score < 50:
            qty_only = True
        how = str(hit.get("how") or hit.get("pass") or "")
        if how in {"second-open-on-po", "first-open-on-po"} and score < 60:
            # Blind first-PO-receipt is a qty-only guess when invoice evidence existed.
            if inv.get("qty") not in (None, "") or inv.get("lines"):
                qty_only = True
    parsed_fees = list(inv.get("fees") or [])
    return {
        "invoice_number": inv.get("invoice_number"),
        "field_sources": inv.get("field_sources") or {},
        "pdf_text": inv.get("pdf_text") or inv.get("text") or "",
        "printed_pos": printed,
        "invoice_type": invoice_type,
        "po": po,
        "multi_po": multi_po,
        "qty_hold": qty_hold,
        "price_hold": bool((price or {}).get("hold")),
        "fees_posted_as_ppv": fees_as_ppv,
        "ppv_over_rule": ppv_over,
        "receipt_qty_only_match": qty_only,
        "receipt_qty_mismatch": receipt_qty_mismatch,
        "fees_required": fees_required(parsed_fees),
        "fees_posted": bool(fees_posted),
        "unmatched_invoice_lines": list((receipt_result or {}).get("unmatched_lines") or []),
        "unmatched_pos": list((receipt_result or {}).get("unmatched_pos") or []),
        "require_pdf_number": bool(inv.get("field_sources")),
        "pdf_path": inv.get("pdf_path"),
        "pdf_on_disk": inv.get("pdf_on_disk"),
        "parsed_vendor": inv.get("vendor"),
    }
