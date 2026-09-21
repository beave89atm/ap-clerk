"""Finalize McMaster 71080498 / KIMCO 10139 only.

Kyle 2026-09-21: line receipts are now entered. Finish this one HOLD.
Do not touch other McMaster HOLDs, Transfer AP, Gas, or O'Neal.

Batch 721. Fees → Additional Charge Fees id 11 (NOTE-44). In-gate PPV
only. Over-gate → do not Select those lines; Transfer AP + Comments_1
@Shawn 104. Prefer Success if part/qty/cost match works.

invent=false. No Mail.Send. No invent Success.
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
from ap_clerk.cli import _optional_graph_client  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence  # noqa: E402
from ap_clerk.kimco import KimcoClient, KimcoError  # noqa: E402
from ap_clerk.quality_v12 import apply_exception_category_owner  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import money  # noqa: E402
from jpsteel_0916 import hydrate_receipts, load_list_receipts, open_receipts_on_po  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    DO_NOT_MUTATE_IDS,
    KNOWN_BATCH_ID,
    LEAVE_ALONE_HOLD_IDS,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    apply_over_ppv_transfer_ap,
    exact_invoice_number,
    finish_hold_header,
    is_mcmaster_message,
    is_over_ppv_price_hold,
    merge_sheet_rows,
    mcmaster_proof,
    over_ppv_hold_comment,
    prior_rows_from_sidecar,
    quality_mcmaster_row,
)
from mcmaster_receipt_retry import (  # noqa: E402
    assert_existing_header,
    download_kimco_pdf,
    enter_row_from_sheet,
    find_local_pdf,
    parsed_from_pdf,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_10139_finalize")

TARGET_INVOICE = "71080498"
TARGET_ID = 10139
TARGET_PO = "59056"
TARGET_AMOUNT = 395.31
ALLOWED_WRITE_IDS = {TARGET_ID}
# Prior PUT 400: Quantity_Remaining 0. Use only if remaining is now open.
KNOWN_LOCKED_RECEIPT_ID = 24247
# Unreceived via 24246 — never first-open this leftover for 71080498.
FIRST_OPEN_DO_NOT_USE = {23841}


def refuse_other_header(kimco_id: int, invoice_number: str | None = None) -> None:
    kid = int(kimco_id)
    inv = exact_invoice_number(invoice_number) or invoice_number
    if kid != TARGET_ID:
        raise KimcoError(f"Refuse: this script writes {TARGET_ID} only, not {kid}")
    if inv not in (None, "") and inv != TARGET_INVOICE:
        raise KimcoError(f"Refuse: this script writes {TARGET_INVOICE} only, not {inv}")


def usable_po_receipts(pool: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop locked / zero-remaining leftovers. Do not first-open 23841."""
    usable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rec in pool:
        rid = rec.get("id")
        raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
        remaining_raw = raw.get("Quantity_Remaining")
        if remaining_raw in (None, ""):
            remaining_raw = raw.get("Qty_Remaining")
        if remaining_raw in (None, ""):
            remaining_raw = raw.get("Quantity_Open")
        remaining = money(remaining_raw)
        if rid in FIRST_OPEN_DO_NOT_USE:
            skipped.append({"id": rid, "reason": "first-open-23841", "remaining": remaining})
            continue
        if remaining is not None and remaining <= 0:
            skipped.append({"id": rid, "reason": "zero-remaining", "remaining": remaining})
            continue
        if rid == KNOWN_LOCKED_RECEIPT_ID and remaining is not None and remaining <= 0:
            skipped.append({"id": rid, "reason": "locked-24247", "remaining": remaining})
            continue
        usable.append(rec)
    return usable, skipped


def find_invoice_message(graph, invoice_number: str) -> dict[str, Any] | None:
    """Already-flagged Entered with issues still needs Entered in AI on Success."""
    if graph is None:
        return None
    try:
        hits = graph.search_messages(ALLOWED_MAILBOX, invoice_number, top=25)
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("Graph search %s failed: %s", invoice_number, type(exc).__name__)
        return None
    for msg in hits:
        subject = str(msg.get("subject") or "")
        preview = str(msg.get("bodyPreview") or "")
        if invoice_number not in subject and invoice_number not in preview:
            continue
        if is_mcmaster_message(msg):
            return msg
    return None


def finish_10139_row(
    client: KimcoClient,
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    refuse_other_header(kimco_id, parsed.get("invoice_number") or enter_row.get("Invoice #"))
    usable, skipped_locked = usable_po_receipts(receipts)
    finish = finish_hold_header(
        client, parsed=parsed, kimco_id=int(kimco_id), receipts=usable
    )
    finish["skipped_locked"] = skipped_locked
    proof = finish.get("after") or mcmaster_proof(client, kimco_id)
    row = quality_mcmaster_row(
        graph,
        parsed=parsed,
        enter_row=enter_row,
        proof=proof,
        finish=finish,
        vendor_id=117,
    )
    extra: dict[str, Any] = {k: v for k, v in finish.items() if k != "after"}
    if is_over_ppv_price_hold(row, finish):
        comment = over_ppv_hold_comment(
            invoice_number=TARGET_INVOICE,
            po=str(parsed.get("po") or enter_row.get("PO") or TARGET_PO),
            pdf_amount=parsed.get("amount") or enter_row.get("Amount") or TARGET_AMOUNT,
        )
        transfer = apply_over_ppv_transfer_ap(
            client,
            kimco_id=int(kimco_id),
            comment=comment,
            allow_ids=ALLOWED_WRITE_IDS,
        )
        extra["transfer_ap"] = transfer
        mention = transfer.get("mention_notify") or {}
        if transfer.get("status") == "moved":
            row["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
        row["Why"] = (
            f"{row.get('Why')} Transfer AP status={transfer.get('status')} "
            f"batch_id={transfer.get('batch_id')} "
            f"@mention={mention.get('report') or mention}."
        )
        row = apply_exception_category_owner(row)
    extra["proof"] = {
        "invoice_amount": proof.get("invoice_amount"),
        "verification": proof.get("verification_amount") or proof.get("verification"),
        "receipts": row.get("Receipts"),
        "vendor_id": proof.get("vendor_id"),
        "batch_id": proof.get("batch_id"),
        "fee_amounts": proof.get("fee_amounts"),
        "ppv_amounts": proof.get("ppv_amounts"),
    }
    return row, extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize McMaster 71080498 / 10139 only")
    parser.add_argument("--preflight-only", action="store_true")
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
        "Target: live. Finalize McMaster 71080498 / 10139 only. "
        "Batch 721. No other HOLDs. No Mail.Send. invent=false.",
        flush=True,
    )
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    client = KimcoClient.authenticate(creds.instance_url, creds.key, creds.password, target="live")
    graph = _optional_graph_client()

    refuse_other_header(TARGET_ID, TARGET_INVOICE)
    if TARGET_ID in DO_NOT_MUTATE_IDS:
        print("10139 is GET-only Success. Stop — no writes.", flush=True)
        return 2

    header = assert_existing_header(client, TARGET_ID, TARGET_INVOICE)
    vals = header.get("values") or {}
    pdf = find_local_pdf(TARGET_INVOICE) or download_kimco_pdf(client, TARGET_ID, TARGET_INVOICE)
    if pdf is None:
        print("Missing PDF for 71080498 / 10139. Stop — no writes.", flush=True)
        return 2
    parsed = parsed_from_pdf(pdf, TARGET_INVOICE, TARGET_PO)
    message = find_invoice_message(graph, TARGET_INVOICE)
    if message and message.get("id"):
        parsed["graph_message_id"] = message.get("id")
        parsed["subject"] = message.get("subject")

    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    enter_row = enter_row_from_sheet(prior_rows, TARGET_INVOICE, TARGET_ID)
    receipts = load_list_receipts(client)
    pool = hydrate_receipts(client, open_receipts_on_po(receipts, TARGET_PO))
    usable, skipped_locked = usable_po_receipts(pool)
    preflight = {
        "invoice": TARGET_INVOICE,
        "kimco": TARGET_ID,
        "pdf": str(pdf),
        "pdf_amount": parsed.get("amount"),
        "pdf_po": parsed.get("po"),
        "lines": parsed.get("lines"),
        "fees": parsed.get("fees"),
        "header_amount": vals.get("Invoice_Amount"),
        "header_verification": vals.get("Invoice_Verification_Amount"),
        "header_batch": vals.get("AP_Invoice_Batch"),
        "header_po": vals.get("Purchase_Order"),
        "open_on_po": [
            {
                "id": r.get("id"),
                "qty": r.get("qty"),
                "unit_price": r.get("unit_price"),
                "part": r.get("part"),
                "remaining": money((r.get("raw") or {}).get("Quantity_Remaining"))
                if isinstance(r.get("raw"), dict)
                else None,
            }
            for r in pool
        ],
        "usable": [
            {
                "id": r.get("id"),
                "qty": r.get("qty"),
                "unit_price": r.get("unit_price"),
                "part": r.get("part"),
            }
            for r in usable
        ],
        "skipped_locked": skipped_locked,
        "leave_alone_still_includes_others": sorted(LEAVE_ALONE_HOLD_IDS - ALLOWED_WRITE_IDS),
        "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
        "outlook_message_found": bool(parsed.get("graph_message_id")),
    }
    print(json.dumps({"preflight": preflight}, indent=2, default=str), flush=True)
    if args.preflight_only:
        return 0

    row, extra = finish_10139_row(
        client,
        graph,
        parsed=parsed,
        enter_row=enter_row,
        kimco_id=TARGET_ID,
        receipts=usable,
    )
    print(
        json.dumps(
            {
                "invoice": TARGET_INVOICE,
                "kimco": TARGET_ID,
                "result": row.get("Result"),
                "receipts": row.get("Receipts"),
                "fees": row.get("Fees and surcharges"),
                "ppv": row.get("PPV"),
                "batch": row.get("Batch"),
                "why": row.get("Why"),
                "select_status": extra.get("select_status"),
                "fee_status": extra.get("fee_status"),
                "ppv_status": extra.get("ppv_status"),
                "skipped_over_ppv": extra.get("skipped_over_ppv"),
                "transfer_ap": extra.get("transfer_ap"),
                "proof": extra.get("proof"),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )

    merged = merge_sheet_rows(prior_rows, [row])
    write_report(report_path, merged)
    sidecar = {
        "proof": "mcmaster-10139-finalize",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": 117,
        "batch_name": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "target": {"invoice": TARGET_INVOICE, "kimco_id": TARGET_ID, "po": TARGET_PO},
        "preflight": preflight,
        "finish": extra,
        "new_rows": [row],
        "rows": merged,
    }
    if report_path.with_suffix(".json").is_file():
        prior = json.loads(report_path.with_suffix(".json").read_text())
        sidecar["prior_proof"] = prior.get("proof")
        sidecar["leftover_pending"] = prior.get("leftover_pending")
        sidecar["catalog"] = prior.get("catalog")
    report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Sheet: {report_path}", flush=True)
    print(
        json.dumps(
            {
                "Invoice": TARGET_INVOICE,
                "KIMCO id": TARGET_ID,
                "Result": row.get("Result"),
                "receipts selected": row.get("Receipts"),
                "Fees": row.get("Fees and surcharges"),
                "PPV": row.get("PPV"),
                "batch": row.get("Batch"),
                "Why": row.get("Why") if row.get("Result") != "Success" else "",
                "sheet": str(report_path),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
