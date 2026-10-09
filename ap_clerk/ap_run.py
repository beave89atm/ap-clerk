"""Reusable AP run guards.

Day scripts under ``scripts/ap_run_*.py`` call these functions. They are pure:
fixtures in, decisions out. No KIMCO writes and no mailbox moves.

PPV dollars come from ``ap_clerk.rules.ppv_limit`` (``AP_PPV_LIMIT``, default 75).
Email moves come from ``email_move_cap`` (``AP_EMAIL_MOVE_CAP``, default 10).
"""

from __future__ import annotations

import os
import re
from typing import Any

from ap_clerk.rules import money, ppv_limit

# Ship-to / customer codes that were stored as invoice numbers.
# O'Neal SHIP TO 885055 is not invoice 15491464. 14748440 is the same class of code.
CUSTOMER_SHIP_TO_CODES = frozenset({"885055", "14748440"})

EMAIL_MOVE_CAP_ENV = "AP_EMAIL_MOVE_CAP"
DEFAULT_EMAIL_MOVE_CAP = 10

# One O'Neal bar may be billed as 1 piece and received in inches when the
# dollars match. Morgan Steel is not on this list.
ONEAL_SINGLE_BAR_RULE = "oneal-single-bar"
_EACH_TOKENS = {"EA", "EACH", "PC", "PCS", "PIECE", "PIECES"}
_NON_INVOICE_KINDS = frozenset(
    {
        "statement",
        "auto-pay",
        "autopay",
        "not-a-bill",
        "payment",
        "pod",
        "check-stop",
        "internal",
    }
)

_ONEAL_INVOICE = re.compile(r"INVOICE\s*(?:NO\.?|NUMBER|#)\s*[:\s]*\n?\s*(15\d{6})", re.I)
_LABELED_INVOICE = re.compile(
    r"INVOICE\s*(?:NO\.?|NUMBER|#)\s*[:\s#]*\n?\s*([A-Z]{0,6}-?[A-Z0-9][A-Z0-9\-]{2,})",
    re.I,
)
_SHIP_TO = re.compile(r"SHIP\s*TO\s*[:\s]*\n?\s*([A-Z0-9\-]{4,})", re.I)
_ONEAL_TOTAL = re.compile(
    r"TOTAL\s+ORDER\s+AMOUNT[\s\S]{0,240}?([\d,]+\.\d{2})\s*\n\s*\.00\s*\n\s*([\d,]+\.\d{2})",
    re.I,
)
_LABELED_TOTAL = re.compile(
    r"(?:INVOICE\s+TOTAL|AMOUNT\s+DUE|TOTAL\s+DUE|BALANCE\s+DUE|PRINTED\s+TOTAL)\s*[:\s$]*([\d,]+\.\d{2})",
    re.I,
)


def email_move_cap() -> int:
    """Per-run mailbox move cap. ``AP_EMAIL_MOVE_CAP`` overrides the default of 10."""
    raw = os.environ.get(EMAIL_MOVE_CAP_ENV)
    if raw in (None, ""):
        return DEFAULT_EMAIL_MOVE_CAP
    return max(0, int(raw))


def _norm_vendor(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def same_vendor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id = int(left.get("vendor_id") or 0)
    right_id = int(right.get("vendor_id") or 0)
    if left_id and right_id:
        return left_id == right_id
    a = _norm_vendor(left.get("vendor"))
    b = _norm_vendor(right.get("vendor"))
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _is_oneal(vendor: str | None) -> bool:
    key = _norm_vendor(vendor)
    return "oneal" in key


def _is_morgan(vendor: str | None) -> bool:
    return "morgansteel" in _norm_vendor(vendor) or "morgan" in _norm_vendor(vendor)


def unit_family(uom: str | None) -> str:
    token = re.sub(r"[^A-Z]", "", str(uom or "").upper())
    if token.startswith("EA") or token in _EACH_TOKENS:
        return "each"
    if token.startswith("FT"):
        return "foot"
    if token.startswith("IN"):
        return "inch"
    return token


def quantities_match(invoice_qty: float, receipt_qty: float, invoice_uom: str, receipt_uom: str) -> bool:
    """Equal counts with different units are not a match. Feet and inches convert."""
    left = unit_family(invoice_uom)
    right = unit_family(receipt_uom)
    if left == "inch" and right == "foot":
        return abs(float(invoice_qty) - float(receipt_qty) * 12) < 0.02
    if left == "foot" and right == "inch":
        return abs(float(invoice_qty) * 12 - float(receipt_qty)) < 0.02
    if left and right and left != right:
        return False
    return abs(float(invoice_qty) - float(receipt_qty)) < 0.02


def oneal_single_bar_allowed(
    *,
    vendor: str,
    invoice_qty: float,
    invoice_uom: str,
    receipt_uom: str,
    invoice_amount: float,
    receipt_amount: float,
) -> bool:
    """Documented exception: one O'Neal bar, piece vs inches, dollars match.

    Morgan Steel is never covered. A 40-piece line is not a single bar.
    """
    if _is_morgan(vendor) or not _is_oneal(vendor):
        return False
    if abs(float(invoice_qty) - 1.0) > 0.001:
        return False
    if unit_family(invoice_uom) != "each" or unit_family(receipt_uom) != "inch":
        return False
    return abs(round(float(invoice_amount), 2) - round(float(receipt_amount), 2)) < 0.02


def cross_unit_selection(item: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide a piece-vs-inch selection. Refuses Morgan and any qty edit."""
    vendor = str(item.get("vendor") or "")
    line = (item.get("lines_match") or [{}])[0]
    want = float(line.get("qty") or 0)
    amount = float(line.get("amount") if line.get("amount") is not None else item.get("merch") or 0)
    invoice_uom = str(line.get("uom") or "PCS")
    open_rows = [
        row
        for row in receipts
        if row.get("id") and row.get("open") and not row.get("placeholder") and not receipt_is_linked_elsewhere(row)
    ]
    if _is_morgan(vendor) or not oneal_single_bar_allowed(
        vendor=vendor,
        invoice_qty=want,
        invoice_uom=invoice_uom,
        receipt_uom="IN",
        invoice_amount=amount,
        receipt_amount=amount,
    ):
        who = "Morgan Steel" if _is_morgan(vendor) else vendor or "This vendor"
        return {
            "allowed": False,
            "ids": [],
            "ppv": 0.0,
            "reason": (
                f"{who} is not covered by the O'Neal single-bar unit rule. "
                f"{want:g} {invoice_uom} is not the same as a receipt in different units. "
                "Nothing was selected."
            ),
        }
    hits = [
        row
        for row in open_rows
        if unit_family(str(row.get("uom") or "")) == "inch"
        and abs(round(float(row.get("extended") or 0), 2) - round(amount, 2)) < 0.02
        and not _qty_would_change(row)
    ]
    if len(hits) != 1:
        return {
            "allowed": False,
            "ids": [],
            "ppv": 0.0,
            "reason": (
                f"O'Neal single-bar rule did not find one inch receipt at ${amount:,.2f}. "
                "Nothing was selected."
            ),
        }
    guarded = guard_receipt_selection(hits)
    if not guarded["ok"]:
        return {"allowed": False, "ids": [], "ppv": 0.0, "reason": guarded["reason"]}
    return {"allowed": True, "ids": guarded["ids"], "ppv": 0.0, "reason": ""}


def _qty_would_change(receipt: dict[str, Any]) -> bool:
    requested = receipt.get("select_qty")
    if requested in (None, ""):
        return False
    return abs(float(requested) - float(receipt.get("qty") or 0)) > 0.001


def receipt_is_linked_elsewhere(receipt: dict[str, Any], *, this_bill_id: int | None = None) -> bool:
    bill = receipt.get("invoiced_bill") or receipt.get("linked_bill")
    if isinstance(bill, dict) and bill.get("id") not in (None, ""):
        if this_bill_id is not None and int(bill["id"]) == int(this_bill_id):
            return False
        return True
    if receipt.get("linked_invoice") not in (None, ""):
        return True
    return False


def guard_receipt_selection(receipts: list[dict[str, Any]], *, this_bill_id: int | None = None) -> dict[str, Any]:
    """Ids only. A linked receipt is not selected. A quantity override is refused."""
    ids: list[int] = []
    for receipt in receipts:
        if receipt.get("id") in (None, ""):
            continue
        if receipt_is_linked_elsewhere(receipt, this_bill_id=this_bill_id):
            other = receipt.get("invoiced_bill") or receipt.get("linked_bill") or receipt.get("linked_invoice")
            return {
                "ok": False,
                "ids": [],
                "reason": (
                    f"Receipt {receipt.get('id')} is already linked to {other}. "
                    "It was not selected and its quantity was not changed."
                ),
            }
        if _qty_would_change(receipt):
            return {
                "ok": False,
                "ids": [],
                "reason": (
                    f"Receipt {receipt.get('id')} quantity {receipt.get('qty')} "
                    f"was not changed to {receipt.get('select_qty')}."
                ),
            }
        ids.append(int(receipt["id"]))
    return {"ok": True, "ids": ids, "reason": ""}


def line_price_variances(lines: list[dict[str, Any]], *, limit: float | None = None) -> dict[str, Any]:
    """Per-line PPV against the configured limit. Line gaps are not netted away."""
    cap = ppv_limit() if limit is None else round(float(limit), 2)
    gaps: list[float] = []
    for line in lines:
        invoice_amount = round(float(line["invoice_amount"]), 2)
        receipt_amount = round(float(line["receipt_amount"]), 2)
        gaps.append(round(invoice_amount - receipt_amount, 2))
    net = round(sum(gaps), 2)
    over = [gap for gap in gaps if abs(gap) >= cap]
    if over:
        return {
            "action": "hold",
            "limit": cap,
            "line_gaps": gaps,
            "net": net,
            "ppv": 0.0,
            "reason": (
                f"A line price gap is ${abs(over[0]):,.2f}, which is ${cap:,.2f} or more. "
                "Line gaps are checked on their own so they cannot cancel."
            ),
        }
    if abs(net) >= cap:
        return {
            "action": "hold",
            "limit": cap,
            "line_gaps": gaps,
            "net": net,
            "ppv": 0.0,
            "reason": (
                f"The price gap is ${abs(net):,.2f}, which is ${cap:,.2f} or more. "
                "Receipts were not selected."
            ),
        }
    return {
        "action": "ppv" if abs(net) >= 0.005 else "match",
        "limit": cap,
        "line_gaps": gaps,
        "net": net,
        "ppv": net,
        "reason": "",
    }


def po_absence_reason(po: str, lines: list[dict[str, Any]]) -> str:
    """PO is in KIMCO and nothing has been received. Not a missing purchase order."""
    parts: list[str] = []
    for line in lines:
        qty = line.get("qty")
        uom = str(line.get("uom") or "EA")
        token = str(line.get("token") or "").strip()
        bit = f"{float(qty):g} {uom}"
        if token:
            bit += f" {token}"
        parts.append(bit)
    what = " and ".join(parts) if parts else "the invoice quantity"
    return f"No receipt recorded yet on PO {po} for {what}. Nothing was selected."


def po_lookup_hold(
    po: str,
    lines: list[dict[str, Any]],
    *,
    po_in_kimco: bool,
    receipts: list[dict[str, Any]],
) -> str | None:
    """A purchase order with no receipt child still exists."""
    real = [row for row in receipts if row.get("id") and not row.get("placeholder")]
    if po_in_kimco and not real:
        return f"no receipt yet. {po_absence_reason(po, lines)}"
    if not po_in_kimco:
        return f"Purchase order {po} was not found. Nothing was selected."
    return None


def _labeled_number(vendor: str, text: str) -> str | None:
    if _is_oneal(vendor) or "ONEAL" in text.upper() or "O'NEAL" in text.upper():
        match = _ONEAL_INVOICE.search(text or "")
        if match:
            return match.group(1)
    match = _LABELED_INVOICE.search(text or "")
    if not match:
        return None
    number = match.group(1).strip().rstrip(".-")
    if number.lower() in {"date", "number", "no"}:
        return None
    return number


def _ship_to_codes(text: str) -> set[str]:
    return {match.group(1) for match in _SHIP_TO.finditer(text or "")}


def _total_of(row: dict[str, Any]) -> float | None:
    raw = row.get("amount")
    if raw is None:
        raw = row.get("total")
    if raw is None:
        return None
    return round(float(raw), 2)


def _day_of(row: dict[str, Any]) -> str:
    raw = row.get("day")
    if raw is None:
        raw = row.get("date") or row.get("invoice_date") or ""
    if hasattr(raw, "isoformat"):
        return raw.isoformat()[:10]
    return str(raw)[:10]


def _po_line_of(row: dict[str, Any]) -> str:
    if row.get("po_line"):
        return str(row["po_line"])
    lines = row.get("lines_match") or []
    if len(lines) == 1 and lines[0].get("token"):
        return str(lines[0]["token"])
    return ""


def number_repeats_across_invoices(
    vendor: str,
    number: str,
    cohort: list[dict[str, Any]],
    *,
    vendor_id: int | None = None,
    amount: float | None = None,
    po: str = "",
) -> bool:
    """True when this vendor used ``number`` on a different invoice."""
    probe = {"vendor": vendor, "vendor_id": vendor_id or 0}
    for row in cohort:
        if not same_vendor(probe, row):
            continue
        if str(row.get("number") or "") != str(number):
            continue
        other_amount = _total_of(row)
        other_po = str(row.get("po") or "")
        if other_amount != amount or other_po != str(po or ""):
            return True
    return False


def resolve_invoice_number(
    vendor: str,
    text: str = "",
    *,
    claimed: str | None = None,
    cohort: list[dict[str, Any]] | None = None,
    vendor_id: int | None = None,
    amount: float | None = None,
    po: str = "",
) -> dict[str, Any]:
    """Labeled invoice-number field per vendor. Ship-to codes are not invoice numbers."""
    cohort = list(cohort or [])
    labeled = _labeled_number(vendor, text) if text else None
    ship_to = _ship_to_codes(text)
    claimed_text = str(claimed).strip() if claimed else ""
    rejected = claimed_text if claimed_text and claimed_text != str(labeled or "") and claimed_text in CUSTOMER_SHIP_TO_CODES | ship_to else None
    number = labeled or claimed_text
    blocked = number in CUSTOMER_SHIP_TO_CODES or (number in ship_to and number != labeled)
    if blocked:
        shown = f" Labeled invoice number is {labeled}." if labeled and labeled != number else ""
        return {
            "ok": False,
            "number": None,
            "rejected": number,
            "reason": f"{number} is a customer ship-to code.{shown} It is not the invoice number.",
        }
    if not number:
        return {"ok": False, "number": None, "rejected": rejected, "reason": "No labeled invoice number was found."}
    if number_repeats_across_invoices(
        vendor, number, cohort, vendor_id=vendor_id, amount=amount, po=po
    ):
        return {
            "ok": False,
            "number": None,
            "rejected": number,
            "reason": (
                f"{number} repeats across different {vendor} invoices. "
                "It is not an invoice number."
            ),
        }
    reason = ""
    if rejected and labeled:
        reason = f"Ignored ship-to {rejected}. Invoice number is {labeled}."
    return {"ok": True, "number": number, "rejected": rejected, "reason": reason}


def classify_duplicate(candidate: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """Vendor + invoice number is a duplicate. Number alone is not.

    Same vendor, same total, and the same date or the same PO line, with a
    different invoice number, is a near-duplicate HOLD. It is not a skip.
    """
    number_only = False
    for row in existing:
        if row is candidate:
            continue
        same_number = str(row.get("number") or "") == str(candidate.get("number") or "") and str(candidate.get("number") or "")
        if same_number and not same_vendor(candidate, row):
            number_only = True
            continue
        if same_number and same_vendor(candidate, row):
            return {
                "action": "already",
                "kind": "duplicate",
                "reason": (
                    f"Duplicate of {candidate.get('vendor')} invoice {candidate.get('number')} "
                    "(vendor and invoice number)."
                ),
            }
        if not same_vendor(candidate, row):
            continue
        left_total = _total_of(candidate)
        right_total = _total_of(row)
        if left_total is None or right_total is None or left_total != right_total:
            continue
        same_day = _day_of(candidate) and _day_of(candidate) == _day_of(row)
        left_line = _po_line_of(candidate)
        same_line = bool(left_line) and left_line == _po_line_of(row)
        if same_day or same_line:
            return {
                "action": "hold",
                "kind": "near-duplicate",
                "reason": (
                    f"Near-duplicate of {row.get('vendor')} invoice {row.get('number')}: "
                    f"same vendor, same total ${left_total:,.2f}, and the same "
                    f"{'date' if same_day else 'PO line'}. Held, not skipped."
                ),
            }
    if number_only:
        return {
            "action": "enter",
            "kind": "number-only",
            "reason": "Invoice number matches a different vendor. Not a duplicate.",
        }
    return {"action": "enter", "kind": "distinct", "reason": ""}


def misc_versus_po(
    *,
    printed_po: str,
    po_in_kimco: bool,
    has_receipt: bool,
    proposed_kind: str = "misc",
) -> dict[str, Any]:
    """A real KIMCO PO printed on the invoice is never miscellaneous."""
    if printed_po and po_in_kimco:
        if not has_receipt:
            return {
                "kind": "po",
                "action": "hold",
                "owner": "shawn",
                "batch": "Transfer AP",
                "reason": (
                    f"Invoice prints KIMCO purchase order {printed_po}. "
                    "No receipt yet. Held in Transfer AP for Shawn. Not coded miscellaneous."
                ),
            }
        return {"kind": "po", "action": "enter", "owner": "", "batch": "", "reason": ""}
    return {"kind": proposed_kind, "action": "enter", "owner": "", "batch": "", "reason": ""}


def preflight_enter(job: dict[str, Any], cohort: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Invoice number, duplicate, and misc-vs-PO checks before a header is created."""
    others = [row for row in (cohort or []) if row is not job]
    number = resolve_invoice_number(
        str(job.get("vendor") or ""),
        str(job.get("page_text") or ""),
        claimed=str(job.get("number") or ""),
        cohort=others,
        vendor_id=int(job.get("vendor_id") or 0) or None,
        amount=_total_of(job),
        po=str(job.get("po") or ""),
    )
    if not number["ok"]:
        return {"action": "hold", "kind": "invoice-number", "reason": number["reason"], "owner": "shawn", "number": ""}
    resolved = dict(job)
    resolved["number"] = number["number"]
    duplicate = classify_duplicate(resolved, others)
    if duplicate["action"] in {"hold", "already"}:
        duplicate["number"] = number["number"]
        duplicate.setdefault("owner", "shawn")
        return duplicate
    printed = str(job.get("printed_po") or "")
    if str(job.get("kind") or "") == "misc" and printed and job.get("po_in_kimco"):
        coding = misc_versus_po(
            printed_po=printed,
            po_in_kimco=True,
            has_receipt=bool(job.get("has_receipt")),
            proposed_kind="misc",
        )
        if coding["action"] == "hold":
            coding["number"] = number["number"]
            coding["kind"] = "misc-po"
            return coding
    out = {"action": "enter", "kind": duplicate["kind"], "reason": number["reason"] or duplicate["reason"], "number": number["number"], "owner": ""}
    return out


def _page_money(raw: str) -> float | None:
    return money(str(raw).replace(",", "").replace("$", ""))


def printed_total_from_page(text: str) -> float | None:
    """Read the total off the rendered page. The typed amount is not an input."""
    oneal = list(_ONEAL_TOTAL.finditer(text or ""))
    if oneal:
        return _page_money(oneal[-1].group(2))
    labeled = list(_LABELED_TOTAL.finditer(text or ""))
    if not labeled:
        return None
    return _page_money(labeled[-1].group(1))


def qc_printed_total(*, page_text: str, typed_amount: float) -> dict[str, Any]:
    printed = printed_total_from_page(page_text)
    if printed is None:
        return {
            "ok": False,
            "printed_total": None,
            "reason": "Printed total was not on the rendered page. The typed amount was not used.",
        }
    typed = round(float(typed_amount), 2)
    if round(float(printed), 2) != typed:
        return {
            "ok": False,
            "printed_total": printed,
            "reason": (
                f"Printed total ${printed:,.2f} does not match the typed amount ${typed:,.2f}."
            ),
        }
    return {"ok": True, "printed_total": printed, "reason": ""}


def attachment_page_check(
    page_texts: list[str],
    invoice_number: str,
    other_numbers: list[str] | None = None,
) -> dict[str, Any]:
    """Every stored page shows this invoice number and no other bill's number."""
    problems: list[str] = []
    others = [str(number) for number in (other_numbers or []) if str(number) and str(number) != str(invoice_number)]
    for index, text in enumerate(page_texts, start=1):
        if str(invoice_number) not in text:
            problems.append(f"page {index} does not show {invoice_number}")
        for other in others:
            if other in text:
                problems.append(f"page {index} shows {other}")
    return {"ok": not problems, "reason": "; ".join(problems)}


def folder_is_archive(path: str | None) -> bool:
    return "archive" in str(path or "").lower()


def verify_move_against_kimco(move: dict[str, Any], bill: dict[str, Any] | None) -> dict[str, Any]:
    """Header + this bill's attachment + note. A hand-typed list is not proof."""
    if not bill:
        return {"ok": False, "reason": "No KIMCO bill for this move. A hand-typed list is not enough."}
    reasons: list[str] = []
    if str(bill.get("vendor") or "") != str(move.get("vendor") or "") or str(bill.get("invoice_number") or "") != str(
        move.get("invoice_number") or ""
    ):
        reasons.append("header does not match")
    bill_amount = money(bill.get("amount"))
    move_amount = money(move.get("amount"))
    if bill_amount is None or move_amount is None or round(float(bill_amount), 2) != round(float(move_amount), 2):
        reasons.append("header total does not match")
    if not bill.get("attachment_ok"):
        reasons.append("attachment is not this bill")
    note = str(bill.get("note") or "")
    if not bill.get("note_id") or not note.startswith("AP Clerk:"):
        reasons.append("note is missing")
    return {"ok": not reasons, "reason": "; ".join(reasons)}


def plan_email_moves(
    candidates: list[dict[str, Any]],
    *,
    kimco_bills: list[dict[str, Any]],
    cap: int | None = None,
) -> dict[str, Any]:
    """Cap the run, stop on the first failed verify, and never touch an archive folder."""
    limit = email_move_cap() if cap is None else int(cap)
    bills = {(str(bill.get("vendor")), str(bill.get("invoice_number"))): bill for bill in kimco_bills}
    planned: list[dict[str, Any]] = []
    allowed = 0
    stopped = False
    stop_reason = ""
    for index, candidate in enumerate(candidates):
        source = str(candidate.get("source_folder") or "")
        dest = str(candidate.get("dest_folder") or "")
        if folder_is_archive(source) or folder_is_archive(dest):
            reason = "Archive folders are not touched."
            planned.append({**candidate, "allow": False, "reason": reason})
            for rest in candidates[index + 1 :]:
                planned.append({**rest, "allow": False, "reason": "Stopped. Archive folders are not touched."})
            stopped = True
            stop_reason = reason
            break
        bill = bills.get((str(candidate.get("vendor")), str(candidate.get("invoice_number"))))
        check = verify_move_against_kimco(candidate, bill)
        if not check["ok"]:
            planned.append({**candidate, "allow": False, "reason": check["reason"]})
            for rest in candidates[index + 1 :]:
                planned.append({**rest, "allow": False, "reason": "Stopped after a failed verify."})
            stopped = True
            stop_reason = check["reason"]
            break
        if allowed >= limit:
            reason = f"Move cap {limit} reached."
            planned.append({**candidate, "allow": False, "reason": reason})
            for rest in candidates[index + 1 :]:
                planned.append({**rest, "allow": False, "reason": reason})
            stopped = True
            stop_reason = reason
            break
        planned.append({**candidate, "allow": True, "reason": ""})
        allowed += 1
    return {"moves": planned, "stopped": stopped, "stop_reason": stop_reason, "allowed_count": allowed}


def plan_from_run_rows(rows: list[dict[str, Any]], *, dest_folder: str, cap: int | None = None) -> dict[str, Any]:
    """Build a move plan from KIMCO readback rows, not from a hand-typed allow list."""
    candidates: list[dict[str, Any]] = []
    bills: list[dict[str, Any]] = []
    for row in rows:
        if row.get("result") not in {"PASS", "HOLD"} or not row.get("bill_id"):
            continue
        amount = row.get("header_total")
        if amount in (None, ""):
            amount = row.get("printed_total")
        candidates.append(
            {
                "received": row.get("received"),
                "vendor": row.get("vendor"),
                "invoice_number": row.get("invoice") or row.get("number"),
                "amount": amount,
                "source_folder": row.get("source_folder") or "Inbox",
                "dest_folder": dest_folder,
            }
        )
        bills.append(
            {
                "vendor": row.get("vendor"),
                "invoice_number": row.get("invoice") or row.get("number"),
                "amount": amount,
                "attachment_ok": bool(row.get("attachment_ok")),
                "note": row.get("note"),
                "note_id": row.get("note_id"),
            }
        )
    return plan_email_moves(candidates, kimco_bills=bills, cap=cap)


def find_orphan_bills(bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Headers created with no AP Clerk note. Xcaliber 10472 is the case."""
    orphans: list[dict[str, Any]] = []
    for bill in bills:
        if not bill.get("bill_id"):
            continue
        note = str(bill.get("note") or "")
        if bill.get("note_id") and note.startswith("AP Clerk:"):
            continue
        orphans.append(
            {
                "bill_id": bill.get("bill_id"),
                "invoice": bill.get("invoice") or bill.get("number") or "",
                "vendor": bill.get("vendor") or "",
                "reason": "Bill was created and has no AP Clerk note.",
            }
        )
    return orphans


def end_of_run_report(bills: list[dict[str, Any]]) -> dict[str, Any]:
    orphans = find_orphan_bills(bills)
    return {"orphans": orphans, "orphan_count": len(orphans), "ok": not orphans}


def intake_action(kind: str) -> dict[str, Any]:
    """Statements, autopay, and other non-invoices are not entered."""
    key = str(kind or "").strip().lower().replace("_", "-")
    if key in _NON_INVOICE_KINDS:
        return {"enter": False, "kind": key, "reason": f"{key} is not an invoice."}
    if key == "invoice":
        return {"enter": True, "kind": key, "reason": ""}
    return {"enter": False, "kind": key or "unknown", "reason": "Not an invoice."}


def purchase_gl_hold(gl_value: Any) -> dict[str, Any]:
    """A blank Purchase_GL_Account is not a hold.

    The API add stores the miscellaneous item and leaves the account null.
    The item default is written when a person posts. See
    ``docs/misc-gl-diagnosis-2026-10-06.md``.
    """
    blank = gl_value in (None, "", {}, [])
    return {
        "hold": False,
        "blank": blank,
        "reason": "" if not blank else "Purchase_GL_Account is blank until a person posts. Not a hold.",
    }


def unifirst_tax_gap(*, vendor: str, printed_total: float, covered: float, tax_gap: float) -> dict[str, Any]:
    """A UniFirst cents tax difference is purchase price variance, not a hold."""
    if "unifirst" not in str(vendor or "").lower():
        return {"action": "unchanged", "ppv": 0.0, "reason": ""}
    gap = round(float(tax_gap), 2)
    cap = ppv_limit()
    if abs(gap) >= cap:
        return {
            "action": "hold",
            "ppv": 0.0,
            "reason": f"UniFirst tax gap ${abs(gap):,.2f} is ${cap:,.2f} or more.",
        }
    if abs(gap) >= 0.005 and abs(gap) < 1:
        return {
            "action": "ppv",
            "ppv": gap,
            "reason": f"UniFirst tax gap ${gap:,.2f} is purchase price variance.",
        }
    if abs(round(float(printed_total), 2) - round(float(covered), 2)) < 0.005:
        return {"action": "match", "ppv": 0.0, "reason": ""}
    return {"action": "unchanged", "ppv": 0.0, "reason": ""}
