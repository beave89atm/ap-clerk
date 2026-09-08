"""QUALITY V1.1 hard gates. Success means a finished bill, not a header create.

No network I/O. Never logs secrets.
"""

from __future__ import annotations

from typing import Any

from ap_clerk.rules import (
    extract_po_number,
    is_fee_or_surcharge,
    known_vendor_id,
    money,
    names_match,
    normalize_part,
    vendor_match_score,
)

RESULT_SUCCESS = "Success"
RESULT_INCOMPLETE = "Incomplete"
RESULT_HOLD = "HOLD"
RESULT_FAIL = "Fail"
RESULT_VALUES = (RESULT_SUCCESS, RESULT_INCOMPLETE, RESULT_HOLD, RESULT_FAIL)

GATE_PREFLIGHT = "preflight-parse"
GATE_PO = "po"
GATE_RECEIPT = "receipt"
GATE_FINISH = "finish"
GATE_BILL_VS_NOISE = "bill-vs-noise"
GATE_PRICE = "price-does-not-match"

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


def why_hold(gate: str, detail: str) -> str:
    clean = (detail or "").strip()
    if gate == GATE_PREFLIGHT:
        prefix = "HOLD parse-error (preflight-parse)"
        return f"{prefix}: {clean}" if clean else f"{prefix}."
    return f"HOLD ({gate}): {clean}" if clean else f"HOLD ({gate})."


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

    Filename/subject alone is not enough. If the PDF total cannot be verified,
    HOLD parse-error — do not create a wrong-amount header.
    """
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

    if has_sources:
        number_src = str(sources.get("invoice_number") or "")
        amount_src = str(sources.get("amount") or "")
        date_src = str(sources.get("date") or "")
        po_src = str(sources.get("po") or "")
        if not number or number_src not in PDF_FIELD_SOURCES:
            return False, why_hold(
                GATE_PREFLIGHT,
                "invoice # must be taken from vendor PDF text, not email subject/filename alone (MSC/McQueary).",
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
) -> tuple[str, str]:
    """Success only if header + (Select Receipts when PO) + PDF attached.

    Header-only with blocked-405 attach or receipts not selected is Incomplete.
    Incomplete is not Success and must not be Entered in AI.
    """
    if not header_created:
        return RESULT_HOLD, why_hold(GATE_FINISH, "header was not created.")
    attached = attach_succeeded(attach_status)
    need_receipts = receipts_required(po=po, multi_po=multi_po)
    receipts_ok = (not need_receipts) or bool(receipts_selected)
    if attached and receipts_ok:
        return RESULT_SUCCESS, ""
    bits = [f"header created (id {kimco_id})." if kimco_id not in (None, "") else "header created."]
    if need_receipts and not receipts_selected:
        bits.append(
            "Select Receipts not posted (API not Editable; live UI path required). "
            "Do not type Add Item."
        )
    if not attached:
        bits.append(
            f"PDF attach={attach_status or 'missing'}. "
            "Success requires the vendor PDF on the header."
        )
    bits.append("Not Entered in AI.")
    return RESULT_INCOMPLETE, why_incomplete(" ".join(bits))


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
