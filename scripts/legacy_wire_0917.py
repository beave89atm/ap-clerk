"""Enter up to 5 recent unflagged Legacy Wire bills.

Dedicated live batch 717 — do NOT reuse Crosslink 715 or JPSteel 716.
Prefer name `API Agent - 9/17/26 Legacy Wire`; fall back to
`API Agent - 9/17/26` if the ERP rejects the suffix.

PDF attachments only. No Mail.Send. invent=false.

Skip already-flagged mail, already-entered invoices (including Kyle's
9995 / 9996, first-pass 10112–10116, plus-5 10123–10127), and signed
packing slips / receipt scans. Prefer invoice dates on/after 2026-08-01.
If fewer than 5 recent remain, enter what's left and stop — do not walk
pre-Aug (NOTE-28). Leave HOLDs 10116 / 10123 / 10125 / 10127 alone.

NOTE-40: new over-PPV HOLDs move to Transfer AP (lookup by name; prior
fact 375 is a hint only — never invent). Comments + @Shawn McKibben.
Still do not Select Receipts on over-gate lines (NOTE-29).
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
from ap_clerk.pdf_invoice import (  # noqa: E402
    ATTACHMENT_PACKING_SLIP,
    ATTACHMENT_POD,
    ATTACHMENT_RECEIPT_SCAN,
    classify_attachment,
    is_receipt_scan_document,
    parse_invoice_pdf,
)
from ap_clerk.quality_v12 import apply_exception_category_owner  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.transfer_ap import (  # noqa: E402
    TRANSFER_AP_PRIOR_ID_HINT as SHARED_TRANSFER_AP_HINT,
    build_over_ppv_transfer_ap_payload,
    find_transfer_ap_batch,
    log_dry_run_payload,
    over_ppv_hold_comment as shared_over_ppv_hold_comment,
)
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    TRANSFER_AP_BATCH_NAME,
    blocked_400_not_a_hold_when_same_item_cover,
    decide_ppv,
    distinctive_vendor_tokens,
    extract_subject_invoice_number,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    length_qty_equivalent,
    usable_open_leftovers,
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
    select_qty_from_receipt,
)

LOGGER = logging.getLogger("ap_clerk.legacy_wire_0917")

PREFERRED_BATCH_NAME = "API Agent - 9/17/26 Legacy Wire"
FALLBACK_BATCH_NAME = "API Agent - 9/17/26"
KNOWN_BATCH_ID = 717
# Kyle: Crosslink 715 / JPSteel 716. Do not reuse.
FORBIDDEN_BATCH_IDS = {715, 716}
RUBEN_PEREZ = "@Ruben Perez"
FORBIDDEN_REUSE_NAMES = {
    "API Agent - 9/16/26",
    "API Agent - 9/16/26 JPSteel",
    "API Agent - 9/16/26-2",
}

VENDOR_NAME = "Legacy Wire Products"
VENDOR_TOKENS = ("legacy wire", "legacywire")
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
# Live 9/15 batch 711. Kyle already worked these. Do not recreate / rewrite.
KNOWN_ENTERED = {
    "PS-INV103979": 9995,
    "PS-INV103980": 9996,
}
# First-pass headers on batch 717. Do not recreate.
CREATED_HEADERS = {
    "PS-INV104020": 10112,
    "PS-INV104019": 10113,
    "PS-INV104018": 10114,
    "PS-INV104015": 10115,
    "PS-INV104017": 10116,
}
FIRST_FIVE = tuple(CREATED_HEADERS)
# Plus-5 pending list (Kyle 2026-09-17). Prefer if still open.
NEXT_FIVE = (
    "PS-INV104013",
    "PS-INV104012",
    "PS-INV104011",
    "PS-INV104010",
    "PS-INV104009",
)
PREFERRED_NEXT = NEXT_FIVE
# Plus-5 headers on batch 717. Do not recreate.
PLUS5_HEADERS = {
    "PS-INV104013": 10123,
    "PS-INV104012": 10124,
    "PS-INV104011": 10125,
    "PS-INV104010": 10126,
    "PS-INV104009": 10127,
}
# Kyle: leave existing HOLDs alone unless finishing after Ruben (not this task).
# 10116 price_variance / Shawn. 10123 / 10125 / 10127 missing_receipt / Ruben.
LEAVE_ALONE_HOLD_IDS = {10116, 10123, 10125, 10127}
# Next plus-5 after 10123–10127. Filled from live discovery (not invented).
PLUS10_HEADERS: dict[str, int] = {}
# Graph needles for gap / newer PS-INV# that list-from-Aug-1 might miss.
PLUS10_SEARCH = (
    *tuple(f"PS-INV{n}" for n in range(104000, 104009)),
    "PS-INV104014",
    "PS-INV104016",
    *tuple(f"PS-INV{n}" for n in range(104021, 104031)),
)
# Prior fact only. Re-lookup Transfer AP by name. Never invent this id.
TRANSFER_AP_PRIOR_ID_HINT = SHARED_TRANSFER_AP_HINT
# 10114 (9@41 + 17@41) before 10113 (17@36 cover of 18@36) so qty-17
# leftover 24190 cannot be stolen. Reload receipts after each select.
FINISH_ORDER = (
    "PS-INV104020",
    "PS-INV104018",
    "PS-INV104015",
    "PS-INV104019",
    "PS-INV104017",
)
DO_NOT_MUTATE_IDS = {9995, 9996}
# Digit-only shortcuts that must never be written as the invoice #.
_PS_INV = re.compile(r"^PS-INV(\d{5,})$", flags=re.I)
NOISE_SUBJECT = re.compile(
    r"statement|past due|account with us|remittance|payment reminder",
    flags=re.I,
)
_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", flags=re.I)
NON_INVOICE_KINDS = {
    ATTACHMENT_PACKING_SLIP,
    ATTACHMENT_POD,
    ATTACHMENT_RECEIPT_SCAN,
}


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _subject_inv(subject: str) -> str:
    printed = extract_subject_invoice_number(subject)
    return invoice_number_key(printed) if printed else ""


def exact_invoice_number(value: str | None) -> str:
    """Printed PS-INV# wins. Never strip to the trailing digits."""
    key = invoice_number_key(value)
    if not key:
        return ""
    printed = extract_subject_invoice_number(key) or extract_subject_invoice_number(
        f"Invoice {key}"
    )
    if printed:
        return invoice_number_key(printed)
    return key


def invoice_aliases(number: str | None) -> set[str]:
    """Already-entered lookup only. Create still uses the exact PDF #."""
    key = exact_invoice_number(number)
    if not key:
        return set()
    out = {key}
    match = _PS_INV.match(key)
    if match:
        out.add(match.group(1))
    elif key.isdigit() and len(key) >= 5:
        out.add(f"PS-INV{key}")
    return out


def blob_has_legacy_wire(blob: str) -> bool:
    """Distinctive Legacy + Wire / LegacyWire. Generic 'wire' alone is never enough."""
    text = (blob or "").lower()
    compact = _compact(text)
    if "legacywire" in compact:
        return True
    if "legacy wire" in text or "legacy-wire" in text:
        return True
    tokens = set(distinctive_vendor_tokens(text))
    return "legacy" in tokens and "wire" in tokens


def is_legacy_wire_message(message: dict[str, Any]) -> bool:
    """Distinctive Legacy Wire only. Never MSC/RMP/JPSteel/Crosslink/AQPC."""
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    blob = " ".join([subject, preview, from_name, from_addr])
    if not blob_has_legacy_wire(blob):
        return False
    tokens = set(distinctive_vendor_tokens(from_name)) | set(
        distinctive_vendor_tokens(subject)
    )
    if tokens & {"msc", "rmp", "crosslink", "jpsteel", "aqpc"}:
        if not blob_has_legacy_wire(blob):
            return False
    return True


def is_legacy_wire_invoice_email(message: dict[str, Any]) -> bool:
    if not is_legacy_wire_message(message):
        return False
    subject = str(message.get("subject") or "")
    if NOISE_SUBJECT.search(subject):
        return False
    if _REPLY_PREFIX.match(subject) and not message.get("hasAttachments"):
        return False
    return bool(_subject_inv(subject) or message.get("hasAttachments"))


def is_legacy_wire_vendor_text(text: str | None) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if names_match(VENDOR_NAME, raw):
        return True
    return blob_has_legacy_wire(raw)


def is_packing_slip_attachment(*, filename: str = "", text: str = "") -> bool:
    """Signed packing slip / receipt scan / POD — not an invoice."""
    kind = classify_attachment(filename=filename, text=text)
    if kind in NON_INVOICE_KINDS:
        return True
    return is_receipt_scan_document(filename=filename, text=text)


def find_legacy_wire_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        "Legacy Wire",
        "LegacyWire",
        "legacywire",
        "Sales Invoice PS-INV",
        "Legacy Wire Products - Sales Invoice",
        "ar@legacywire.com",
        *[f"Sales Invoice {inv}" for inv in (*NEXT_FIVE, *PLUS10_SEARCH)],
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_legacy_wire_message(msg):
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
    except Exception as exc:  # noqa: BLE001 - search hits still usable
        LOGGER.info("Graph list from 2026-08-01 failed: %s", type(exc).__name__)
        listed = []
    for msg in listed:
        if not is_legacy_wire_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def confirm_legacy_wire_vendor(client: KimcoClient) -> dict[str, Any]:
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
        if not is_legacy_wire_vendor_text(vendor_txt):
            continue
        number = exact_invoice_number(vals.get("Invoice_Number"))
        kid = item.get("id")
        if number and kid not in (None, ""):
            entered[number] = int(kid)
            for alias in invoice_aliases(number):
                entered.setdefault(alias, int(kid))
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
        if posted in (None, "") or not is_legacy_wire_vendor_text(posted_txt):
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


def already_set(entered: dict[str, int]) -> set[str]:
    out: set[str] = set()
    for number in (
        list(entered)
        + list(KNOWN_ENTERED)
        + list(CREATED_HEADERS)
        + list(PLUS5_HEADERS)
        + list(PLUS10_HEADERS)
    ):
        out |= invoice_aliases(number)
    return out


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return parse_iso_date(str(value)[:10])


def bills_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> list[dict[str, Any]]:
    """One row per sales-invoice PDF. Packing slips / PODs are ignored."""
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    wanted = exact_invoice_number(message.get("_wanted_invoice") or _subject_inv(subject))
    pdfs: list[tuple[str, bytes]] = []
    if message.get("hasAttachments"):
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id)
    ignored: list[str] = []
    bills: list[dict[str, Any]] = []
    for filename, content in pdfs:
        dest = pdf_dir / (
            f"{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_"
            f"{_safe_filename(filename)}"
        )
        dest.write_bytes(content)
        if is_packing_slip_attachment(filename=filename):
            ignored.append(filename)
            continue
        parsed = parse_invoice_pdf(
            dest, subject=subject, from_name=from_name, from_address=from_addr
        )
        pdf_text = str(parsed.get("pdf_text") or parsed.get("text") or "")
        if is_packing_slip_attachment(filename=filename, text=pdf_text):
            ignored.append(filename)
            continue
        # Signed scans that inherit the subject PS-INV# but have no invoice face.
        if (
            not parsed.get("date")
            and parsed.get("amount") in (None, "")
            and not parsed.get("lines")
            and (
                re.search(r"_dragged_|receipt_", filename, flags=re.I)
                or classify_attachment(filename=filename, text=pdf_text) != "invoice"
            )
        ):
            ignored.append(filename)
            continue
        parsed["pdf_path"] = str(dest)
        parsed["graph_message_id"] = message_id
        parsed["subject"] = subject
        parsed["receivedDateTime"] = message.get("receivedDateTime")
        parsed["from_name"] = from_name
        parsed["action"] = "create"
        parsed["id"] = message_id
        parsed["download_method"] = "attachment"
        parsed["pdf_bytes"] = len(content)
        parsed["ignored_packing_slips"] = []
        if not names_match(VENDOR_NAME, str(parsed.get("vendor") or "")):
            if is_legacy_wire_vendor_text(str(parsed.get("vendor") or "")) or blob_has_legacy_wire(
                " ".join([str(parsed.get("vendor") or ""), from_name, subject])
            ):
                parsed["vendor"] = VENDOR_NAME
        printed = exact_invoice_number(parsed.get("invoice_number"))
        if printed:
            parsed["invoice_number"] = printed
        elif wanted:
            parsed["invoice_number"] = wanted
            sources = dict(parsed.get("field_sources") or {})
            sources.setdefault("invoice_number", "subject")
            parsed["field_sources"] = sources
        bills.append(parsed)
    if bills:
        for bill in bills:
            bill["ignored_packing_slips"] = ignored
        return bills
    if ignored:
        return [
            {
                "vendor": VENDOR_NAME,
                "invoice_number": wanted,
                "date": None,
                "po": None,
                "amount": None,
                "hold_reason": "packing_slip",
                "action": "skip",
                "skip_reason": "packing_slip",
                "graph_message_id": message_id,
                "subject": subject,
                "receivedDateTime": message.get("receivedDateTime"),
                "from_name": from_name,
                "ignored_packing_slips": ignored,
                "is_receipt_scan_doc": True,
                "attachment_class": "packing_slip",
            }
        ]
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
            "field_sources": {"invoice_number": "subject"} if wanted else {},
            "pdf_unavailable": True,
        }
    ]


def catalog_row(msg: dict[str, Any], entered: dict[str, int]) -> dict[str, Any]:
    inv = exact_invoice_number(_subject_inv(str(msg.get("subject") or "")))
    already = None
    for alias in invoice_aliases(inv):
        if alias in entered:
            already = entered[alias]
            break
    return {
        "invoice": inv,
        "received": msg.get("receivedDateTime"),
        "flagged": is_already_flagged(msg),
        "categories": msg.get("categories") or [],
        "flagStatus": (msg.get("flag") or {}).get("flagStatus"),
        "already_kimco": already,
        "hasAttachments": bool(msg.get("hasAttachments")),
        "from": sender_name(msg),
        "subject": str(msg.get("subject") or "")[:140],
    }


def pick_recent(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
    preferred: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep recent unentered invoices. Packing slips and older leftovers are reported.

    Plus-5 prefers NEXT_FIVE when those bills are still open. Date-desc fills
    any remaining slots. Never walks pre-Aug (NOTE-28).
    """
    recent: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    slips: list[dict[str, Any]] = []
    for bill in parsed_bills:
        if bill.get("skip_reason") == "packing_slip" or bill.get("hold_reason") == "packing_slip":
            slips.append(bill)
            continue
        inv = exact_invoice_number(bill.get("invoice_number"))
        if not inv or inv in already or already.intersection(invoice_aliases(inv)):
            continue
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            older.append(bill)
            continue
        recent.append(bill)
    recent.sort(
        key=lambda b: (
            str(b.get("date") or ""),
            str(b.get("receivedDateTime") or ""),
        ),
        reverse=True,
    )
    if preferred:
        by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in recent}
        pref_keys = [exact_invoice_number(n) for n in preferred if exact_invoice_number(n)]
        ordered = [by_inv[n] for n in pref_keys if n in by_inv]
        extras = [b for b in recent if exact_invoice_number(b.get("invoice_number")) not in set(pref_keys)]
        recent = ordered + extras
    return recent[:cap], older, slips


def prefer_candidate_messages(
    by_inv: dict[str, dict[str, Any]],
    *,
    preferred: tuple[str, ...] | list[str] = NEXT_FIVE,
    cap: int = CAP,
) -> list[dict[str, Any]]:
    """NEXT_FIVE first if still unentered, then newest extras. Cap discovery."""
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for inv in preferred:
        key = exact_invoice_number(inv)
        msg = by_inv.get(key)
        if msg is None:
            continue
        ordered.append(msg)
        seen.add(key)
    extras = sorted(
        (msg for key, msg in by_inv.items() if key not in seen and not key.startswith("unparsed-")),
        key=lambda m: str(m.get("receivedDateTime") or ""),
        reverse=True,
    )
    extras.extend(
        msg
        for key, msg in by_inv.items()
        if key.startswith("unparsed-") and msg not in extras and msg not in ordered
    )
    return (ordered + extras)[: max(cap * 3, cap)]


def prior_rows_from_sidecar(report_path: Path) -> list[dict[str, Any]]:
    sidecar = report_path.with_suffix(".json")
    if not sidecar.exists():
        return []
    payload = json.loads(sidecar.read_text())
    return [dict(r) for r in payload.get("rows") or [] if isinstance(r, dict)]


def merge_sheet_rows(
    prior_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep first-pass rows (including 10116 HOLD), then append this pass. No dup #."""
    seen = {exact_invoice_number(r.get("Invoice #")) for r in new_rows}
    seen.discard("")
    keep = [r for r in prior_rows if exact_invoice_number(r.get("Invoice #")) not in seen]
    return keep + new_rows


def create_legacy_wire_batch(client: KimcoClient) -> dict[str, Any]:
    """New dedicated batch. Never 715 / 716 / yesterday's Crosslink or JPSteel names."""
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
            raise KimcoError(f"Refusing to use forbidden batch id={bid} name={got_name}")
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
    """List-view receipts omit qty/unit. Record GET fills those fields."""
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
        qty = money(rec.get("qty") if rec.get("qty") is not None else rec.get("quantity"))
        if qty is not None and qty <= 0:
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


def legacy_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
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
            select_qty = money(hit.get("select_qty"))
            if select_qty is None:
                select_qty = select_qty_from_receipt(line, rec)
            unit = money(rec.get("unit_price"))
            rq = money(rec.get("qty") if rec.get("qty") is not None else rec.get("quantity"))
            if unit is None and rq and one is not None:
                unit = round(one / rq, 4)
            if select_qty is not None and unit is not None:
                one = round(select_qty * unit, 2)
                rec_qty_sum = round(rec_qty_sum + select_qty, 4)
            elif rq is not None:
                rec_qty_sum = round(rec_qty_sum + rq, 4)
            if unit is not None:
                rec_units.append(unit)
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


def _merch_summary(parsed: dict[str, Any]) -> str:
    bits: list[str] = []
    for ln in parsed.get("lines") or []:
        q = money(ln.get("qty"))
        u = money(ln.get("unit_price"))
        part = str(ln.get("part") or ln.get("label") or "")
        if q is not None and u is not None:
            bits.append(f"{part} {q:g}@{u:g}".strip())
        elif q is not None:
            bits.append(f"{part} qty {q:g}".strip())
    return "; ".join(bits) if bits else "no merch lines"


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
    """True when selected qty does not match invoice merch qty.

    Inches on a description are not rolled qty (Legacy PS-INV103979 77\" TUBE).
    length_qty_equivalent only when the invoice line carries explicit inches.
    """
    merch_qty = 0.0
    have_merch = False
    lines = list(parsed.get("lines") or [])
    for ln in lines:
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
    if abs(merch_qty - rec_qty) <= 0.001:
        return False
    if lines and recs and len(lines) == len(recs):
        if all(length_qty_equivalent(ln, rec) for ln, rec in zip(lines, recs)):
            return False
    return True


def finish_hold_header(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hydrate PO receipts, Select Receipts, post Fees then in-gate PPV."""
    if int(kimco_id) in DO_NOT_MUTATE_IDS:
        return {
            "wanted": [],
            "select_status": "do-not-mutate",
            "fee_status": "do-not-mutate",
            "ppv_status": "do-not-mutate",
            "ppv_amount": 0.0,
            "do_not_stamp_outlook": True,
            "after": legacy_proof(client, kimco_id),
        }
    proof = legacy_proof(client, kimco_id)
    have = _receipt_ids_from_proof(proof)
    pool = usable_open_leftovers(
        hydrate_receipts(client, open_receipts_on_po(receipts, str(parsed.get("po") or "")))
    )
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

    after = legacy_proof(client, kimco_id)
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
            {
                "id": r.get("id"),
                "qty": r.get("qty"),
                "unit_price": r.get("unit_price"),
                "amount": r.get("amount"),
                "part": r.get("part"),
            }
            for r in pool
        ],
        "after": after,
    }


def finish_rounding_ppv_only(
    client: KimcoClient,
    *,
    kimco_id: int,
    pdf_amount: Any,
) -> dict[str, Any]:
    """NOTE-38: post signed PPV so Invoice_Amount hits the PDF. No Select Receipts."""
    if int(kimco_id) in DO_NOT_MUTATE_IDS:
        return {
            "ppv_amount": 0.0,
            "ppv_status": "do-not-mutate",
            "decision": {"action": "skip", "ppv": 0.0},
            "after": legacy_proof(client, kimco_id),
            "mutated": False,
        }
    before = legacy_proof(client, kimco_id)
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
    after = legacy_proof(client, kimco_id)
    return {
        "ppv_amount": needed if decision.get("action") == "ppv" else 0.0,
        "ppv_status": status,
        "decision": decision,
        "before": {
            "invoice_amount": before.get("invoice_amount"),
            "verification": before.get("verification") or before.get("verification_amount"),
            "receipts": format_receipts(before),
            "ppv_amounts": before.get("ppv_amounts"),
        },
        "after": after,
        "mutated": status == "posted",
    }


def quality_legacy_row(
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
    try:
        kid_int = int(kid) if kid not in (None, "") else None
    except (TypeError, ValueError):
        kid_int = None
    do_not_stamp = kid_int in DO_NOT_MUTATE_IDS or bool((finish or {}).get("do_not_stamp_outlook"))
    cover_blocked = blocked_400_not_a_hold_when_same_item_cover(
        select_status, (finish or {}).get("matched")
    )
    posted_number = exact_invoice_number(proof.get("invoice_number") or enter_row.get("Invoice #"))
    pdf_number = exact_invoice_number(parsed.get("invoice_number") or enter_row.get("Invoice #"))
    number_ok = bool(pdf_number) and posted_number == pdf_number and not (
        pdf_number.isdigit() and len(pdf_number) >= 5
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
            f"Header PO set={po or 'none'}. Select Receipts "
            f"{format_receipts(proof)} on PO {po or 'n/a'}. "
            f"Invoice #={pdf_number} (exact PDF, not stripped). "
            f"Fees={out['Fees and surcharges']} (Additional Charge Fees id 11, "
            f"not PPV). PPV={out['PPV']}. Attach status=attached. "
            f"{extra}Flag status=entered-in-ai."
        )
        out["Flag status"] = "entered-in-ai"
        if graph is not None and message_id and not do_not_stamp:
            out["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, message_id)
        else:
            out["outlook"] = enter_row.get("outlook") or "left-as-kyle"
    elif price_hold:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (price-does-not-match): leftover vs invoice line is over the "
            f"PPV gate. Do not Select Receipts on that line. {SHAWN_MCKIBBEN}: "
            "purchasing must unreceive, change the PO price, and re-receive. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif qty_hold:
        out["Result"] = "HOLD"
        if not recs:
            invented = ""
            if ppv_amts:
                invented = (
                    f" Live GET still has invented PPV {ppv_amts} with no receipts "
                    "(Removed PUT rolled back). Treyce should delete that PPV. "
                )
            out["Why"] = (
                f"HOLD (receipt): no open receipt leftover on PO "
                f"{po or 'n/a'} for Legacy Wire invoice #{pdf_number}. "
                f"PDF merch={_merch_summary(parsed)}; selected=none. "
                "Do not first-open guess. Do not invent Success. "
                f"{RUBEN_PEREZ}: receiving must receive the PO so AP can "
                "Select Receipts. "
                f"{invented}{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        else:
            out["Why"] = (
                f"HOLD (receipt): PDF merch {_merch_summary(parsed)} vs selected "
                f"({format_receipts(proof)}) on PO {po or 'n/a'}. "
                "Inches on a description are not rolled qty. Do not invent Success. "
                f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
            )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif fee_on_ppv or not fees_ok:
        out["Result"] = "HOLD"
        out["Why"] = (
            "HOLD (fees): Legacy Wire freight/surcharges must be Additional Charge "
            "Fees (id 11), never PPV. "
            f"Posted fees={fee_amts} ppv={ppv_amts}. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif cover_blocked:
        out["Result"] = "Incomplete"
        out["Why"] = (
            "NOTE-37: same-item same-unit-cost leftovers uniquely sum to the "
            "invoice line. Do not HOLD as Select Receipts blocked-400 when that "
            f"sum matches. {extra}Do not invent Success until live GET shows "
            "the combined receipts. Outlook left alone."
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
        if graph is not None and message_id and kid not in (None, "") and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    return apply_exception_category_owner(out)


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
    by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = exact_invoice_number(enter_row.get("Invoice #"))
        kid = enter_row.get("KIMCO id")
        parsed = by_inv.get(inv) or {}
        result = str(enter_row.get("Result") or "")
        if kid in (None, "") or not parsed:
            rows.append(enter_row)
            continue
        if result in {"Fail", "Skipped"}:
            rows.append(enter_row)
            continue
        if kid not in (None, "") and int(kid) in LEAVE_ALONE_HOLD_IDS | DO_NOT_MUTATE_IDS:
            rows.append(enter_row)
            continue
        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=int(kid), receipts=receipts
        )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or legacy_proof(client, kid)
        row = quality_legacy_row(
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
            transfer = apply_over_ppv_transfer_ap(
                client,
                kimco_id=int(kid),
                comment=comment,
            )
            finishes[inv]["transfer_ap"] = transfer
            mention = transfer.get("mention_notify") or {}
            if transfer.get("status") == "moved":
                row["Batch"] = (
                    f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
                )
                proof = legacy_proof(client, kid)
            row["Why"] = (
                f"{row.get('Why')} Transfer AP status={transfer.get('status')} "
                f"batch_id={transfer.get('batch_id')} "
                f"comment={transfer.get('comment')!r} "
                f"@mention={mention.get('report') or mention}."
            )
        rows.append(row)
        gets[str(kid)] = proof
        receipts = load_list_receipts(client)
    return rows, finishes, gets


def remove_invented_ppv(client: KimcoClient, kimco_id: int) -> str:
    """Remove invented PPV (10113 −$85). Leave Fees alone. Persist if rolled back."""
    from ap_clerk.kimco import _ppv_charge_removals

    if int(kimco_id) in LEAVE_ALONE_HOLD_IDS | DO_NOT_MUTATE_IDS:
        return "leave-alone"
    item = client.get_item("ap_invoices", int(kimco_id))
    name, charges = _ppv_charge_removals(item)
    if not name or not charges:
        return "none"
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {name: charges},
    }
    url = client._record_url("ap_invoices", int(kimco_id))
    put = client.request("PUT", url, json=payload)
    if put.status_code >= 400:
        return f"blocked-{put.status_code}"
    after = client.get_item("ap_invoices", int(kimco_id))
    _, leftover = _ppv_charge_removals(after)
    if not leftover:
        return "removed"
    orig_ver = money((item.get("values") or {}).get("Invoice_Verification_Amount"))
    persist = {
        "state": "Modified",
        "id": int(kimco_id),
        "values": {"Invoice_Verification_Amount": 0},
        "lists": {name: charges},
    }
    put2 = client.request("PUT", url, json=persist)
    if put2.status_code >= 400:
        return "removed-rolled-back"
    after2 = client.get_item("ap_invoices", int(kimco_id))
    _, leftover2 = _ppv_charge_removals(after2)
    if orig_ver not in (None, 0, 0.0):
        client.update(
            "ap_invoices",
            kimco_id,
            {
                "state": "Modified",
                "id": int(kimco_id),
                "values": {"Invoice_Verification_Amount": orig_ver},
            },
        )
    if leftover2:
        return "removed-rolled-back"
    return "removed-persisted"


def remove_ppv_without_receipts(client: KimcoClient, kimco_id: int) -> str:
    """Remove invented PPV when no receipts are selected. Leave Fees alone."""
    item = client.get_item("ap_invoices", int(kimco_id))
    recs = []
    for line in (item.get("lists") or {}).get("APInvoiceLine") or []:
        lv = line.get("values") if isinstance(line, dict) else {}
        if lv.get("Receipt") not in (None, "", {}):
            recs.append(lv.get("Receipt"))
    if recs:
        return "keep-has-receipts"
    return remove_invented_ppv(client, kimco_id)


def find_invoice_message_id(graph, invoice_number: str) -> str:
    """Graph id for Outlook stamp. Search only; no Mail.Send."""
    if graph is None:
        return ""
    wanted = exact_invoice_number(invoice_number)
    if not wanted:
        return ""
    try:
        hits = graph.search_messages(
            ALLOWED_MAILBOX, f"Legacy Wire Products - Sales Invoice {wanted}", top=8
        )
    except Exception:  # noqa: BLE001 - stamp is best-effort
        return ""
    for msg in hits:
        subject = str(msg.get("subject") or "")
        if wanted in subject and is_legacy_wire_message(msg):
            return str(msg.get("id") or "")
    return ""


def download_invoice_pdf(graph, invoice_number: str, pdf_dir: Path) -> Path | None:
    """Attachment PDF for a first-pass header. invent=false — exact PS-INV#."""
    pdf_dir.mkdir(parents=True, exist_ok=True)
    matches = sorted(pdf_dir.glob(f"*Sales_Invoice_{invoice_number}.pdf")) or sorted(
        pdf_dir.glob(f"*{invoice_number}*.pdf")
    )
    if matches:
        return matches[0]
    if graph is None:
        return None
    mid = find_invoice_message_id(graph, invoice_number)
    if not mid:
        return None
    try:
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, mid)
    except Exception:  # noqa: BLE001 - refresh can still GET
        return None
    for filename, content in pdfs:
        if invoice_number.lower() not in (filename or "").lower():
            continue
        if is_packing_slip_attachment(filename=filename):
            continue
        dest = pdf_dir / f"2026-09-17_{_safe_filename(filename)}"
        dest.write_bytes(content)
        return dest
    return None


def leftover_from_catalog(
    catalog: list[dict[str, Any]],
    *,
    entered: dict[str, int],
    chosen: set[str],
) -> list[dict[str, Any]]:
    """Unflagged bills still pending after the cap. Do not walk pre-Aug."""
    already = already_set(entered)
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in catalog:
        inv = exact_invoice_number(row.get("invoice"))
        if not inv or inv in seen or inv in chosen:
            continue
        if (
            inv in CREATED_HEADERS
            or inv in PLUS5_HEADERS
            or inv in PLUS10_HEADERS
            or already.intersection(invoice_aliases(inv) | {inv})
        ):
            continue
        if row.get("flagged"):
            continue
        received = _parse_date(str(row.get("received") or "")[:10])
        if received is not None and received < MIN_INVOICE_DATE:
            continue
        seen.add(inv)
        pending.append(
            {
                "invoice_number": inv,
                "received": row.get("received"),
                "subject": row.get("subject"),
                "why": "unflagged leftover after plus-10 cap 5; not entered",
            }
        )
    return pending


def over_ppv_hold_comment(
    *,
    invoice_number: str,
    po: str | None,
    pdf_amount: Any,
) -> str:
    """KIMCO Comments text for a new over-PPV HOLD. Always @tags Shawn."""
    return shared_over_ppv_hold_comment(
        invoice_number=exact_invoice_number(invoice_number) or str(invoice_number or ""),
        po=po,
        pdf_amount=pdf_amount,
        vendor="Legacy Wire",
    )


def is_over_ppv_price_hold(
    row: dict[str, Any],
    finish: dict[str, Any] | None,
) -> bool:
    """True only for price-variance / over-gate HOLD. Not missing-receipt."""
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
    """Report exactly whether @mention notify can be confirmed. invent=false."""
    vals = after.get("values") if isinstance(after.get("values"), dict) else {}
    comments = str((vals or {}).get("Comments") or "")
    tagged = SHAWN_MCKIBBEN in comments
    mention_value_keys = sorted(
        k for k in (vals or {}) if re.search(r"mention|notif|tagged", str(k), flags=re.I)
    )
    mention_list_keys = sorted(
        k
        for k in (after.get("lists") or {})
        if re.search(r"comment|mention|notif", str(k), flags=re.I)
    )
    probes: list[dict[str, Any]] = []
    for suffix in ("comments", "mentions", "notifications"):
        try:
            url = client._record_url("ap_invoices", int(kimco_id), suffix)
            resp = client.request("GET", url)
            probes.append({"suffix": suffix, "http": resp.status_code})
        except Exception as exc:  # noqa: BLE001 - probe only
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
        report = (
            f"Comments did not persist {SHAWN_MCKIBBEN}. @mention notify did not work."
        )
        worked = False
    return {
        "worked": worked,
        "comments_persisted": tagged,
        "mention_value_keys": mention_value_keys,
        "mention_list_keys": mention_list_keys,
        "probes": probes,
        "report": report,
    }


def apply_over_ppv_transfer_ap(
    client: KimcoClient,
    *,
    kimco_id: int,
    comment: str,
    leave_alone_ids: set[int] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move a new over-PPV HOLD to Transfer AP and stamp @Shawn Comments.

    Lookup batch by name. Prior fact 375 is never a fallback. NOTE-29: do not
    Select Receipts here. dry_run=True builds/logs the PUT body and returns
    without PUT/PATCH/POST.
    """
    blocked = set(leave_alone_ids or LEAVE_ALONE_HOLD_IDS) | set(DO_NOT_MUTATE_IDS)
    if int(kimco_id) in blocked:
        return {"status": "leave-alone", "kimco_id": int(kimco_id), "invent": False, "dry_run": dry_run}
    try:
        batches = client.list_items("ap_batches")
    except KimcoError as exc:
        return {
            "status": "batch-list-failed",
            "error": str(exc)[:240],
            "invent": False,
            "dry_run": dry_run,
        }
    found = find_transfer_ap_batch(batches)
    if not found.get("found"):
        return {
            "status": "batch-not-found",
            "lookup": found,
            "invent": False,
            "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
            "dry_run": dry_run,
        }
    bid = int(found["id"])
    payload = build_over_ppv_transfer_ap_payload(
        kimco_id=int(kimco_id),
        batch_id=bid,
        comment=comment,
    )
    if dry_run:
        log_dry_run_payload(payload)
        return {
            "status": "dry-run",
            "kimco_id": int(kimco_id),
            "batch_id": bid,
            "batch_name": found.get("name"),
            "comment": comment,
            "payload": payload,
            "put": None,
            "error": "",
            "mention_notify": {"worked": None, "report": "dry-run: no PUT, notify not probed"},
            "invent": False,
            "writes": False,
            "dry_run": True,
        }
    body, status, error = client.update(
        "ap_invoices",
        int(kimco_id),
        payload,
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
        "comment_requested": comment,
        "put": status,
        "error": error,
        "put_body_keys": sorted(body) if isinstance(body, dict) else [],
        "mention_notify": mention,
        "invent": False,
    }


def run_refresh_sheet(
    client: KimcoClient,
    graph,
    report_path: Path,
    batch_info: dict[str, Any],
    *,
    vendor_id: int,
    vendor_info: dict[str, Any],
    stamp_outlook: bool = False,
) -> int:
    """Re-GET first-pass headers. Select new leftovers. Leave 10116 alone."""
    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    receipts = load_list_receipts(client)
    rows_by_inv: dict[str, dict[str, Any]] = {}
    finishes: dict[str, Any] = {}
    gets: dict[str, Any] = {}
    parsed_all: list[dict[str, Any]] = []
    batch_label = f"{batch_info.get('name')} ({batch_info.get('id')})"
    ppv_fix: dict[str, str] = {}

    for inv in FINISH_ORDER:
        kid = CREATED_HEADERS[inv]
        pdf_path = download_invoice_pdf(graph, inv, pdf_dir)
        if pdf_path is None:
            print(f"Missing PDF for {inv}; cannot refresh.", flush=True)
            continue
        parsed = parse_invoice_pdf(
            pdf_path,
            subject=f"Legacy Wire Products - Sales Invoice {inv}",
            from_name=VENDOR_NAME,
        )
        parsed["pdf_path"] = str(pdf_path)
        parsed["vendor"] = VENDOR_NAME
        parsed["invoice_number"] = inv
        message_id = find_invoice_message_id(graph, inv)
        if message_id:
            parsed["graph_message_id"] = message_id
        parsed_all.append(parsed)

        if kid in LEAVE_ALONE_HOLD_IDS:
            proof = legacy_proof(client, kid)
            finish = {
                "wanted": [],
                "select_status": "leave-alone",
                "fee_status": "already-posted",
                "ppv_status": "none",
                "ppv_amount": 0.0,
                "skipped_over_ppv": True,
                "select_zero": True,
                "do_not_stamp_outlook": True,
            }
            finishes[inv] = dict(finish)
            live_batch = proof.get("batch_text") or batch_info.get("name")
            live_bid = proof.get("batch_id") or batch_info.get("id")
            enter_row = {
                "Vendor": VENDOR_NAME,
                "Invoice #": inv,
                "date": parsed.get("date"),
                "PO": parsed.get("po") or "",
                "Amount": parsed.get("amount"),
                "Result": "HOLD",
                "Why": "",
                "KIMCO id": kid,
                "Batch": f"{live_batch} ({live_bid})",
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "",
                "Flag status": "entered-with-issues",
                "Flag in Outlook": "Yes",
                "Notes": "Left alone (Shawn price-does-not-match; receipts not selected).",
                "outlook": "left-as-kyle",
            }
            row = quality_legacy_row(
                None,
                parsed=parsed,
                enter_row=enter_row,
                proof=proof,
                finish=finish,
                vendor_id=vendor_id,
            )
            row["outlook"] = "left-as-kyle"
            row["Notes"] = enter_row["Notes"]
            rows_by_inv[inv] = row
            gets[str(kid)] = proof
            print(
                json.dumps(
                    {
                        "invoice": inv,
                        "kimco_id": kid,
                        "result": row.get("Result"),
                        "receipts": row.get("Receipts"),
                        "leave_alone": True,
                        "why": row.get("Why"),
                    },
                    indent=2,
                    default=str,
                ),
                flush=True,
            )
            continue

        proof_before = legacy_proof(client, kid)
        # 10113: leftover 18@$36 / selected qty 17 is same-unit cover, not PPV.
        # Never re-post the invented −$85 / +$170 once receipts are on the bill.
        if inv == "PS-INV104019" and (proof_before.get("ppv_amounts") or []):
            ppv_fix[inv] = remove_invented_ppv(client, kid)
            proof_before = legacy_proof(client, kid)
        elif proof_before.get("ppv_amounts") and not (proof_before.get("receipt_lines") or []):
            ppv_fix[inv] = remove_invented_ppv(client, kid)
            proof_before = legacy_proof(client, kid)

        already_selected = bool(proof_before.get("receipt_lines"))
        if already_selected:
            finish = {
                "wanted": [],
                "select_status": "already-selected",
                "fee_status": "already-posted",
                "ppv_status": "removed" if ppv_fix.get(inv) else "none",
                "ppv_amount": 0.0 if inv == "PS-INV104019" else sum(
                    proof_before.get("ppv_amounts") or []
                ),
                "match_how": "live-get-already-selected",
                "matched": [],
                "skipped_over_ppv": False,
                "select_zero": False,
                "open_on_po": [],
            }
            finish["after"] = proof_before
        else:
            finish = finish_hold_header(
                client, parsed=parsed, kimco_id=kid, receipts=receipts
            )
            after_ppv = finish.get("after") or {}
            if inv == "PS-INV104019" and (after_ppv.get("ppv_amounts") or []):
                extra = remove_invented_ppv(client, kid)
                ppv_fix[inv] = f"{ppv_fix.get(inv) or ''}+after:{extra}".strip("+")
                finish["after"] = legacy_proof(client, kid)
                finish["ppv_status"] = extra
                finish["ppv_amount"] = 0.0
        finish["do_not_stamp_outlook"] = not stamp_outlook
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or legacy_proof(client, kid)
        live_batch = proof.get("batch_text") or batch_info.get("name")
        live_bid = proof.get("batch_id") or batch_info.get("id")
        enter_row = {
            "Vendor": VENDOR_NAME,
            "Invoice #": inv,
            "date": parsed.get("date"),
            "PO": parsed.get("po") or "",
            "Amount": parsed.get("amount"),
            "Result": "HOLD",
            "Why": "",
            "KIMCO id": kid,
            "Batch": f"{live_batch} ({live_bid})",
            "Fees and surcharges": "none",
            "PPV": "none",
            "Attach status": "",
            "Flag status": "entered-with-issues",
            "Flag in Outlook": "Yes",
            "Notes": "",
            "outlook": "left-as-kyle",
        }
        row = quality_legacy_row(
            graph if stamp_outlook else None,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
        )
        if not stamp_outlook:
            row["outlook"] = "left-as-kyle"
        rows_by_inv[inv] = row
        gets[str(kid)] = proof
        receipts = load_list_receipts(client)
        print(
            json.dumps(
                {
                    "invoice": inv,
                    "kimco_id": kid,
                    "result": row.get("Result"),
                    "receipts": row.get("Receipts"),
                    "live_amount": proof.get("invoice_amount"),
                    "fees": row.get("Fees and surcharges"),
                    "ppv": row.get("PPV"),
                    "ppv_fix": ppv_fix.get(inv),
                    "outlook": row.get("outlook") or row.get("Flag status"),
                    "why": row.get("Why"),
                },
                indent=2,
                default=str,
            ),
            flush=True,
        )

    rows = [rows_by_inv[inv] for inv in CREATED_HEADERS if inv in rows_by_inv]

    leftover_pending = leftover_from_catalog(
        [],
        entered=dict(vendor_info.get("entered") or {}),
        chosen=set(CREATED_HEADERS),
    )
    sidecar_path = report_path.with_suffix(".json")
    prior = {}
    if sidecar_path.exists():
        prior = json.loads(sidecar_path.read_text())
        leftover_pending = list(prior.get("leftover_pending") or leftover_pending)
        catalog = list(prior.get("catalog") or [])
        leftover_pending = leftover_from_catalog(
            catalog,
            entered=dict(prior.get("kimco_already") or vendor_info.get("entered") or {}),
            chosen=set(CREATED_HEADERS),
        ) or leftover_pending

    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)
    sidecar = {
        "proof": "legacy-wire-0917",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": vendor_id,
        "vendor_confirm": {
            "vendor_id": vendor_id,
            "vendor_text_samples": vendor_info.get("vendor_text_samples"),
            "id_counts": vendor_info.get("id_counts"),
        },
        "batch_name": batch_info.get("name"),
        "batch_id": batch_info.get("id"),
        "batch": batch_info,
        "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
        "created_headers": CREATED_HEADERS,
        "known_entered": KNOWN_ENTERED,
        "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
        "leave_alone_holds": sorted(LEAVE_ALONE_HOLD_IDS),
        "finish_order": list(FINISH_ORDER),
        "chosen": list(CREATED_HEADERS),
        "parsed": [summarize_parse(b) for b in parsed_all],
        "rows": rows,
        "new_rows": rows,
        "finishes": finishes,
        "kimco_gets": gets,
        "ppv_fix": ppv_fix,
        "leftover_pending": leftover_pending,
        "catalog": prior.get("catalog"),
        "kimco_already": prior.get("kimco_already"),
        "discovered": prior.get("discovered"),
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


def leftover_slip_rows(slips: list[dict[str, Any]], batch_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bill in slips:
        names = bill.get("ignored_packing_slips") or []
        rows.append(
            {
                "Vendor": VENDOR_NAME,
                "Invoice #": "",
                "date": bill.get("date") or "",
                "PO": "",
                "Amount": "",
                "Result": "Skipped",
                "Why": (
                    "Skipped (bill-vs-noise): packing slip / POD / signed delivery "
                    f"receipt {', '.join(str(n) for n in names) or '(unnamed)'}. "
                    "Not an invoice. Did not create a header. Did not invent an "
                    "invoice # from Receipt_*.pdf. Kyle: packing slips do not need "
                    "to be actioned. Outlook left unflagged."
                ),
                "KIMCO id": "",
                "Batch": batch_label,
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "",
                "Flag in Outlook": "No",
                "Flag status": "none",
                "Notes": "",
            }
        )
    return rows


def _older_hold_rows(older: list[dict[str, Any]], batch_label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bill in older:
        inv = exact_invoice_number(bill.get("invoice_number"))
        rows.append(
            {
                "Vendor": VENDOR_NAME,
                "Invoice #": inv,
                "date": bill.get("date"),
                "PO": bill.get("po") or "",
                "Amount": bill.get("amount"),
                "Result": "HOLD",
                "Why": (
                    f"HOLD (too-old): Legacy Wire invoice #{inv} date "
                    f"{bill.get('date')} is before {MIN_INVOICE_DATE}. "
                    "Did not create a header. Do not walk pre-Aug Legacy Wire "
                    "without asking. Flag status=none (email left unflagged)."
                ),
                "KIMCO id": "",
                "Batch": batch_label,
                "Fees and surcharges": "none",
                "PPV": "none",
                "Attach status": "pdf-on-vm" if bill.get("pdf_path") else "",
                "Flag in Outlook": "No",
                "Flag status": "none",
                "Notes": "",
            }
        )
    return rows


FINISH_10126 = "PS-INV104010"
FINISH_10126_ID = 10126
FINISH_10126_PDF = 1195.5


def ensure_on_legacy_wire_batch(
    client: KimcoClient, proof: dict[str, Any]
) -> dict[str, Any]:
    """Keep 10126 on batch 717. Reverse Transfer AP if someone already moved it."""
    bid = proof.get("batch_id")
    name = str(proof.get("batch_text") or "").strip()
    if bid == KNOWN_BATCH_ID:
        return {"status": "already-on-717", "batch_id": bid, "batch_text": name}
    if name.casefold() == "transfer ap":
        _body, status, error = client.update(
            "ap_invoices",
            FINISH_10126_ID,
            {
                "state": "Modified",
                "id": FINISH_10126_ID,
                "values": {"AP_Invoice_Batch": {"id": KNOWN_BATCH_ID}},
            },
        )
        after = legacy_proof(client, FINISH_10126_ID)
        return {
            "status": "reversed-from-transfer-ap",
            "put": status,
            "error": error,
            "batch_id": after.get("batch_id"),
            "batch_text": after.get("batch_text"),
        }
    return {"status": "unexpected-batch", "batch_id": bid, "batch_text": name}


def run_finish_10126(
    client: KimcoClient,
    graph,
    report_path: Path,
    batch_info: dict[str, Any],
    *,
    vendor_id: int,
    vendor_info: dict[str, Any],
) -> int:
    """Shawn repriced PO 59008. Finish 10126 only. No Transfer AP. No Mail.Send."""
    kid = FINISH_10126_ID
    inv = FINISH_10126
    proof = legacy_proof(client, kid)
    if exact_invoice_number(proof.get("invoice_number")) != inv:
        print(
            f"GET {kid} is {proof.get('invoice_number')}, not {inv}. Abort.",
            flush=True,
        )
        return 2
    batch_move = ensure_on_legacy_wire_batch(client, proof)
    print(json.dumps({"batch_move": batch_move}, indent=2, default=str), flush=True)
    if batch_move.get("status") == "unexpected-batch":
        print("10126 is not on 717 or Transfer AP. Will not invent a batch. Abort.", flush=True)
        return 2
    proof = legacy_proof(client, kid)
    if proof.get("batch_id") != KNOWN_BATCH_ID:
        print(
            f"10126 still off batch 717 (id={proof.get('batch_id')} "
            f"{proof.get('batch_text')!r}). Abort.",
            flush=True,
        )
        return 2

    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    pdf_path = download_invoice_pdf(graph, inv, pdf_dir)
    if pdf_path is None:
        print(f"Missing PDF for {inv}; cannot finish.", flush=True)
        return 2
    parsed = parse_invoice_pdf(
        pdf_path,
        subject=f"Legacy Wire Products - Sales Invoice {inv}",
        from_name=VENDOR_NAME,
    )
    parsed["pdf_path"] = str(pdf_path)
    parsed["vendor"] = VENDOR_NAME
    parsed["invoice_number"] = inv
    message_id = find_invoice_message_id(graph, inv)
    if message_id:
        parsed["graph_message_id"] = message_id

    receipts = load_list_receipts(client)
    finish = finish_hold_header(
        client, parsed=parsed, kimco_id=kid, receipts=receipts
    )
    proof = finish.get("after") or legacy_proof(client, kid)
    live_batch = proof.get("batch_text") or batch_info.get("name")
    live_bid = proof.get("batch_id") or batch_info.get("id")
    enter_row = {
        "Vendor": VENDOR_NAME,
        "Invoice #": inv,
        "date": parsed.get("date"),
        "PO": parsed.get("po") or "59008",
        "Amount": parsed.get("amount") or FINISH_10126_PDF,
        "Result": "HOLD",
        "Why": "",
        "KIMCO id": kid,
        "Batch": f"{live_batch} ({live_bid})",
        "Fees and surcharges": "none",
        "PPV": "none",
        "Attach status": "",
        "Flag status": "entered-with-issues",
        "Flag in Outlook": "Yes",
        "Notes": "Shawn repriced PO 59008; finished on batch 717. No Transfer AP.",
        "outlook": "entered-with-issues",
    }
    row = quality_legacy_row(
        graph,
        parsed=parsed,
        enter_row=enter_row,
        proof=proof,
        finish=finish,
        vendor_id=vendor_id,
    )
    if row.get("Result") == "Success":
        row["Notes"] = enter_row["Notes"]
    print(
        json.dumps(
            {
                "invoice": inv,
                "kimco_id": kid,
                "result": row.get("Result"),
                "receipts": row.get("Receipts"),
                "live_amount": proof.get("invoice_amount"),
                "verification": proof.get("verification") or proof.get("verification_amount"),
                "batch_id": proof.get("batch_id"),
                "batch_text": proof.get("batch_text"),
                "ppv": row.get("PPV"),
                "outlook": row.get("outlook") or row.get("Flag status"),
                "select_status": finish.get("select_status"),
                "wanted": finish.get("wanted"),
                "why": row.get("Why"),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )

    prior_rows = prior_rows_from_sidecar(report_path)
    rows = merge_sheet_rows(prior_rows, [row])
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    sidecar_path = report_path.with_suffix(".json")
    prior = json.loads(sidecar_path.read_text()) if sidecar_path.exists() else {}
    sidecar = dict(prior)
    sidecar.update(
        {
            "proof": "legacy-wire-0917-10126-success",
            "invent": False,
            "mail_send": False,
            "shawn_repriced": True,
            "transfer_ap": False,
            "comment_added": False,
            "finish_10126": {
                k: v for k, v in finish.items() if k != "after"
            },
            "kimco_get_10126": proof,
            "batch_move": batch_move,
            "rows": rows,
            "report": str(report_path),
        }
    )
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0 if row.get("Result") == "Success" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enter up to 5 Legacy Wire bills on a dedicated 9/17 batch"
    )
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--create-batch-only", action="store_true")
    parser.add_argument(
        "--refresh-sheet",
        action="store_true",
        help="Re-GET first-pass headers 10112–10116, fix Why, do not recreate.",
    )
    parser.add_argument(
        "--finish-receipts",
        action="store_true",
        help="Select new leftovers on 10112–10115, stamp Outlook on Success, leave 10116.",
    )
    parser.add_argument(
        "--finish-10126",
        action="store_true",
        help="Finish PS-INV104010 / 10126 after Shawn reprice. Stay on batch 717.",
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-17-legacy-wire.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(
        "Target: live. Legacy Wire only. New batch (not 715/716). "
        "No Mail.Send. invent=false.",
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

    vendor_info = confirm_legacy_wire_vendor(client)
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
        print(
            "Live Legacy Wire vendor id not confirmed from KIMCO invoices. "
            "Will not invent. Stop.",
            flush=True,
        )
        return 2

    if args.create_batch_only:
        batch = create_legacy_wire_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden"):
            print("Created/found batch is 715 or 716 — abort.", flush=True)
            return 2
        return 0

    if args.finish_10126:
        batch = create_legacy_wire_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden") or batch.get("id") in FORBIDDEN_BATCH_IDS:
            print("Refusing batch 715/716. Abort 10126 finish.", flush=True)
            return 2
        if int(batch.get("id") or 0) != KNOWN_BATCH_ID:
            print(
                f"Expected batch {KNOWN_BATCH_ID}; got {batch.get('id')}. Abort 10126 finish.",
                flush=True,
            )
            return 2
        return run_finish_10126(
            client,
            graph,
            Path(args.report),
            verified,
            vendor_id=int(vendor_id),
            vendor_info=vendor_info,
        )

    if args.refresh_sheet or args.finish_receipts:
        batch = create_legacy_wire_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden") or batch.get("id") in FORBIDDEN_BATCH_IDS:
            print("Refusing batch 715/716. Abort refresh.", flush=True)
            return 2
        if int(batch.get("id") or 0) != KNOWN_BATCH_ID:
            print(
                f"Expected batch {KNOWN_BATCH_ID}; got {batch.get('id')}. Abort refresh.",
                flush=True,
            )
            return 2
        return run_refresh_sheet(
            client,
            graph,
            Path(args.report),
            verified,
            vendor_id=int(vendor_id),
            vendor_info=vendor_info,
            stamp_outlook=bool(args.finish_receipts),
        )

    entered = dict(vendor_info.get("entered") or {})
    entered.update(KNOWN_ENTERED)
    print(
        f"KIMCO Legacy Wire invoices already present: {sorted(entered)} ({len(entered)})",
        flush=True,
    )

    messages = find_legacy_wire_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(
        key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")),
        reverse=True,
    )
    print(
        json.dumps({"discovered": len(catalog), "legacy_wire_mail": catalog}, indent=2, default=str),
        flush=True,
    )

    already = already_set(entered)
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    skipped_entered = 0
    skipped_noise = 0
    for msg in messages:
        inv = exact_invoice_number(_subject_inv(str(msg.get("subject") or "")))
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_legacy_wire_invoice_email(msg):
            skipped_noise += 1
            continue
        if inv and (inv in already or already.intersection(invoice_aliases(inv))):
            skipped_entered += 1
            continue
        msg["_wanted_invoice"] = inv
        candidates.append(msg)
    by_inv: dict[str, dict[str, Any]] = {}
    for msg in candidates:
        inv = str(msg.get("_wanted_invoice") or "")
        if not inv:
            inv = f"unparsed-{msg.get('id')}"
        prior = by_inv.get(inv)
        if prior is None or str(msg.get("receivedDateTime") or "") > str(
            prior.get("receivedDateTime") or ""
        ):
            by_inv[inv] = msg
    candidates = prefer_candidate_messages(by_inv, preferred=NEXT_FIVE, cap=CAP)
    print(
        json.dumps(
            {
                "unflagged_not_on_kimco": [
                    {
                        "invoice": m.get("_wanted_invoice"),
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
    parsed_bills: list[dict[str, Any]] = []
    for msg in candidates:
        parsed_bills.extend(bills_from_message(graph, msg, pdf_dir))
    parsed = [summarize_parse(inv) for inv in parsed_bills]
    print(json.dumps({"parsed_candidates": parsed}, indent=2, default=str), flush=True)

    recent, older, slips = pick_recent(
        parsed_bills, already=already, cap=CAP, preferred=NEXT_FIVE
    )
    print(
        json.dumps(
            {
                "recent_picked": [summarize_parse(b) for b in recent],
                "older_not_entered": [summarize_parse(b) for b in older],
                "packing_slips_ignored": [
                    {
                        "subject": b.get("subject"),
                        "files": b.get("ignored_packing_slips"),
                    }
                    for b in slips
                ],
                "min_invoice_date": str(MIN_INVOICE_DATE),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.parse_only:
        return 0

    batch = create_legacy_wire_batch(client)
    verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
    print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
    if verified.get("is_forbidden") or batch.get("id") in FORBIDDEN_BATCH_IDS:
        print("Refusing batch 715 (Crosslink) or 716 (JPSteel). Abort enter.", flush=True)
        return 2
    if str(batch.get("name")) in FORBIDDEN_REUSE_NAMES:
        print("Refusing Crosslink/JPSteel batch name. Abort enter.", flush=True)
        return 2
    if int(batch.get("id") or 0) != KNOWN_BATCH_ID:
        print(
            f"Expected batch {KNOWN_BATCH_ID}; got {batch.get('id')}. Abort.",
            flush=True,
        )
        return 2

    report_path = Path(args.report)
    batch_label = f"{batch['name']} ({batch['id']})"
    prior_rows = prior_rows_from_sidecar(report_path)

    leftover_pending = leftover_from_catalog(
        catalog,
        entered=entered,
        chosen={exact_invoice_number(b.get("invoice_number")) for b in recent},
    )
    leftover_pending.extend(summarize_parse(b) for b in older)
    leftover_pending.extend(
        {
            "invoice_number": "",
            "skip_reason": "packing_slip",
            "files": b.get("ignored_packing_slips"),
            "subject": b.get("subject"),
            "why": "packing slip / receipt scan ignored; not an invoice",
        }
        for b in slips
    )

    if not recent:
        reason = (
            "No unflagged Legacy Wire invoices dated on/after 2026-08-01 that are "
            "not already in KIMCO. Stopped — did not walk pre-Aug leftovers "
            "(AQPC NOTE-28). Packing slips ignored (not actioned)."
        )
        print(reason, flush=True)
        rows = merge_sheet_rows(prior_rows, leftover_slip_rows(slips, batch_label))
        write_report(report_path, rows)
        sidecar = {
            "proof": "legacy-wire-0917-plus10",
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
            "known_entered": KNOWN_ENTERED,
            "created_headers": CREATED_HEADERS,
            "leave_alone_holds": sorted(LEAVE_ALONE_HOLD_IDS),
            "preferred_next": list(NEXT_FIVE),
            "plus5_headers": dict(PLUS5_HEADERS),
            "plus10_headers": dict(PLUS10_HEADERS),
            "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
            "discovered": len(catalog),
            "catalog": catalog,
            "kimco_already": entered,
            "recent_picked": [],
            "new_rows": [],
            "older_not_entered": leftover_pending,
            "leftover_pending": leftover_pending,
            "packing_slips_ignored": [b.get("ignored_packing_slips") for b in slips],
            "rows": rows,
            "blocker": reason,
            "treyce_emailed": False,
            "report": str(report_path),
        }
        report_path.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2, default=str) + "\n"
        )
        print(f"Wrote {report_path} and sidecar (no new enter).", flush=True)
        _print_summary(rows)
        return 0

    enter_rows = run_enter(
        client,
        recent,
        batch_name=str(batch["name"]),
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    for row in enter_rows:
        batch_cell = str(row.get("Batch") or "")
        if re.search(r"\((715|716)\)", batch_cell) or batch_cell in FORBIDDEN_REUSE_NAMES:
            raise KimcoError(f"Enter posted to forbidden batch: {batch_cell}")
        printed = exact_invoice_number(row.get("Invoice #"))
        if printed:
            row["Invoice #"] = printed

    receipts = load_list_receipts(client)
    new_rows, finishes, gets = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=int(vendor_id),
    )
    rows = merge_sheet_rows(prior_rows, new_rows)
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    sidecar = {
        "proof": "legacy-wire-0917-plus10",
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
        "kimco_already": entered,
        "chosen": [exact_invoice_number(b.get("invoice_number")) for b in recent],
        "known_entered": KNOWN_ENTERED,
        "created_headers": CREATED_HEADERS,
        "leave_alone_holds": sorted(LEAVE_ALONE_HOLD_IDS),
        "preferred_next": list(NEXT_FIVE),
        "plus5_headers": dict(PLUS5_HEADERS),
        "plus10_headers": {
            exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id")
            for r in new_rows
            if exact_invoice_number(r.get("Invoice #"))
            and r.get("KIMCO id") not in (None, "")
        }
        or dict(PLUS10_HEADERS),
        "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
        "new_rows": new_rows,
        "chosen_because": (
            "Plus-10 on batch 717. First-pass 10112–10115 Success + 10126 "
            "Success + 10124 Success left as-is. HOLDs 10116/10123/10125/10127 "
            "left alone. Next 5 unflagged Legacy Wire Aug 1+ not on KIMCO. "
            "Over-PPV HOLD → Transfer AP (name lookup) + @Shawn comment. "
            "NOTE-29: do not Select Receipts on over-gate lines. Packing slips "
            "ignored. Do not walk pre-Aug. Do not reuse 715/716. No Mail.Send."
        ),
        "parsed": [summarize_parse(b) for b in recent],
        "older_not_entered": leftover_pending,
        "leftover_pending": leftover_pending,
        "packing_slips_ignored": [b.get("ignored_packing_slips") for b in slips],
        "rows": rows,
        "finishes": finishes,
        "kimco_gets": gets,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
