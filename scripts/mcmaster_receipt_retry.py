"""Finish existing McMaster missing_receipt HOLDs after receipts are entered.

Do not create headers. Batch 721 only. invent=false. No Mail.Send.

Kyle 2026-09-21: 10139 / 10142 / 10144 / 10145 / 10146 / 10147 now have
receipts. Select Receipts by part/qty/PO, shipping → Fees id 11, in-gate
PPV only. Over-gate → Transfer AP + Comments_1 @Shawn (NOTE-43).
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
from ap_clerk.graph import format_graph_presence  # noqa: E402
from ap_clerk.kimco import KimcoClient, KimcoError  # noqa: E402
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.quality_v12 import apply_exception_category_owner  # noqa: E402
from ap_clerk.report import write_report  # noqa: E402
from ap_clerk.rules import lookup_id, lookup_text  # noqa: E402
from jpsteel_0916 import load_list_receipts  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    DO_NOT_MUTATE_IDS,
    KNOWN_BATCH_ID,
    LEAVE_ALONE_HOLD_IDS,
    PREFERRED_BATCH_NAME,
    RETRY_HEADERS,
    VENDOR_NAME,
    apply_over_ppv_transfer_ap,
    exact_invoice_number,
    finish_hold_header,
    is_over_ppv_price_hold,
    merge_sheet_rows,
    mcmaster_proof,
    over_ppv_hold_comment,
    prior_rows_from_sidecar,
    quality_mcmaster_row,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_receipt_retry")
PDF_DIR = ROOT / "runs" / "inbox-pdfs"
VENDOR_ID = 117
RETRY_POS = {
    "71080498": "59056",
    "72094446": "59224",
    "72087570": "59235",
    "72012111": "59219",
    "72013304": "58221",
    "71839575": "59191",
}
RETRY_AMOUNTS = {
    "71080498": 395.31,
    "72094446": 131.21,
    "72087570": 30.32,
    "72012111": 217.39,
    "72013304": 443.49,
    "71839575": 217.39,
}


def find_local_pdf(invoice_number: str) -> Path | None:
    hits = sorted(PDF_DIR.glob(f"*Invoice_{invoice_number}*"))
    hits += sorted(PDF_DIR.glob(f"*Invoice {invoice_number}*"))
    return hits[0] if hits else None


def download_kimco_pdf(client: KimcoClient, kimco_id: int, invoice_number: str) -> Path | None:
    """GET the attached invoice PDF from the existing header. No new attach."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    attachments = client.list_attachments(int(kimco_id))
    for item in attachments:
        name = str(item.get("name") or item.get("fileName") or item.get("FileName") or "")
        url = (
            item.get("url")
            or item.get("downloadUrl")
            or item.get("download_url")
            or item.get("fileUrl")
        )
        aid = item.get("id")
        candidates = []
        if url:
            candidates.append(str(url))
        if aid not in (None, ""):
            candidates.append(client._record_url("ap_invoices", int(kimco_id), f"attachments/{aid}"))
            candidates.append(client._record_url("ap_invoices", int(kimco_id), f"attachments/{aid}/content"))
        for href in candidates:
            resp = client.request("GET", href)
            if resp.status_code != 200:
                LOGGER.info("GET attachment %s HTTP %s", href, resp.status_code)
                continue
            data = resp.content or b""
            if data[:5] != b"%PDF-":
                continue
            dest = PDF_DIR / f"kimco_{kimco_id}_Invoice_{invoice_number}.PDF"
            dest.write_bytes(data)
            return dest
        LOGGER.info("attachment skip id=%s name=%s keys=%s", aid, name, sorted(item.keys())[:20])
    return None


def parsed_from_pdf(path: Path, invoice_number: str, po: str) -> dict[str, Any]:
    parsed = parse_invoice_pdf(
        path,
        subject=f"Invoice {invoice_number} for PO {po}",
        from_name="McMaster-Carr",
    )
    parsed["pdf_path"] = str(path)
    parsed["invoice_number"] = exact_invoice_number(parsed.get("invoice_number")) or invoice_number
    parsed["po"] = exact_invoice_number(parsed.get("po")) or po
    parsed["vendor"] = VENDOR_NAME
    return parsed


def enter_row_from_sheet(prior: list[dict[str, Any]], invoice_number: str, kimco_id: int) -> dict[str, Any]:
    for row in prior:
        if exact_invoice_number(row.get("Invoice #")) == invoice_number:
            out = dict(row)
            out["KIMCO id"] = kimco_id
            return out
    return {
        "Vendor": VENDOR_NAME,
        "Invoice #": invoice_number,
        "PO": RETRY_POS[invoice_number],
        "Amount": RETRY_AMOUNTS[invoice_number],
        "Result": "HOLD",
        "KIMCO id": kimco_id,
        "Batch": f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
    }


def assert_existing_header(client: KimcoClient, kimco_id: int, invoice_number: str) -> dict[str, Any]:
    item = client.get_item("ap_invoices", int(kimco_id))
    vals = item.get("values") or {}
    posted = exact_invoice_number(vals.get("Invoice_Number"))
    if posted != invoice_number:
        raise KimcoError(
            f"Refuse recreate: {kimco_id} is {posted!r}, expected {invoice_number}"
        )
    vendor_id = lookup_id(vals.get("Vendor"))
    if vendor_id != VENDOR_ID:
        raise KimcoError(f"Refuse: {kimco_id} vendor {vendor_id} is not live McMaster 117")
    batch_id = lookup_id(vals.get("AP_Invoice_Batch"))
    if batch_id not in {KNOWN_BATCH_ID, 375}:
        raise KimcoError(f"Refuse: {kimco_id} batch {batch_id} is not 721/375")
    return item


def finish_retry_row(
    client: KimcoClient,
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    kimco_id: int,
    receipts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if int(kimco_id) in DO_NOT_MUTATE_IDS | LEAVE_ALONE_HOLD_IDS:
        return apply_exception_category_owner(dict(enter_row)), {"status": "leave-alone"}
    finish = finish_hold_header(client, parsed=parsed, kimco_id=int(kimco_id), receipts=receipts)
    proof = finish.get("after") or mcmaster_proof(client, kimco_id)
    row = quality_mcmaster_row(
        graph,
        parsed=parsed,
        enter_row=enter_row,
        proof=proof,
        finish=finish,
        vendor_id=VENDOR_ID,
    )
    extra: dict[str, Any] = {k: v for k, v in finish.items() if k != "after"}
    if is_over_ppv_price_hold(row, finish):
        comment = over_ppv_hold_comment(
            invoice_number=exact_invoice_number(parsed.get("invoice_number")),
            po=str(parsed.get("po") or enter_row.get("PO") or ""),
            pdf_amount=parsed.get("amount") or enter_row.get("Amount"),
        )
        transfer = apply_over_ppv_transfer_ap(client, kimco_id=int(kimco_id), comment=comment)
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
    }
    return row, extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finish McMaster HOLDs after receipts are entered")
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
        "Target: live. McMaster receipt retry on existing headers. "
        "Batch 721. No new invoices. No Mail.Send. invent=false.",
        flush=True,
    )
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    client = KimcoClient.authenticate(creds.instance_url, creds.key, creds.password, target="live")
    graph = _optional_graph_client()

    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    receipts = load_list_receipts(client)
    preflight: list[dict[str, Any]] = []
    parsed_bills: list[dict[str, Any]] = []
    enter_rows: list[dict[str, Any]] = []

    for inv, kid in RETRY_HEADERS.items():
        header = assert_existing_header(client, kid, inv)
        vals = header.get("values") or {}
        pdf = find_local_pdf(inv) or download_kimco_pdf(client, kid, inv)
        if pdf is None:
            preflight.append({"invoice": inv, "kimco": kid, "pdf": None, "blocker": "no-pdf"})
            continue
        parsed = parsed_from_pdf(pdf, inv, RETRY_POS[inv])
        parsed_bills.append(parsed)
        enter_rows.append(enter_row_from_sheet(prior_rows, inv, kid))
        preflight.append(
            {
                "invoice": inv,
                "kimco": kid,
                "pdf": str(pdf),
                "pdf_amount": parsed.get("amount"),
                "pdf_po": parsed.get("po"),
                "lines": parsed.get("lines"),
                "fees": parsed.get("fees"),
                "header_amount": vals.get("Invoice_Amount"),
                "header_verification": vals.get("Invoice_Verification_Amount"),
                "header_batch": lookup_text(vals.get("AP_Invoice_Batch")),
                "header_po": lookup_text(vals.get("Purchase_Order")),
            }
        )

    print(json.dumps({"preflight": preflight}, indent=2, default=str), flush=True)
    if args.preflight_only:
        return 0
    if len(parsed_bills) != len(RETRY_HEADERS):
        print("Missing PDF for a retry invoice. Stop — no writes.", flush=True)
        return 2

    new_rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    for parsed, enter_row in zip(parsed_bills, enter_rows):
        inv = exact_invoice_number(parsed.get("invoice_number"))
        kid = int(RETRY_HEADERS[inv])
        row, extra = finish_retry_row(
            client,
            graph,
            parsed=parsed,
            enter_row=enter_row,
            kimco_id=kid,
            receipts=receipts,
        )
        new_rows.append(row)
        finishes[inv] = extra
        receipts = load_list_receipts(client)
        print(
            json.dumps(
                {
                    "invoice": inv,
                    "kimco": kid,
                    "result": row.get("Result"),
                    "receipts": row.get("Receipts"),
                    "fees": row.get("Fees and surcharges"),
                    "ppv": row.get("PPV"),
                    "why": row.get("Why"),
                    "select_status": extra.get("select_status"),
                    "fee_status": extra.get("fee_status"),
                    "ppv_status": extra.get("ppv_status"),
                },
                indent=2,
                default=str,
            ),
            flush=True,
        )

    merged = merge_sheet_rows(prior_rows, new_rows)
    write_report(report_path, merged)
    sidecar = {
        "proof": "mcmaster-receipt-retry",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": VENDOR_ID,
        "batch_name": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "retry_headers": RETRY_HEADERS,
        "preflight": preflight,
        "finishes": finishes,
        "new_rows": new_rows,
        "rows": merged,
    }
    if report_path.with_suffix(".json").is_file():
        prior = json.loads(report_path.with_suffix(".json").read_text())
        sidecar["prior_proof"] = prior.get("proof")
        sidecar["leftover_pending"] = prior.get("leftover_pending")
        sidecar["catalog"] = prior.get("catalog")
    report_path.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Sheet: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
