"""Enter up to 5 recent unflagged McMaster-Carr invoices.

Dedicated live batch — do NOT reuse Crosslink 715, JPSteel 716, Legacy 717,
or Gas 720. Prefer name `API Agent - 9/18/26 McMaster`.

PDF attachments only. No Mail.Send. invent=false.

Skip already-flagged mail, credits, and invoices already on the live
McMaster vendor. Prefer newest clean PO+receipt matches dated on/after
2026-08-01 from the 23-PO unflagged census set. Do not walk pre-Aug
(NOTE-28).
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

from aqpc_plus4 import live_get_proof, summarize_parse  # noqa: E402
from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.cli import _find_or_create_batch, _optional_graph_client, _print_summary, run_enter  # noqa: E402
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
from ap_clerk.comments_tab import (  # noqa: E402
    EXCEPTION_MAIL_SEND,
    SHAWN_MENTION,
    add_invoice_comment_tab,
    apply_missing_receipt_comment_tab,
    comment_tab_proof,
    transfer_ap_batch_only_payload,
)
from ap_clerk.quality_v12 import apply_exception_category_owner  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    TRANSFER_AP_BATCH_NAME,
    blocked_400_not_a_hold_when_same_item_cover,
    distinctive_vendor_tokens,
    extract_subject_pos,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    non_receipt_dollars_are_additional_charge_fees,
    names_match,
    normalize_receipt,
    parse_iso_date,
    receipt_select_refs,
    rounding_ppv_to_hit_pdf_total,
)
from jpsteel_0916 import (  # noqa: E402
    _is_fee_charge,
    _is_ppv_charge,
    _qty_hold,
    _receipt_ids_from_proof,
    charges_from_item,
    format_receipts,
    hydrate_receipts,
    load_list_receipts,
    open_receipts_on_po,
    ppv_total_from_selectable,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_0918")

PREFERRED_BATCH_NAME = "API Agent - 9/18/26 McMaster"
FALLBACK_BATCH_NAME = "API Agent - 9/18/26"
KNOWN_BATCH_ID = 721
# Kyle: Crosslink 715 / JPSteel 716 / Legacy 717 / Gas 720. Do not reuse.
FORBIDDEN_BATCH_IDS = {715, 716, 717, 720}
FORBIDDEN_REUSE_NAMES = {
    "API Agent - 9/16/26",
    "API Agent - 9/16/26 JPSteel",
    "API Agent - 9/16/26-2",
    "API Agent - 9/17/26",
    "API Agent - 9/17/26 Legacy Wire",
    "API Agent - 9/17/26 Gas & Supply",
}

VENDOR_NAME = "McMaster-Carr Supply Company"
VENDOR_TOKENS = ("mcmaster", "mcmastercarr", "mcmaster-carr")
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
# Inbox census 2026-09-18: distinct unflagged McMaster POs. Prefer these if open.
CENSUS_POS = {
    "58139",
    "58206",
    "58221",
    "58492",
    "58889",
    "58992",
    "59001",
    "59014",
    "59019",
    "59046",
    "59056",
    "59069",
    "59078",
    "59089",
    "59103",
    "59120",
    "59125",
    "59159",
    "59161",
    "59191",
    "59219",
    "59224",
    "59235",
}
# Prior fact only. Re-lookup Transfer AP by name. Never invent this id.
TRANSFER_AP_PRIOR_ID_HINT = 375
RUBEN_PEREZ = "@Ruben Perez"
# First-pass headers on batch 721. Do not recreate.
CREATED_HEADERS = {
    "71647463": 10138,
    "71080498": 10139,
    "71001379": 10140,
    "70747918": 10141,
    "72094446": 10142,
}
# Transfer AP price_variance + receipt-retry HOLDs that still cannot finish.
LEAVE_ALONE_HOLD_IDS = {10140, 10142, 10143, 10146, 10148, 10154, 10158, 10159}
# Finished Successes. GET-only — do not re-Select / edit.
DO_NOT_MUTATE_IDS = {10138, 10139, 10141, 10144, 10145, 10147, 10149, 10150, 10151, 10152, 10153, 10155, 10156, 10157}
# Kyle 2026-09-21: receipts now entered — finish these existing headers.
RETRY_HEADERS = {
    "71080498": 10139,
    "72094446": 10142,
    "72087570": 10144,
    "72012111": 10145,
    "72013304": 10146,
    "71839575": 10147,
}
# Plus-5 leftovers from the 9/18 first pass, newest first.
PREFERRED_NEXT = (
    "72068812",
    "72087570",
    "72012111",
    "72013304",
    "71839575",
)
PLUS5_HEADERS = {
    "72068812": 10143,
    "72087570": 10144,
    "72012111": 10145,
    "72013304": 10146,
    "71839575": 10147,
}
# Next leftover window after plus-5. Live pick still excludes KIMCO/sheet.
PREFERRED_PLUS10 = (
    "71743140",
    "71740547",
    "71642803",
    "71668723",
    "71401129",
)
PLUS10_HEADERS = {
    "71743140": 10148,
    "71740547": 10149,
    "71642803": 10150,
    "71668723": 10151,
    "71401129": 10152,
}
# Next leftover window after plus-10 (Kyle 2026-09-21).
PREFERRED_PLUS15 = (
    "71254641",
    "71236251",
    "71183051",
    "71098307",
    "70907154",
)
PLUS15_HEADERS = {
    "71254641": 10153,
    "71236251": 10154,
    "71183051": 10155,
    "71098307": 10156,
    "70907154": 10157,
}
# Wave-4 leftovers first (Kyle 2026-09-21), then next unflagged Aug 1+.
PREFERRED_PLUS20 = (
    "70759737",
    "70758802",
)
PLUS20_HEADERS = {
    "70759737": 10158,
    "70758802": 10159,
}
# Every header already on the 9/18 McMaster sheet. Later plus waves must not mutate these.
EXISTING_HEADER_IDS = set(range(10138, 10160))

CREDIT_SUBJECT = re.compile(
    r"\bcredit from your order\b|\bplease deduct credit\b|\bcredit memo\b",
    flags=re.I,
)
PAY_REMINDER = re.compile(r"please pay for your po|follow up:\s*please pay", flags=re.I)
_KIMCO_PO = re.compile(r"^5[7-9]\d{3}$")
_MCMASTER_INV = re.compile(r"\b(7\d{7})\b")
_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", flags=re.I)
NOISE_SUBJECT = re.compile(
    r"statement|past due|account with us|remittance|payment reminder",
    flags=re.I,
)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def blob_has_mcmaster(blob: str) -> bool:
    text = (blob or "").lower()
    compact = _compact(text)
    if "mcmaster" in compact or "mcmastercarr" in compact:
        return True
    tokens = set(distinctive_vendor_tokens(text))
    return "mcmaster" in tokens


def is_mcmaster_message(message: dict[str, Any]) -> bool:
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    blob = " ".join([subject, preview, from_name, from_addr])
    return blob_has_mcmaster(blob)


def is_credit_subject(subject: str) -> bool:
    return bool(CREDIT_SUBJECT.search(subject or ""))


def is_mcmaster_invoice_email(message: dict[str, Any]) -> bool:
    if not is_mcmaster_message(message):
        return False
    subject = str(message.get("subject") or "")
    if NOISE_SUBJECT.search(subject):
        return False
    if is_credit_subject(subject):
        return False
    if PAY_REMINDER.search(subject):
        return False
    if _REPLY_PREFIX.match(subject) and not message.get("hasAttachments"):
        return False
    return bool(message.get("hasAttachments") or re.search(r"\binvoice\b", subject, flags=re.I))


def is_mcmaster_vendor_text(text: str | None) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if names_match(VENDOR_NAME, raw):
        return True
    return blob_has_mcmaster(raw)


def exact_invoice_number(value: str | None) -> str:
    """8-digit McMaster invoice # from PDF/filename. Never the 5-digit Order/PO."""
    key = invoice_number_key(value)
    if not key:
        return ""
    hit = _MCMASTER_INV.search(key) or _MCMASTER_INV.search(str(value or ""))
    if hit:
        return hit.group(1)
    if key.isdigit() and len(key) == 8 and key.startswith("7"):
        return key
    return ""


def subject_po(subject: str) -> str:
    pos = extract_subject_pos(subject or "")
    for number in pos:
        if _KIMCO_PO.fullmatch(str(number)):
            return str(number)
    match = re.search(r"\b(?:order|po)\s+(\d{5})\b", subject or "", flags=re.I)
    if match and _KIMCO_PO.fullmatch(match.group(1)):
        return match.group(1)
    return ""


def find_mcmaster_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        "McMaster-Carr",
        "McMaster",
        "Invoice for Your Order",
        "mcmaster.com",
        "ar@mcmaster.com",
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_mcmaster_message(msg):
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
        if not is_mcmaster_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def confirm_mcmaster_vendor(client: KimcoClient) -> dict[str, Any]:
    """Live vendor id from existing KIMCO invoices. invent=false — never guess."""
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
        if not is_mcmaster_vendor_text(vendor_txt):
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
    for kid, number, vendor_txt in list_hits[:10]:
        if any(s.get("invoice_id") == kid and s.get("source") == "record" for s in samples):
            continue
        try:
            rec = client.get_item("ap_invoices", kid)
        except KimcoError:
            continue
        vals = rec.get("values") or {}
        posted = lookup_id(vals.get("Vendor"))
        posted_txt = str(lookup_text(vals.get("Vendor")) or vendor_txt)
        if posted in (None, "") or not is_mcmaster_vendor_text(posted_txt):
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
        second_n = counts.most_common(2)[1][1]
        if top_n > second_n:
            confirmed = top_id
    return {
        "vendor_id": confirmed,
        "vendor_text_samples": sorted(list_texts | {s["vendor_text"] for s in samples}),
        "id_counts": dict(counts),
        "samples": [s for s in samples if s.get("source") == "record"][:12] or samples[:12],
        "entered": entered,
        "invent": False,
    }


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_iso_date(str(value)[:10])


def _choose_invoice_pdf(pdfs: list[tuple[str, bytes]]) -> tuple[str, bytes] | None:
    invoices = [
        item
        for item in pdfs
        if re.search(r"invoice", item[0], flags=re.I) and not re.search(r"credit", item[0], flags=re.I)
    ]
    if invoices:
        return invoices[0]
    non_credit = [item for item in pdfs if not re.search(r"credit", item[0], flags=re.I)]
    return (non_credit or pdfs)[0] if (non_credit or pdfs) else None


def bill_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> dict[str, Any]:
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    pdfs: list[tuple[str, bytes]] = []
    if message.get("hasAttachments"):
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id)
    chosen = _choose_invoice_pdf(pdfs)
    if not chosen:
        return {
            "vendor": VENDOR_NAME,
            "invoice_number": "",
            "date": None,
            "po": subject_po(subject) or None,
            "amount": None,
            "hold_reason": "parse-error",
            "action": "hold",
            "graph_message_id": message_id,
            "subject": subject,
            "receivedDateTime": message.get("receivedDateTime"),
            "from_name": from_name,
            "pdf_unavailable": True,
        }
    filename, content = chosen
    dest = pdf_dir / (
        f"{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_"
        f"{_safe_filename(filename)}"
    )
    dest.write_bytes(content)
    parsed = parse_invoice_pdf(
        dest, subject=subject, from_name=from_name, from_address=from_addr
    )
    parsed["pdf_path"] = str(dest)
    parsed["graph_message_id"] = message_id
    parsed["subject"] = subject
    parsed["receivedDateTime"] = message.get("receivedDateTime")
    parsed["from_name"] = from_name
    parsed["action"] = "create"
    parsed["id"] = message_id
    parsed["download_method"] = "attachment"
    parsed["pdf_bytes"] = len(content)
    if not names_match(VENDOR_NAME, str(parsed.get("vendor") or "")):
        if is_mcmaster_vendor_text(str(parsed.get("vendor") or "")) or blob_has_mcmaster(
            " ".join([str(parsed.get("vendor") or ""), from_name, subject])
        ):
            parsed["vendor"] = VENDOR_NAME
    printed = exact_invoice_number(parsed.get("invoice_number")) or exact_invoice_number(filename)
    if printed:
        parsed["invoice_number"] = printed
    else:
        parsed["invoice_number"] = ""
    po = str(parsed.get("po") or "") or subject_po(subject)
    if po and _KIMCO_PO.fullmatch(invoice_number_key(po) or ""):
        parsed["po"] = invoice_number_key(po)
    if is_credit_subject(subject) or re.search(r"credit", filename, flags=re.I):
        parsed["hold_reason"] = "credit"
        parsed["action"] = "skip"
        parsed["skip_reason"] = "credit"
    return parsed


def catalog_row(msg: dict[str, Any], entered: dict[str, int]) -> dict[str, Any]:
    subject = str(msg.get("subject") or "")
    po = subject_po(subject)
    return {
        "invoice": "",
        "po": po,
        "received": msg.get("receivedDateTime"),
        "flagged": is_already_flagged(msg),
        "categories": msg.get("categories") or [],
        "flagStatus": (msg.get("flag") or {}).get("flagStatus"),
        "already_kimco": None,
        "hasAttachments": bool(msg.get("hasAttachments")),
        "from": sender_name(msg),
        "subject": subject[:140],
        "credit": is_credit_subject(subject),
        "pay_reminder": bool(PAY_REMINDER.search(subject)),
    }


def is_credit_bill(bill: dict[str, Any]) -> bool:
    if bill.get("skip_reason") == "credit" or bill.get("hold_reason") == "credit":
        return True
    if is_credit_subject(str(bill.get("subject") or "")):
        return True
    amt = money(bill.get("amount"))
    if amt is not None and amt < 0:
        return True
    path = str(bill.get("pdf_path") or "")
    if re.search(r"credit", path, flags=re.I) and not re.search(r"invoice", path, flags=re.I):
        return True
    return False


def preflight_match(bill: dict[str, Any], receipts: list[dict[str, Any]] | None) -> dict[str, Any]:
    po = invoice_number_key(bill.get("po") or "")
    lines = list(bill.get("lines") or [])
    if not po or not receipts:
        return {"clean": False, "matched": [], "skipped": [], "select_zero": False, "open": 0}
    pool = open_receipts_on_po(receipts, po)
    if not pool:
        return {"clean": False, "matched": [], "skipped": [], "select_zero": False, "open": 0}
    match = match_receipts(
        invoice_number=str(bill.get("invoice_number") or ""),
        invoice_lines=lines,
        receipts=pool,
        po_number=po,
        invoice_amount=bill.get("amount"),
    )
    matched = list(match.get("matched") or [])
    locked = filter_matches_outside_ppv_gate(matched, invoice_total=bill.get("amount"))
    skipped = list(locked.get("skipped") or [])
    selectable = list(locked.get("selectable") or [])
    merch = [ln for ln in lines if money(ln.get("qty")) not in (None, 0)]
    all_lines = bool(merch) and len(selectable) >= len(merch)
    clean = bool(merch) and bool(selectable) and not skipped and not locked.get("select_zero") and all_lines
    return {
        "clean": clean,
        "matched": selectable,
        "skipped": skipped,
        "select_zero": bool(locked.get("select_zero")),
        "open": len(pool),
        "how": match.get("hows") or match.get("how"),
    }


def pick_recent(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
    receipts: list[dict[str, Any]] | None = None,
    preferred: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """PREFERRED_NEXT leftovers first if still open, then newest clean matches.

    Credits are leftovers, not entered. Pre-Aug dates are older, not entered.
    """
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
    for bill in recent:
        pre = preflight_match(bill, receipts)
        bill["_clean"] = bool(pre.get("clean"))
        bill["_census_po"] = invoice_number_key(bill.get("po") or "") in CENSUS_POS
        bill["_preflight"] = {k: v for k, v in pre.items() if k != "matched"}
    recent.sort(
        key=lambda b: (
            0 if b.get("_clean") else 1,
            0 if b.get("_census_po") else 1,
            str(b.get("date") or ""),
            str(b.get("receivedDateTime") or ""),
        ),
        reverse=False,
    )
    # After grouping clean/census, newest within each group.
    clean = [b for b in recent if b.get("_clean")]
    rest = [b for b in recent if not b.get("_clean")]
    clean.sort(key=lambda b: (str(b.get("date") or ""), str(b.get("receivedDateTime") or "")), reverse=True)
    rest.sort(key=lambda b: (str(b.get("date") or ""), str(b.get("receivedDateTime") or "")), reverse=True)
    extras = clean + rest
    if preferred:
        by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in extras}
        pref_keys = [exact_invoice_number(n) for n in preferred if exact_invoice_number(n)]
        ordered = [by_inv[n] for n in pref_keys if n in by_inv]
        used = {exact_invoice_number(b.get("invoice_number")) for b in ordered}
        ordered.extend(b for b in extras if exact_invoice_number(b.get("invoice_number")) not in used)
    else:
        ordered = extras
    leftover = ordered[cap:]
    return ordered[:cap], older + leftover, credits


def create_mcmaster_batch(client: KimcoClient) -> dict[str, Any]:
    """New dedicated batch. Never 715/716/717/720."""
    batches = client.list_items("ap_batches")
    by_name: dict[str, dict[str, Any]] = {}
    for item in batches:
        name = str((item.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
        if name:
            by_name[name] = item

    def _from_existing(name: str) -> dict[str, Any] | None:
        item = by_name.get(name)
        if not item:
            return None
        bid = item.get("id")
        got = str((item.get("values") or {}).get("AP_Invoice_Batch_ID") or name)
        if bid in FORBIDDEN_BATCH_IDS or got in FORBIDDEN_REUSE_NAMES:
            raise KimcoError(f"Refusing forbidden batch id={bid} name={got}")
        vals = item.get("values") or {}
        return {
            "id": bid,
            "name": name,
            "created": False,
            "status": vals.get("Status"),
            "unposted": vals.get("Unposted_Count"),
        }

    for name in (PREFERRED_BATCH_NAME, FALLBACK_BATCH_NAME):
        found = _from_existing(name)
        if found:
            return found

    last_error = ""
    for name in (PREFERRED_BATCH_NAME, FALLBACK_BATCH_NAME):
        try:
            created = _find_or_create_batch(client, batches, name)
        except KimcoError as exc:
            last_error = str(exc)
            LOGGER.info("Batch create %s failed: %s", name, type(exc).__name__)
            continue
        bid = created.get("id")
        got_name = created.get("name") or name
        if bid in FORBIDDEN_BATCH_IDS or got_name in FORBIDDEN_REUSE_NAMES:
            raise KimcoError(f"Refusing to use leftover batch id={bid} name={got_name}")
        created["status"] = 0
        return created
    raise KimcoError(
        f"Could not create {PREFERRED_BATCH_NAME} or {FALLBACK_BATCH_NAME}: {last_error}"
    )


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


def mcmaster_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
    proof = live_get_proof(client, invoice_id)
    if not proof:
        return proof
    item = client.get_item("ap_invoices", int(invoice_id))
    vals = item.get("values") or {}
    charges = charges_from_item(item)
    proof["verification_amount"] = money(vals.get("Invoice_Verification_Amount"))
    proof["charges"] = charges
    proof["fee_amounts"] = [
        c["amount"] for c in charges if _is_fee_charge(c) and c.get("amount") is not None
    ]
    proof["ppv_amounts"] = [
        c["amount"] for c in charges if _is_ppv_charge(c) and c.get("amount") is not None
    ]
    proof["comments"] = str(vals.get("Comments") or "")
    return proof


def finish_hold_header(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hydrate PO receipts, Select Receipts, post Fees then in-gate PPV."""
    proof = mcmaster_proof(client, kimco_id)
    have = _receipt_ids_from_proof(proof)
    pool = hydrate_receipts(client, open_receipts_on_po(receipts, str(parsed.get("po") or "")))
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=pool,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    matched = list(match.get("matched") or [])
    locked = filter_matches_outside_ppv_gate(
        matched,
        invoice_total=parsed.get("amount"),
    )
    selectable = list(locked.get("selectable") or [])
    skipped = list(locked.get("skipped") or [])
    wanted_refs = receipt_select_refs(selectable)
    wanted: list[Any] = []
    for ref in wanted_refs:
        rid = ref.get("id") if isinstance(ref, dict) else ref
        if rid not in (None, "") and int(rid) not in have:
            wanted.append(ref)
    select_status = "already-selected" if have and not wanted else "held-unfinished"
    if locked.get("select_zero") or (skipped and not selectable):
        select_status = "ppv-lock-select-zero"
    elif wanted:
        select_status = client.try_select_receipts(kimco_id, wanted)

    parsed_fees = list(parsed.get("fees") or [])
    fee_status = "none"
    if fees_with_amounts(parsed_fees):
        already = fees_posted_cover_parsed(proof.get("fee_amounts") or [], parsed_fees)
        if already:
            fee_status = "already-posted"
        elif select_status in {"selected", "already-selected"}:
            fee_status = client.try_post_fees(kimco_id, fees_with_amounts(parsed_fees))
        else:
            fee_status = "held-no-select"

    ppv_amt = ppv_total_from_selectable(selectable, invoice_total=parsed.get("amount"))
    ppv_status = "none"
    existing_ppv = sum(proof.get("ppv_amounts") or [])
    if ppv_amt and abs(ppv_amt - existing_ppv) > 0.02:
        if select_status in {"selected", "already-selected"}:
            ppv_status = client.try_post_ppv(kimco_id, ppv_amt)
        else:
            ppv_status = "held-no-select"
    elif ppv_amt and existing_ppv:
        ppv_status = "already-posted"

    after = mcmaster_proof(client, kimco_id)
    if (
        kimco_id not in DO_NOT_MUTATE_IDS
        and select_status in {"selected", "already-selected"}
        and (after.get("receipt_lines") or [])
    ):
        before_amt = money(after.get("invoice_amount"))
        decision = rounding_ppv_to_hit_pdf_total(
            parsed.get("amount"),
            before_amt,
            receipts_selected=True,
        )
        needed = money(decision.get("ppv")) or 0.0
        existing = round(sum(after.get("ppv_amounts") or []), 2)
        if decision.get("action") == "ppv" and needed and abs(needed - existing) > 0.02:
            remaining = round(needed - existing, 2)
            ppv_status = client.try_post_ppv(int(kimco_id), remaining)
            ppv_amt = needed
            after = mcmaster_proof(client, kimco_id)
    return {
        "wanted": wanted,
        "select_status": select_status,
        "fee_status": fee_status,
        "ppv_status": ppv_status,
        "ppv_amount": ppv_amt,
        "match_how": match.get("hows") or match.get("how"),
        "matched": selectable,
        "skipped_over_ppv": bool(skipped),
        "select_zero": bool(locked.get("select_zero")),
        "open_on_po": [
            {
                "id": r.get("id"),
                "qty": r.get("qty"),
                "unit_price": r.get("unit_price"),
                "part": r.get("part"),
            }
            for r in pool
        ],
        "after": after,
    }


def _format_select_wanted(wanted: list[Any] | None) -> str:
    """Receipt ids for Why — never dump a raw dict list."""
    bits: list[str] = []
    for ref in wanted or []:
        if isinstance(ref, dict):
            rid = ref.get("id")
            qty = ref.get("qty")
            if qty not in (None, ""):
                bits.append(f"{rid} qty={qty}")
            else:
                bits.append(str(rid))
        else:
            bits.append(str(ref))
    return "[" + ", ".join(bits) + "]"


def quality_mcmaster_row(
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    proof: dict[str, Any],
    finish: dict[str, Any] | None,
    vendor_id: int | None,
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
            f"ids={_format_select_wanted(finish.get('wanted'))} "
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
        and select_ok
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
    if finished:
        out["Result"] = "Success"
        out["Why"] = (
            f"Finished bill (Invoice_Type {proof.get('invoice_type')}). "
            f"Header PO set={po or 'none'}. Select Receipts "
            f"{format_receipts(proof)} on PO {po or 'n/a'}. "
            f"Invoice #={pdf_number} (exact PDF, not subject Order #). "
            f"Fees={out['Fees and surcharges']} (Additional Charge Fees id "
            f"{FEE_CHARGE_LOOKUP_ID}, not missing merch lines / not PPV). "
            f"PPV={out['PPV']}. Attach status=attached. {extra}"
            "Flag status=entered-in-ai."
        )
        out["Flag status"] = "entered-in-ai"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, message_id)
        else:
            out["outlook"] = enter_row.get("outlook") or "left-as-kyle"
    elif missing_po and kid not in (None, "") and not recs:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (po): McMaster invoice #{pdf_number} has no findable live PO. "
            f"{SHAWN_MCKIBBEN}: purchasing / missing_po. Do not invent a PO. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif price_hold:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (price-does-not-match): leftover vs invoice line is over the "
            f"PPV gate. Do not Select Receipts on that line. {SHAWN_MCKIBBEN}: "
            "purchasing must unreceive, change the PO price, and re-receive. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif qty_hold:
        out["Result"] = "HOLD"
        if not recs:
            out["Why"] = (
                f"HOLD (receipt): no open receipt leftover on PO {po or 'n/a'} "
                f"for McMaster invoice #{pdf_number}. Do not first-open guess. "
                f"{SHAWN_MCKIBBEN}: receiving must receive the PO so AP can "
                "Select Receipts. Comments_1 @Shawn (NOTE-45). Do not Transfer AP. "
                f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        else:
            out["Why"] = (
                f"HOLD (receipt): PDF merch vs selected ({format_receipts(proof)}) "
                f"on PO {po or 'n/a'}. Do not invent Success. "
                f"{SHAWN_MCKIBBEN}: receiving leftover merch. Comments_1 @Shawn "
                "(NOTE-45). Do not Transfer AP. "
                f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif fee_on_ppv or not fees_ok:
        out["Result"] = "HOLD"
        out["Why"] = (
            "HOLD (fees): McMaster freight/surcharges must be Additional Charge "
            f"Fees (id {FEE_CHARGE_LOOKUP_ID}), never PPV. "
            f"Posted fees={fee_amts} ppv={ppv_amts}. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif cover_blocked:
        out["Result"] = "Incomplete"
        out["Why"] = (
            "NOTE-37: same-item same-unit-cost leftovers uniquely sum to the "
            "invoice line. Do not invent Success until live GET shows the "
            f"combined receipts. {extra}Outlook left alone."
        )
        out["Flag status"] = enter_row.get("Flag status") or "entered-with-issues"
        out["outlook"] = enter_row.get("outlook") or "left-as-kyle"
    else:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD after live GET of {kid}: PDF amount={pdf_amt} posted={posted} "
            f"verification={ver} rolled={rolled} receipts={format_receipts(proof)} "
            f"attach={attach_ok} type={proof.get('invoice_type')} "
            f"vendor_id={proof.get('vendor_id')} expected={vendor_id} "
            f"invoice # PDF={pdf_number} posted={posted_number}. "
            f"Do not invent Success. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and kid not in (None, ""):
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    return apply_exception_category_owner(out)


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
        f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on McMaster-Carr {inv} "
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
    """NOTE-45 McMaster missing_receipt: @Shawn on Comments_1, stay on 721."""
    amt = money(pdf_amount)
    amt_txt = f"{amt:.2f}" if amt is not None else "unknown"
    inv = exact_invoice_number(invoice_number) or str(invoice_number or "")
    return (
        f"{SHAWN_MCKIBBEN} HOLD (receipt) on McMaster-Carr {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Open leftover merch is not received "
        "or does not match PDF lines. Header stays on the current API Agent "
        "batch — do not Transfer AP. Receiving / purchasing: receive the PO "
        "so AP can Select Receipts. No email."
    )


def is_missing_receipt_hold(row: dict[str, Any]) -> bool:
    if str(row.get("Result") or "") != "HOLD":
        return False
    if str(row.get("Exception category") or "") == "price_variance":
        return False
    if str(row.get("Exception category") or "") == "missing_receipt":
        return True
    why = str(row.get("Why") or "").lower()
    return "hold (receipt)" in why or "no open receipt" in why


def find_transfer_ap_batch(batches: list[dict[str, Any]]) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    wanted = TRANSFER_AP_BATCH_NAME.casefold()
    for item in batches or []:
        vals = item.get("values") if isinstance(item.get("values"), dict) else {}
        name = str(
            (vals or {}).get("AP_Invoice_Batch_ID")
            or (vals or {}).get("Name")
            or item.get("name")
            or ""
        ).strip()
        if name.casefold() != wanted:
            continue
        if item.get("id") in (None, ""):
            continue
        hits.append({"id": int(item["id"]), "name": name})
    if len(hits) == 1:
        return {"found": True, "id": hits[0]["id"], "name": hits[0]["name"], "invent": False}
    if len(hits) > 1:
        return {"found": False, "ambiguous": True, "hits": hits, "invent": False}
    return {
        "found": False,
        "id": None,
        "name": None,
        "invent": False,
        "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
    }


def is_over_ppv_price_hold(row: dict[str, Any], finish: dict[str, Any] | None) -> bool:
    finish = finish or {}
    # NOTE-46: in-gate UOM/pack PPV stays on the current batch — not Transfer AP.
    if finish.get("uom_pack_in_gate") or finish.get("note46_in_gate_ppv"):
        return False
    if str(row.get("Exception category") or "") == "price_variance":
        return True
    if str(row.get("Result") or "") != "HOLD":
        return False
    why = str(row.get("Why") or "").lower()
    if "price-does-not-match" in why or "over the ppv gate" in why:
        return True
    return bool(finish.get("select_zero") or finish.get("skipped_over_ppv"))


def probe_mention_notify(client: KimcoClient, kimco_id: int, after: dict[str, Any]) -> dict[str, Any]:
    """Comments TAB proof only. Header Comments string is the wrong surface."""
    proof = comment_tab_proof(after)
    probes: list[dict[str, Any]] = []
    for suffix in ("comments", "mentions", "notifications"):
        try:
            url = client._record_url("ap_invoices", int(kimco_id), suffix)
            resp = client.request("GET", url)
            probes.append({"suffix": suffix, "http": resp.status_code})
        except Exception as exc:  # noqa: BLE001
            probes.append({"suffix": suffix, "http": None, "error": type(exc).__name__})
    proof["probes"] = probes
    return proof


def apply_over_ppv_transfer_ap(
    client: KimcoClient,
    *,
    kimco_id: int,
    comment: str,
    allow_ids: set[int] | None = None,
) -> dict[str, Any]:
    blocked = (set(LEAVE_ALONE_HOLD_IDS) | set(DO_NOT_MUTATE_IDS)) - set(allow_ids or ())
    if int(kimco_id) in blocked:
        return {"status": "leave-alone", "kimco_id": int(kimco_id), "invent": False}
    try:
        batches = client.list_items("ap_batches")
    except KimcoError as exc:
        return {"status": "batch-list-failed", "error": str(exc)[:240], "invent": False}
    found = find_transfer_ap_batch(batches)
    if not found.get("found"):
        return {
            "status": "batch-not-found",
            "lookup": found,
            "invent": False,
            "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
        }
    bid = int(found["id"])
    batch_payload = transfer_ap_batch_only_payload(invoice_id=int(kimco_id), batch_id=bid)
    if "Comments" in (batch_payload.get("values") or {}):
        return {"status": "refused-header-comments", "kimco_id": int(kimco_id), "invent": False}
    body, status, error = client.update(
        "ap_invoices",
        int(kimco_id),
        batch_payload,
    )
    tab = add_invoice_comment_tab(client, invoice_id=int(kimco_id), body=comment)
    try:
        after = client.get_item("ap_invoices", int(kimco_id))
    except KimcoError:
        after = {}
    vals = after.get("values") if isinstance(after.get("values"), dict) else {}
    mention = probe_mention_notify(client, int(kimco_id), after or {})
    live_bid = lookup_id((vals or {}).get("AP_Invoice_Batch"))
    live_name = lookup_text((vals or {}).get("AP_Invoice_Batch"))
    moved = status < 400 and live_bid == bid
    return {
        "status": "moved" if moved else f"blocked-{status}",
        "kimco_id": int(kimco_id),
        "batch_id": live_bid if live_bid not in (None, "") else bid,
        "batch_name": live_name or found.get("name"),
        "comment": (mention.get("report") or comment),
        "comment_requested": comment,
        "comment_tab": tab,
        "put": status,
        "error": error,
        "put_body_keys": sorted(body) if isinstance(body, dict) else [],
        "mention_notify": mention,
        "invent": False,
        "mail_send": EXCEPTION_MAIL_SEND,
    }


def finish_entered_rows(
    client: KimcoClient,
    graph,
    *,
    parsed_bills: list[dict[str, Any]],
    enter_rows: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    vendor_id: int | None,
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
        row = quality_mcmaster_row(
            graph,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
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
                mention=SHAWN_MENTION,
            )
            finishes[inv]["missing_receipt_comment"] = notify
            after_batch = ""
            try:
                after = client.get_item("ap_invoices", int(kid))
                after_vals = after.get("values") if isinstance(after.get("values"), dict) else {}
                after_batch = lookup_text((after_vals or {}).get("AP_Invoice_Batch")) or ""
            except KimcoError:
                after_batch = ""
            row["Why"] = (
                f"{row.get('Why')} Comments_1 missing_receipt "
                f"status={notify.get('status')} "
                f"tab={notify.get('report') or notify} "
                f"transfer_ap={notify.get('transfer_ap')} "
                f"batch_still={after_batch or 'current'} "
                f"mail_send={notify.get('mail_send')}."
            )
            row = apply_exception_category_owner(row)
        rows.append(row)
        receipts = load_list_receipts(client)
    return rows, finishes


def leftover_from_catalog(
    parsed_bills: list[dict[str, Any]],
    *,
    chosen: set[str],
    older: list[dict[str, Any]],
    credits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bill in older + [b for b in parsed_bills if exact_invoice_number(b.get("invoice_number")) not in chosen]:
        inv = exact_invoice_number(bill.get("invoice_number")) or str(bill.get("invoice_number") or "")
        key = inv or str(bill.get("subject") or "")[:40]
        if (
            key in seen
            or inv in chosen
            or inv in CREATED_HEADERS
            or inv in PLUS5_HEADERS
            or inv in PLUS10_HEADERS
            or inv in PLUS15_HEADERS
            or inv in PLUS20_HEADERS
        ):
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
    for bill in credits:
        inv = exact_invoice_number(bill.get("invoice_number")) or ""
        key = inv or str(bill.get("subject") or "")[:40]
        if key in seen:
            continue
        seen.add(key)
        pending.append(
            {
                "invoice_number": inv,
                "po": bill.get("po"),
                "date": bill.get("date"),
                "amount": bill.get("amount"),
                "received": bill.get("receivedDateTime"),
                "subject": str(bill.get("subject") or "")[:120],
                "why": "credit memo / not entered as invoice",
            }
        )
    return pending


def prior_rows_from_sidecar(report_path: Path) -> list[dict[str, Any]]:
    sidecar = report_path.with_suffix(".json")
    if not sidecar.is_file():
        return []
    try:
        payload = json.loads(sidecar.read_text())
    except json.JSONDecodeError:
        return []
    return list(payload.get("rows") or [])


def merge_sheet_rows(prior: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_inv: dict[str, dict[str, Any]] = {}
    for row in prior:
        key = exact_invoice_number(row.get("Invoice #")) or str(row.get("Invoice #") or "")
        if key:
            by_inv[key] = row
    for row in new_rows:
        key = exact_invoice_number(row.get("Invoice #")) or str(row.get("Invoice #") or "")
        if key:
            by_inv[key] = row
        else:
            by_inv[f"anon-{len(by_inv)}"] = row
    return list(by_inv.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter up to 5 McMaster-Carr bills on a dedicated 9/18 batch")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--create-batch-only", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-18-mcmaster.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(
        "Target: live. McMaster-Carr only. Batch 721 (not 715/716/717/720). "
        "No Gas. No Mail.Send. invent=false.",
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

    vendor_info = confirm_mcmaster_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    print(
        json.dumps(
            {
                "vendor_confirm": {
                    "vendor_id": vendor_id,
                    "vendor_text_samples": vendor_info.get("vendor_text_samples"),
                    "id_counts": vendor_info.get("id_counts"),
                    "sample_count": len(vendor_info.get("samples") or []),
                    "entered_count": len(vendor_info.get("entered") or {}),
                    "invent": False,
                }
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if vendor_id in (None, ""):
        print("Live McMaster vendor id not confirmed from KIMCO invoices. Will not invent. Stop.", flush=True)
        return 2

    if args.create_batch_only:
        batch = create_mcmaster_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden"):
            print("Created/found batch is forbidden — abort.", flush=True)
            return 2
        return 0

    entered = dict(vendor_info.get("entered") or {})
    print(f"KIMCO McMaster invoices already present: {len(entered)}", flush=True)

    messages = find_mcmaster_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(key=lambda r: str(r.get("received") or ""), reverse=True)
    print(json.dumps({"discovered": len(catalog), "mcmaster_mail": catalog}, indent=2, default=str), flush=True)

    already = (
        set(entered) | set(CREATED_HEADERS) | set(PLUS5_HEADERS) | set(PLUS10_HEADERS) | set(PLUS15_HEADERS) | set(PLUS20_HEADERS)
    )
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    skipped_entered = 0
    skipped_noise = 0
    for msg in messages:
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_mcmaster_invoice_email(msg):
            skipped_noise += 1
            continue
        candidates.append(msg)
    candidates.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    print(
        json.dumps(
            {
                "unflagged_invoice_emails": [
                    {
                        "po": subject_po(str(m.get("subject") or "")),
                        "received": m.get("receivedDateTime"),
                        "subject": str(m.get("subject") or "")[:120],
                    }
                    for m in candidates
                ],
                "skipped_flagged": skipped_flagged,
                "skipped_already_entered": skipped_entered,
                "skipped_noise": skipped_noise,
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
    parsed_bills = [bill_from_message(graph, msg, pdf_dir) for msg in candidates]
    parsed_bills = [
        b
        for b in parsed_bills
        if exact_invoice_number(b.get("invoice_number")) not in already
    ]
    print(json.dumps({"parsed_candidates": [summarize_parse(inv) for inv in parsed_bills]}, indent=2, default=str), flush=True)

    try:
        receipts = load_list_receipts(client)
    except KimcoError:
        receipts = []
    recent, older, credits = pick_recent(
        parsed_bills,
        already=already,
        cap=CAP,
        receipts=receipts,
        preferred=PREFERRED_NEXT,
    )
    print(
        json.dumps(
            {
                "recent_picked": [summarize_parse(b) | {"clean": b.get("_clean"), "census_po": b.get("_census_po")} for b in recent],
                "older_or_unclean_leftover": [summarize_parse(b) for b in older],
                "credits_skipped": [summarize_parse(b) for b in credits],
                "min_invoice_date": str(MIN_INVOICE_DATE),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.parse_only:
        return 0

    batch = create_mcmaster_batch(client)
    verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
    print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
    if verified.get("is_forbidden") or batch.get("id") in FORBIDDEN_BATCH_IDS:
        print("Refusing leftover weekday batch. Abort enter.", flush=True)
        return 2
    if str(batch.get("name")) in FORBIDDEN_REUSE_NAMES:
        print("Refusing leftover weekday batch name. Abort enter.", flush=True)
        return 2
    if int(batch.get("id") or 0) != KNOWN_BATCH_ID:
        print(f"Expected batch {KNOWN_BATCH_ID}; got {batch.get('id')}. Abort.", flush=True)
        return 2

    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    leftover_pending = leftover_from_catalog(
        parsed_bills, chosen={exact_invoice_number(b.get("invoice_number")) for b in recent}, older=older, credits=credits
    )
    if not recent:
        reason = (
            "No unflagged McMaster-Carr invoices dated on/after 2026-08-01 that "
            "are not already in KIMCO. Stopped — did not walk pre-Aug (NOTE-28)."
        )
        print(reason, flush=True)
        rows = prior_rows
        write_report(report_path, rows)
        sidecar = {
            "proof": "mcmaster-0918",
            "invent": False,
            "mail_send": False,
            "vendor": VENDOR_NAME,
            "vendor_id": vendor_id,
            "batch_name": batch["name"],
            "batch_id": batch["id"],
            "batch": verified,
            "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
            "discovered": len(catalog),
            "catalog": catalog,
            "kimco_already": entered,
            "recent_picked": [],
            "new_rows": [],
            "leftover_pending": leftover_pending,
            "rows": rows,
            "blocker": reason,
        }
        report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
        print(f"Sheet: {report_path} leftovers={len(leftover_pending)}", flush=True)
        return 0

    enter_rows = run_enter(
        client,
        recent,
        batch_name=str(batch["name"]),
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=False,
    )
    receipts = load_list_receipts(client)
    rows, finishes = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=int(vendor_id),
    )
    merged = merge_sheet_rows(prior_rows, rows)
    write_report(report_path, merged)
    sidecar = {
        "proof": "mcmaster-0918",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": vendor_id,
        "vendor_confirm": {
            "vendor_id": vendor_id,
            "vendor_text_samples": vendor_info.get("vendor_text_samples"),
            "id_counts": vendor_info.get("id_counts"),
        },
        "batch_name": batch["name"],
        "batch_id": batch["id"],
        "batch": verified,
        "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
        "created_headers": {
            exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id")
            for r in rows
            if r.get("KIMCO id") not in (None, "")
        },
        "discovered": len(catalog),
        "catalog": catalog,
        "kimco_already": entered,
        "recent_picked": [summarize_parse(b) for b in recent],
        "finishes": finishes,
        "new_rows": rows,
        "leftover_pending": leftover_pending,
        "rows": merged,
    }
    report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    _print_summary(rows)
    print(
        json.dumps(
            {
                "batch_id": batch["id"],
                "batch_name": batch["name"],
                "sheet": str(report_path),
                "leftovers": len(leftover_pending),
                "results": [
                    {
                        "invoice": r.get("Invoice #"),
                        "result": r.get("Result"),
                        "amount": r.get("Amount"),
                        "po": r.get("PO"),
                        "kimco": r.get("KIMCO id"),
                        "fees": r.get("Fees and surcharges"),
                        "ppv": r.get("PPV"),
                        "why": r.get("Why"),
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
