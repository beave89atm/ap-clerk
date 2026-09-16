"""Enter up to 5 recent unflagged Crosslink Powder Coating bills.

Reuses live batch API Agent - 9/16/26 (715) from the weekday run.
PDF attachments only (no Intuit). No Mail.Send. invent=false.

Skip already-flagged mail, NOTE-30 reminders 27447/27448/27591, and any
invoice already on KIMCO vendor 278. Prefer invoice dates on/after
2026-08-01. If only older remain, stop and report — do not walk ancient
Crosslink (AQPC NOTE-28 lesson).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
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
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged  # noqa: E402
from ap_clerk.inbox import sender_address, sender_name  # noqa: E402
from ap_clerk.kimco import (  # noqa: E402
    ADDITIONAL_CHARGE_LISTS,
    FEE_CHARGE_LOOKUP_ID,
    PPV_CHARGE_LOOKUP_ID,
    KimcoClient,
    fees_posted_cover_parsed,
    fees_with_amounts,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    decide_ppv,
    distinctive_vendor_tokens,
    extract_subject_invoice_number,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
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

LOGGER = logging.getLogger("ap_clerk.crosslink_0916")

BATCH_NAME = "API Agent - 9/16/26"
# Weekday 9/16 created this batch. Reuse — a second same-name batch 400s.
KNOWN_BATCH_ID = 715
VENDOR_ID = 278
VENDOR_NAME = "Crosslink Powder Coating"
VENDOR_TOKEN = "crosslink"
MIN_INVOICE_DATE = date(2026, 8, 1)
CAP = 5
# NOTE-30: reminder emails. Correct already-entered HOLD. Do not recreate.
NOTE30_REMINDERS = {
    "27447": 9382,
    "27448": 9384,
    "27591": 9587,
}
# Named already-entered from prior AP runs (do not recreate).
KNOWN_ENTERED = {
    **NOTE30_REMINDERS,
    "27321": 9199,
    "27319": 9193,
    "27419": 9345,
    "28166": 10101,
    "28113": 10102,
    "28114": 10103,
    "28100": 10104,
    "28102": 10105,
}
# First-pass Success on batch 715. Do not recreate.
FIRST_FIVE = ["28166", "28113", "28114", "28100", "28102"]
CREATED_HEADERS = {
    "28166": 10101,
    "28113": 10102,
    "28114": 10103,
    "28100": 10104,
    "28102": 10105,
}
# Second pass: 28008 first if still open. Discovery fills extras if more recent.
NEXT_FIVE = ["28008"]
PREFERRED_FIVE = NEXT_FIVE
HOLD_TO_FINISH = {
    "28113": 10102,
    "28114": 10103,
    "28100": 10104,
    "28102": 10105,
}
NOISE_SUBJECT = re.compile(
    r"statement|past due|friendly payment reminder|account with us",
    flags=re.I,
)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def _subject_inv(subject: str) -> str:
    printed = extract_subject_invoice_number(subject)
    return invoice_number_key(printed) if printed else ""


def is_crosslink_invoice_email(message: dict[str, Any]) -> bool:
    """Real Crosslink invoice email. Statements and past-due reminders are not."""
    if not is_crosslink_message(message):
        return False
    subject = str(message.get("subject") or "")
    if NOISE_SUBJECT.search(subject):
        return False
    return bool(_subject_inv(subject) or message.get("hasAttachments"))


def is_crosslink_message(message: dict[str, Any]) -> bool:
    """Distinctive Crosslink token only. Never MSC/RMP generic overlap."""
    subject = str(message.get("subject") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    blob = " ".join([subject, preview, from_name, from_addr]).lower()
    if VENDOR_TOKEN not in blob:
        return False
    tokens = set(distinctive_vendor_tokens(from_name)) | set(
        distinctive_vendor_tokens(subject)
    )
    if "msc" in tokens or "rmp" in tokens:
        if VENDOR_TOKEN not in tokens and VENDOR_TOKEN not in blob:
            return False
    return True


def find_crosslink_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        "from Crosslink Powder Coating",
        "Invoice # from Crosslink Powder Coating",
        "Crosslink Powder Coating",
        "crosslinktx.com",
        "invoice-27",
        "invoice-28",
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            if not is_crosslink_message(msg):
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
        if not is_crosslink_message(msg):
            continue
        mid = str(msg.get("id") or "")
        if mid:
            seen[mid] = msg
    return list(seen.values())


def kimco_crosslink_numbers(client: KimcoClient) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        vendor_id = lookup_id(vals.get("Vendor"))
        vendor_txt = str(
            lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name")) or ""
        )
        if vendor_id != VENDOR_ID and VENDOR_TOKEN not in vendor_txt.lower():
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
    if not names_match(VENDOR_NAME, str(parsed.get("vendor") or "")):
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
        "already_kimco": entered.get(inv) or KNOWN_ENTERED.get(inv),
        "note30_reminder": inv in NOTE30_REMINDERS,
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


def verify_batch(client: KimcoClient) -> dict[str, Any]:
    batch = client.get_item("ap_batches", KNOWN_BATCH_ID)
    vals = batch.get("values") or {}
    name = vals.get("AP_Invoice_Batch_ID")
    return {
        "id": batch.get("id"),
        "name": name,
        "status": vals.get("Status"),
        "unposted": vals.get("Unposted_Count"),
        "matches_today": name == BATCH_NAME,
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


def crosslink_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
    proof = live_get_proof(client, invoice_id)
    if not proof:
        return proof
    item = client.get_item("ap_invoices", int(invoice_id))
    vals = item.get("values") or {}
    charges = charges_from_item(item)
    proof["verification_amount"] = money(vals.get("Invoice_Verification_Amount"))
    proof["charges"] = charges
    proof["fee_amounts"] = [c["amount"] for c in charges if _is_fee_charge(c) and c.get("amount") is not None]
    proof["ppv_amounts"] = [c["amount"] for c in charges if _is_ppv_charge(c) and c.get("amount") is not None]
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


def pdf_for_invoice(inv: str, pdf_dir: Path) -> Path | None:
    matches = sorted(pdf_dir.glob(f"*invoice-{inv}.pdf"))
    return matches[0] if matches else None


def parsed_from_disk(inv: str, pdf_dir: Path) -> dict[str, Any] | None:
    path = pdf_for_invoice(inv, pdf_dir)
    if path is None or not path.exists():
        return None
    parsed = parse_invoice_pdf(
        path,
        subject=f"Invoice #{inv} from Crosslink Powder Coating",
        from_name=VENDOR_NAME,
        from_address="ap@crosslinktx.com",
    )
    parsed["pdf_path"] = str(path)
    parsed["vendor"] = VENDOR_NAME
    if not parsed.get("invoice_number"):
        parsed["invoice_number"] = inv
    return parsed


def finish_hold_header(
    client: KimcoClient,
    *,
    parsed: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Hydrate PO receipts, Select Receipts, post Fees then in-gate PPV."""
    proof = crosslink_proof(client, kimco_id)
    have = _receipt_ids_from_proof(proof)
    pool = hydrate_receipts(client, open_receipts_on_po(receipts, str(parsed.get("po") or "")))
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=pool,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    locked = filter_matches_outside_ppv_gate(
        list(match.get("matched") or []),
        invoice_total=parsed.get("amount"),
    )
    selectable = list(locked.get("selectable") or [])
    skipped = list(locked.get("skipped") or [])
    wanted_refs = receipt_select_refs(selectable)
    wanted_ids: list[int] = []
    for ref in wanted_refs:
        rid = ref.get("id") if isinstance(ref, dict) else ref
        if rid not in (None, "") and int(rid) not in have:
            wanted_ids.append(int(rid))
    select_status = "already-selected" if have and not wanted_ids else "held-unfinished"
    if locked.get("select_zero") or (skipped and not selectable):
        select_status = "ppv-lock-select-zero"
    elif wanted_ids:
        select_status = client.try_select_receipts(kimco_id, wanted_ids)

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

    after = crosslink_proof(client, kimco_id)
    return {
        "wanted": wanted_ids,
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


def quality_crosslink_row(
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    proof: dict[str, Any],
    finish: dict[str, Any] | None,
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

    merch_qty = 0.0
    have_merch = False
    for ln in parsed.get("lines") or []:
        q = money(ln.get("qty"))
        if q is not None:
            merch_qty = round(merch_qty + q, 4)
            have_merch = True
    rec_qty = 0.0
    have_rec = False
    rec_merch = 0.0
    for rec in recs:
        q = money(rec.get("qty"))
        u = money(rec.get("unit"))
        if q is not None:
            rec_qty = round(rec_qty + q, 4)
            have_rec = True
        if q is not None and u is not None:
            rec_merch = round(rec_merch + q * u, 2)

    qty_hold = have_merch and (not have_rec or abs(merch_qty - rec_qty) > 0.001)
    fee_amts = list(proof.get("fee_amounts") or [])
    ppv_amts = list(proof.get("ppv_amounts") or [])
    parsed_fees = list(parsed.get("fees") or [])
    fees_ok = fees_posted_cover_parsed(fee_amts, parsed_fees) if parsed_fees else True
    supply_on_ppv = False
    for fee in fees_with_amounts(parsed_fees):
        for amt in ppv_amts:
            if amt is not None and abs(amt - fee["amount"]) <= 0.02:
                supply_on_ppv = True
    charge_sum = round(sum(a or 0 for a in fee_amts) + sum(a or 0 for a in ppv_amts), 2)
    rolled = round(rec_merch + charge_sum, 2)
    amount_ok = False
    if pdf_amt is not None:
        if posted is not None and abs(posted - pdf_amt) <= 0.02:
            amount_ok = True
        elif ver is not None and abs(ver - pdf_amt) <= 0.02 and abs(rolled - pdf_amt) <= 0.02:
            amount_ok = True
    price_hold = bool((finish or {}).get("select_zero") or (finish or {}).get("skipped_over_ppv"))

    finished = (
        attach_ok
        and recs
        and have_rec
        and not qty_hold
        and fees_ok
        and not supply_on_ppv
        and amount_ok
        and proof.get("invoice_type") == 3
        and proof.get("vendor_id") == VENDOR_ID
        and not price_hold
        and (finish or {}).get("select_status") in {None, "selected", "already-selected"}
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
        out["Why"] = (
            f"Finished bill (Invoice_Type 3). Header PO set. Select Receipts "
            f"{format_receipts(proof)} on PO {parsed.get('po')}. "
            f"Fees={out['Fees and surcharges']} (Additional Charge Fees id 11, "
            f"not PPV). PPV={out['PPV']}. Attach status=attached. "
            f"{extra}Flag status=entered-in-ai."
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
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif qty_hold:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD (receipt): PDF merch qty {merch_qty:g} vs selected "
            f"{rec_qty:g} ({format_receipts(proof)}). "
            "Do not invent Success. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif supply_on_ppv or not fees_ok:
        out["Result"] = "HOLD"
        out["Why"] = (
            "HOLD (fees): Crosslink Packaging/Shop Supplies Recovery must be "
            "Additional Charge Fees (id 11), never PPV (SH:27591 class). "
            f"Posted fees={fee_amts} ppv={ppv_amts}. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    else:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD after live GET of {kid}: PDF amount={pdf_amt} posted={posted} "
            f"verification={ver} rolled={rolled} receipts={format_receipts(proof)} "
            f"attach={attach_ok} type={proof.get('invoice_type')}. "
            f"Do not invent Success. {extra}"
            "Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        if graph is not None and message_id and kid not in (None, ""):
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    return out


def graph_message_id_for(graph, inv: str) -> str:
    needle = f"Invoice #{inv} from Crosslink Powder Coating"
    try:
        hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=10)
    except Exception as exc:  # noqa: BLE001 - finish still posts
        LOGGER.info("Graph search for %s failed: %s", inv, type(exc).__name__)
        return ""
    best = ""
    best_recv = ""
    for msg in hits:
        if is_already_flagged(msg) and "Entered with issues" not in (msg.get("categories") or []):
            # Prefer the invoice email we already stamped this session.
            cats = msg.get("categories") or []
            if "Entered in AI" in cats:
                return str(msg.get("id") or "")
        subject = str(msg.get("subject") or "")
        if invoice_number_key(extract_subject_invoice_number(subject) or "") != inv:
            continue
        if NOISE_SUBJECT.search(subject):
            continue
        recv = str(msg.get("receivedDateTime") or "")
        if recv >= best_recv:
            best_recv = recv
            best = str(msg.get("id") or "")
    return best


def load_list_receipts(client: KimcoClient) -> list[dict[str, Any]]:
    return [normalize_receipt(item) for item in client.list_items("receipts")]


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
    """Keep first-pass Success rows, then append this pass. No duplicate invoice #."""
    seen = {str(r.get("Invoice #") or "") for r in new_rows}
    keep = [r for r in prior_rows if str(r.get("Invoice #") or "") not in seen]
    return keep + new_rows


def finish_entered_rows(
    client: KimcoClient,
    graph,
    *,
    parsed_bills: list[dict[str, Any]],
    enter_rows: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Hydrate + Select Receipts + Fees + in-gate PPV on new headers."""
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    gets: dict[str, Any] = {}
    by_inv = {invoice_number_key(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = invoice_number_key(enter_row.get("Invoice #"))
        kid = enter_row.get("KIMCO id")
        parsed = by_inv.get(inv) or {}
        if kid in (None, "") or not parsed:
            rows.append(enter_row)
            continue
        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=int(kid), receipts=receipts
        )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or crosslink_proof(client, kid)
        row = quality_crosslink_row(
            graph, parsed=parsed, enter_row=enter_row, proof=proof, finish=finish
        )
        rows.append(row)
        gets[str(kid)] = proof
    return rows, finishes, gets


def run_finish_only(
    client: KimcoClient,
    graph,
    report_path: Path,
    batch_info: dict[str, Any],
    *,
    catalog: list[dict[str, Any]] | None = None,
    entered: dict[str, int] | None = None,
) -> int:
    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    receipts = load_list_receipts(client)
    prior = {str(r.get("Invoice #")): r for r in prior_rows_from_sidecar(report_path)}
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    gets: dict[str, Any] = {}
    parsed_all: list[dict[str, Any]] = []

    keep_order = list(FIRST_FIVE)
    for inv in NEXT_FIVE:
        if inv not in keep_order:
            keep_order.append(inv)
    for inv in prior:
        if inv not in keep_order:
            keep_order.append(inv)
    for inv in keep_order:
        kid = CREATED_HEADERS.get(inv) or (prior.get(inv) or {}).get("KIMCO id")
        if kid in (None, ""):
            continue
        parsed = parsed_from_disk(inv, pdf_dir)
        if parsed is None:
            print(f"Missing PDF for {inv}; cannot finish.", flush=True)
            continue
        mid = graph_message_id_for(graph, inv) if graph is not None else ""
        if mid:
            parsed["graph_message_id"] = mid
        parsed_all.append(parsed)
        enter_row = prior.get(inv) or {
            "Vendor": VENDOR_NAME,
            "Invoice #": inv,
            "date": parsed.get("date"),
            "PO": parsed.get("po") or "",
            "Amount": parsed.get("amount"),
            "Result": "HOLD",
            "Why": "",
            "KIMCO id": kid,
            "Batch": f"{BATCH_NAME} ({KNOWN_BATCH_ID})",
            "Fees and surcharges": "none",
            "PPV": "none",
            "Attach status": "",
            "Flag status": "entered-with-issues",
            "Flag in Outlook": "Yes",
            "Notes": "",
        }
        if inv in HOLD_TO_FINISH:
            finish = finish_hold_header(
                client, parsed=parsed, kimco_id=kid, receipts=receipts
            )
            finishes[inv] = {
                k: v for k, v in finish.items() if k != "after"
            }
            proof = finish.get("after") or crosslink_proof(client, kid)
            row = quality_crosslink_row(
                graph, parsed=parsed, enter_row=enter_row, proof=proof, finish=finish
            )
        else:
            proof = crosslink_proof(client, kid)
            row = quality_crosslink_row(
                graph, parsed=parsed, enter_row=enter_row, proof=proof, finish=None
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
        "proof": "crosslink-0916",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": VENDOR_ID,
        "batch_name": BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "batch": batch_info,
        "discovered": len(catalog or []),
        "catalog": catalog or [],
        "kimco_already": entered or {},
        "note30_left_alone": NOTE30_REMINDERS,
        "chosen": keep_order,
        "chosen_because": (
            "Unflagged Crosslink Powder Coating only; not already on KIMCO; "
            f"invoice date on/after {MIN_INVOICE_DATE}; cap {CAP}. "
            "NOTE-30 reminders not recreated. Distinctive token Crosslink "
            "(never MSC/RMP). Second pass starts at 28008; first-pass "
            "10101–10105 kept. Finish hydrates list-view receipts then "
            "Select Receipts + Fees + in-gate PPV."
        ),
        "parsed": [summarize_parse(b) for b in parsed_all],
        "older_not_entered": [],
        "rows": rows,
        "finishes": finishes,
        "kimco_gets": gets,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    prior_sidecar = report_path.with_suffix(".json")
    if prior_sidecar.exists():
        old = json.loads(prior_sidecar.read_text())
        sidecar["discovered"] = old.get("discovered", sidecar["discovered"])
        sidecar["catalog"] = old.get("catalog") or sidecar["catalog"]
        sidecar["kimco_already"] = old.get("kimco_already") or sidecar["kimco_already"]
        sidecar["older_not_entered"] = old.get("older_not_entered") or []
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter up to 5 Crosslink bills on 9/16 batch")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument(
        "--finish-only",
        action="store_true",
        help="Hydrate + Select Receipts + Fees + in-gate PPV on first-pass HOLD headers.",
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-16-crosslink.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print("Target: live. Crosslink only. No Mail.Send. invent=false.", flush=True)
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
    batch_info = verify_batch(client)
    print(json.dumps({"batch": batch_info}, indent=2, default=str), flush=True)
    if not batch_info.get("matches_today"):
        print(
            f"Batch {KNOWN_BATCH_ID} name={batch_info.get('name')} is not {BATCH_NAME}. "
            "Will let run_enter find-or-create today's API Agent batch.",
            flush=True,
        )
    if args.finish_only:
        return run_finish_only(client, graph, Path(args.report), batch_info)

    entered = kimco_crosslink_numbers(client)
    entered.update({k: v for k, v in KNOWN_ENTERED.items() if k not in entered})
    print(
        f"KIMCO Crosslink invoices already present: {sorted(entered)} ({len(entered)})",
        flush=True,
    )

    messages = find_crosslink_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(
        key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")),
        reverse=True,
    )
    print(json.dumps({"discovered": len(catalog), "crosslink_mail": catalog}, indent=2, default=str), flush=True)

    already = set(entered)
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    skipped_entered = 0
    skipped_noise = 0
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_crosslink_invoice_email(msg):
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
            continue
        prior = by_inv.get(inv)
        if prior is None or str(msg.get("receivedDateTime") or "") > str(
            prior.get("receivedDateTime") or ""
        ):
            by_inv[inv] = msg
    ordered: list[dict[str, Any]] = []
    for inv in PREFERRED_FIVE:
        if inv in by_inv and inv not in already:
            ordered.append(by_inv.pop(inv))
    extras = sorted(
        by_inv.values(),
        key=lambda m: str(m.get("receivedDateTime") or ""),
        reverse=True,
    )
    candidates = (ordered + extras)[: max(CAP, len(PREFERRED_FIVE))]
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

    report_path = Path(args.report)
    if not recent:
        reason = (
            "No unflagged Crosslink invoices dated on/after 2026-08-01 that are "
            "not already in KIMCO. Stopped — did not walk ancient Crosslink "
            "(AQPC NOTE-28 lesson). NOTE-30 reminders 27447/27448/27591 left as "
            "already-entered HOLD (9382/9384/9587)."
        )
        print(reason, flush=True)
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
                        f"HOLD (too-old): Crosslink invoice #{inv} date "
                        f"{bill.get('date')} is before {MIN_INVOICE_DATE}. "
                        "Did not create a header. Do not walk ancient Crosslink "
                        "without asking. Flag status=none (email left unflagged)."
                    ),
                    "KIMCO id": "",
                    "Batch": f"{BATCH_NAME} ({KNOWN_BATCH_ID})",
                    "Fees and surcharges": "none",
                    "PPV": "none",
                    "Attach status": "pdf-on-vm" if bill.get("pdf_path") else "",
                    "Flag in Outlook": "No",
                    "Flag status": "none",
                    "Notes": "",
                }
            )
        write_report(report_path, rows)
        sidecar = {
            "proof": "crosslink-0916",
            "invent": False,
            "mail_send": False,
            "vendor": VENDOR_NAME,
            "vendor_id": VENDOR_ID,
            "batch_name": BATCH_NAME,
            "batch_id": KNOWN_BATCH_ID,
            "batch": batch_info,
            "discovered": len(catalog),
            "catalog": catalog,
            "kimco_already": entered,
            "note30_left_alone": NOTE30_REMINDERS,
            "recent_picked": [],
            "older_not_entered": [summarize_parse(b) for b in older],
            "rows": rows,
            "blocker": reason,
            "treyce_emailed": False,
            "report": str(report_path),
        }
        report_path.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2, default=str) + "\n"
        )
        print(f"Wrote {report_path} and sidecar (no enter).", flush=True)
        return 0

    enter_rows = run_enter(
        client,
        recent,
        batch_name=BATCH_NAME,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    receipts = load_list_receipts(client)
    new_rows, finishes, gets = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
    )
    prior_rows = prior_rows_from_sidecar(report_path)
    rows = merge_sheet_rows(prior_rows, new_rows)
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    leftover_pending = [
        summarize_parse(b)
        for b in older
    ]
    sidecar = {
        "proof": "crosslink-0916",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": VENDOR_ID,
        "batch_name": BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "batch": batch_info,
        "discovered": len(catalog),
        "catalog": catalog,
        "kimco_already": entered,
        "note30_left_alone": NOTE30_REMINDERS,
        "first_five": FIRST_FIVE,
        "first_headers": CREATED_HEADERS,
        "chosen": [invoice_number_key(b.get("invoice_number")) for b in recent],
        "chosen_because": (
            "Second Crosslink pass on batch 715. Start 28008 if still open. "
            "Unflagged Crosslink only; not already on KIMCO; "
            f"invoice date on/after {MIN_INVOICE_DATE}; cap {CAP}. "
            "Fewer than 5 recent remain — enter what's left and stop. "
            "Do not recreate 10101–10105 or NOTE-30. Distinctive token "
            "Crosslink (never MSC/RMP)."
        ),
        "parsed": [summarize_parse(b) for b in recent],
        "older_not_entered": leftover_pending,
        "leftover_pending": leftover_pending,
        "rows": rows,
        "new_rows": new_rows,
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
