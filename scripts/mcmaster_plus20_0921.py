"""Add up to 5 more McMaster-Carr invoices to existing batch 721.

Do not create a new batch. Do not mutate headers 10138–10157.
invent=false. No Mail.Send.

NOTE-45: missing_receipt stays on 721 + Comments_1 @Shawn 104.
Over-PPV → Transfer AP + Comments_1 @Shawn. Fees = id 11 (NOTE-44).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged  # noqa: E402
from ap_clerk.kimco import KimcoClient, KimcoError  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from jpsteel_0916 import load_list_receipts  # noqa: E402
from aqpc_plus4 import summarize_parse  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    CAP,
    CREATED_HEADERS,
    EXISTING_HEADER_IDS,
    FORBIDDEN_BATCH_IDS,
    KNOWN_BATCH_ID,
    PLUS10_HEADERS,
    PLUS15_HEADERS,
    PLUS20_HEADERS,
    PLUS5_HEADERS,
    PREFERRED_BATCH_NAME,
    PREFERRED_PLUS20,
    VENDOR_NAME,
    bill_from_message,
    catalog_row,
    confirm_mcmaster_vendor,
    exact_invoice_number,
    find_mcmaster_messages,
    finish_entered_rows,
    is_mcmaster_invoice_email,
    leftover_from_catalog,
    merge_sheet_rows,
    pick_recent,
    prior_rows_from_sidecar,
    subject_po,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_plus20_0921")
SHEET_INVOICES = (
    set(CREATED_HEADERS) | set(PLUS5_HEADERS) | set(PLUS10_HEADERS) | set(PLUS15_HEADERS) | set(PLUS20_HEADERS)
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter up to 5 more McMaster bills on existing batch 721")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--parse-only", action="store_true")
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
        "Target: live. McMaster plus-20 on existing batch 721. "
        "Leave 10138-10157 alone (10142 Kyle; Transfer AP 10140/10143/10148/10154 Treyce). "
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
    verified = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    print(json.dumps({"batch": verified}, indent=2, default=str), flush=True)
    if (
        verified.get("is_forbidden")
        or int(verified.get("id") or 0) != KNOWN_BATCH_ID
        or not verified.get("matches_expected")
    ):
        print("Refusing to enter: batch 721 name/id mismatch or forbidden.", flush=True)
        return 2

    vendor_info = confirm_mcmaster_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    entered = dict(vendor_info.get("entered") or {})
    print(
        json.dumps(
            {
                "vendor_id": vendor_id,
                "entered_count": len(entered),
                "invent": False,
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if vendor_id in (None, ""):
        print("Live McMaster vendor id not confirmed. Will not invent. Stop.", flush=True)
        return 2
    already_on_sheet = {inv for inv, kid in entered.items() if int(kid) in EXISTING_HEADER_IDS}
    collision = set(PREFERRED_PLUS20) & (set(entered) | SHEET_INVOICES | already_on_sheet)
    if collision:
        print(f"Preferred plus-20 already on KIMCO/sheet: {sorted(collision)}. Stop.", flush=True)
        return 2

    messages = find_mcmaster_messages(graph)
    catalog = [catalog_row(msg, entered) for msg in messages]
    catalog.sort(key=lambda r: str(r.get("received") or ""), reverse=True)
    already = set(entered) | SHEET_INVOICES
    candidates: list[dict[str, Any]] = []
    skipped_flagged = 0
    for msg in messages:
        if is_already_flagged(msg):
            skipped_flagged += 1
            continue
        if not is_mcmaster_invoice_email(msg):
            continue
        candidates.append(msg)
    candidates.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    print(
        json.dumps(
            {
                "discovered": len(catalog),
                "unflagged_invoice_emails": [
                    {
                        "po": subject_po(str(m.get("subject") or "")),
                        "received": m.get("receivedDateTime"),
                        "subject": str(m.get("subject") or "")[:120],
                    }
                    for m in candidates
                ],
                "skipped_flagged": skipped_flagged,
                "preferred_plus20": list(PREFERRED_PLUS20),
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
        preferred=PREFERRED_PLUS20,
    )
    print(
        json.dumps(
            {
                "recent_picked": [
                    summarize_parse(b) | {"clean": b.get("_clean"), "census_po": b.get("_census_po")}
                    for b in recent
                ],
                "older_or_unclean_leftover": [summarize_parse(b) for b in older],
                "credits_skipped": [summarize_parse(b) for b in credits],
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if args.parse_only:
        return 0

    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    leftover_pending = leftover_from_catalog(
        parsed_bills,
        chosen={exact_invoice_number(b.get("invoice_number")) for b in recent},
        older=older,
        credits=credits,
    )
    if not recent:
        print("No remaining unflagged McMaster invoices on/after 2026-08-01. Stop.", flush=True)
        write_report(report_path, prior_rows)
        return 0

    enter_rows = run_enter(
        client,
        recent,
        batch_name=PREFERRED_BATCH_NAME,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=False,
    )
    for row in enter_rows:
        kid = row.get("KIMCO id")
        try:
            kid_int = int(kid) if kid not in (None, "") else None
        except (TypeError, ValueError):
            kid_int = None
        if kid_int in EXISTING_HEADER_IDS:
            print(f"Refusing to finish existing header {kid_int}. Abort.", flush=True)
            return 2

    receipts = load_list_receipts(client)
    rows, finishes = finish_entered_rows(
        client,
        graph,
        parsed_bills=recent,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=int(vendor_id),
    )
    created = {
        exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id")
        for r in rows
        if r.get("KIMCO id") not in (None, "")
    }
    PLUS20_HEADERS.update({k: int(v) for k, v in created.items() if v not in (None, "")})
    merged = merge_sheet_rows(prior_rows, rows)
    write_report(report_path, merged)
    sidecar = {
        "proof": "mcmaster-plus20-0921",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": vendor_id,
        "batch_name": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "batch": verified,
        "forbidden_batch_ids": sorted(FORBIDDEN_BATCH_IDS),
        "leave_alone": sorted(EXISTING_HEADER_IDS),
        "preferred_plus20": list(PREFERRED_PLUS20),
        "plus20_headers": dict(PLUS20_HEADERS),
        "created_headers": created,
        "discovered": len(catalog),
        "catalog": catalog,
        "kimco_already": entered,
        "recent_picked": [summarize_parse(b) for b in recent],
        "finishes": finishes,
        "new_rows": rows,
        "leftover_pending": leftover_pending,
        "rows": merged,
        "note45": "missing_receipt Comments_1 @Shawn 104, no Transfer AP; over-PPV Transfer AP + @Shawn; no Mail.Send",
    }
    report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    _print_summary(rows)
    print(
        json.dumps(
            {
                "batch_id": KNOWN_BATCH_ID,
                "sheet": str(report_path),
                "new": [
                    {
                        "invoice": r.get("Invoice #"),
                        "result": r.get("Result"),
                        "amount": r.get("Amount"),
                        "po": r.get("PO"),
                        "kimco": r.get("KIMCO id"),
                        "receipts": r.get("Receipts"),
                        "fees": r.get("Fees and surcharges"),
                        "ppv": r.get("PPV"),
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
