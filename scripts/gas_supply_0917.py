"""Enter up to 5 recent unflagged Gas & Supply bills.

Dedicated live batch — do NOT reuse Crosslink 715, JPSteel 716, or
Legacy Wire 717. Prefer name `API Agent - 9/17/26 Gas & Supply`;
fall back to `API Agent - 9/17/26-G` if the ERP rejects the suffix.

PDF attachments only. No Mail.Send. invent=false.

Skip already-flagged mail and invoices already on the live Gas vendor.
Invoice date on/after 2026-08-01. Cap 5 bills (a multi-invoice PDF can
produce multiple of the 5). Split multi-invoice PDFs. Amount = after-tax
Total / Amount Due / Amount This Invoice Including Tax — never merchandise
subtotal (NOTE-36). One-invoice PDF is not multiple Misc. Order numbers
like 0011118988-00 are not invoices.

NOTE-39 exception category/owner on the sheet.
NOTE-40: new over-PPV HOLDs move to Transfer AP (lookup by name; prior
fact 375 is a hint only — never invent). Comments + @Shawn McKibben.
Missing receipts → HOLD missing_receipt / @Ruben Perez (parent email).

Plus-5 (Kyle 2026-09-17): reuse batch 720. Do not recreate 10128–10131
or HOLD 0040430010. Purchasing owner is Shawn McKibben — do not tag Misty.

Plus-10: next 5 after the plus-5 leftovers. Discover the next Aug 1+
window. Leave 0040430010 / 0040424839 / 10134 alone.

Plus-15: next 5 after plus-10. Prefer no-PO shop-supply bills and finish
Lines-K (NOTE-42). Leave plus-10 HOLDs 0040438057/56/55/53 alone.

NOTE-42: Type 4 Gas Misc Success requires nonempty Lines-K (desc/qty/cost
+ Shop Supplies - G&S). Header-only is Incomplete, never Success.
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
    ADDITIONAL_CHARGE_LISTS,
    FEE_CHARGE_LOOKUP_ID,
    PPV_CHARGE_LOOKUP_ID,
    KimcoClient,
    KimcoError,
    fees_posted_cover_parsed,
    fees_with_amounts,
)
from ap_clerk.misc_lines import (  # noqa: E402
    collect_shop_supplies_gs_from_records,
    existing_misc_lines_match_pdf,
    misc_add_item_payload,
    misc_line_snapshot,
    payload_has_receipt,
    type4_shop_supplies_lines_ok,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.quality_v12 import (  # noqa: E402
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
)
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    is_fee_or_surcharge,
    TRANSFER_AP_BATCH_NAME,
    blocked_400_not_a_hold_when_same_item_cover,
    decide_ppv,
    distinctive_vendor_tokens,
    extract_subject_invoice_number,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    is_gas_and_supply,
    line_cost,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    names_match,
    normalize_receipt,
    parse_iso_date,
    receipt_cost,
    receipt_select_refs,
    rounding_ppv_to_hit_pdf_total,
)

LOGGER = logging.getLogger("ap_clerk.gas_supply_0917")

PREFERRED_BATCH_NAME = "API Agent - 9/17/26 Gas & Supply"
FALLBACK_BATCH_NAME = "API Agent - 9/17/26-G"
# First-pass live batch. Plus-5 reuses this id/name — never 715/716/717.
KNOWN_BATCH_ID = 720
# Live confirm 2026-09-18 via record GET (10087 / 0040379954). Hint only.
VENDOR_ID_HINT = 71
VENDOR_NAME = "Gas and Supply North Texas, LLC"
VENDOR_SHORT = "Gas and Supply"
# Kyle 2026-09-17: Shawn owns purchasing. Misty is not a hard missing_po rule.
PURCHASING_OWNER = "Shawn McKibben"
# Kyle: do not reuse these dedicated / weekday batches.
FORBIDDEN_BATCH_IDS = {715, 716, 717}
FORBIDDEN_REUSE_NAMES = {
    "API Agent - 9/16/26",
    "API Agent - 9/16/26 JPSteel",
    "API Agent - 9/16/26-2",
    "API Agent - 9/17/26 Legacy Wire",
    "API Agent - 9/17/26",
}
OTHER_VENDORS = (
    "msc industrial",
    "rmp industrial",
    "tube supply",
    "austin hardware",
    "jp steel",
    "jpsteel",
    "crosslink",
    "legacy wire",
    "american quality powder",
    "air products",
)
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
RUBEN_PEREZ = "@Ruben Perez"
TRANSFER_AP_PRIOR_ID_HINT = 375
# First-pass Success headers on batch 720. Do not recreate.
CREATED_HEADERS = {
    "0040435122": 10128,
    "0040434973": 10129,
    "0040431060": 10130,
    "0040425657": 10131,
}
# HOLDs leave alone. Do not recreate or tag Misty.
KNOWN_HOLD = {"0040430010", "0040424839"}
# Next plus-5 leftovers if still unflagged / not on KIMCO. Plus-10 skips these.
PLUS5_PREFERRED = (
    "0040425612",
    "0040424839",
    "0040424382",
    "0040423658",
    "0040421569",
)
# Plus-5 headers on batch 720. 0040424839 is HOLD missing_po (no header).
PLUS5_HEADERS = {
    "0040425612": 10132,
    "0040424382": 10133,
    "0040423658": 10134,
    "0040421569": 10135,
}
# Plus-10 headers on batch 720. 0040438057/8056/8055/8053 HOLD missing_po (no header).
PLUS10_HEADERS = {
    "0040438494": 10136,
}
# Plus-10 HOLDs (Shawn PO 59081). Leave alone — do not recreate.
PLUS10_HOLDS = frozenset({"0040438057", "0040438056", "0040438055", "0040438053"})
# 10134 missing_receipt / Ruben on PO 58948. Leave alone.
LEAVE_ALONE_HOLD_IDS = {10134}
DO_NOT_MUTATE_IDS = (
    set(CREATED_HEADERS.values()) | set(PLUS5_HEADERS.values()) | set(PLUS10_HEADERS.values())
)
# Live Type 4 shop-supplies lookup (hint only). 10135 Kyle-checked.
LINES_K_LOOKUP_IDS = (9966, 9970, 10135)
NOISE_SUBJECT = re.compile(
    r"past due|account with us|remittance|payment reminder|"
    r"over\s+100\s*\+\s*days|chk#|thank you for your payment|"
    r"payment confirmation|copies of invoices",
    flags=re.I,
)
_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", flags=re.I)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _subject_inv(subject: str) -> str:
    printed = extract_subject_invoice_number(subject)
    if printed:
        return invoice_number_key(printed)
    hit = re.search(r"\b(00\d{8})\b", subject or "")
    return hit.group(1) if hit else ""


def exact_invoice_number(value: str | None) -> str:
    return invoice_number_key(value)


def blob_has_gas_supply(blob: str) -> bool:
    """Distinctive Gas & Supply / Gas and Supply. Generic 'supply' is never enough."""
    text = (blob or "").lower()
    compact = _compact(text)
    if "gasandsupply" in compact:
        return True
    if "gas and supply" in text or "gas & supply" in text or "gas&supply" in text.replace(" ", ""):
        return True
    return bool(is_gas_and_supply(text))


def is_other_vendor(blob: str) -> bool:
    text = (blob or "").lower()
    return any(token in text for token in OTHER_VENDORS) and not blob_has_gas_supply(text)


def is_gas_message(message: dict[str, Any]) -> bool:
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    blob = " ".join([subject, preview, from_name, from_addr])
    if is_other_vendor(blob):
        return False
    if not blob_has_gas_supply(blob):
        return False
    tokens = set(distinctive_vendor_tokens(from_name)) | set(
        distinctive_vendor_tokens(subject)
    )
    if tokens & {"msc", "rmp", "crosslink", "jpsteel", "legacy", "aqpc"}:
        if not blob_has_gas_supply(blob):
            return False
    return True


def is_gas_invoice_email(message: dict[str, Any]) -> bool:
    """Invoice/Statement PDFs are bills. Past-due / check mail is noise.

    CHECK STOP on a Gas subject is not a blanket skip (NOTE-10) — the PDF
    may still hold Misc invoice pages.
    """
    if not is_gas_message(message):
        return False
    subject = str(message.get("subject") or "")
    if NOISE_SUBJECT.search(subject):
        return False
    if _REPLY_PREFIX.match(subject) and not message.get("hasAttachments"):
        return False
    if "invoice/statement" in subject.lower() and message.get("hasAttachments"):
        return True
    return bool(_subject_inv(subject) or message.get("hasAttachments"))


def is_gas_vendor_text(text: str | None) -> bool:
    raw = str(text or "")
    if not raw.strip() or is_other_vendor(raw):
        return False
    if names_match(VENDOR_NAME, raw) or names_match(VENDOR_SHORT, raw):
        return True
    return blob_has_gas_supply(raw)


def find_gas_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        "Gas and Supply",
        "Gas & Supply",
        "Gas&Supply",
        "gasandsupply.com",
        "Gas and Supply North Texas",
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_gas_message(msg):
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
        if not is_gas_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def confirm_gas_vendor(client: KimcoClient) -> dict[str, Any]:
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
        if not is_gas_vendor_text(vendor_txt):
            continue
        number = exact_invoice_number(vals.get("Invoice_Number"))
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
        try:
            rec = client.get_item("ap_invoices", kid)
        except KimcoError:
            continue
        vals = rec.get("values") or {}
        posted = lookup_id(vals.get("Vendor"))
        posted_txt = str(lookup_text(vals.get("Vendor")) or vendor_txt)
        if posted in (None, "") or not is_gas_vendor_text(posted_txt):
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
        "vendor_id_hint": VENDOR_ID_HINT,
        "hint_used": False,
        "vendor_text_samples": sorted(list_texts | {s["vendor_text"] for s in samples}),
        "id_counts": dict(counts),
        "samples": (record_samples or samples)[:12],
        "entered": entered,
        "invent": False,
    }


def kimco_gas_numbers(client: KimcoClient, vendor_id: int | None) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        posted_id = lookup_id(vals.get("Vendor"))
        vendor_txt = str(
            lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name")) or ""
        )
        if vendor_id not in (None, "") and posted_id == vendor_id:
            pass
        elif is_gas_vendor_text(vendor_txt):
            pass
        else:
            continue
        number = exact_invoice_number(vals.get("Invoice_Number"))
        if number and item.get("id") not in (None, ""):
            found[number] = int(item["id"])
    return found


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_iso_date(str(value)[:10])


def catalog_row(msg: dict[str, Any], entered: dict[str, int]) -> dict[str, Any]:
    inv = _subject_inv(str(msg.get("subject") or ""))
    return {
        "invoice": inv,
        "received": msg.get("receivedDateTime"),
        "flagged": is_already_flagged(msg),
        "categories": msg.get("categories") or [],
        "flagStatus": (msg.get("flag") or {}).get("flagStatus"),
        "already_kimco": entered.get(inv),
        "hasAttachments": bool(msg.get("hasAttachments")),
        "from": sender_name(msg),
        "from_addr": sender_address(msg),
        "subject": str(msg.get("subject") or "")[:140],
    }


def bills_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> list[dict[str, Any]]:
    """One email → one bill per printed invoice #. Expand siblings. invent=false."""
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    pdfs = []
    if message.get("hasAttachments"):
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id)
    if not pdfs:
        return [
            {
                "vendor": VENDOR_NAME,
                "invoice_number": _subject_inv(subject),
                "date": None,
                "po": None,
                "amount": None,
                "hold_reason": "parse-error",
                "action": "hold",
                "graph_message_id": message_id,
                "subject": subject,
                "receivedDateTime": message.get("receivedDateTime"),
                "from_name": from_name,
                "field_sources": {},
                "pdf_unavailable": True,
            }
        ]
    out: list[dict[str, Any]] = []
    for filename, content in pdfs:
        dest = pdf_dir / (
            f"gas_{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_"
            f"{_safe_filename(filename)}"
        )
        if dest.exists():
            dest = pdf_dir / f"{len(out)}_{dest.name}"
        dest.write_bytes(content)
        parsed = parse_invoice_pdf(
            dest, subject=subject, from_name=from_name, from_address=from_addr
        )
        extras = list(parsed.pop("siblings", []) or [])
        for bill in [parsed, *extras]:
            if bill.get("is_purchase_order_doc") or bill.get("is_receipt_scan_doc"):
                continue
            if bill.get("is_statement_doc"):
                continue
            if bill.get("check_stop") and not bill.get("invoice_number"):
                continue
            if not names_match(VENDOR_NAME, str(bill.get("vendor") or "")):
                if is_gas_vendor_text(str(bill.get("vendor") or "")) or blob_has_gas_supply(
                    " ".join([str(bill.get("vendor") or ""), from_name, subject])
                ):
                    bill["vendor"] = VENDOR_NAME
            bill["pdf_path"] = str(bill.get("pdf_path") or dest)
            bill["graph_message_id"] = message_id
            bill["subject"] = subject
            bill["receivedDateTime"] = message.get("receivedDateTime")
            bill["from_name"] = from_name
            bill["action"] = "create"
            bill["id"] = message_id
            bill["download_method"] = "attachment"
            bill["pdf_bytes"] = dest.stat().st_size if dest.is_file() else len(content)
            if bill.get("amount") in (None, "") and not bill.get("gas_misc_ambiguous"):
                # Labeled total truly missing — do not invent.
                bill["gas_misc_ambiguous"] = True
            out.append(bill)
    return out or [
        {
            "vendor": VENDOR_NAME,
            "invoice_number": _subject_inv(subject),
            "amount": None,
            "hold_reason": "parse-error",
            "graph_message_id": message_id,
            "subject": subject,
            "receivedDateTime": message.get("receivedDateTime"),
            "pdf_unavailable": True,
        }
    ]


def pick_recent(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Newest unentered bills dated on/after Aug 1. Cap is bills, not emails."""
    recent: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bill in parsed_bills:
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in already or inv in seen:
            continue
        seen.add(inv)
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            older.append(bill)
            continue
        recent.append(bill)
    recent.sort(
        key=lambda b: (
            str(b.get("date") or ""),
            str(b.get("receivedDateTime") or ""),
            exact_invoice_number(b.get("invoice_number")),
        ),
        reverse=True,
    )
    return recent[:cap], older + recent[cap:]


def already_entered_numbers() -> set[str]:
    """First-pass + plus-5 + plus-10 Success/HOLD. Do not pick again."""
    return (
        set(CREATED_HEADERS)
        | set(PLUS5_HEADERS)
        | set(KNOWN_HOLD)
        | set(PLUS10_HEADERS)
        | set(PLUS10_HOLDS)
    )


def pick_plus5(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    preferred: tuple[str, ...] = PLUS5_PREFERRED,
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prefer leftover invoices if still open; fill from newest Aug 1+."""
    blocked = set(already)
    by_inv: dict[str, dict[str, Any]] = {}
    for bill in parsed_bills:
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in blocked or inv in by_inv:
            continue
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            continue
        by_inv[inv] = bill
    chosen: list[dict[str, Any]] = []
    for inv in preferred:
        bill = by_inv.get(inv)
        if bill:
            chosen.append(bill)
        if len(chosen) >= cap:
            break
    if len(chosen) < cap:
        rest, _older = pick_recent(
            parsed_bills,
            already=blocked | {exact_invoice_number(b.get("invoice_number")) for b in chosen},
            cap=cap - len(chosen),
        )
        chosen.extend(rest)
    chosen_invs = {exact_invoice_number(b.get("invoice_number")) for b in chosen}
    leftover = [
        b
        for b in parsed_bills
        if exact_invoice_number(b.get("invoice_number"))
        and exact_invoice_number(b.get("invoice_number")) not in chosen_invs
        and exact_invoice_number(b.get("invoice_number")) not in blocked
    ]
    return chosen[:cap], leftover


def pick_plus10(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Next Aug 1+ window after plus-5 leftovers. Discover — do not prefer them."""
    blocked = set(already) | already_entered_numbers()
    return pick_recent(parsed_bills, already=blocked, cap=cap)


def _eligible_aug1(
    parsed_bills: list[dict[str, Any]],
    *,
    blocked: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bill in parsed_bills:
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in blocked or inv in seen:
            continue
        seen.add(inv)
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            older.append(bill)
            continue
        eligible.append(bill)
    eligible.sort(
        key=lambda b: (
            str(b.get("date") or ""),
            str(b.get("receivedDateTime") or ""),
            exact_invoice_number(b.get("invoice_number")),
        ),
        reverse=True,
    )
    return eligible, older


def is_pickable_gas_bill(bill: dict[str, Any]) -> bool:
    """Real invoice page with a #. Skip statement aging / CHECK STOP notices."""
    if bill.get("is_statement_doc"):
        return False
    inv = exact_invoice_number(bill.get("invoice_number"))
    if not inv:
        return False
    hold = str(bill.get("hold_reason") or "").upper()
    has_amt = bill.get("amount") not in (None, "")
    has_lines = bool(bill.get("lines") or [])
    has_po = bool(str(bill.get("po") or "").strip())
    if hold == "CHECK STOP" and not has_amt and not has_lines:
        return False
    # Aging / STATEMENT pages leak invoice #s with no total and no merch.
    if not has_po and not has_amt and not has_lines:
        return False
    return True


def is_finishable_nopo(bill: dict[str, Any]) -> bool:
    """No-PO shop-supply page we can Lines-K (has after-tax amount)."""
    if not is_pickable_gas_bill(bill):
        return False
    if str(bill.get("po") or "").strip():
        return False
    return bill.get("amount") not in (None, "")


def pick_plus15(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Next 5 after plus-10. Prefer no-PO shop-supply bills we can Lines-K.

    If the remaining Aug 1+ window is PO-cited, fill honestly (HOLD missing_po
    when the PO is not live). Do not prefer 59081 leftovers over a no-PO bill.
    Statement / CHECK STOP pages are not bills.
    """
    blocked = set(already) | already_entered_numbers()
    eligible, older = _eligible_aug1(parsed_bills, blocked=blocked)
    eligible = [b for b in eligible if is_pickable_gas_bill(b)]
    no_po = [b for b in eligible if is_finishable_nopo(b)]
    with_po = [b for b in eligible if str(b.get("po") or "").strip()]
    chosen = (no_po + with_po)[:cap]
    chosen_invs = {exact_invoice_number(b.get("invoice_number")) for b in chosen}
    leftover = [
        b for b in eligible if exact_invoice_number(b.get("invoice_number")) not in chosen_invs
    ] + older
    return chosen, leftover


def leftover_from_catalog(
    leftover_bills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for bill in leftover_bills:
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in seen:
            continue
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            continue
        if not is_pickable_gas_bill(bill):
            continue
        seen.add(inv)
        out.append(
            {
                "invoice_number": inv,
                "date": bill.get("date"),
                "po": bill.get("po"),
                "amount": bill.get("amount"),
                "received": bill.get("receivedDateTime"),
                "why": "unflagged leftover after plus-15 cap 5; not entered",
            }
        )
    return out


def merchandise_lines_from_parsed(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """PDF merch rows only. Fuel surcharge is Fees, not Lines-K."""
    out: list[dict[str, Any]] = []
    for line in parsed.get("lines") or []:
        blob = f"{line.get('part') or ''} {line.get('description') or line.get('label') or ''}"
        if is_fee_or_surcharge(blob) or str(line.get("part") or "").startswith("$SUR"):
            continue
        out.append(line)
    return out


def apply_type4_misc_lines(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    vendor_id: int,
    proof: dict[str, Any],
    lookup_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """NOTE-42: Type 4 no-PO needs Lines-K + Shop Supplies - G&S before Success."""
    if parsed.get("po") or proof.get("invoice_type") != 4:
        return {"status": "skip-not-type4"}
    if int(kimco_id) in DO_NOT_MUTATE_IDS and type4_shop_supplies_lines_ok(
        proof.get("misc_lines") or []
    ):
        return {"status": "already-ok", "skipped_existing": True}
    merch = merchandise_lines_from_parsed(parsed)
    existing = list(proof.get("misc_lines") or [])
    if existing_misc_lines_match_pdf(existing, merch):
        return {"status": "already-ok", "lines": len(existing)}
    if existing:
        return {
            "status": "blocker",
            "why": "Lines-K already has items that do not match this PDF",
        }
    if not merch:
        return {"status": "blocker", "why": "No merchandise PDF lines for this invoice"}
    found = (lookup_cache or {}).get("found")
    if not found:
        records = [client.get_item("ap_invoices", iid) for iid in LINES_K_LOOKUP_IDS]
        found = collect_shop_supplies_gs_from_records(records)
        if lookup_cache is not None:
            lookup_cache["found"] = found
    if int((found.get("misc_item") or {}).get("id") or 0) != 31:
        return {
            "status": "blocker",
            "why": f"Live Shop Supplies - G&S id is not 31: {found}",
        }
    payload = misc_add_item_payload(
        merch,
        invoice_id=int(kimco_id),
        vendor_id=int(vendor_id),
        misc_item=found["misc_item"],
        gl_account=found.get("gl_account"),
    )
    if payload_has_receipt(payload):
        raise KimcoError("Refusing payload that includes Receipt")
    put = client.request("PUT", client._record_url("ap_invoices", kimco_id), json=payload)
    return {
        "status": "added" if put.status_code < 400 else "blocker",
        "http": put.status_code,
        "lines": len(merch),
        "category": found.get("misc_item"),
        "why": None if put.status_code < 400 else f"PUT HTTP {put.status_code}",
    }


def apply_gas_exception_category_owner(row: dict[str, Any]) -> dict[str, Any]:
    """NOTE-39 columns. Purchasing / missing_po owner is Shawn — never Misty."""
    out = apply_exception_category_owner(dict(row))
    if str(out.get(COL_EXCEPTION_CATEGORY) or "") != "missing_po":
        return out
    out[COL_EXCEPTION_OWNER] = PURCHASING_OWNER
    why = str(out.get("Why") or "")
    why = re.sub(
        r"category=missing_po;\s*owner=[^.]*",
        f"category=missing_po; owner={PURCHASING_OWNER}",
        why,
        count=1,
        flags=re.I,
    )
    why = re.sub(r"@?Misty McCoy(?:\s*/\s*Transfer AP)?", PURCHASING_OWNER, why)
    out["Why"] = why
    return out


def create_gas_supply_batch(client: KimcoClient) -> dict[str, Any]:
    """Reuse batch 720 when it still has the Gas & Supply name. Never 715/716/717."""
    try:
        known = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    except KimcoError:
        known = None
    if (
        known
        and known.get("matches_expected")
        and not known.get("is_forbidden")
        and int(known.get("id") or 0) == KNOWN_BATCH_ID
    ):
        return {
            "id": KNOWN_BATCH_ID,
            "name": PREFERRED_BATCH_NAME,
            "created": False,
            "status": known.get("status"),
            "unposted": known.get("unposted"),
        }

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
        if bid in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_REUSE_NAMES:
            raise KimcoError(f"Refusing forbidden batch id={bid} name={name}")
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
            raise KimcoError(f"Refusing forbidden batch id={bid} name={got_name}")
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
        "is_forbidden": batch.get("id") in FORBIDDEN_BATCH_IDS,
    }


def hydrate_receipts(client: KimcoClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in rows:
        if rec.get("qty") is not None and rec.get("unit_price") is not None:
            out.append(rec)
            continue
        rid = rec.get("id")
        if rid in (None, ""):
            out.append(rec)
            continue
        item = client.get_item("receipts", int(rid))
        filled = normalize_receipt(item)
        merged = dict(rec)
        for key in ("qty", "unit_price", "amount", "part", "po", "name"):
            if merged.get(key) in (None, "") and filled.get(key) not in (None, ""):
                merged[key] = filled.get(key)
        out.append(merged)
    return out


def open_receipts_on_po(receipts: list[dict[str, Any]], po: str | None) -> list[dict[str, Any]]:
    wanted = invoice_number_key(po or "")
    if not wanted:
        return []
    open_rows: list[dict[str, Any]] = []
    for rec in receipts:
        if invoice_number_key(str(rec.get("po") or rec.get("name") or "")) != wanted:
            continue
        raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
        invoiced = raw.get("Invoiced") or raw.get("invoiced")
        if invoiced in {True, "true", 1, "1"}:
            continue
        open_rows.append(rec)
    return open_rows


def charges_from_item(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    out: list[dict[str, Any]] = []
    lists = item.get("lists") if isinstance(item.get("lists"), dict) else {}
    for key in ADDITIONAL_CHARGE_LISTS:
        for raw in lists.get(key) or []:
            if not isinstance(raw, dict):
                continue
            values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
            kind = (
                values.get("Additional_Charges")
                or values.get("Additional_Charge")
                or values.get("Name")
                or ""
            )
            lookup = None
            text = ""
            if isinstance(kind, dict):
                lookup = lookup_id(kind)
                text = str(lookup_text(kind) or "")
            else:
                text = str(kind or "")
            out.append(
                {
                    "lookup_id": lookup,
                    "text": text,
                    "amount": money(values.get("Amount") or values.get("Charge_Amount")),
                    "description": str(values.get("Description") or text),
                }
            )
    return out


def _is_fee_charge(charge: dict[str, Any]) -> bool:
    lookup = charge.get("lookup_id")
    blob = f"{charge.get('text') or ''} {charge.get('description') or ''}".lower()
    if lookup == FEE_CHARGE_LOOKUP_ID:
        return True
    if lookup == PPV_CHARGE_LOOKUP_ID:
        return False
    return "fee" in blob or "surcharge" in blob


def _is_ppv_charge(charge: dict[str, Any]) -> bool:
    lookup = charge.get("lookup_id")
    blob = f"{charge.get('text') or ''} {charge.get('description') or ''}".lower()
    if lookup == PPV_CHARGE_LOOKUP_ID:
        return True
    return "purchase price variance" in blob or blob.strip() == "ppv"


def gas_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
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
    proof["fee_count"] = len(proof["fee_amounts"]) + len(proof["ppv_amounts"])
    proof["comments"] = str(vals.get("Comments") or "")
    proof["misc_lines"] = misc_line_snapshot(item)
    return proof


def ppv_total_from_selectable(
    selectable: list[dict[str, Any]],
    *,
    invoice_total: Any,
) -> float:
    running = 0.0
    groups: dict[Any, list[dict[str, Any]]] = {}
    for hit in selectable:
        line = hit.get("line") if isinstance(hit.get("line"), dict) else {}
        groups.setdefault(id(line) if line else id(hit), []).append(hit)
    for hits in groups.values():
        line = hits[0].get("line") if isinstance(hits[0].get("line"), dict) else {}
        inv_amt = line_cost(line)
        rec_amt = 0.0
        rec_units: list[float] = []
        rec_qty_sum = 0.0
        have_rec = False
        for hit in hits:
            rec = hit.get("receipt") if isinstance(hit.get("receipt"), dict) else {}
            one = receipt_cost(rec)
            unit = money(rec.get("unit_price"))
            rq = money(rec.get("qty") if rec.get("qty") is not None else rec.get("quantity"))
            if unit is not None:
                rec_units.append(unit)
            if rq is not None:
                rec_qty_sum = round(rec_qty_sum + rq, 4)
            if one is not None:
                rec_amt = round(rec_amt + one, 2)
                have_rec = True
        if inv_amt is None or not have_rec:
            continue
        shared_unit = rec_units[0] if rec_units and all(u == rec_units[0] for u in rec_units) else None
        decision = decide_ppv(
            invoice_line_amount=inv_amt,
            po_line_amount=rec_amt,
            invoice_total=float(money(invoice_total) or 0.0),
            ppv_already_on_bill=running,
            po_unit_price=shared_unit,
            invoice_unit_price=money(line.get("unit_price")),
            qty=money(line.get("qty")) if money(line.get("qty")) is not None else rec_qty_sum,
            label=str(line.get("label") or line.get("part") or ""),
        )
        if decision.get("action") == "ppv":
            running = round(running + float(decision.get("ppv") or 0.0), 2)
    return running


def _receipt_ids_from_proof(proof: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        if rid not in (None, ""):
            out.add(int(rid))
    return out


def format_receipts(proof: dict[str, Any]) -> str:
    bits: list[str] = []
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        qty = line.get("qty")
        unit = line.get("unit")
        if qty is not None and unit is not None:
            bits.append(f"{rid} {qty:g}@{unit}")
        else:
            bits.append(str(rid))
    return "; ".join(bits) if bits else "none"


def load_list_receipts(client: KimcoClient) -> list[dict[str, Any]]:
    return [normalize_receipt(item) for item in client.list_items("receipts")]


def _qty_hold(parsed: dict[str, Any], recs: list[dict[str, Any]]) -> bool:
    merch_qty = 0.0
    have_merch = False
    for ln in parsed.get("lines") or []:
        q = money(ln.get("qty"))
        if q is not None:
            merch_qty = round(merch_qty + q, 4)
            have_merch = True
    rec_qty = 0.0
    have_rec = False
    for rec in recs:
        q = money(rec.get("qty"))
        if q is not None:
            rec_qty = round(rec_qty + q, 4)
            have_rec = True
    if not have_merch:
        return False
    if not have_rec:
        return True
    return abs(merch_qty - rec_qty) > 0.001


def _merch_summary(parsed: dict[str, Any]) -> str:
    bits = []
    for ln in parsed.get("lines") or []:
        bits.append(f"{ln.get('part') or ln.get('label')} {ln.get('qty')}@{ln.get('unit_price')}")
    return "; ".join(bits) if bits else "no merch lines"


def finish_rounding_ppv_only(
    client: KimcoClient,
    *,
    kimco_id: int,
    pdf_amount: Any,
) -> dict[str, Any]:
    before = gas_proof(client, kimco_id)
    recs = before.get("receipt_lines") or []
    decision = rounding_ppv_to_hit_pdf_total(
        pdf_amount,
        before.get("invoice_amount"),
        receipts_selected=bool(recs),
    )
    existing = round(sum(before.get("ppv_amounts") or []), 2)
    needed = money(decision.get("ppv")) or 0.0
    status = "none"
    if decision.get("action") == "ppv" and needed:
        remaining = round(needed - existing, 2)
        if abs(remaining) <= 0.02:
            status = "already-posted"
        else:
            status = client.try_post_ppv(int(kimco_id), remaining)
    elif decision.get("action") == "match":
        status = "match"
    elif decision.get("action") == "hold":
        status = "over-ppv-lock"
    after = gas_proof(client, kimco_id)
    return {
        "ppv_amount": needed if decision.get("action") == "ppv" else 0.0,
        "ppv_status": status,
        "decision": decision,
        "after": after,
        "mutated": status == "posted",
    }


def finish_hold_header(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select Receipts when a PO exists. Combine same-item. Over-PPV → select zero."""
    po = str(parsed.get("po") or "").strip()
    proof = gas_proof(client, kimco_id)
    if not po:
        return {
            "wanted": [],
            "select_status": "no-po-misc",
            "fee_status": "none",
            "ppv_status": "none",
            "ppv_amount": 0.0,
            "skipped_over_ppv": False,
            "select_zero": False,
            "matched": [],
            "open_on_po": [],
            "after": proof,
        }
    have = _receipt_ids_from_proof(proof)
    pool = hydrate_receipts(client, open_receipts_on_po(receipts, po))
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=pool,
        po_number=po,
        invoice_amount=parsed.get("amount"),
    )
    matched = list(match.get("matched") or [])
    locked = filter_matches_outside_ppv_gate(matched, invoice_total=parsed.get("amount"))
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
        elif select_status in {"selected", "already-selected", "no-po-misc"}:
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

    after = gas_proof(client, kimco_id)
    if select_status in {"selected", "already-selected"} and (after.get("receipt_lines") or []):
        round_ppv = finish_rounding_ppv_only(
            client, kimco_id=kimco_id, pdf_amount=parsed.get("amount")
        )
        if round_ppv.get("ppv_amount"):
            ppv_amt = round_ppv["ppv_amount"]
            ppv_status = round_ppv.get("ppv_status") or ppv_status
            after = round_ppv.get("after") or after
    return {
        "wanted": wanted,
        "select_status": select_status,
        "fee_status": fee_status,
        "ppv_status": ppv_status,
        "ppv_amount": ppv_amt,
        "match_how": match.get("hows") or match.get("how"),
        "matched": matched,
        "skipped_over_ppv": bool(skipped),
        "select_zero": bool(locked.get("select_zero")),
        "open_on_po": [
            {"id": r.get("id"), "qty": r.get("qty"), "unit_price": r.get("unit_price")}
            for r in pool
        ],
        "after": after,
    }


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
        f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on Gas and Supply {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Leftover vs invoice line is over the "
        "PPV gate. Receipts were NOT selected so purchasing can unreceive, "
        "change the PO price, and re-receive. Do not alter receipt unit price in GI."
    )


def find_transfer_ap_batch(batches: list[dict[str, Any]]) -> dict[str, Any]:
    """Lookup Transfer AP by name. Never invent id 375."""
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
    if str(row.get("Exception category") or "") == "price_variance":
        return True
    if str(row.get("Result") or "") != "HOLD":
        return False
    finish = finish or {}
    why = str(row.get("Why") or "").lower()
    if "price-does-not-match" in why or "over the ppv gate" in why:
        return True
    return bool(finish.get("select_zero") or finish.get("skipped_over_ppv"))


def probe_mention_notify(
    client: KimcoClient,
    kimco_id: int,
    after: dict[str, Any],
) -> dict[str, Any]:
    vals = after.get("values") if isinstance(after.get("values"), dict) else {}
    comments = str((vals or {}).get("Comments") or "")
    tagged = SHAWN_MCKIBBEN in comments
    mention_value_keys = sorted(
        k for k in (vals or {}) if re.search(r"mention|notif|tagged", str(k), flags=re.I)
    )
    probes: list[dict[str, Any]] = []
    for suffix in ("comments", "mentions", "notifications"):
        try:
            url = client._record_url("ap_invoices", int(kimco_id), suffix)
            resp = client.request("GET", url)
            probes.append({"suffix": suffix, "http": resp.status_code})
        except Exception as exc:  # noqa: BLE001
            probes.append({"suffix": suffix, "http": None, "error": type(exc).__name__})
    if tagged and mention_value_keys:
        report = (
            f"Comments persisted {SHAWN_MCKIBBEN}. Record has mention-ish fields "
            f"{mention_value_keys}. Dedicated GET probes={probes}."
        )
        worked = "fields-present-notify-unconfirmed"
    elif tagged:
        report = (
            f"Comments persisted {SHAWN_MCKIBBEN}. No mention/notify field on the "
            f"record. Dedicated GET comments/mentions/notifications → {probes}. "
            "@mention notify not confirmed — cannot claim a user alert fired."
        )
        worked = False
    else:
        report = f"Comments did not persist {SHAWN_MCKIBBEN}. @mention notify did not work."
        worked = False
    return {
        "worked": worked,
        "comments_persisted": tagged,
        "mention_value_keys": mention_value_keys,
        "probes": probes,
        "report": report,
    }


def apply_over_ppv_transfer_ap(
    client: KimcoClient,
    *,
    kimco_id: int,
    comment: str,
    leave_alone_ids: set[int] | None = None,
) -> dict[str, Any]:
    blocked = set(leave_alone_ids or LEAVE_ALONE_HOLD_IDS) | set(DO_NOT_MUTATE_IDS)
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
    body, status, error = client.update(
        "ap_invoices",
        int(kimco_id),
        {
            "state": "Modified",
            "id": int(kimco_id),
            "values": {"AP_Invoice_Batch": {"id": bid}, "Comments": comment},
        },
    )
    try:
        after = client.get_item("ap_invoices", int(kimco_id))
    except KimcoError:
        after = {}
    vals = after.get("values") if isinstance(after.get("values"), dict) else {}
    comments_after = str((vals or {}).get("Comments") or "")
    mention = probe_mention_notify(client, int(kimco_id), after or {})
    live_bid = lookup_id((vals or {}).get("AP_Invoice_Batch"))
    live_name = lookup_text((vals or {}).get("AP_Invoice_Batch"))
    moved = status < 400 and live_bid == bid
    return {
        "status": "moved" if moved else f"blocked-{status}",
        "kimco_id": int(kimco_id),
        "batch_id": live_bid if live_bid not in (None, "") else bid,
        "batch_name": live_name or found.get("name"),
        "comment": comments_after or comment,
        "put": status,
        "error": error,
        "mention_notify": mention,
        "invent": False,
    }


def quality_gas_row(
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    proof: dict[str, Any],
    finish: dict[str, Any] | None,
    vendor_id: int | None,
    stamp_outlook: bool = False,
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
            f"ids={finish.get('wanted')} fees={finish.get('fee_status')} "
            f"ppv={finish.get('ppv_status')}. "
        )
    rec_merch = 0.0
    for rec in recs:
        q = money(rec.get("qty"))
        u = money(rec.get("unit"))
        if q is not None and u is not None:
            rec_merch = round(rec_merch + q * u, 2)
    qty_hold = _qty_hold(parsed, recs) if recs or parsed.get("lines") else bool(parsed.get("po"))
    fee_amts = list(proof.get("fee_amounts") or [])
    ppv_amts = list(proof.get("ppv_amounts") or [])
    parsed_fees = list(parsed.get("fees") or [])
    fees_ok = fees_posted_cover_parsed(fee_amts, parsed_fees) if parsed_fees else True
    fee_on_ppv = False
    for fee in fees_with_amounts(parsed_fees):
        for amt in ppv_amts:
            if amt is not None and abs(amt - fee["amount"]) <= 0.02:
                fee_on_ppv = True
    charge_sum = round(sum(a or 0 for a in fee_amts) + sum(a or 0 for a in ppv_amts), 2)
    rolled = round(rec_merch + charge_sum, 2)
    po = str(parsed.get("po") or enter_row.get("PO") or "").strip()
    needs_receipts = bool(po)
    type4_misc = (not needs_receipts) and proof.get("invoice_type") == 4
    misc_lines = list(proof.get("misc_lines") or [])
    lines_k_ok = type4_shop_supplies_lines_ok(misc_lines) if type4_misc else True
    amount_ok = False
    if pdf_amt is not None:
        if posted is not None and abs(posted - pdf_amt) <= 0.02:
            amount_ok = True
        elif (
            not type4_misc
            and ver is not None
            and abs(ver - pdf_amt) <= 0.02
            and abs(rolled - pdf_amt) <= 0.02
        ):
            amount_ok = True
    price_hold = bool((finish or {}).get("select_zero") or (finish or {}).get("skipped_over_ppv"))
    vendor_ok = vendor_id not in (None, "") and proof.get("vendor_id") == vendor_id
    already_posted = bool(recs) and amount_ok and (not qty_hold if needs_receipts else True)
    select_status = (finish or {}).get("select_status")
    select_ok = (
        already_posted
        or select_status in {None, "selected", "already-selected", "no-po-misc"}
        or not needs_receipts
    )
    cover_blocked = blocked_400_not_a_hold_when_same_item_cover(
        select_status, (finish or {}).get("matched")
    )
    pdf_number = exact_invoice_number(parsed.get("invoice_number") or enter_row.get("Invoice #"))
    posted_number = exact_invoice_number(proof.get("invoice_number") or enter_row.get("Invoice #"))
    number_ok = bool(pdf_number) and posted_number == pdf_number
    type_ok = (
        proof.get("invoice_type") == 3
        if needs_receipts
        else proof.get("invoice_type") in {3, 4}
    )
    finished = (
        attach_ok
        and (recs if needs_receipts else True)
        and (not qty_hold if needs_receipts else True)
        and fees_ok
        and not fee_on_ppv
        and amount_ok
        and type_ok
        and vendor_ok
        and not price_hold
        and select_ok
        and number_ok
        and lines_k_ok
        and pdf_amt not in (None, "")
    )
    out["Amount"] = pdf_amt
    out["KIMCO id"] = kid
    out["Invoice #"] = pdf_number or enter_row.get("Invoice #")
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
            f"Header PO set={po or 'none'}. "
            f"{'Select Receipts ' + format_receipts(proof) + ' on PO ' + (po or 'n/a') + '. ' if needs_receipts else 'Misc Type 4 Lines-K Shop Supplies - G&S (no PO). '}"
            f"Invoice #={pdf_number} (exact PDF). Amount={pdf_amt} after-tax "
            f"(never merchandise subtotal). "
            f"Fees={out['Fees and surcharges']} (Additional Charge Fees id 11, "
            f"not PPV). PPV={out['PPV']}. Attach status=attached. "
            f"{extra}Flag status=entered-in-ai."
        )
        out["Flag status"] = "entered-in-ai"
    elif price_hold:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (price-does-not-match): leftover vs invoice line is over the "
            f"PPV gate. Do not Select Receipts on that line. {SHAWN_MCKIBBEN}: "
            "purchasing must unreceive, change the PO price, and re-receive. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
    elif needs_receipts and (qty_hold or not recs):
        out["Result"] = "HOLD"
        if not recs:
            out["Why"] = (
                f"HOLD (receipt): no open receipt leftover on PO "
                f"{po or 'n/a'} for Gas and Supply invoice #{pdf_number}. "
                f"PDF merch={_merch_summary(parsed)}; selected=none. "
                "Do not first-open guess. Do not invent Success. "
                f"{RUBEN_PEREZ}: receiving must receive the PO so AP can "
                "Select Receipts. Parent email will be Entered with issues. "
                f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        else:
            out["Why"] = (
                f"HOLD (receipt): PDF merch {_merch_summary(parsed)} vs selected "
                f"({format_receipts(proof)}) on PO {po or 'n/a'}. "
                "Do not invent Success. "
                f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        out["Flag status"] = "entered-with-issues"
    elif fee_on_ppv or not fees_ok:
        out["Result"] = "HOLD"
        out["Why"] = (
            "HOLD (fees): Gas fuel/surcharge amounts must be Additional Charge "
            "Fees (id 11), never PPV. "
            f"Posted fees={fee_amts} ppv={ppv_amts}. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
    elif type4_misc and not lines_k_ok:
        out["Result"] = "Incomplete"
        out["Why"] = (
            "NOTE-42: Gas Misc Type 4 header-only is incomplete. Lines-K must "
            "have description, qty, unit cost, and live category Shop Supplies "
            "- G&S before Success. Fuel surcharge stays Additional Charge Fees "
            f"id 11. Posted lines={len(misc_lines)} amount={posted} "
            f"verification={ver} pdf={pdf_amt}. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
    elif cover_blocked:
        out["Result"] = "Incomplete"
        out["Why"] = (
            "NOTE-37: same-item same-unit-cost leftovers uniquely sum to the "
            "invoice line. Do not HOLD as Select Receipts blocked-400 when that "
            f"sum matches. {extra}Do not invent Success until live GET shows "
            "the combined receipts."
        )
        out["Flag status"] = enter_row.get("Flag status") or "entered-with-issues"
    elif pdf_amt in (None, ""):
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (preflight-parse): Gas and Supply {pdf_number} labeled "
            "Total / Amount Due / Amount This Invoice Including Tax is missing. "
            "Never invent a total. Not multiple Misc. "
            "Outlook AI HOLD. Flag status=ai-hold."
        )
        out["Flag status"] = "ai-hold"
    else:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD after live GET of {kid}: PDF amount={pdf_amt} posted={posted} "
            f"verification={ver} rolled={rolled} receipts={format_receipts(proof)} "
            f"attach={attach_ok} type={proof.get('invoice_type')} "
            f"vendor_id={proof.get('vendor_id')} expected={vendor_id}. "
            f"Do not invent Success. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
    if stamp_outlook and graph is not None and message_id:
        if out["Result"] == "Success":
            out["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, message_id)
        elif out.get("KIMCO id") not in (None, ""):
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
        else:
            out["outlook"] = graph.flag_hold(ALLOWED_MAILBOX, message_id)
    else:
        out["outlook"] = enter_row.get("outlook") or "parent-pending"
    return apply_gas_exception_category_owner(out)


def stamp_parent_emails(graph, rows: list[dict[str, Any]], parsed_bills: list[dict[str, Any]]) -> dict[str, str]:
    """One Outlook stamp per parent email after all sibling bills are decided."""
    if graph is None:
        return {}
    by_mid: dict[str, list[dict[str, Any]]] = {}
    mid_for_inv = {
        exact_invoice_number(b.get("invoice_number")): str(b.get("graph_message_id") or "")
        for b in parsed_bills
    }
    for row in rows:
        mid = mid_for_inv.get(exact_invoice_number(row.get("Invoice #"))) or ""
        if mid:
            by_mid.setdefault(mid, []).append(row)
    stamped: dict[str, str] = {}
    for mid, kids in by_mid.items():
        results = [str(r.get("Result") or "") for r in kids]
        has_header = any(r.get("KIMCO id") not in (None, "") for r in kids)
        if results and all(r == "Success" for r in results):
            stamped[mid] = str(graph.flag_matched(ALLOWED_MAILBOX, mid))
            flag = "entered-in-ai"
        elif has_header:
            stamped[mid] = str(graph.flag_issues(ALLOWED_MAILBOX, mid))
            flag = "entered-with-issues"
        else:
            stamped[mid] = str(graph.flag_hold(ALLOWED_MAILBOX, mid))
            flag = "ai-hold"
        for row in kids:
            row["outlook"] = stamped[mid]
            row["Flag in Outlook"] = "Yes"
            row["Flag status"] = flag
    return stamped


def finish_entered_rows(
    client: KimcoClient,
    graph,
    *,
    parsed_bills: list[dict[str, Any]],
    enter_rows: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    vendor_id: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    gets: dict[str, Any] = {}
    lines_k_lookup: dict[str, Any] = {}
    by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = exact_invoice_number(enter_row.get("Invoice #"))
        kid = enter_row.get("KIMCO id")
        parsed = by_inv.get(inv) or {}
        result = str(enter_row.get("Result") or "")
        if kid in (None, "") or not parsed:
            cleaned = dict(enter_row)
            cleaned["Why"] = re.sub(
                r"^category=[a-z0-9_]+;\s*owner=[^.]*\.\s*",
                "",
                str(cleaned.get("Why") or ""),
                flags=re.I,
            )
            rows.append(apply_gas_exception_category_owner(cleaned))
            continue
        if result in {"Fail", "Skipped"}:
            cleaned = dict(enter_row)
            cleaned["Why"] = re.sub(
                r"^category=[a-z0-9_]+;\s*owner=[^.]*\.\s*",
                "",
                str(cleaned.get("Why") or ""),
                flags=re.I,
            )
            rows.append(apply_gas_exception_category_owner(cleaned))
            continue
        if int(kid) in (LEAVE_ALONE_HOLD_IDS | DO_NOT_MUTATE_IDS) and result == "HOLD":
            proof = gas_proof(client, kid)
            row = apply_gas_exception_category_owner(dict(enter_row))
            rows.append(row)
            gets[str(kid)] = proof
            continue
        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=int(kid), receipts=receipts
        )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or gas_proof(client, kid)
        if (
            not parsed.get("po")
            and fees_with_amounts(list(parsed.get("fees") or []))
            and not fees_posted_cover_parsed(proof.get("fee_amounts") or [], parsed.get("fees") or [])
        ):
            fee_status = client.try_post_fees(int(kid), fees_with_amounts(parsed.get("fees") or []))
            finishes[inv]["fee_status"] = fee_status
            proof = gas_proof(client, kid)
        if not parsed.get("po") and proof.get("invoice_type") == 4:
            lines_k = apply_type4_misc_lines(
                client,
                parsed=parsed,
                kimco_id=int(kid),
                vendor_id=int(vendor_id or 0),
                proof=proof,
                lookup_cache=lines_k_lookup,
            )
            finishes[inv]["lines_k"] = lines_k
            if lines_k.get("status") in {"added", "already-ok"}:
                proof = gas_proof(client, kid)
        row = quality_gas_row(
            graph,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
            stamp_outlook=False,
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
                proof = gas_proof(client, kid)
            row["Why"] = (
                f"{row.get('Why')} Transfer AP status={transfer.get('status')} "
                f"batch_id={transfer.get('batch_id')} "
                f"comment={transfer.get('comment')!r} "
                f"@mention={mention.get('report') or mention}."
            )
            row = apply_gas_exception_category_owner(row)
        rows.append(row)
        gets[str(kid)] = proof
        receipts = load_list_receipts(client)
    return rows, finishes, gets


def merge_sheet_rows(
    prior_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen = {str(r.get("Invoice #") or "") for r in new_rows}
    keep = [r for r in prior_rows if str(r.get("Invoice #") or "") not in seen]
    return keep + new_rows


def prior_rows_from_sidecar(report_path: Path) -> list[dict[str, Any]]:
    sidecar = report_path.with_suffix(".json")
    if not sidecar.exists():
        return []
    payload = json.loads(sidecar.read_text())
    return [dict(r) for r in payload.get("rows") or [] if isinstance(r, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gas & Supply 9/17 dedicated session")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--create-batch-only", action="store_true")
    parser.add_argument("--refresh-sheet", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print("Target: live. No Mail.Send. invent=false.", flush=True)
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

    vendor_info = confirm_gas_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    print(
        json.dumps(
            {
                "vendor_confirm": {
                    "vendor_id": vendor_id,
                    "vendor_id_hint": VENDOR_ID_HINT,
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
        print("Live Gas & Supply vendor id not confirmed. Will not invent. Stop.", flush=True)
        return 2

    if args.refresh_sheet:
        report_path = Path(args.report)
        sidecar_path = report_path.with_suffix(".json")
        if not sidecar_path.exists():
            print(f"Missing sidecar {sidecar_path}", flush=True)
            return 2
        prior = json.loads(sidecar_path.read_text())
        batch = {
            "id": prior.get("batch_id"),
            "name": prior.get("batch_name"),
        }
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"refresh_batch": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden") or int(batch["id"]) in FORBIDDEN_BATCH_IDS:
            print("Refusing forbidden batch on refresh.", flush=True)
            return 2
        parsed_by_inv = {
            exact_invoice_number(p.get("invoice_number")): p
            for p in (prior.get("parsed") or [])
        }
        messages = find_gas_messages(graph)
        attach_index: list[tuple[str, str, str]] = []
        for msg in messages:
            if not msg.get("hasAttachments"):
                continue
            mid = str(msg.get("id") or "")
            recv = str(msg.get("receivedDateTime") or "")
            try:
                names = graph.list_attachment_names(ALLOWED_MAILBOX, mid)
            except Exception:  # noqa: BLE001
                names = []
            for name in names:
                attach_index.append((mid, recv, str(name or "").lower()))
        receipts = load_list_receipts(client)
        enter_rows = list(prior.get("rows") or prior.get("new_rows") or [])
        recent = []
        for row in enter_rows:
            inv = exact_invoice_number(row.get("Invoice #"))
            parsed = dict(parsed_by_inv.get(inv) or {})
            parsed.setdefault("invoice_number", inv)
            parsed.setdefault("amount", row.get("Amount"))
            parsed.setdefault("po", row.get("PO") or None)
            parsed.setdefault("vendor", VENDOR_NAME)
            path = str(parsed.get("pdf_path") or "").lower()
            if not parsed.get("graph_message_id") and path:
                stem = Path(path).name.lower()
                day_hit = re.search(r"20\d{2}-\d{2}-\d{2}", path)
                day = day_hit.group(0) if day_hit else ""
                for mid, recv, aname in attach_index:
                    if day and day not in recv:
                        continue
                    if aname and (aname in stem or stem.endswith(aname)):
                        parsed["graph_message_id"] = mid
                        break
                    if "g1378" in stem and "g1378" in aname:
                        parsed["graph_message_id"] = mid
                        break
                    if "a3050" in stem and "a3050" in aname:
                        parsed["graph_message_id"] = mid
                        break
            recent.append(parsed)
        new_rows, finishes, gets = finish_entered_rows(
            client,
            graph,
            parsed_bills=recent,
            enter_rows=enter_rows,
            receipts=receipts,
            vendor_id=int(vendor_id),
        )
        parent_stamps = stamp_parent_emails(graph, new_rows, recent)
        leftover_pending = list(prior.get("leftover_pending") or [])
        write_report(report_path, new_rows)
        print(f"Wrote {report_path}", flush=True)
        _print_summary(new_rows)
        prior.update(
            {
                "proof": "gas-supply-0917-refresh",
                "invent": False,
                "mail_send": False,
                "batch": verified,
                "new_rows": new_rows,
                "rows": new_rows,
                "finishes": finishes,
                "kimco_gets": gets,
                "parent_outlook": parent_stamps,
                "leftover_pending": leftover_pending,
                "report": str(report_path),
            }
        )
        sidecar_path.write_text(json.dumps(prior, indent=2, default=str) + "\n")
        print(f"Wrote {sidecar_path}", flush=True)
        return 0

    if args.create_batch_only:
        batch = create_gas_supply_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden"):
            print("Created/found batch is 715/716/717 — abort.", flush=True)
            return 2
        return 0

    entered = dict(vendor_info.get("entered") or {})
    if not entered:
        entered.update(kimco_gas_numbers(client, int(vendor_id)))
    print(f"KIMCO Gas & Supply invoices already present: {len(entered)}", flush=True)

    messages = find_gas_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(key=lambda r: str(r.get("received") or ""), reverse=True)
    print(json.dumps({"discovered": len(catalog), "gas_mail": catalog}, indent=2, default=str), flush=True)

    already = set(entered) | already_entered_numbers()
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    skipped_noise = 0
    for msg in messages:
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_gas_invoice_email(msg):
            skipped_noise += 1
            continue
        candidates.append(msg)
    candidates.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    print(
        json.dumps(
            {
                "unflagged_invoice_emails": [
                    {
                        "received": m.get("receivedDateTime"),
                        "subject": str(m.get("subject") or "")[:120],
                        "from": sender_address(m),
                    }
                    for m in candidates
                ],
                "skipped_flagged": skipped_flagged,
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
    parsed_bills: list[dict[str, Any]] = []
    for msg in candidates:
        parsed_bills.extend(bills_from_message(graph, msg, pdf_dir))
        recent_so_far, leftover_so_far = pick_plus15(parsed_bills, already=already, cap=CAP)
        no_po_picked = [b for b in recent_so_far if is_finishable_nopo(b)]
        leftover_no_po = [
            x for x in leftover_from_catalog(leftover_so_far) if not x.get("po")
        ]
        if len(no_po_picked) >= CAP:
            break
        if len(recent_so_far) >= CAP and not leftover_no_po:
            continue
    print(json.dumps({"parsed_candidates": [summarize_parse(b) for b in parsed_bills]}, indent=2, default=str), flush=True)

    recent, leftover_bills = pick_plus15(parsed_bills, already=already, cap=CAP)
    print(
        json.dumps(
            {
                "recent_picked": [summarize_parse(b) for b in recent],
                "leftover_not_entered": leftover_from_catalog(leftover_bills),
                "min_invoice_date": str(MIN_INVOICE_DATE),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.parse_only:
        return 0

    batch = create_gas_supply_batch(client)
    verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
    print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
    if verified.get("is_forbidden") or batch.get("id") in FORBIDDEN_BATCH_IDS:
        print("Refusing batch 715/716/717. Abort enter.", flush=True)
        return 2
    if str(batch.get("name")) in FORBIDDEN_REUSE_NAMES:
        print("Refusing Crosslink/JPSteel/Legacy batch name. Abort enter.", flush=True)
        return 2

    report_path = Path(args.report)
    leftover_pending = leftover_from_catalog(leftover_bills)
    prior_rows = prior_rows_from_sidecar(report_path)

    if not recent:
        reason = (
            "No unflagged Gas & Supply invoices dated on/after 2026-08-01 that "
            "are not already in KIMCO. Stopped — did not walk pre-Aug (NOTE-28)."
        )
        print(reason, flush=True)
        write_report(report_path, prior_rows)
        sidecar = {
            "proof": "gas-supply-0917-plus15",
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
            "discovered": len(catalog),
            "catalog": catalog,
            "kimco_already_count": len(entered),
            "recent_picked": [],
            "new_rows": [],
            "leftover_pending": leftover_pending,
            "rows": prior_rows,
            "blocker": reason,
            "treyce_emailed": False,
            "report": str(report_path),
        }
        report_path.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2, default=str) + "\n"
        )
        _print_summary(prior_rows)
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
    for row in enter_rows:
        batch_cell = str(row.get("Batch") or "")
        if re.search(r"\((715|716|717)\)", batch_cell) or batch_cell in FORBIDDEN_REUSE_NAMES:
            raise KimcoError(f"Enter posted to forbidden batch: {batch_cell}")

    receipts = load_list_receipts(client)
    new_rows, finishes, gets = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=int(vendor_id),
    )
    parent_stamps = stamp_parent_emails(graph, new_rows, recent)
    rows = merge_sheet_rows(prior_rows, new_rows)
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    sidecar = {
        "proof": "gas-supply-0917-plus15",
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
        "discovered": len(catalog),
        "catalog": catalog,
        "kimco_already_count": len(entered),
        "chosen": [exact_invoice_number(b.get("invoice_number")) for b in recent],
        "chosen_because": (
            "Plus-15: next 5 unflagged Gas & Supply after plus-10. Prefer "
            "no-PO Type 4 shop-supply bills and finish Lines-K (NOTE-42). "
            "Skip Success 10128–10133 / 10135 / 10136 and HOLDs 0040430010 / "
            "0040424839 / 0040423658/10134 / 0040438057/56/55/53. If the "
            "window is PO-cited, HOLD missing_po / Shawn honestly. "
            "Multi-invoice PDFs split. After-tax Amount This Invoice "
            "Including Tax. No Mail.Send."
        ),
        "plus5_preferred": list(PLUS5_PREFERRED),
        "plus5_headers": dict(PLUS5_HEADERS),
        "plus10_headers": dict(PLUS10_HEADERS),
        "plus10_holds": sorted(PLUS10_HOLDS),
        "created_headers": dict(CREATED_HEADERS),
        "known_hold": sorted(KNOWN_HOLD),
        "parsed": [summarize_parse(b) for b in recent],
        "leftover_pending": leftover_pending,
        "new_rows": new_rows,
        "rows": rows,
        "finishes": finishes,
        "kimco_gets": gets,
        "parent_outlook": parent_stamps,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
