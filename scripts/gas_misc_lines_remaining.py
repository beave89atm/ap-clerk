"""Add Lines-K on remaining Gas Misc Successes (batch 720).

Kyle 2026-09-18: 10135 / 0040421569 looks good. Do the other Successes
the same way. Leave HOLDs and 10135 alone. invent=false.
No Mail.Send. No Validate Invoice.
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
from ap_clerk.misc_lines import (  # noqa: E402
    collect_shop_supplies_gs_from_records,
    existing_misc_lines_match_pdf,
    misc_add_item_payload,
    misc_line_snapshot,
    payload_has_receipt,
    type4_shop_supplies_lines_ok,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from gas_misc_lines_10135 import (  # noqa: E402
    LOOKUP_TYPE4_IDS,
    TARGET_ID as DONE_10135_ID,
    TARGET_INVOICE as DONE_10135_NUMBER,
    VENDOR_ID_HINT,
    _fee_snapshot,
    _header,
    merchandise_lines,
)

REMAINING: tuple[tuple[int, str], ...] = (
    (10128, "0040435122"),
    (10129, "0040434973"),
    (10130, "0040431060"),
    (10131, "0040425657"),
    (10132, "0040425612"),
    (10133, "0040424382"),
    (10136, "0040438494"),
)
HOLD_NUMBERS = frozenset(
    {
        "0040430010",
        "0040424839",
        "0040423658",
        "0040438057",
        "0040438056",
        "0040438055",
        "0040438053",
    }
)
LEAVE_ALONE_IDS = frozenset({DONE_10135_ID, 10134})
LOOKUP_IDS = (*LOOKUP_TYPE4_IDS, DONE_10135_ID)


def download_target_bill(graph, pdf_dir: Path, invoice_number: str) -> dict[str, Any]:
    hits = graph.search_messages(ALLOWED_MAILBOX, invoice_number, top=10)
    if not hits:
        raise KimcoError(f"Graph had no message for {invoice_number}")
    message = next((h for h in hits if h.get("hasAttachments")), hits[0])
    pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, str(message.get("id") or ""))
    if not pdfs:
        raise KimcoError(f"Graph PDF download empty for {invoice_number}")
    for name, content in pdfs:
        dest = pdf_dir / f"{invoice_number}_{name}"
        dest.write_bytes(content)
        parsed = parse_invoice_pdf(
            dest,
            subject=str(message.get("subject") or ""),
            from_name="Gas and Supply",
            from_address="billing@gasandsupply.com",
        )
        extras = list(parsed.pop("siblings", []) or [])
        for bill in [parsed, *extras]:
            if str(bill.get("invoice_number") or "") == invoice_number:
                bill["pdf_path"] = str(dest)
                bill["graph_message_id"] = message.get("id")
                return bill
    raise KimcoError(f"PDF pack had no section for {invoice_number} only")


def process_one(
    client: KimcoClient,
    graph,
    *,
    invoice_id: int,
    invoice_number: str,
    found: dict[str, Any],
    pdf_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    if invoice_id in LEAVE_ALONE_IDS or invoice_number in HOLD_NUMBERS:
        return {
            "invoice_id": invoice_id,
            "invoice_number": invoice_number,
            "status": "leave-alone",
        }
    before = client.get_item("ap_invoices", invoice_id)
    header = _header(before)
    existing = misc_line_snapshot(before)
    report: dict[str, Any] = {
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "before_header": header,
        "before_lines": existing,
        "before_fees": _fee_snapshot(before),
    }
    if header.get("invoice_number") != invoice_number:
        report["status"] = "blocker"
        report["why"] = f"Header invoice # {header.get('invoice_number')} is not {invoice_number}"
        return report
    if int(header.get("invoice_type") or 0) != 4:
        report["status"] = "blocker"
        report["why"] = "Header is not Misc Type 4"
        return report
    vendor = header.get("vendor") or {}
    vendor_id = vendor.get("id") if isinstance(vendor, dict) else None
    if int(vendor_id or 0) != VENDOR_ID_HINT:
        report["status"] = "blocker"
        report["why"] = f"Vendor id {vendor_id} is not live 71 GAS AND SUPPLY"
        return report

    bill = download_target_bill(graph, pdf_dir, invoice_number)
    lines = merchandise_lines(bill)
    report["pdf_invoice"] = bill.get("invoice_number")
    report["pdf_amount"] = bill.get("amount")
    report["pdf_lines"] = lines
    report["pdf_fees"] = bill.get("fees") or []
    report["siblings_not_used"] = True
    if not lines:
        report["status"] = "blocker"
        report["why"] = "No merchandise PDF lines for this invoice"
        return report

    if existing_misc_lines_match_pdf(existing, lines):
        report["status"] = "already-ok"
        report["after_header"] = header
        report["after_lines"] = existing
        report["put_http"] = None
        return report
    if existing:
        report["status"] = "blocker"
        report["why"] = "Lines-K already has items that do not match this PDF. Will not add duplicates."
        return report

    payload = misc_add_item_payload(
        lines,
        invoice_id=invoice_id,
        vendor_id=int(vendor_id),
        misc_item=found["misc_item"],
        gl_account=found.get("gl_account"),
    )
    if payload_has_receipt(payload):
        raise KimcoError("Refusing payload that includes Receipt")
    report["put_payload"] = payload
    report["dry_run"] = dry_run
    if dry_run:
        report["status"] = "dry-run"
        return report

    put = client.request("PUT", client._record_url("ap_invoices", invoice_id), json=payload)
    report["put_http"] = put.status_code
    report["put_text"] = (put.text or "")[:400]
    if put.status_code >= 400:
        report["status"] = "blocker"
        report["why"] = f"PUT HTTP {put.status_code}"
        return report

    after = client.get_item("ap_invoices", invoice_id)
    report["after_header"] = _header(after)
    report["after_lines"] = misc_line_snapshot(after)
    report["after_fees"] = _fee_snapshot(after)
    report["lines_ok"] = type4_shop_supplies_lines_ok(report["after_lines"])
    report["status"] = "added" if report["lines_ok"] else "blocker"
    if not report["lines_ok"]:
        report["why"] = "PUT 200 but Lines-K still missing Shop Supplies - G&S"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add Lines-K on remaining Gas Successes")
    parser.add_argument("--dry-run", action="store_true", help="Build PUT bodies; no live PUT")
    parser.add_argument("--get-only", action="store_true", help="GET headers only; no Graph/PUT")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print("Target: live. No Mail.Send. No Validate. Leave 10135 and HOLDs alone.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    lookup_records = [client.get_item("ap_invoices", iid) for iid in LOOKUP_IDS]
    found = collect_shop_supplies_gs_from_records(lookup_records)
    print(json.dumps({"category_lookup": found, "from_ids": list(LOOKUP_IDS)}, default=str), flush=True)
    if int((found.get("misc_item") or {}).get("id") or 0) != 31:
        print("Live Shop Supplies - G&S id is not 31. Stop (invent=false).", flush=True)
        return 2

    headers = []
    for invoice_id, invoice_number in REMAINING:
        item = client.get_item("ap_invoices", invoice_id)
        headers.append(
            {
                "header": _header(item),
                "lines": misc_line_snapshot(item),
                "fees": _fee_snapshot(item),
                "lines_ok": type4_shop_supplies_lines_ok(misc_line_snapshot(item)),
            }
        )
    print(json.dumps({"before_remaining": headers}, default=str), flush=True)
    if args.get_only:
        proof = ROOT / "runs" / "kimco-gas-misc-lines-remaining.json"
        proof.write_text(json.dumps({"lookup": found, "before_remaining": headers}, default=str, indent=2))
        print(f"proof={proof}", flush=True)
        return 0

    graph = _optional_graph_client()
    if graph is None:
        print("Graph authenticate failed", flush=True)
        return 2

    pdf_dir = Path("/tmp/gas-misc-remaining")
    pdf_dir.mkdir(exist_ok=True)
    results = []
    for invoice_id, invoice_number in REMAINING:
        row = process_one(
            client,
            graph,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            found=found,
            pdf_dir=pdf_dir,
            dry_run=args.dry_run,
        )
        print(json.dumps(row, default=str), flush=True)
        results.append(row)

    proof = ROOT / "runs" / "kimco-gas-misc-lines-remaining.json"
    proof.write_text(
        json.dumps(
            {
                "lookup": found,
                "leave_alone": {"10135": DONE_10135_NUMBER, "holds": sorted(HOLD_NUMBERS)},
                "mail_send": False,
                "validate_invoice": False,
                "invent": False,
                "results": results,
            },
            default=str,
            indent=2,
        )
    )
    print(f"proof={proof}", flush=True)
    blockers = [r for r in results if r.get("status") == "blocker"]
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
