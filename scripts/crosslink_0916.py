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
from ap_clerk.kimco import KimcoClient  # noqa: E402
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    distinctive_vendor_tokens,
    extract_subject_invoice_number,
    invoice_number_key,
    lookup_id,
    lookup_text,
    names_match,
    parse_iso_date,
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
}
# Locked after 2026-09-16 discovery: newest unflagged Crosslink not on KIMCO.
PREFERRED_FIVE = ["28166", "28100", "28102", "28113", "28114"]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter up to 5 Crosslink bills on 9/16 batch")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
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

    rows = run_enter(
        client,
        recent,
        batch_name=BATCH_NAME,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    gets: dict[str, Any] = {}
    for row in rows:
        kid = row.get("KIMCO id")
        if kid not in (None, ""):
            gets[str(kid)] = live_get_proof(client, kid)

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
        "chosen": [invoice_number_key(b.get("invoice_number")) for b in recent],
        "chosen_because": (
            "Unflagged Crosslink Powder Coating only; not already on KIMCO; "
            f"invoice date on/after {MIN_INVOICE_DATE}; cap {CAP}. "
            "NOTE-30 reminders not recreated. Distinctive token Crosslink "
            "(never MSC/RMP)."
        ),
        "parsed": [summarize_parse(b) for b in recent],
        "older_not_entered": [summarize_parse(b) for b in older],
        "rows": rows,
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
