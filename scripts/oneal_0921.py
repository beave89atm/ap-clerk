"""Enter the first 5 unprocessed O'Neal Steel invoices on a dedicated batch.

Create `API Agent - M/D/YY O'Neal` (America/Chicago). Never reuse McMaster 721,
Gas 720, Legacy 717, JPSteel 716, Crosslink 715, or Transfer AP.

O'Neal only. invent=false. No Mail.Send.

NOTE-45: missing_receipt stays on the O'Neal batch. Comments_1 names Ruben Perez;
mention-id is unproven — plain text, no invented mention-node.
Over-PPV → Transfer AP + Comments_1 @Shawn 104. Fees = id 11 (NOTE-44).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from aqpc_plus4 import summarize_parse  # noqa: E402
from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.cli import (  # noqa: E402
    _find_or_create_batch,
    _optional_graph_client,
    _print_summary,
    run_enter,
)
from ap_clerk.comments_tab import (  # noqa: E402
    EXCEPTION_MAIL_SEND,
    RUBEN_MENTION,
    RUBEN_MENTION_ID,
    RUBEN_MENTION_NAME,
    SHAWN_MENTION,
    apply_missing_receipt_comment_tab,
)
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged  # noqa: E402
from ap_clerk.inbox import sender_address, sender_name  # noqa: E402
from ap_clerk.kimco import (  # noqa: E402
    FEE_CHARGE_LOOKUP_ID,
    KimcoClient,
    KimcoError,
    fees_posted_cover_parsed,
    fees_with_amounts,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.quality_v12 import apply_exception_category_owner  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    blocked_400_not_a_hold_when_same_item_cover,
    chicago_today,
    invoice_number_key,
    lookup_id,
    lookup_text,
    money,
    names_match,
    non_receipt_dollars_are_additional_charge_fees,
    parse_iso_date,
)
from jpsteel_0916 import load_list_receipts  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    apply_over_ppv_transfer_ap,
    finish_hold_header,
    format_receipts,
    is_missing_receipt_hold,
    is_over_ppv_price_hold,
    mcmaster_proof,
)

LOGGER = logging.getLogger("ap_clerk.oneal_0921")

VENDOR_NAME = "O'Neal Steel - Dallas (GP)"
VENDOR_TOKENS = ("o'neal", "oneal", "o-neal", "onealsteel")
OTHER_STEEL = (
    "mcmaster",
    "gas & supply",
    "gas and supply",
    "legacy wire",
    "jp steel",
    "jpsteel",
    "morgan steel",
    "leeco",
    "ryerson",
    "emj",
)
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
_ONEAL_INV = re.compile(r"\b(15\d{6})\b")
_KIMCO_PO = re.compile(r"^5[7-9]\d{3}$")
NOISE_SUBJECT = re.compile(
    r"statement|past due|account with us|remittance|payment reminder|"
    r"payment status|payment confirmation|pending payment|"
    r"reminder for payment|market informer|offset notice",
    flags=re.I,
)
CREDIT_SUBJECT = re.compile(
    r"\bcredit from your order\b|\bplease deduct credit\b|\bcredit memo\b|"
    r"\bcredit invoice\b",
    flags=re.I,
)

FORBIDDEN_BATCH_IDS = {375, 715, 716, 717, 720, 721}
FORBIDDEN_REUSE_NAMES = {
    "TRANSFER AP",
    "API Agent - 9/16/26",
    "API Agent - 9/16/26 JPSteel",
    "API Agent - 9/16/26-2",
    "API Agent - 9/17/26",
    "API Agent - 9/17/26 Legacy Wire",
    "API Agent - 9/17/26 Gas & Supply",
    "API Agent - 9/18/26 McMaster",
}

CREATED_HEADERS: dict[str, int] = {}
LEAVE_ALONE_HOLD_IDS: set[int] = set()
DO_NOT_MUTATE_IDS: set[int] = set()


def preferred_batch_name(day: date | None = None) -> str:
    day = day or chicago_today()
    yy = day.strftime("%y")
    return f"API Agent - {day.month}/{day.day}/{yy} O'Neal"


def exact_invoice_number(value: Any) -> str:
    text = str(value or "")
    match = _ONEAL_INV.search(text)
    return match.group(1) if match else ""


def is_oneal_vendor_text(text: str) -> bool:
    blob = (text or "").lower()
    if any(tok in blob for tok in OTHER_STEEL):
        return False
    return any(tok in blob for tok in VENDOR_TOKENS)


def blob_has_oneal(*parts: str) -> bool:
    return is_oneal_vendor_text(" ".join(parts))


def is_oneal_message(msg: dict[str, Any]) -> bool:
    from_name = sender_name(msg)
    from_addr = sender_address(msg)
    subject = str(msg.get("subject") or "")
    return blob_has_oneal(from_name, from_addr, subject)


def is_oneal_invoice_email(msg: dict[str, Any]) -> bool:
    subject = str(msg.get("subject") or "")
    if NOISE_SUBJECT.search(subject) or CREDIT_SUBJECT.search(subject):
        return False
    return is_oneal_message(msg)


def is_credit_bill(bill: dict[str, Any]) -> bool:
    subject = str(bill.get("subject") or "")
    return bool(CREDIT_SUBJECT.search(subject) or bill.get("credit"))


def create_oneal_batch(client: KimcoClient, name: str) -> dict[str, Any]:
    """New dedicated O'Neal batch. Never reuse forbidden vendor/Transfer AP batches."""
    batches = client.list_items("ap_batches")
    for item in batches:
        got = str((item.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
        bid = item.get("id")
        if got == name:
            if bid in FORBIDDEN_BATCH_IDS or got in FORBIDDEN_REUSE_NAMES:
                raise KimcoError(f"Refusing forbidden batch id={bid} name={got}")
            vals = item.get("values") or {}
            return {
                "id": bid,
                "name": got,
                "created": False,
                "status": vals.get("Status"),
                "unposted": vals.get("Unposted_Count"),
            }
    created = _find_or_create_batch(client, batches, name)
    bid = created.get("id")
    got_name = created.get("name") or name
    if bid in FORBIDDEN_BATCH_IDS or got_name in FORBIDDEN_REUSE_NAMES:
        raise KimcoError(f"Refusing forbidden batch id={bid} name={got_name}")
    if got_name != name:
        raise KimcoError(f"Created batch name {got_name!r} != {name!r}")
    created["status"] = 0
    return created


def verify_batch(client: KimcoClient, batch_id: int, expected_name: str) -> dict[str, Any]:
    batch = client.get_item("ap_batches", batch_id)
    vals = batch.get("values") or {}
    name = vals.get("AP_Invoice_Batch_ID")
    return {
        "id": batch.get("id"),
        "name": name,
        "status": vals.get("Status"),
        "unposted": vals.get("Unposted_Count"),
        "matches_expected": name == expected_name,
        "is_forbidden": batch.get("id") in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_REUSE_NAMES,
    }


def confirm_oneal_vendor(client: KimcoClient) -> dict[str, Any]:
    """Live vendor id from existing KIMCO invoices. invent=false."""
    samples: list[dict[str, Any]] = []
    entered: dict[str, int] = {}
    list_hits: list[tuple[int, str, str]] = []
    list_texts: set[str] = set()
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        vendor_txt = str(
            lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name")) or ""
        )
        vendor_id = lookup_id(vals.get("Vendor"))
        if not is_oneal_vendor_text(vendor_txt):
            continue
        number = exact_invoice_number(vals.get("Invoice_Number")) or invoice_number_key(
            vals.get("Invoice_Number")
        )
        kid = item.get("id")
        if number and kid not in (None, ""):
            entered[number] = int(kid)
            list_hits.append((int(kid), number, vendor_txt))
            list_texts.add(vendor_txt)
        if vendor_id not in (None, ""):
            samples.append(
                {
                    "invoice_id": kid,
                    "invoice_number": number,
                    "vendor_id": int(vendor_id),
                    "vendor_text": vendor_txt,
                    "source": "list",
                }
            )
    list_hits.sort(reverse=True)
    for kid, number, vendor_txt in list_hits[:8]:
        if any(s.get("invoice_id") == kid and s.get("source") == "record" for s in samples):
            continue
        try:
            rec = client.get_item("ap_invoices", kid)
        except KimcoError:
            continue
        vals = rec.get("values") or {}
        posted = lookup_id(vals.get("Vendor"))
        posted_txt = str(lookup_text(vals.get("Vendor")) or vendor_txt)
        if posted in (None, "") or not is_oneal_vendor_text(posted_txt):
            continue
        samples.append(
            {
                "invoice_id": kid,
                "invoice_number": number,
                "vendor_id": int(posted),
                "vendor_text": posted_txt,
                "source": "record",
            }
        )
    record_samples = [s for s in samples if s.get("source") == "record"]
    counts = Counter(s["vendor_id"] for s in (record_samples or samples))
    confirmed = None
    if len(counts) == 1:
        confirmed = next(iter(counts))
    elif counts:
        top_id, top_n = counts.most_common(1)[0]
        second_n = counts.most_common(2)[1][1] if len(counts) > 1 else 0
        if top_n > second_n:
            confirmed = top_id
    return {
        "vendor_id": confirmed,
        "vendor_text_samples": sorted(list_texts | {s["vendor_text"] for s in samples}),
        "id_counts": dict(counts),
        "samples": [s for s in samples if s.get("source") == "record"][:12] or samples[:12],
        "entered": entered,
        "invent": False,
        "ruben_mention_id": RUBEN_MENTION_ID,
    }


def find_oneal_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = ["O'Neal Steel", "ONeal Steel", "onealsteel", "O'Neal"]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_oneal_message(msg):
                continue
            mid = str(msg.get("id") or "")
            if mid:
                seen[mid] = msg
    try:
        listed = graph.list_messages(
            ALLOWED_MAILBOX,
            received_from=date(2026, 8, 1),
            oldest_first=False,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("Graph list from 2026-08-01 failed: %s", type(exc).__name__)
        listed = []
    for msg in listed:
        if not is_oneal_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_iso_date(str(value)[:10])


def _flatten_bills(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    bills = [parsed]
    for sib in parsed.get("siblings") or []:
        if isinstance(sib, dict):
            bills.append(sib)
    return bills


def bill_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> list[dict[str, Any]]:
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    wanted = exact_invoice_number(subject)
    pdfs = []
    if message.get("hasAttachments"):
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id)
    if not pdfs:
        return [
            {
                "vendor": VENDOR_NAME,
                "invoice_number": wanted,
                "date": None,
                "po": None,
                "amount": None,
                "hold_reason": "parse-error",
                "action": "hold",
                "graph_message_id": message_id,
                "subject": subject,
                "receivedDateTime": message.get("receivedDateTime"),
                "from_name": from_name,
                "pdf_unavailable": True,
            }
        ]
    filename, content = pdfs[0]
    dest = pdf_dir / (
        f"{str(message.get('receivedDateTime') or '')[:10]}_"
        f"{re.sub(r'[^A-Za-z0-9._-]+', '_', filename)}"
    )
    dest.write_bytes(content)
    parsed = parse_invoice_pdf(
        dest, subject=subject, from_name=from_name, from_address=from_addr
    )
    out: list[dict[str, Any]] = []
    for bill in _flatten_bills(parsed):
        bill = dict(bill)
        bill["pdf_path"] = str(bill.get("pdf_path") or dest)
        bill["graph_message_id"] = message_id
        bill["subject"] = subject
        bill["receivedDateTime"] = message.get("receivedDateTime")
        bill["from_name"] = from_name
        bill["action"] = "create"
        bill["id"] = message_id
        bill["download_method"] = "attachment"
        bill["pdf_bytes"] = len(content)
        if not names_match(VENDOR_NAME, str(bill.get("vendor") or "")):
            if is_oneal_vendor_text(str(bill.get("vendor") or "")) or blob_has_oneal(
                str(bill.get("vendor") or ""), from_name, subject
            ):
                bill["vendor"] = VENDOR_NAME
        if not exact_invoice_number(bill.get("invoice_number")) and wanted:
            bill["invoice_number"] = wanted
        out.append(bill)
    return out


def catalog_row(msg: dict[str, Any], entered: dict[str, int]) -> dict[str, Any]:
    inv = exact_invoice_number(str(msg.get("subject") or ""))
    return {
        "invoice": inv,
        "received": msg.get("receivedDateTime"),
        "flagged": is_already_flagged(msg),
        "categories": msg.get("categories") or [],
        "already_kimco": entered.get(inv),
        "hasAttachments": bool(msg.get("hasAttachments")),
        "from": sender_name(msg),
        "subject": str(msg.get("subject") or "")[:140],
    }


def pick_recent(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    recent: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    credits: list[dict[str, Any]] = []
    for bill in parsed_bills:
        if is_credit_bill(bill):
            credits.append(bill)
            continue
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in already:
            continue
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            older.append(bill)
            continue
        recent.append(bill)
    recent.sort(
        key=lambda b: (str(b.get("date") or ""), str(b.get("receivedDateTime") or "")),
        reverse=True,
    )
    return recent[:cap], older + recent[cap:], credits


def leftover_from_catalog(
    parsed_bills: list[dict[str, Any]],
    *,
    chosen: set[str],
    older: list[dict[str, Any]],
    credits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bill in older + credits + [
        b for b in parsed_bills if exact_invoice_number(b.get("invoice_number")) not in chosen
    ]:
        inv = exact_invoice_number(bill.get("invoice_number")) or str(bill.get("invoice_number") or "")
        key = inv or str(bill.get("subject") or "")[:40]
        if key in seen or inv in chosen or inv in CREATED_HEADERS:
            continue
        seen.add(key)
        why = "unflagged leftover after cap 5; not entered"
        if is_credit_bill(bill):
            why = "credit memo / not entered as invoice"
        inv_date = _parse_date(bill.get("date"))
        if inv_date is not None and inv_date < MIN_INVOICE_DATE:
            why = "too-old (before 2026-08-01); not entered"
        pending.append(
            {
                "invoice_number": inv,
                "po": bill.get("po"),
                "date": bill.get("date"),
                "amount": bill.get("amount"),
                "received": bill.get("receivedDateTime"),
                "subject": str(bill.get("subject") or "")[:120],
                "why": why,
            }
        )
    return pending


def over_ppv_hold_comment(
    *,
    invoice_number: str,
    po: str | None,
    pdf_amount: Any,
) -> str:
    amt = money(pdf_amount)
    amt_txt = f"{amt:.2f}" if amt is not None else "unknown"
    inv = exact_invoice_number(invoice_number) or str(invoice_number or "")
    return (
        f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on O'Neal Steel {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Leftover vs invoice line is over the "
        "PPV gate. Receipts were NOT selected so purchasing can unreceive, "
        "change the PO price, and re-receive. Do not alter receipt unit price in GI."
    )


def missing_receipt_hold_comment(
    *,
    invoice_number: str,
    po: str | None,
    pdf_amount: Any,
) -> str:
    amt = money(pdf_amount)
    amt_txt = f"{amt:.2f}" if amt is not None else "unknown"
    inv = exact_invoice_number(invoice_number) or str(invoice_number or "")
    return (
        f"{RUBEN_MENTION_NAME} HOLD (receipt) on O'Neal Steel {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Open leftover merch is not received "
        "or does not match PDF lines. Header stays on the current API Agent "
        "O'Neal batch — do not Transfer AP. Receiving / purchasing: receive the "
        "PO so AP can Select Receipts. Comments_1 mention-id for Ruben Perez is "
        "not proven (invent=false). No email."
    )


def _qty_hold(parsed: dict[str, Any], recs: list[dict[str, Any]]) -> bool:
    lines = list(parsed.get("lines") or [])
    if not lines:
        return not recs
    return len(recs) < len([ln for ln in lines if money(ln.get("amount") or ln.get("qty"))])


def quality_oneal_row(
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    proof: dict[str, Any],
    finish: dict[str, Any] | None,
    vendor_id: int | None,
    batch_label: str,
) -> dict[str, Any]:
    """Success only when Treyce would not rework. invent=false."""
    out = dict(enter_row)
    kid = proof.get("id") or enter_row.get("KIMCO id")
    pdf_amt = money(parsed.get("amount"))
    posted = money(proof.get("invoice_amount"))
    ver = money(proof.get("verification_amount") or proof.get("verification"))
    recs = proof.get("receipt_lines") or []
    attach_ok = bool(proof.get("attachments"))
    message_id = str(parsed.get("graph_message_id") or "")
    extra = ""
    if finish and finish.get("wanted"):
        extra = (
            f"Finish Select Receipts {finish.get('select_status')} "
            f"ids={finish.get('wanted')} "
            f"fees={finish.get('fee_status')} "
            f"ppv={finish.get('ppv_status')}. "
        )
    rec_merch = 0.0
    for rec in recs:
        q = money(rec.get("qty"))
        u = money(rec.get("unit"))
        if q is not None and u is not None:
            rec_merch = round(rec_merch + q * u, 2)
    fee_amts = list(proof.get("fee_amounts") or [])
    ppv_amts = list(proof.get("ppv_amounts") or [])
    parsed_fees = list(parsed.get("fees") or [])
    qty_hold = _qty_hold(parsed, recs) if recs or parsed.get("lines") else bool(parsed.get("po"))
    if qty_hold and non_receipt_dollars_are_additional_charge_fees(
        pdf_amount=pdf_amt,
        receipt_lines=recs,
        posted_fee_amounts=fee_amts,
        parsed_fees=parsed_fees,
    ):
        qty_hold = False
    fees_ok = fees_posted_cover_parsed(fee_amts, parsed_fees) if parsed_fees else True
    fee_on_ppv = False
    for fee in fees_with_amounts(parsed_fees):
        for amt in ppv_amts:
            if amt is not None and abs(amt - fee["amount"]) <= 0.02:
                fee_on_ppv = True
    charge_sum = round(sum(a or 0 for a in fee_amts) + sum(a or 0 for a in ppv_amts), 2)
    rolled = round(rec_merch + charge_sum, 2)
    amount_ok = False
    if pdf_amt is not None:
        if posted is not None and abs(posted - pdf_amt) <= 0.02:
            amount_ok = True
        elif ver is not None and abs(ver - pdf_amt) <= 0.02 and abs(rolled - pdf_amt) <= 0.02:
            amount_ok = True
    price_hold = bool((finish or {}).get("select_zero") or (finish or {}).get("skipped_over_ppv"))
    vendor_ok = vendor_id not in (None, "") and proof.get("vendor_id") == vendor_id
    po = str(parsed.get("po") or enter_row.get("PO") or "").strip()
    needs_receipts = bool(po)
    already_posted = bool(recs) and amount_ok and (not qty_hold if needs_receipts else True)
    select_status = (finish or {}).get("select_status")
    select_ok = already_posted or select_status in {None, "selected", "already-selected"}
    cover_blocked = blocked_400_not_a_hold_when_same_item_cover(
        select_status, (finish or {}).get("matched")
    )
    posted_number = exact_invoice_number(proof.get("invoice_number") or enter_row.get("Invoice #"))
    pdf_number = exact_invoice_number(parsed.get("invoice_number") or enter_row.get("Invoice #"))
    number_ok = bool(pdf_number) and posted_number == pdf_number
    missing_po = bool(parsed.get("po") in (None, "")) or (
        po and not _KIMCO_PO.fullmatch(invoice_number_key(po) or "")
    )
    finished = (
        attach_ok
        and (recs if needs_receipts else True)
        and (not qty_hold if needs_receipts else True)
        and fees_ok
        and not fee_on_ppv
        and amount_ok
        and (proof.get("invoice_type") == 3 if needs_receipts else proof.get("invoice_type") in {3, 4})
        and vendor_ok
        and not price_hold
        and (select_ok or cover_blocked)
        and number_ok
        and not missing_po
    )
    out["Amount"] = pdf_amt
    out["KIMCO id"] = kid
    out["Invoice #"] = pdf_number or enter_row.get("Invoice #")
    out["PO"] = po
    out["Receipts"] = format_receipts(proof)
    if parsed_fees:
        out["Fees and surcharges"] = ", ".join(
            f"{f.get('name')} {f.get('amount')}" for f in parsed_fees
        )
    else:
        out["Fees and surcharges"] = enter_row.get("Fees and surcharges") or "none"
    if (finish or {}).get("ppv_amount"):
        out["PPV"] = f"{float(finish['ppv_amount']):.2f}"
    elif ppv_amts:
        out["PPV"] = ", ".join(f"{a:.2f}" for a in ppv_amts)
    else:
        out["PPV"] = enter_row.get("PPV") or "none"
    out["Attach status"] = "attached" if attach_ok else enter_row.get("Attach status") or ""
    out["Flag in Outlook"] = "Yes" if message_id else enter_row.get("Flag in Outlook") or "No"
    out["Notes"] = ""
    out["Batch"] = enter_row.get("Batch") or batch_label
    if finished:
        out["Result"] = "Success"
        out["Why"] = (
            f"Finished bill (Invoice_Type {proof.get('invoice_type')}). "
            f"Header PO set={po or 'none'}. Select Receipts "
            f"{format_receipts(proof)} on PO {po or 'n/a'}. "
            f"Invoice #={pdf_number} (exact PDF). "
            f"Fees={out['Fees and surcharges']} (Additional Charge Fees id "
            f"{FEE_CHARGE_LOOKUP_ID}, not missing merch lines / not PPV). "
            f"PPV={out['PPV']}. Attach status=attached. {extra}"
            "Flag status=entered-in-ai."
        )
        out["Flag status"] = "entered-in-ai"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, message_id)
    elif price_hold:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (price-does-not-match): leftover vs invoice line is over the "
            f"PPV gate. Do not Select Receipts on that line. {SHAWN_MCKIBBEN}: "
            "purchasing must unreceive, change the PO price, and re-receive. "
            f"{extra}Outlook AI HOLD. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_hold(ALLOWED_MAILBOX, message_id)
    elif qty_hold:
        out["Result"] = "HOLD"
        if not recs:
            out["Why"] = (
                f"HOLD (receipt): no open receipt leftover on PO {po or 'n/a'} "
                f"for O'Neal invoice #{pdf_number}. Do not first-open guess. "
                f"{RUBEN_MENTION_NAME}: receiving must receive the PO so AP can "
                "Select Receipts. Comments_1 names Ruben Perez (mention-id "
                "unproven). Do not Transfer AP. "
                f"{extra}Outlook AI HOLD. Flag status=entered-with-issues."
            )
        else:
            out["Why"] = (
                f"HOLD (receipt): PDF merch vs selected ({format_receipts(proof)}) "
                f"on PO {po or 'n/a'}. Do not invent Success. "
                f"{RUBEN_MENTION_NAME}: receiving leftover merch. Comments_1 "
                "names Ruben Perez (mention-id unproven). Do not Transfer AP. "
                f"{extra}Outlook AI HOLD. Flag status=entered-with-issues."
            )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_hold(ALLOWED_MAILBOX, message_id)
    else:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD: not Success — amount_ok={amount_ok} attach={attach_ok} "
            f"vendor_ok={vendor_ok} number_ok={number_ok} fees_ok={fees_ok} "
            f"select={select_status}. Do not invent Success. {extra}"
            "Outlook AI HOLD."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_hold(ALLOWED_MAILBOX, message_id)
    return apply_exception_category_owner(out)


def finish_entered_rows(
    client: KimcoClient,
    graph,
    *,
    parsed_bills: list[dict[str, Any]],
    enter_rows: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    vendor_id: int | None,
    batch_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = exact_invoice_number(enter_row.get("Invoice #"))
        kid = enter_row.get("KIMCO id")
        parsed = by_inv.get(inv) or {}
        result = str(enter_row.get("Result") or "")
        if kid in (None, "") or not parsed:
            rows.append(apply_exception_category_owner(dict(enter_row)))
            continue
        if result in {"Fail", "Skipped"}:
            rows.append(enter_row)
            continue
        try:
            kid_int = int(kid)
        except (TypeError, ValueError):
            kid_int = None
        if kid_int in LEAVE_ALONE_HOLD_IDS | DO_NOT_MUTATE_IDS:
            rows.append(enter_row)
            continue
        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=int(kid), receipts=receipts
        )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or mcmaster_proof(client, kid)
        row = quality_oneal_row(
            graph,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
            batch_label=batch_label,
        )
        if is_over_ppv_price_hold(row, finish):
            comment = over_ppv_hold_comment(
                invoice_number=inv,
                po=str(parsed.get("po") or enter_row.get("PO") or ""),
                pdf_amount=parsed.get("amount") or enter_row.get("Amount"),
            )
            transfer = apply_over_ppv_transfer_ap(client, kimco_id=int(kid), comment=comment)
            finishes[inv]["transfer_ap"] = transfer
            mention = transfer.get("mention_notify") or {}
            if transfer.get("status") == "moved":
                row["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
                proof = mcmaster_proof(client, kid)
            row["Why"] = (
                f"{row.get('Why')} Transfer AP status={transfer.get('status')} "
                f"batch_id={transfer.get('batch_id')} "
                f"comment={transfer.get('comment')!r} "
                f"@mention={mention.get('report') or mention}."
            )
        elif is_missing_receipt_hold(row):
            comment = missing_receipt_hold_comment(
                invoice_number=inv,
                po=str(parsed.get("po") or enter_row.get("PO") or ""),
                pdf_amount=parsed.get("amount") or enter_row.get("Amount"),
            )
            notify = apply_missing_receipt_comment_tab(
                client,
                invoice_id=int(kid),
                body=comment,
                mention=RUBEN_MENTION,
            )
            finishes[inv]["missing_receipt_comment"] = notify
            row["Why"] = (
                f"{row.get('Why')} Comments_1 missing_receipt "
                f"status={notify.get('status')} tab={notify.get('report')} "
                f"transfer_ap=False mention_id={RUBEN_MENTION_ID!r} "
                f"plain={notify.get('plain')} mail_send={EXCEPTION_MAIL_SEND}."
            )
        rows.append(apply_exception_category_owner(row))
        receipts = load_list_receipts(client)
    return rows, finishes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter first 5 O'Neal bills on a dedicated batch")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--create-batch-only", action="store_true")
    parser.add_argument(
        "--report",
        default="",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    day = chicago_today()
    batch_name = preferred_batch_name(day)
    report_path = Path(args.report) if args.report else ROOT / "runs" / f"AP-run-{day.isoformat()}-oneal.xlsx"

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(
        f"Target: live. O'Neal only. New batch {batch_name!r}. "
        "Do not touch Gas/McMaster/Legacy/Transfer AP. No Mail.Send. invent=false.",
        flush=True,
    )
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    graph = _optional_graph_client()
    if graph is None:
        print("Graph authenticate failed", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    vendor_info = confirm_oneal_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    entered = dict(vendor_info.get("entered") or {})
    print(
        json.dumps(
            {
                "vendor_id": vendor_id,
                "entered_count": len(entered),
                "ruben_mention_id": RUBEN_MENTION_ID,
                "invent": False,
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if vendor_id in (None, ""):
        print("Live O'Neal vendor id not confirmed. Will not invent. Stop.", flush=True)
        return 2

    if args.create_batch_only:
        batch = create_oneal_batch(client, batch_name)
        verified = verify_batch(client, int(batch["id"]), batch_name)
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden") or not verified.get("matches_expected"):
            print("Created/found batch is forbidden or name mismatch — abort.", flush=True)
            return 2
        return 0

    messages = find_oneal_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(key=lambda r: str(r.get("received") or ""), reverse=True)
    already = set(entered) | set(CREATED_HEADERS)
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    for msg in messages:
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_oneal_invoice_email(msg):
            continue
        candidates.append(msg)
    candidates.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    print(
        json.dumps(
            {
                "discovered": len(catalog),
                "unflagged_invoice_emails": [
                    {
                        "invoice": exact_invoice_number(str(m.get("subject") or "")),
                        "received": m.get("receivedDateTime"),
                        "subject": str(m.get("subject") or "")[:120],
                    }
                    for m in candidates
                ],
                "skipped_flagged": skipped_flagged,
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.discover_only:
        return 0

    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    parsed_bills: list[dict[str, Any]] = []
    for msg in candidates:
        parsed_bills.extend(bill_from_message(graph, msg, pdf_dir))
    parsed_bills = [
        b for b in parsed_bills if exact_invoice_number(b.get("invoice_number")) not in already
    ]
    print(json.dumps({"parsed_candidates": [summarize_parse(inv) for inv in parsed_bills]}, indent=2, default=str), flush=True)

    try:
        receipts = load_list_receipts(client)
    except KimcoError:
        receipts = []
    recent, older, credits = pick_recent(parsed_bills, already=already, cap=CAP)
    print(
        json.dumps(
            {
                "recent_picked": [summarize_parse(b) for b in recent],
                "older_or_unclean_leftover": [summarize_parse(b) for b in older],
                "credits_skipped": [summarize_parse(b) for b in credits],
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    leftover_pending = leftover_from_catalog(
        parsed_bills,
        chosen={exact_invoice_number(b.get("invoice_number")) for b in recent},
        older=older,
        credits=credits,
    )
    if args.parse_only:
        return 0
    if not recent:
        print("No remaining unflagged O'Neal invoices on/after 2026-08-01. Stop.", flush=True)
        write_report(report_path, [])
        return 0

    batch = create_oneal_batch(client, batch_name)
    verified = verify_batch(client, int(batch["id"]), batch_name)
    print(json.dumps({"batch": verified}, indent=2, default=str), flush=True)
    if (
        verified.get("is_forbidden")
        or not verified.get("matches_expected")
        or int(verified.get("id") or 0) in FORBIDDEN_BATCH_IDS
    ):
        print("Refusing to enter: batch name/id mismatch or forbidden.", flush=True)
        return 2
    batch_label = f"{verified.get('name')} ({verified.get('id')})"

    enter_rows = run_enter(
        client,
        recent,
        batch_name=batch_name,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    for row in enter_rows:
        kid = row.get("KIMCO id")
        try:
            kid_int = int(kid) if kid not in (None, "") else None
        except (TypeError, ValueError):
            kid_int = None
        if kid_int in FORBIDDEN_BATCH_IDS:
            print(f"Refusing to finish header on forbidden id {kid_int}. Abort.", flush=True)
            return 2

    receipts = load_list_receipts(client)
    rows, finishes = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=int(vendor_id),
        batch_label=batch_label,
    )
    created = {
        exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id")
        for r in rows
        if r.get("KIMCO id") not in (None, "")
    }
    CREATED_HEADERS.update({k: int(v) for k, v in created.items() if v not in (None, "")})
    write_report(report_path, rows)
    sidecar = {
        "proof": "oneal-0921",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": vendor_id,
        "ruben_mention_id": RUBEN_MENTION_ID,
        "batch_name": batch_name,
        "batch_id": verified.get("id"),
        "batch": verified,
        "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
        "created_headers": created,
        "discovered": len(catalog),
        "catalog": catalog,
        "kimco_already": entered,
        "recent_picked": [summarize_parse(b) for b in recent],
        "finishes": finishes,
        "leftover_pending": leftover_pending,
        "rows": rows,
        "note45": (
            "missing_receipt Comments_1 names Ruben Perez, mention-id unproven, "
            "plain text, no Transfer AP; over-PPV Transfer AP + @Shawn; no Mail.Send"
        ),
    }
    report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    _print_summary(rows)
    print(
        json.dumps(
            {
                "batch_id": verified.get("id"),
                "batch_name": batch_name,
                "sheet": str(report_path),
                "ruben_mention_id": RUBEN_MENTION_ID,
                "new": [
                    {
                        "invoice": r.get("Invoice #"),
                        "result": r.get("Result"),
                        "amount": r.get("Amount"),
                        "po": r.get("PO"),
                        "date": r.get("date"),
                        "kimco": r.get("KIMCO id"),
                        "why": r.get("Why"),
                        "receipts": r.get("Receipts"),
                        "fees": r.get("Fees and surcharges"),
                        "ppv": r.get("PPV"),
                        "batch": r.get("Batch"),
                    }
                    for r in rows
                ],
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
