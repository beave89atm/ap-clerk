"""Enter 5 more AQPC payment-request bills on batch 711 (40-row sheet).

Skips KIMCO 10007–10041 and Kyle-entered 10917/10918/10920/10921.
Guest Intuit View-details click only. No Intuit login. No Mail.Send.
invent=false.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from aqpc_batch711_plus5 import (  # noqa: E402
    BATCH_ID,
    BATCH_NAME,
    VENDOR_NEEDLE,
    format_receipts,
    kimco_aqpc_numbers,
    write_kyle_sheet,
)
from aqpc_batch711_plus5_20 import (  # noqa: E402
    quality_row,
    try_finish_receipts,
)
from aqpc_plus4 import bill_from_message, live_get_proof, summarize_parse  # noqa: E402
from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.browser_pdf import format_intuit_session_presence  # noqa: E402
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged  # noqa: E402
from ap_clerk.kimco import KimcoClient  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    AQPC_MIN_INVOICE_DATE,
    AQPC_TOO_OLD_INVOICES,
    AQPC_TOO_OLD_KIMCO_IDS,
    aqpc_discover_skip_invoice,
    aqpc_invoice_too_old,
    money,
    normalize_receipt,
)

LOGGER = logging.getLogger("ap_clerk.aqpc_batch711_40")

ALREADY_ON_711 = {
    "11002",
    "10999",
    "11003",
    "11004",
    "11005",
    "10998",
    "10991",
    "10984",
    "10969",
    "10968",
    "10967",
    "10964",
    "10962",
    "10958",
    "10956",
    "10955",
    "10954",
    "10953",
    "10952",
    "10950",
    "10946",
    "10945",
    "10939",
    "10938",
    "10934",
    "10933",
    "10932",
    "10929",
    "10928",
    "10927",
    "10926",
    "10925",
    "10924",
    "10696",
    "10523",
}
KYLE_ENTERED = {"10917", "10918", "10920", "10921"}
# Kyle 2026-09-16: voided too-old headers 10040–10046. Do not re-enter.
TOO_OLD = set(AQPC_TOO_OLD_INVOICES)
ALREADY = ALREADY_ON_711 | KYLE_ENTERED | TOO_OLD
# After Aug/Sep AQPC is exhausted, stop. Do not walk older payment-requests.
PREFERRED_FIVE: list[str] = []
KNOWN_THIRTY_FIVE = [
    {"invoice": "11002", "kimco_id": 10007},
    {"invoice": "10999", "kimco_id": 10008},
    {"invoice": "11003", "kimco_id": 10009},
    {"invoice": "11004", "kimco_id": 10010},
    {"invoice": "11005", "kimco_id": 10011},
    {"invoice": "10998", "kimco_id": 10012},
    {"invoice": "10991", "kimco_id": 10013},
    {"invoice": "10984", "kimco_id": 10014},
    {"invoice": "10969", "kimco_id": 10015},
    {"invoice": "10968", "kimco_id": 10016},
    {"invoice": "10967", "kimco_id": 10017},
    {"invoice": "10964", "kimco_id": 10018},
    {"invoice": "10962", "kimco_id": 10019},
    {"invoice": "10958", "kimco_id": 10020},
    {"invoice": "10956", "kimco_id": 10021},
    {"invoice": "10955", "kimco_id": 10022},
    {"invoice": "10954", "kimco_id": 10023},
    {"invoice": "10953", "kimco_id": 10024},
    {"invoice": "10952", "kimco_id": 10025},
    {"invoice": "10950", "kimco_id": 10026},
    {"invoice": "10946", "kimco_id": 10027},
    {"invoice": "10945", "kimco_id": 10028},
    {"invoice": "10939", "kimco_id": 10029},
    {"invoice": "10938", "kimco_id": 10031},
    {"invoice": "10934", "kimco_id": 10030},
    {"invoice": "10933", "kimco_id": 10032},
    {"invoice": "10932", "kimco_id": 10033},
    {"invoice": "10929", "kimco_id": 10034},
    {"invoice": "10928", "kimco_id": 10035},
    {"invoice": "10927", "kimco_id": 10036},
    {"invoice": "10926", "kimco_id": 10037},
    {"invoice": "10925", "kimco_id": 10038},
    {"invoice": "10924", "kimco_id": 10039},
    {"invoice": "10696", "kimco_id": 10040},
    {"invoice": "10523", "kimco_id": 10041},
]
PRIOR_SHEET = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-35.json"
HOLD_PDF_AMOUNTS = {
    10009: 10.0,
    10013: 199.0,
    10038: 730.0,
}
# NOTE-28: only Aug/Sep 2026 mail. Do not list older months into KIMCO.
AUG_SEP_MONTHS = [
    (date(2026, 8, 1), date(2026, 8, 31)),
    (date(2026, 9, 1), date(2026, 9, 30)),
]


def _subject_inv(subject: str) -> str:
    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def _absorb(seen: dict[str, dict[str, Any]], hits: list[dict[str, Any]]) -> None:
    for msg in hits:
        mid = str(msg.get("id") or "")
        subject = str(msg.get("subject") or "")
        if VENDOR_NEEDLE not in subject.upper():
            continue
        if "payment request" not in subject.lower() and "invoice" not in subject.lower():
            continue
        if mid:
            seen[mid] = msg


def find_aqpc_payment_requests(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    generics = [
        f"New payment request from {VENDOR_NEEDLE}",
        f"{VENDOR_NEEDLE} - invoice",
        "AMERICAN QUALITY POWDER COATING payment request",
    ]
    for needle in generics:
        try:
            _absorb(seen, graph.search_messages(ALLOWED_MAILBOX, needle, top=50))
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
    for start, end in AUG_SEP_MONTHS:
        try:
            _absorb(
                seen,
                graph.list_messages(ALLOWED_MAILBOX, received_from=start, received_to=end),
            )
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph list %s–%s failed: %s", start, end, type(exc).__name__)
    found = {_subject_inv(str(m.get("subject") or "")) for m in seen.values()}
    extras = [
        n
        for n in [
            *PREFERRED_FIVE,
            *ALREADY_ON_711,
            *[str(n) for n in range(11006, 11016)],
        ]
        if n not in found
    ]
    for n in extras:
        try:
            _absorb(
                seen,
                graph.search_messages(
                    ALLOWED_MAILBOX,
                    f"New payment request from {VENDOR_NEEDLE} - invoice {n}",
                    top=5,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search invoice %s failed: %s", n, type(exc).__name__)
    return list(seen.values())


def pick_five(
    messages: list[dict[str, Any]],
    *,
    already: set[str],
) -> list[dict[str, Any]]:
    by_inv: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        if not inv or inv in already or aqpc_discover_skip_invoice(inv):
            continue
        if is_already_flagged(msg):
            continue
        by_inv.setdefault(inv, []).append(msg)
    extras: list[tuple[str, dict[str, Any]]] = []
    for inv, cands in by_inv.items():
        cands.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        extras.append((inv, cands[0]))
    extras.sort(key=lambda pair: (0 if pair[0] in PREFERRED_FIVE else 1, -int(pair[0] or 0)))
    chosen: list[dict[str, Any]] = []
    for inv, msg in extras:
        if len(chosen) >= 5:
            break
        msg["_wanted_invoice"] = inv
        chosen.append(msg)
    return chosen


def prior_rows_from_sheet() -> dict[str, dict[str, Any]]:
    if not PRIOR_SHEET.exists():
        return {}
    payload = json.loads(PRIOR_SHEET.read_text())
    return {str(r.get("Invoice #")): r for r in payload.get("rows") or []}


def catalog_messages(messages: list[dict[str, Any]], entered: dict[str, int]) -> list[dict[str, Any]]:
    catalog = []
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        catalog.append(
            {
                "invoice": inv,
                "received": msg.get("receivedDateTime"),
                "flagged": is_already_flagged(msg),
                "categories": msg.get("categories") or [],
                "already_kimco": entered.get(inv),
                "already_skip": inv in ALREADY,
                "subject": str(msg.get("subject") or "")[:120],
            }
        )
    catalog.sort(key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")), reverse=True)
    return catalog


def _needs_finish(parsed_row: dict[str, Any], proof: dict[str, Any]) -> bool:
    recs = proof.get("receipt_lines") or []
    lines = parsed_row.get("lines") or []
    pdf_amt = money(parsed_row.get("amount"))
    posted = money(proof.get("invoice_amount"))
    if not recs:
        return True
    if lines and len(recs) < len(lines):
        return True
    if pdf_amt is not None and posted is not None and abs(pdf_amt - posted) > 0.02:
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter 5 more AQPC invoices on batch 711 (40-row sheet)")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--skip-enter", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-40.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(format_intuit_session_presence(), flush=True)
    print("Target: live. No Intuit login / no storage-state. No Mail.Send. invent=false.", flush=True)
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
    batch = client.get_item("ap_batches", BATCH_ID)
    bvals = batch.get("values") or {}
    print(
        f"Batch {BATCH_ID} name={bvals.get('AP_Invoice_Batch_ID')} "
        f"status={bvals.get('Status')} unposted={bvals.get('Unposted_Count')}",
        flush=True,
    )

    entered = kimco_aqpc_numbers(client)
    print(f"KIMCO AQPC invoices already present: {sorted(entered)}", flush=True)
    messages = find_aqpc_payment_requests(graph)
    catalog = catalog_messages(messages, entered)
    print(json.dumps({"aqpc_mail": catalog}, indent=2, default=str), flush=True)

    already = set(ALREADY) | set(entered)
    picked = pick_five(messages, already=already)
    print("Picked five:", [m.get("_wanted_invoice") for m in picked], flush=True)
    for msg in picked:
        print(
            f"  {msg.get('_wanted_invoice')} recv={msg.get('receivedDateTime')} "
            f"flagged={is_already_flagged(msg)} subj={str(msg.get('subject') or '')[:90]}",
            flush=True,
        )
    if args.discover_only:
        print(
            f"NOTE-28: {len(picked)} remaining Aug/Sep AQPC "
            f"(min invoice date {AQPC_MIN_INVOICE_DATE.isoformat()}; "
            "will not walk older payment-requests).",
            flush=True,
        )
        return 0

    enter_rows: list[dict[str, Any]] = []
    parsed: list[dict[str, Any]] = []
    invoices: list[dict[str, Any]] = []
    if not args.skip_enter:
        if not picked:
            print(
                "Aug/Sep AQPC exhausted. NOTE-28: stop; do not walk older "
                "payment-requests into KIMCO.",
                flush=True,
            )
        pdf_dir = ROOT / "runs" / "inbox-pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        invoices = [bill_from_message(graph, msg, pdf_dir) for msg in picked]
        invoices = [
            inv
            for inv in invoices
            if not aqpc_invoice_too_old(
                vendor=str(inv.get("vendor") or ""),
                invoice_date=inv.get("date"),
                invoice_number=str(inv.get("invoice_number") or ""),
            )
        ]
        parsed = [summarize_parse(inv) for inv in invoices]
        print(json.dumps({"parsed": parsed}, indent=2, default=str), flush=True)
        enter_rows = run_enter(
            client,
            invoices,
            batch_name=BATCH_NAME,
            pdf_dir=pdf_dir,
            graph_client=graph,
            mailbox=ALLOWED_MAILBOX,
            flag_outlook=True,
        ) if invoices else []
        if enter_rows:
            _print_summary(enter_rows)

    receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
    prior = prior_rows_from_sheet()
    known_rows: list[dict[str, Any]] = []
    known_gets: dict[str, Any] = {}
    for spec in KNOWN_THIRTY_FIVE:
        kid = spec["kimco_id"]
        inv = spec["invoice"]
        if kid in AQPC_TOO_OLD_KIMCO_IDS or inv in TOO_OLD:
            old = prior.get(inv) or {}
            row = dict(old) if old else {
                "Vendor": "American Quality Powder Coating",
                "Invoice #": inv,
                "KIMCO id": kid,
                "Batch": f"{BATCH_NAME} ({BATCH_ID})",
            }
            row["Result"] = "Voided"
            row["Why"] = (
                "Voided (too-old / Kyle reverse 2026-09-15). AQPC invoice date "
                "before 2026-08-01. Header 10040–10046 reversed. Do not re-enter. NOTE-28."
            )
            row["Flag status"] = "cleared"
            row["Flag in Outlook"] = "No"
            row["Notes"] = "voided as too-old"
            known_rows.append(row)
            continue
        got = live_get_proof(client, kid)
        known_gets[str(kid)] = got
        old = prior.get(inv) or {}
        row = dict(old) if old else {
            "Vendor": "American Quality Powder Coating",
            "Invoice #": inv,
            "KIMCO id": kid,
            "Batch": f"{BATCH_NAME} ({BATCH_ID})",
        }
        row["Receipts"] = format_receipts(got) or old.get("Receipts") or ""
        row["Attach"] = "attached" if got.get("attachments") else old.get("Attach") or old.get("Attach status") or ""
        row["Attach status"] = row["Attach"]
        row["Fees"] = old.get("Fees") or old.get("Fees and surcharges") or "none"
        row["Fees and surcharges"] = row["Fees"]
        row["PPV"] = old.get("PPV") or "none"
        row["Flag in Outlook"] = "Yes"
        row["Notes"] = ""
        if kid in HOLD_PDF_AMOUNTS:
            row["Amount"] = HOLD_PDF_AMOUNTS[kid]
            row["Result"] = "HOLD"
            row["Flag status"] = "entered-with-issues"
        known_rows.append(row)

    new_gets: dict[str, Any] = {}
    finishes: dict[str, Any] = {}
    sheet_new: list[dict[str, Any]] = []
    parsed_by_inv = {str(p.get("invoice_number")): p for p in parsed}
    invoice_by_num = {str(inv.get("invoice_number")): inv for inv in invoices}
    for row in enter_rows:
        inv_no = str(row.get("Invoice #") or "")
        kid = row.get("KIMCO id")
        parsed_row = invoice_by_num.get(inv_no) or parsed_by_inv.get(inv_no) or {}
        finish = None
        why = str(row.get("Why") or "").lower()
        if "already-entered" in why or "already entered" in why:
            out = dict(row)
            out["Fees"] = row.get("Fees and surcharges") or "none"
            out["Attach"] = row.get("Attach status") or ""
            out["Receipts"] = ""
            out["Flag in Outlook"] = "Yes"
            out["Notes"] = ""
            sheet_new.append(out)
            continue
        if kid not in (None, ""):
            proof = live_get_proof(client, int(kid))
            if _needs_finish(parsed_row, proof):
                finish = try_finish_receipts(
                    client,
                    kimco_id=int(kid),
                    parsed=parsed_row,
                    receipts=receipts,
                )
                proof = finish.get("after") or live_get_proof(client, int(kid))
                finishes[inv_no] = {k: v for k, v in finish.items() if k != "after"}
            new_gets[str(kid)] = proof
            quality = quality_row(
                graph,
                parsed=parsed_row,
                enter_row=row,
                proof=proof,
                finish=finish,
            )
            sheet_new.append(quality)
        else:
            out = dict(row)
            out["Fees"] = row.get("Fees and surcharges") or "none"
            out["Attach"] = row.get("Attach status") or ""
            out["Receipts"] = ""
            if out.get("Result") == "Skipped":
                out["Result"] = "HOLD"
                out["Why"] = (
                    f"HOLD (pdf-behind-link or enter-failed) for AQPC {inv_no}. "
                    "Never AI Skipped for AQPC. " + str(row.get("Why") or "")
                )
                out["Flag status"] = "ai-hold"
            sheet_new.append(out)

    all_rows = known_rows + sheet_new
    report_path = Path(args.report)
    write_kyle_sheet(report_path, all_rows)
    also = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.xlsx"
    if report_path.resolve() != also.resolve():
        write_kyle_sheet(also, all_rows)
    print(f"Wrote {report_path}", flush=True)

    sidecar = {
        "proof": "aqpc-batch711-40",
        "invent": False,
        "mail_send": False,
        "storage_state": False,
        "intuit_login": False,
        "batch_name": BATCH_NAME,
        "batch_id": BATCH_ID,
        "skipped_already_on_711": {inv: entered.get(inv) for inv in sorted(ALREADY_ON_711)},
        "skipped_kyle_entered": {inv: entered.get(inv) for inv in sorted(KYLE_ENTERED)},
        "chosen": [m.get("_wanted_invoice") for m in picked],
        "parsed": parsed,
        "enter_rows": enter_rows,
        "finishes": finishes,
        "known_gets": known_gets,
        "new_gets": new_gets,
        "rows": all_rows,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    also_json = also.with_suffix(".json")
    if sidecar_path.resolve() != also_json.resolve():
        also_json.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    success = sum(1 for r in sheet_new if r.get("Result") == "Success")
    hold = sum(1 for r in sheet_new if r.get("Result") == "HOLD")
    all_success = sum(1 for r in all_rows if r.get("Result") == "Success")
    all_hold = sum(1 for r in all_rows if r.get("Result") == "HOLD")
    print(f"NEW five: Success={success} HOLD={hold}", flush=True)
    print(f"ALL 40: Success={all_success} HOLD={all_hold}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
