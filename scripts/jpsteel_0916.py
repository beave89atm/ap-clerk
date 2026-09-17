"""Enter up to 5 recent unflagged JPSteel / JP Steel bills.

Dedicated live batch — do NOT reuse Crosslink batch 715
(API Agent - 9/16/26). Prefer name `API Agent - 9/16/26 JPSteel`;
fall back to `API Agent - 9/16/26-2` if the ERP rejects the suffix.

PDF attachments only. No Mail.Send. invent=false.

Skip already-flagged mail and any invoice already on the live JPSteel
vendor. Prefer invoice dates on/after 2026-08-01. If fewer than 5 recent
remain, enter what's left and stop — do not walk pre-Aug (NOTE-28).
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
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    blocked_400_not_a_hold_when_same_item_cover,
    decide_ppv,
    distinctive_vendor_tokens,
    extract_subject_invoice_number,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    length_qty_equivalent,
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
)

LOGGER = logging.getLogger("ap_clerk.jpsteel_0916")

PREFERRED_BATCH_NAME = "API Agent - 9/16/26 JPSteel"
FALLBACK_BATCH_NAME = "API Agent - 9/16/26-2"
KNOWN_BATCH_ID = 716
# Kyle: Crosslink/morning run. Do not reuse.
FORBIDDEN_BATCH_IDS = {715}
CROSSLINK_TODAY_NAME = "API Agent - 9/16/26"

VENDOR_NAME = "JP Steel"
VENDOR_TOKENS = ("jpsteel", "jp steel", "jp-steel")
# Other steel companies — distinctive-token miss is not enough if "steel" leaks.
OTHER_STEEL = (
    "morgan steel",
    "leeco",
    "beshert",
    "triple-s",
    "triple s",
    "ryerson",
    "willbanks",
    "o'neal",
    "oneal",
    "earle",
    "emj",
    "metal supermarket",
    "kloeckner",
    "tube supply",
)
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
# 9/8 weekday HOLD — skip if still sitting; do not recreate.
KNOWN_HOLD = {"124747"}
# First-pass headers on batch 716. Do not recreate.
CREATED_HEADERS = {
    "125315": 10107,
    "125316": 10108,
    "125314": 10109,
    "125122": 10110,
    "125051": 10111,
}
# Kyle: leave these HOLDs for morning remedies. Do not finish/rework tonight.
LEAVE_ALONE_HOLD_IDS = {10108, 10111}
# Kyle 2026-09-17 finished 10107 live. GET-only — do not Select Receipts / edit.
DO_NOT_MUTATE_IDS = {10107}
# Pre-Aug leftovers. Do not walk without asking (NOTE-28).
DO_NOT_WALK = {"124506", "123248"}
NOISE_SUBJECT = re.compile(
    r"statement|past due|account with us|remittance|payment reminder",
    flags=re.I,
)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _subject_inv(subject: str) -> str:
    printed = extract_subject_invoice_number(subject)
    return invoice_number_key(printed) if printed else ""


def blob_has_jpsteel(blob: str) -> bool:
    """Distinctive JP + Steel / JPSteel. Generic 'steel' alone is never enough."""
    text = (blob or "").lower()
    compact = _compact(text)
    if "jpsteel" in compact:
        return True
    if "jp steel" in text or "jp-steel" in text:
        return True
    tokens = set(distinctive_vendor_tokens(text))
    return "jp" in tokens and ("steel" in text or "jpsteel" in compact)


def is_other_steel_vendor(blob: str) -> bool:
    text = (blob or "").lower()
    return any(token in text for token in OTHER_STEEL)


def is_jpsteel_message(message: dict[str, Any]) -> bool:
    """Distinctive JPSteel / JP Steel only. Never Morgan/Leeco/MSC/RMP/Crosslink."""
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    blob = " ".join([subject, preview, from_name, from_addr])
    if is_other_steel_vendor(blob) and not blob_has_jpsteel(blob):
        return False
    if not blob_has_jpsteel(blob):
        return False
    tokens = set(distinctive_vendor_tokens(from_name)) | set(
        distinctive_vendor_tokens(subject)
    )
    if "msc" in tokens or "rmp" in tokens or "crosslink" in tokens:
        if not blob_has_jpsteel(blob):
            return False
    return True


_REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", flags=re.I)
_CREDIT_MEMO = re.compile(r"\bCM\d{5,}\b", flags=re.I)


def is_jpsteel_invoice_email(message: dict[str, Any]) -> bool:
    if not is_jpsteel_message(message):
        return False
    subject = str(message.get("subject") or "")
    if NOISE_SUBJECT.search(subject):
        return False
    if _CREDIT_MEMO.search(subject):
        return False
    if _REPLY_PREFIX.match(subject) and not message.get("hasAttachments"):
        return False
    return bool(_subject_inv(subject) or message.get("hasAttachments"))


def is_jpsteel_vendor_text(text: str | None) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return False
    if is_other_steel_vendor(raw) and not blob_has_jpsteel(raw):
        return False
    if names_match(VENDOR_NAME, raw):
        return True
    return blob_has_jpsteel(raw)


def find_jpsteel_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        "JP Steel",
        "JPSteel",
        "jpsteel",
        "Invoice from JP Steel",
        "Invoice from JPSteel",
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_jpsteel_message(msg):
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
        if not is_jpsteel_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def confirm_jpsteel_vendor(client: KimcoClient) -> dict[str, Any]:
    """Live vendor id from existing KIMCO invoices. invent=false — never guess.

    List view often stores Vendor as display text (`1098-JP STEEL`) without
    Vendor.id. Record GET is required for the API vendor id (100, not 1098).
    """
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
        if not is_jpsteel_vendor_text(vendor_txt):
            continue
        number = invoice_number_key(vals.get("Invoice_Number"))
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
    # Newest first. GET enough records to prove Vendor.id (list view omits it).
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
        if posted in (None, "") or not is_jpsteel_vendor_text(posted_txt):
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
        # Majority only. A 1–1 tie is not a confirmation (invent=false).
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


def kimco_jpsteel_numbers(client: KimcoClient, vendor_id: int | None) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        posted_id = lookup_id(vals.get("Vendor"))
        vendor_txt = str(
            lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name")) or ""
        )
        if vendor_id not in (None, "") and posted_id == vendor_id:
            pass
        elif is_jpsteel_vendor_text(vendor_txt):
            pass
        else:
            continue
        number = invoice_number_key(vals.get("Invoice_Number"))
        if number and item.get("id") not in (None, ""):
            found[number] = int(item["id"])
    return found


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed = parse_iso_date(str(value)[:10])
    return parsed


def bill_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> dict[str, Any]:
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    wanted = str(message.get("_wanted_invoice") or _subject_inv(subject))
    pdfs = []
    if message.get("hasAttachments"):
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id)
    if not pdfs:
        return {
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
    filename, content = pdfs[0]
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
    # PDF-is-truth for vendor, but compact JPSteel / From-person must not win.
    if not names_match(VENDOR_NAME, str(parsed.get("vendor") or "")):
        if is_jpsteel_vendor_text(str(parsed.get("vendor") or "")) or blob_has_jpsteel(
            " ".join([str(parsed.get("vendor") or ""), from_name, subject])
        ):
            parsed["vendor"] = VENDOR_NAME
    if not parsed.get("invoice_number"):
        parsed["invoice_number"] = wanted
        sources = dict(parsed.get("field_sources") or {})
        sources.setdefault("invoice_number", "subject")
        parsed["field_sources"] = sources
    return parsed


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
        "subject": str(msg.get("subject") or "")[:140],
    }


def pick_recent(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep recent unentered bills. Older leftovers are reported, not entered."""
    recent: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    for bill in parsed_bills:
        inv = invoice_number_key(bill.get("invoice_number"))
        if not inv or inv in already:
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
    return recent[:cap], older


def create_jpsteel_batch(client: KimcoClient) -> dict[str, Any]:
    """New dedicated batch. Never 715 / never today's Crosslink name."""
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
        if bid in FORBIDDEN_BATCH_IDS or name == CROSSLINK_TODAY_NAME:
            raise KimcoError(
                f"Refusing forbidden Crosslink batch id={bid} name={name}"
            )
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
        if bid in FORBIDDEN_BATCH_IDS or got_name == CROSSLINK_TODAY_NAME:
            raise KimcoError(
                f"Refusing to use Crosslink batch id={bid} name={got_name}"
            )
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
        "is_forbidden_715": batch.get("id") in FORBIDDEN_BATCH_IDS,
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


def jpsteel_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
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
    """True when selected qty does not match invoice merch qty.

    Inches on a description are not rolled qty. length_qty_equivalent only
    when the invoice line carries an explicit length_inches / inches UOM.
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


def match_jpsteel_inch_partial(
    lines: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    already_ids: set[int],
) -> list[dict[str, Any]]:
    """Foot-priced JP Steel line → unique per-inch leftover, take invoice inches.

    Receipt 14400@0.77 can cover invoice 1445" (120.42'). Do not guess when
    two leftovers share a unit. Per-piece cut length is not a candidate qty.
    """
    hits: list[dict[str, Any]] = []
    used: set[int] = set(already_ids)
    for line in lines:
        if str(line.get("qty_uom") or "") != "in":
            continue
        need = money(line.get("qty"))
        unit = money(line.get("unit_price"))
        if need is None or unit is None:
            continue
        cands: list[dict[str, Any]] = []
        for rec in pool:
            rid = rec.get("id")
            if rid in (None, "") or int(rid) in used:
                continue
            ru = money(rec.get("unit_price"))
            rq = money(rec.get("qty"))
            if ru is None or rq is None or rq + 0.001 < need:
                continue
            if abs(ru - unit) <= 0.012:
                cands.append(rec)
        if len(cands) != 1:
            continue
        rec = cands[0]
        rid = int(rec["id"])
        used.add(rid)
        hits.append({"line": line, "receipt": rec, "select_qty": need, "id": rid})
    return hits


def finish_hold_header(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hydrate PO receipts, Select Receipts, post Fees then in-gate PPV."""
    proof = jpsteel_proof(client, kimco_id)
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
    have_ids = set(have)
    for hit in matched:
        rid = (hit.get("receipt") or {}).get("id")
        if rid not in (None, ""):
            have_ids.add(int(rid))
    extra = match_jpsteel_inch_partial(list(parsed.get("lines") or []), pool, have_ids)
    for hit in extra:
        matched.append(
            {
                "line": hit["line"],
                "receipt": hit["receipt"],
                "select_qty": hit["select_qty"],
                "how": "jpsteel-inch-partial",
            }
        )
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

    after = jpsteel_proof(client, kimco_id)
    return {
        "wanted": wanted,
        "select_status": select_status,
        "fee_status": fee_status,
        "ppv_status": ppv_status,
        "ppv_amount": ppv_amt,
        "match_how": match.get("hows") or match.get("how"),
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


def quality_jpsteel_row(
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
    )
    out["Amount"] = pdf_amt
    out["KIMCO id"] = kid
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
        if kid_int in DO_NOT_MUTATE_IDS:
            out["Why"] = (
                "Kyle finished Select Receipts 2026-09-17 (NOTE-37: combine "
                f"same-item same-unit-cost {format_receipts(proof)} = PDF "
                f"21@$33=$693). Live GET {kid} Invoice_Amount={posted} "
                f"verification={ver} Type {proof.get('invoice_type')} "
                f"vendor {proof.get('vendor_id')} batch "
                f"{proof.get('batch_id') or '716'}. Did not Select Receipts / "
                "edit / re-finish — Kyle's live state left alone. Outlook "
                "left as Kyle set (not re-stamped)."
            )
        else:
            out["Why"] = (
                f"Finished bill (Invoice_Type {proof.get('invoice_type')}). "
                f"Header PO set={po or 'none'}. Select Receipts "
                f"{format_receipts(proof)} on PO {po or 'n/a'}. "
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
        out["Why"] = (
            f"HOLD (receipt): PDF merch qty vs selected "
            f"({format_receipts(proof)}). Inches are not rolled qty. "
            "Do not invent Success. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif fee_on_ppv or not fees_ok:
        out["Result"] = "HOLD"
        out["Why"] = (
            "HOLD (fees): JPSteel fees/surcharges must be Additional Charge "
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
            f"vendor_id={proof.get('vendor_id')} expected={vendor_id}. "
            f"Do not invent Success. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and kid not in (None, "") and not do_not_stamp:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    return out


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
    by_inv = {invoice_number_key(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = invoice_number_key(enter_row.get("Invoice #"))
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
        proof = finish.get("after") or jpsteel_proof(client, kid)
        row = quality_jpsteel_row(
            graph,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
        )
        rows.append(row)
        gets[str(kid)] = proof
    return rows, finishes, gets


def graph_message_id_for(graph, inv: str) -> str:
    needle = f"JP Steel Invoice#  ({inv})"
    try:
        hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=10)
    except Exception as exc:  # noqa: BLE001 - finish still posts
        LOGGER.info("Graph search for %s failed: %s", inv, type(exc).__name__)
        return ""
    best = ""
    best_recv = ""
    for msg in hits:
        if not is_jpsteel_invoice_email(msg) and not is_jpsteel_message(msg):
            continue
        subject = str(msg.get("subject") or "")
        if invoice_number_key(extract_subject_invoice_number(subject) or "") != inv:
            continue
        recv = str(msg.get("receivedDateTime") or "")
        if recv >= best_recv:
            best_recv = recv
            best = str(msg.get("id") or "")
    return best


def parsed_from_disk(inv: str, pdf_dir: Path) -> dict[str, Any] | None:
    matches = sorted(pdf_dir.glob(f"*Invoice_{inv}.pdf"))
    if not matches:
        matches = sorted(pdf_dir.glob(f"*{inv}*.pdf"))
    if not matches:
        return None
    path = matches[0]
    parsed = parse_invoice_pdf(
        path,
        subject=f"JP Steel Invoice#  ({inv}) Transmission for KANNON MFG",
        from_name=VENDOR_NAME,
    )
    parsed["pdf_path"] = str(path)
    parsed["vendor"] = VENDOR_NAME
    if not parsed.get("invoice_number"):
        parsed["invoice_number"] = inv
    return parsed


def run_finish_only(
    client: KimcoClient,
    graph,
    report_path: Path,
    batch_info: dict[str, Any],
    *,
    vendor_id: int,
    catalog: list[dict[str, Any]] | None = None,
    entered: dict[str, int] | None = None,
) -> int:
    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    receipts = load_list_receipts(client)
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    gets: dict[str, Any] = {}
    parsed_all: list[dict[str, Any]] = []
    batch_label = f"{batch_info.get('name')} ({batch_info.get('id')})"

    for inv, kid in CREATED_HEADERS.items():
        parsed = parsed_from_disk(inv, pdf_dir)
        if parsed is None:
            print(f"Missing PDF for {inv}; cannot finish.", flush=True)
            continue
        mid = graph_message_id_for(graph, inv) if graph is not None else ""
        if mid:
            parsed["graph_message_id"] = mid
        parsed_all.append(parsed)
        enter_row = {
            "Vendor": VENDOR_NAME,
            "Invoice #": inv,
            "date": parsed.get("date"),
            "PO": parsed.get("po") or "",
            "Amount": parsed.get("amount"),
            "Result": "HOLD",
            "Why": "",
            "KIMCO id": kid,
            "Batch": batch_label,
            "Fees and surcharges": "none",
            "PPV": "none",
            "Attach status": "",
            "Flag status": "entered-with-issues",
            "Flag in Outlook": "Yes",
            "Notes": "",
        }
        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=kid, receipts=receipts
        )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or jpsteel_proof(client, kid)
        row = quality_jpsteel_row(
            graph,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
        )
        rows.append(row)
        gets[str(kid)] = proof
        print(
            json.dumps(
                {
                    "invoice": inv,
                    "kimco_id": kid,
                    "result": row.get("Result"),
                    "receipts": row.get("Receipts"),
                    "fees": row.get("Fees and surcharges"),
                    "ppv": row.get("PPV"),
                    "finish": finishes.get(inv),
                },
                indent=2,
                default=str,
            ),
            flush=True,
        )

    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)
    sidecar = {
        "proof": "jpsteel-0916",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": vendor_id,
        "batch_name": batch_info.get("name"),
        "batch_id": batch_info.get("id"),
        "batch": batch_info,
        "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
        "created_headers": CREATED_HEADERS,
        "chosen": list(CREATED_HEADERS),
        "parsed": [summarize_parse(b) for b in parsed_all],
        "rows": rows,
        "finishes": finishes,
        "kimco_gets": gets,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    if catalog is not None:
        sidecar["catalog"] = catalog
        sidecar["discovered"] = len(catalog)
    if entered is not None:
        sidecar["kimco_already"] = entered
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


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
    """Keep first-pass rows (including HOLDs), then append this pass. No dup #."""
    seen = {str(r.get("Invoice #") or "") for r in new_rows}
    keep = [r for r in prior_rows if str(r.get("Invoice #") or "") not in seen]
    return keep + new_rows


def _older_hold_rows(
    older: list[dict[str, Any]],
    batch_label: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bill in older:
        inv = invoice_number_key(bill.get("invoice_number"))
        rows.append(
            {
                "Vendor": VENDOR_NAME,
                "Invoice #": inv,
                "date": bill.get("date"),
                "PO": bill.get("po") or "",
                "Amount": bill.get("amount"),
                "Result": "HOLD",
                "Why": (
                    f"HOLD (too-old): JPSteel invoice #{inv} date "
                    f"{bill.get('date')} is before {MIN_INVOICE_DATE}. "
                    "Did not create a header. Do not walk pre-Aug JPSteel "
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter up to 5 JPSteel bills on a dedicated 9/16 batch")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--create-batch-only", action="store_true")
    parser.add_argument(
        "--finish-only",
        action="store_true",
        help="Select Receipts + Fees + in-gate PPV on first-pass headers 10107–10111.",
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-16-jpsteel.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(
        "Target: live. JPSteel only. New batch (not 715). No Mail.Send. invent=false.",
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

    vendor_info = confirm_jpsteel_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    print(json.dumps({"vendor_confirm": {
        "vendor_id": vendor_id,
        "vendor_text_samples": vendor_info.get("vendor_text_samples"),
        "id_counts": vendor_info.get("id_counts"),
        "sample_count": len(vendor_info.get("samples") or []),
        "entered_count": len(vendor_info.get("entered") or {}),
        "invent": False,
    }}, indent=2, default=str), flush=True)
    if vendor_id in (None, ""):
        print(
            "Live JPSteel vendor id not confirmed from KIMCO invoices. "
            "Will not invent. Stop.",
            flush=True,
        )
        return 2

    if args.create_batch_only:
        batch = create_jpsteel_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden_715"):
            print("Created/found batch is 715 — abort.", flush=True)
            return 2
        return 0

    if args.finish_only:
        batch = create_jpsteel_batch(client)
        verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
        print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
        if verified.get("is_forbidden_715") or batch.get("id") in FORBIDDEN_BATCH_IDS:
            print("Refusing batch 715 (Crosslink). Abort finish.", flush=True)
            return 2
        return run_finish_only(
            client,
            graph,
            Path(args.report),
            verified,
            vendor_id=int(vendor_id),
        )

    entered = dict(vendor_info.get("entered") or {})
    if not entered:
        entered.update(kimco_jpsteel_numbers(client, int(vendor_id)))
    print(
        f"KIMCO JPSteel invoices already present: {sorted(entered)} ({len(entered)})",
        flush=True,
    )

    messages = find_jpsteel_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(
        key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")),
        reverse=True,
    )
    print(json.dumps({"discovered": len(catalog), "jpsteel_mail": catalog}, indent=2, default=str), flush=True)

    already = set(entered) | set(KNOWN_HOLD) | set(CREATED_HEADERS) | set(DO_NOT_WALK)
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    skipped_entered = 0
    skipped_noise = 0
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_jpsteel_invoice_email(msg):
            skipped_noise += 1
            continue
        if inv and inv in already:
            skipped_entered += 1
            continue
        msg["_wanted_invoice"] = inv
        candidates.append(msg)
    by_inv: dict[str, dict[str, Any]] = {}
    for msg in candidates:
        inv = str(msg.get("_wanted_invoice") or "")
        if not inv:
            # Keep one no-number candidate if it has a PDF; parse will decide.
            inv = f"unparsed-{msg.get('id')}"
        prior = by_inv.get(inv)
        if prior is None or str(msg.get("receivedDateTime") or "") > str(
            prior.get("receivedDateTime") or ""
        ):
            by_inv[inv] = msg
    extras = sorted(
        by_inv.values(),
        key=lambda m: str(m.get("receivedDateTime") or ""),
        reverse=True,
    )
    candidates = extras[: max(CAP * 3, CAP)]
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
    parsed_bills = [bill_from_message(graph, msg, pdf_dir) for msg in candidates]
    parsed = [summarize_parse(inv) for inv in parsed_bills]
    print(json.dumps({"parsed_candidates": parsed}, indent=2, default=str), flush=True)

    recent, older = pick_recent(parsed_bills, already=already, cap=CAP)
    print(
        json.dumps(
            {
                "recent_picked": [summarize_parse(b) for b in recent],
                "older_not_entered": [summarize_parse(b) for b in older],
                "min_invoice_date": str(MIN_INVOICE_DATE),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.parse_only:
        return 0

    batch = create_jpsteel_batch(client)
    verified = verify_batch(client, int(batch["id"]), str(batch["name"]))
    print(json.dumps({"batch": batch, "verified": verified}, indent=2, default=str), flush=True)
    if verified.get("is_forbidden_715") or batch.get("id") in FORBIDDEN_BATCH_IDS:
        print("Refusing batch 715 (Crosslink). Abort enter.", flush=True)
        return 2
    if str(batch.get("name")) == CROSSLINK_TODAY_NAME:
        print("Refusing Crosslink today-name batch. Abort enter.", flush=True)
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
    if not recent:
        reason = (
            "No unflagged JPSteel invoices dated on/after 2026-08-01 that are "
            "not already in KIMCO. Stopped — did not walk pre-Aug 124506 / "
            "123248 (AQPC NOTE-28). First-pass HOLDs 10107/10108/10111 left "
            "alone for morning remedies."
        )
        print(reason, flush=True)
        leftover_pending = [
            {
                "invoice_number": "124506",
                "date": "2026-07-28",
                "po": "58637",
                "amount": 7396.75,
                "why": "too-old (before 2026-08-01); not entered",
            },
            {
                "invoice_number": "123248",
                "date": "2026-05-06",
                "po": "57950",
                "amount": 495.80,
                "why": "too-old; not entered",
            },
        ]
        leftover_pending.extend(summarize_parse(b) for b in older if invoice_number_key(b.get("invoice_number")) not in DO_NOT_WALK)
        rows = prior_rows
        write_report(report_path, rows)
        sidecar = {
            "proof": "jpsteel-0916-plus5",
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
            "created_headers": CREATED_HEADERS,
            "leave_alone_holds": sorted(LEAVE_ALONE_HOLD_IDS),
            "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
            "do_not_walk": sorted(DO_NOT_WALK),
            "discovered": len(catalog),
            "catalog": catalog,
            "kimco_already": entered,
            "recent_picked": [],
            "new_rows": [],
            "older_not_entered": leftover_pending,
            "leftover_pending": leftover_pending,
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
    # Refuse only the Crosslink morning batch (id 715 / exact today-name).
    # Do not substring-match "API Agent - 9/16/26 JPSteel (716)".
    for row in enter_rows:
        batch_cell = str(row.get("Batch") or "")
        if re.search(r"\(715\)", batch_cell) or batch_cell == CROSSLINK_TODAY_NAME:
            raise KimcoError(f"Enter posted to forbidden Crosslink batch: {batch_cell}")

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

    leftover_pending = [summarize_parse(b) for b in older]
    sidecar = {
        "proof": "jpsteel-0916",
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
        "chosen": [invoice_number_key(b.get("invoice_number")) for b in recent],
        "created_headers": CREATED_HEADERS,
        "leave_alone_holds": sorted(LEAVE_ALONE_HOLD_IDS),
        "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
        "do_not_walk": sorted(DO_NOT_WALK),
        "new_rows": new_rows,
        "chosen_because": (
            "Plus-5 on batch 716. Unflagged JPSteel only; not already on "
            "KIMCO; invoice date on/after 2026-08-01; cap 5. First-pass "
            "HOLDs 10107/10108/10111 left alone. Do not walk 124506/123248."
        ),
        "parsed": [summarize_parse(b) for b in recent],
        "older_not_entered": leftover_pending,
        "leftover_pending": leftover_pending,
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
