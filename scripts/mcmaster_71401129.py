"""Finish McMaster 71401129 / KIMCO 10152 only.

Kyle 2026-09-22: looks like it falls within PPV — process if so.
Live GET: Type 3 on batch 721, receipts 23939/23940/23941 already selected
(Invoiced, remaining negative), Shipping Fees 14.60 already posted,
Invoice_Amount 272.32 vs PDF/verification 257.72 → |PPV| $14.60.

NOTE-47: |bill PPV| under $75 → signed PPV + Success on 721.
$75+ → do not Select over-gate; Transfer AP + Comments_1 @Shawn 104.
Fees stay id 11 (shipping), never PPV.

Do not touch other McMaster HOLDs, Gas, or O'Neal.
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

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.quality_v12 import apply_exception_category_owner
from ap_clerk.report import write_report
from ap_clerk.rules import lookup_id, money, rounding_ppv_to_hit_pdf_total
from mcmaster_0918 import (
    DO_NOT_MUTATE_IDS,
    KNOWN_BATCH_ID,
    LEAVE_ALONE_HOLD_IDS,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    apply_over_ppv_transfer_ap,
    finish_hold_header,
    is_mcmaster_message,
    is_over_ppv_price_hold,
    mcmaster_proof,
    merge_sheet_rows,
    over_ppv_hold_comment,
    prior_rows_from_sidecar,
    quality_mcmaster_row,
)
from mcmaster_receipt_retry import (
    assert_existing_header,
    download_kimco_pdf,
    enter_row_from_sheet,
    find_local_pdf,
    parsed_from_pdf,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_71401129")

TARGET_INVOICE = "71401129"
TARGET_ID = 10152
TARGET_PO = "59125"
TARGET_AMOUNT = 257.72
KNOWN_RECEIPT_IDS = (23939, 23940, 23941)
NOTE47_PPV_MAX_ABS = 75.00
ALLOWED_WRITE_IDS = {TARGET_ID}
VENDOR_ID = 117


def ppv_abs_over_note47(ppv_total: Any) -> bool:
    amt = money(ppv_total)
    if amt is None:
        return False
    return abs(amt) >= NOTE47_PPV_MAX_ABS


def refuse_other_header(kimco_id: int, invoice_number: str) -> None:
    if int(kimco_id) != TARGET_ID:
        raise KimcoError(f"Refuse: this script writes 10152 only, not {kimco_id}")
    if str(invoice_number or "") != TARGET_INVOICE:
        raise KimcoError(f"Refuse: this script writes 71401129 only, not {invoice_number}")


def find_invoice_message(graph, invoice_number: str) -> dict[str, Any] | None:
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


def finish_10152_row(
    client: KimcoClient,
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    refuse_other_header(kimco_id, parsed.get("invoice_number") or enter_row.get("Invoice #"))
    before = mcmaster_proof(client, kimco_id)
    posted = money(before.get("invoice_amount"))
    pdf_amt = money(parsed.get("amount")) or TARGET_AMOUNT
    gap = None if posted is None else round(pdf_amt - posted, 2)
    extra: dict[str, Any] = {
        "before_amount": posted,
        "pdf_amount": pdf_amt,
        "gap": gap,
        "over_note47": ppv_abs_over_note47(gap),
        "already_receipts": [ln.get("receipt") for ln in (before.get("receipt_lines") or [])],
        "already_fees": before.get("fee_amounts"),
    }
    if gap is not None and ppv_abs_over_note47(gap):
        comment = over_ppv_hold_comment(
            invoice_number=TARGET_INVOICE,
            po=TARGET_PO,
            pdf_amount=pdf_amt,
        )
        transfer = apply_over_ppv_transfer_ap(
            client,
            kimco_id=int(kimco_id),
            comment=comment,
            allow_ids=ALLOWED_WRITE_IDS,
        )
        row = dict(enter_row)
        row["Result"] = "HOLD"
        row["Why"] = (
            f"HOLD (price-does-not-match): |bill PPV| {gap} is over NOTE-47 "
            f"${NOTE47_PPV_MAX_ABS:.0f}. Receipts were NOT re-selected. "
            f"Transfer AP status={transfer.get('status')}."
        )
        row = apply_exception_category_owner(row)
        if transfer.get("status") == "moved":
            row["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
        extra["transfer_ap"] = transfer
        extra["select_status"] = "ppv-lock-select-zero"
        return row, extra

    finish = finish_hold_header(
        client, parsed=parsed, kimco_id=int(kimco_id), receipts=receipts
    )
    proof = finish.get("after") or mcmaster_proof(client, kimco_id)
    # Already-selected path: finish_hold_header posts rounding PPV to hit PDF.
    if not finish.get("ppv_amount"):
        decision = rounding_ppv_to_hit_pdf_total(
            parsed.get("amount"),
            proof.get("invoice_amount"),
            receipts_selected=bool(proof.get("receipt_lines")),
        )
        extra["rounding"] = decision
        needed = money(decision.get("ppv")) or 0.0
        if (
            decision.get("action") == "ppv"
            and needed
            and not ppv_abs_over_note47(needed)
        ):
            status = client.try_post_ppv(int(kimco_id), needed)
            finish["ppv_status"] = status
            finish["ppv_amount"] = needed
            proof = mcmaster_proof(client, kimco_id)
    extra.update({k: v for k, v in finish.items() if k != "after"})
    row = quality_mcmaster_row(
        graph,
        parsed=parsed,
        enter_row=enter_row,
        proof=proof,
        finish=finish,
        vendor_id=VENDOR_ID,
    )
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
        if transfer.get("status") == "moved":
            row["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
        row["Why"] = (
            f"{row.get('Why')} Transfer AP status={transfer.get('status')}."
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-18-mcmaster.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live (McMaster 71401129 / 10152).")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    graph = None if args.preflight_only else _optional_graph_client()

    refuse_other_header(TARGET_ID, TARGET_INVOICE)
    header = assert_existing_header(client, TARGET_ID, TARGET_INVOICE)
    vals = header.get("values") or {}
    pdf = find_local_pdf(TARGET_INVOICE) or download_kimco_pdf(
        client, TARGET_ID, TARGET_INVOICE
    )
    if pdf is None:
        print("Missing PDF for 71401129 / 10152. Stop — no writes.", flush=True)
        return 2
    parsed = parsed_from_pdf(pdf, TARGET_INVOICE, TARGET_PO)
    message = find_invoice_message(graph, TARGET_INVOICE)
    if message and message.get("id"):
        parsed["graph_message_id"] = message.get("id")
        parsed["subject"] = message.get("subject")

    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    enter_row = enter_row_from_sheet(prior_rows, TARGET_INVOICE, TARGET_ID)
    proof = mcmaster_proof(client, TARGET_ID)
    preflight = {
        "invoice": TARGET_INVOICE,
        "kimco": TARGET_ID,
        "po": TARGET_PO,
        "pdf": str(pdf),
        "pdf_amount": parsed.get("amount"),
        "lines": parsed.get("lines"),
        "fees": parsed.get("fees"),
        "header_amount": money(vals.get("Invoice_Amount")),
        "header_verification": money(vals.get("Invoice_Verification_Amount")),
        "header_batch": lookup_id(vals.get("AP_Invoice_Batch")),
        "already_selected": proof.get("receipt_lines"),
        "already_fees": proof.get("fee_amounts"),
        "already_ppv": proof.get("ppv_amounts"),
        "gap": None
        if money(vals.get("Invoice_Amount")) is None
        else round(TARGET_AMOUNT - money(vals.get("Invoice_Amount")), 2),
        "note47": NOTE47_PPV_MAX_ABS,
        "leave_alone_still_includes_others": sorted(LEAVE_ALONE_HOLD_IDS - ALLOWED_WRITE_IDS),
        "do_not_mutate": sorted(DO_NOT_MUTATE_IDS),
    }
    print(json.dumps({"preflight": preflight}, indent=2, default=str), flush=True)
    if args.preflight_only:
        return 0

    row, extra = finish_10152_row(
        client,
        graph,
        parsed=parsed,
        enter_row=enter_row,
        kimco_id=TARGET_ID,
        receipts=[],
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
        "proof": "mcmaster-71401129",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": VENDOR_ID,
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
    report_path.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2, default=str) + "\n"
    )
    print(f"Sheet: {report_path}", flush=True)
    return 0 if row.get("Result") == "Success" else 0


if __name__ == "__main__":
    raise SystemExit(main())
