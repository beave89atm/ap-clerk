"""QUALITY V1.2 hard gates. Success means a finished bill, not a header create.

Treyce 2026-09-10 notes on the 8/16 dry-10 sheet. No network I/O. Never logs secrets.
"""

from __future__ import annotations

import re
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
    vendor_match_score,
)

RESULT_SUCCESS = "Success"
RESULT_INCOMPLETE = "Incomplete"
RESULT_HOLD = "HOLD"
RESULT_FAIL = "Fail"
# Bill-attempt outcomes only. Mailbox noise is RESULT_SKIPPED and does not count toward N.
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
    """Cap = Success + Incomplete + real bill HOLD + Fail. Noise does not count."""
    return (result or "").strip() in RESULT_VALUES


def is_noise_result(result: str | None) -> bool:
    return (result or "").strip() in RESULT_NOISE_ALIASES


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


def preflight_parse_gate(inv: dict[str, Any]) -> tuple[bool, str]:
    """Invoice #, date, amount, and PO must come from vendor PDF text.

    Filename/subject alone is not enough. If the PDF cannot be obtained,
    HOLD with no-pdf-on-vm — do not pretend the PDF lacked a number that is
    printed on it (Insight 1809 / MSC 5157357).
    """
    if is_auto_pay(
        vendor=str(inv.get("vendor") or ""),
        subject=str(inv.get("subject") or ""),
        preview=str(inv.get("bodyPreview") or inv.get("preview") or ""),
        text=str(inv.get("text") or ""),
    ) or str(inv.get("hold_reason") or "").strip().lower() in {"auto-pay", "auto pay"}:
        return False, why_hold(GATE_AUTO_PAY, "Toyota Commercial Finance / auto-pay. Do not enter in ERP.")
    if str(inv.get("hold_reason") or "").strip().lower() == GATE_PDF_LINK or inv.get("pdf_behind_link"):
        return False, why_hold(
            GATE_PDF_LINK,
            "vendor PDF is behind a download link that needs auth or failed unauthenticated GET. "
            "Not a silent not-a-bill.",
        )
    if inv.get("check_stop") or str(inv.get("hold_reason") or "").strip().upper() == "CHECK STOP":
        # Real notices are mailbox noise (Skipped), not a fake parse-error HOLD.
        return True, ""
    if inv.get("gas_misc_ambiguous"):
        item = misc_purchase_item_for(str(inv.get("vendor") or "")) or "Shop Supplies - G&S"
        return False, why_hold(
            GATE_PREFLIGHT,
            "Gas & Supply PDF has multiple Misc invoices but amounts could not be split. "
            f"When entering, use Invoice_Type 4 and miscellaneous purchase item {item}. "
            "HOLD when ambiguous rather than inventing amounts.",
        )
    if inv.get("is_purchase_order_doc") or inv.get("is_po_document"):
        return False, why_hold(
            GATE_PREFLIGHT,
            "attachment is a purchase order, not an invoice. Legacy Purchase_Order_*.pdf and any PO-not-invoice stay skipped.",
        )
    number = str(inv.get("invoice_number") or "").strip()
    amount = inv.get("amount")
    invoice_date = inv.get("date")
    sources = inv.get("field_sources") or {}
    has_sources = bool(sources)
    pdf_missing = bool(inv.get("pdf_unavailable") or inv.get("pdf_text_empty"))

    if pdf_missing and (not has_sources or str(sources.get("invoice_number") or "") not in PDF_FIELD_SOURCES):
        return False, why_hold(
            GATE_PREFLIGHT,
            "PDF could not be obtained (no-pdf-on-vm / not reading PDF). "
            "Will not invent invoice # from filename/subject. "
            "If the vendor PDF text has the number (Insight 1809, MSC 5157357), read the PDF.",
        )

    if has_sources:
        number_src = str(sources.get("invoice_number") or "")
        amount_src = str(sources.get("amount") or "")
        date_src = str(sources.get("date") or "")
        po_src = str(sources.get("po") or "")
        if not number or number_src not in PDF_FIELD_SOURCES:
            return False, why_hold(
                GATE_PREFLIGHT,
                "invoice # must be taken from vendor PDF text, not email subject/filename alone "
                "(MSC/Rob Brown 5157357 vs filename 191471; Insight 1809).",
            )
        if amount in (None, "") or amount_src not in PDF_FIELD_SOURCES:
            return False, why_hold(
                GATE_PREFLIGHT,
                "PDF total could not be verified; will not create a wrong-amount header (Gas 0040323616).",
            )
        if not invoice_date or date_src not in PDF_FIELD_SOURCES:
            return False, why_hold(
                GATE_PREFLIGHT,
                "invoice date must be taken from vendor PDF text, not the email received date.",
            )
        if inv.get("po") and po_src and po_src not in PDF_FIELD_SOURCES:
            return False, why_hold(
                GATE_PREFLIGHT,
                "printed PO must be taken from vendor PDF text, not filename/subject alone.",
            )
        return True, ""

    # Fixtures without provenance still cannot invent a total or number.
    if not number:
        return False, why_hold(GATE_PREFLIGHT, "invoice # missing from PDF text.")
    if amount in (None, ""):
        return False, why_hold(
            GATE_PREFLIGHT,
            "PDF total could not be verified; will not create a wrong-amount header.",
        )
    if not invoice_date:
        return False, why_hold(
            GATE_PREFLIGHT,
            "invoice date missing from PDF text; will not use the email received date.",
        )
    return True, ""


def finish_gate(
    *,
    header_created: bool,
    attach_status: str | None,
    po: Any = None,
    multi_po: bool = False,
    receipts_selected: bool = False,
    kimco_id: Any = None,
    selfcheck: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Success only if header + (Select Receipts when PO) + PDF attached
    AND the Treyce-load self-check passes (she would not need to rework).

    Header-only with blocked-405 attach or receipts not selected is Incomplete.
    Incomplete is not Success and must not be Entered in AI.
    """
    if not header_created:
        return RESULT_HOLD, why_hold(GATE_FINISH, "header was not created. Treyce cannot finish this bill.")
    attached = attach_succeeded(attach_status)
    need_receipts = receipts_required(po=po, multi_po=multi_po)
    receipts_ok = (not need_receipts) or bool(receipts_selected)
    if not (attached and receipts_ok):
        bits = [f"header created (id {kimco_id})." if kimco_id not in (None, "") else "header created."]
        if need_receipts and not receipts_selected:
            bits.append(
                "Select Receipts not posted on the invoice record "
                "(check Can Edit Items / Inline on the list). "
                "Do not type Add Item. Treyce would still select receipts."
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
        if not number or number_src not in PDF_FIELD_SOURCES:
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
        "require_pdf_number": bool(inv.get("field_sources")),
    }
