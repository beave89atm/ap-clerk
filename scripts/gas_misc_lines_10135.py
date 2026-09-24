"""Add Lines-K on ONE Gas Misc Success: 0040421569 / 10135.

Kyle 2026-09-18: prove Type 4 Add Item (description, qty, cost, shop
supplies - g&s) then stop. Do not touch other Success invoices.
No Mail.Send. No Validate Invoice. invent=false.
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
    misc_add_item_payload,
    payload_has_receipt,
)
from ap_clerk.pdf_invoice import parse_invoice_pdf  # noqa: E402
from ap_clerk.rules import is_fee_or_surcharge  # noqa: E402

TARGET_INVOICE = "0040421569"
TARGET_ID = 10135
LOOKUP_TYPE4_IDS = (9966, 9970)
VENDOR_ID_HINT = 71


def _line_snapshot(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (item.get("lists") or {}).get("APInvoiceLine") or []:
        values = line.get("values") or {}
        rows.append(
            {
                "id": line.get("id"),
                "desc": values.get("Misc_Description"),
                "qty": values.get("Quantity"),
                "unit": values.get("Unit_Price"),
                "ext": values.get("Extended_Amount"),
                "misc": values.get("MFG_Miscellaneous_Item"),
                "gl": values.get("Purchase_GL_Account"),
                "receipt": values.get("Receipt"),
            }
        )
    return rows


def _fee_snapshot(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for charge in (item.get("lists") or {}).get("InvoiceAdditionalCharges") or []:
        values = charge.get("values") or charge
        rows.append(
            {
                "lookup": values.get("Additional_Charges"),
                "amount": values.get("Amount"),
                "name": values.get("Name"),
            }
        )
    return rows


def _header(item: dict[str, Any]) -> dict[str, Any]:
    values = item.get("values") or {}
    return {
        "id": item.get("id"),
        "invoice_number": values.get("Invoice_Number"),
        "vendor": values.get("Vendor"),
        "invoice_type": values.get("Invoice_Type"),
        "invoice_amount": values.get("Invoice_Amount"),
        "verification": values.get("Invoice_Verification_Amount"),
        "batch": values.get("AP_Invoice_Batch"),
        "po": values.get("Purchase_Order"),
        "lines_count": values.get("Lines_Count"),
        "posted": values.get("Posted"),
    }


def merchandise_lines(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for line in parsed.get("lines") or []:
        blob = f"{line.get('part') or ''} {line.get('description') or ''}"
        if is_fee_or_surcharge(blob) or str(line.get("part") or "").startswith("$SUR"):
            continue
        out.append(line)
    return out


def download_target_bill(graph, pdf_dir: Path) -> dict[str, Any]:
    hits = graph.search_messages(ALLOWED_MAILBOX, TARGET_INVOICE, top=10)
    if not hits:
        raise SystemExit(f"Graph had no message for {TARGET_INVOICE}")
    message = next((h for h in hits if h.get("hasAttachments")), hits[0])
    pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, str(message.get("id") or ""))
    if not pdfs:
        raise SystemExit("Graph PDF download empty")
    for name, content in pdfs:
        dest = pdf_dir / name
        dest.write_bytes(content)
        parsed = parse_invoice_pdf(
            dest,
            subject=str(message.get("subject") or ""),
            from_name="Gas and Supply",
            from_address="billing@gasandsupply.com",
        )
        extras = list(parsed.pop("siblings", []) or [])
        for bill in [parsed, *extras]:
            if str(bill.get("invoice_number") or "") == TARGET_INVOICE:
                bill["pdf_path"] = str(dest)
                return bill
    raise SystemExit(f"PDF pack had no section for {TARGET_INVOICE} only")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add Lines-K on 0040421569 / 10135 only")
    parser.add_argument("--dry-run", action="store_true", help="Build PUT body; no live PUT")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print("Target: live. No Mail.Send. No Validate Invoice. One invoice only.", flush=True)
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
    before = client.get_item("ap_invoices", TARGET_ID)
    header = _header(before)
    print(json.dumps({"before_header": header, "before_lines": _line_snapshot(before), "before_fees": _fee_snapshot(before)}, default=str), flush=True)
    if header.get("invoice_number") != TARGET_INVOICE:
        print("Header invoice # is not 0040421569. Stop.", flush=True)
        return 2
    if int(header.get("invoice_type") or 0) != 4:
        print("Header is not Misc Type 4. Stop.", flush=True)
        return 2
    vendor = header.get("vendor") or {}
    vendor_id = vendor.get("id") if isinstance(vendor, dict) else None
    if int(vendor_id or 0) != VENDOR_ID_HINT:
        print(f"Vendor id {vendor_id} is not live 71 GAS AND SUPPLY. Stop.", flush=True)
        return 2
    if _line_snapshot(before):
        print("Lines-K already has items. Will not add duplicates. Stop.", flush=True)
        return 2

    lookup_records = [client.get_item("ap_invoices", iid) for iid in LOOKUP_TYPE4_IDS]
    found = collect_shop_supplies_gs_from_records(lookup_records)
    print(json.dumps({"category_lookup": found, "from_ids": list(LOOKUP_TYPE4_IDS)}, default=str), flush=True)

    pdf_dir = Path("/tmp/gas-misc-10135")
    pdf_dir.mkdir(exist_ok=True)
    bill = download_target_bill(graph, pdf_dir)
    lines = merchandise_lines(bill)
    fees = bill.get("fees") or []
    print(
        json.dumps(
            {
                "pdf_invoice": bill.get("invoice_number"),
                "pdf_amount": bill.get("amount"),
                "pdf_lines": lines,
                "pdf_fees": fees,
                "siblings_not_used": True,
            },
            default=str,
        ),
        flush=True,
    )
    if not lines:
        print("No merchandise PDF lines for this invoice. Stop.", flush=True)
        return 2

    payload = misc_add_item_payload(
        lines,
        invoice_id=TARGET_ID,
        vendor_id=int(vendor_id),
        misc_item=found["misc_item"],
        gl_account=found.get("gl_account"),
    )
    if payload_has_receipt(payload):
        raise KimcoError("Refusing payload that includes Receipt")
    print(json.dumps({"put_payload": payload, "dry_run": args.dry_run}, default=str), flush=True)
    if args.dry_run:
        return 0

    put = client.request("PUT", client._record_url("ap_invoices", TARGET_ID), json=payload)
    print(json.dumps({"put_http": put.status_code, "put_text": (put.text or "")[:400]}, default=str), flush=True)
    if put.status_code >= 400:
        return 2

    after = client.get_item("ap_invoices", TARGET_ID)
    report = {
        "after_header": _header(after),
        "after_lines": _line_snapshot(after),
        "after_fees": _fee_snapshot(after),
        "touched_other_invoices": False,
        "mail_send": False,
        "validate_invoice": False,
    }
    print(json.dumps(report, default=str), flush=True)
    proof = ROOT / "runs" / "kimco-gas-misc-lines-10135.json"
    proof.write_text(json.dumps({"before": header, "lookup": found, "pdf_lines": lines, "payload": payload, **report}, default=str, indent=2))
    print(f"proof={proof}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
