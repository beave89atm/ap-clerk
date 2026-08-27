"""Daily-runnable AP Clerk CLI for KIMCO prototype only."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.report import write_report
from ap_clerk.rules import (
    COMMENTS,
    CURRENCY_USD_ID,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_BATCH_NAMES,
    FORBIDDEN_INVOICE_IDS,
    INVOICE_TYPE_PO,
    batch_name_for,
    chicago_today,
    due_date_from_terms,
    extract_po_number,
    format_fees,
    invoice_number_key,
    kimco_datetime,
    lookup_id,
    lookup_text,
    names_match,
    parse_iso_date,
)

LOGGER = logging.getLogger("ap_clerk")
ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kannon AP Clerk (KIMCO prototype only)")
    parser.add_argument("command", choices=["enter"], help="enter fixture invoices as header-only AP bills")
    parser.add_argument("--fixture", default=str(ROOT / "fixtures" / "testrun-727-803.json"))
    parser.add_argument("--report", default=None, help="Output xlsx path. Default: runs/AP-run-YYYY-MM-DD.xlsx")
    parser.add_argument("--as-of", default=None, help="Override Chicago calendar date YYYY-MM-DD")
    parser.add_argument("--pdf-dir", default=None, help="Optional directory of invoice PDFs to attach")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials()
    print(format_presence(creds.presence), flush=True)
    if creds.key_source:
        print(f"Using credential pair: {creds.key_source}", flush=True)
    print(f"Instance host: {creds.instance_url}", flush=True)

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    batch_name = batch_name_for(as_of)
    report_path = Path(args.report) if args.report else ROOT / "runs" / f"AP-run-{as_of.isoformat()}.xlsx"
    fixture_path = Path(args.fixture)
    invoices = _load_fixture(fixture_path)

    if not creds.ready:
        print(creds.error or "Credentials not ready. Writing HOLD report and stopping.", flush=True)
        rows = [_offline_row(inv, batch_name, creds.error or "credentials missing") for inv in invoices]
        write_report(report_path, rows)
        print(f"Wrote {report_path}", flush=True)
        return 2

    try:
        client = KimcoClient.authenticate(creds.instance_url, creds.key or "", creds.password or "")
        rows = run_enter(client, invoices, batch_name=batch_name, pdf_dir=Path(args.pdf_dir) if args.pdf_dir else None)
    except KimcoError as exc:
        print(f"Prototype call failed: {exc}", flush=True)
        rows = [_offline_row(inv, batch_name, f"Fail: {exc}") for inv in invoices]
        write_report(report_path, rows)
        return 1

    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)
    return 0


def run_enter(
    client: KimcoClient,
    invoices: list[dict[str, Any]],
    *,
    batch_name: str,
    pdf_dir: Path | None = None,
) -> list[dict[str, Any]]:
    LOGGER.info("Loading prototype lists (invoices, batches, purchase lines)")
    existing_invoices = client.list_items("ap_invoices")
    batches = client.list_items("ap_batches")
    purchase_lines = client.list_items("purchase_lines")

    invoice_by_number = _index_invoices(existing_invoices)
    vendor_samples = _vendor_samples(existing_invoices)
    po_index = _index_purchase_orders(purchase_lines)
    batch = _find_or_create_batch(client, batches, batch_name)
    batch_label = f"{batch_name} ({batch['id']})"
    LOGGER.info("Using batch %s", batch_label)

    rows = []
    for inv in invoices:
        rows.append(
            _process_invoice(
                client,
                inv,
                batch=batch,
                batch_label=batch_label,
                invoice_by_number=invoice_by_number,
                vendor_samples=vendor_samples,
                po_index=po_index,
                pdf_dir=pdf_dir,
            )
        )
    return rows


def _load_fixture(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    invoices = payload.get("invoices") or []
    if not invoices:
        raise SystemExit(f"Fixture {path} has no invoices")
    return invoices


def _offline_row(inv: dict[str, Any], batch_name: str, why: str) -> dict[str, Any]:
    result = "HOLD"
    if why.lower().startswith("fail"):
        result = "Fail"
    return {
        "Vendor": inv.get("vendor"),
        "Invoice #": inv.get("invoice_number"),
        "date": inv.get("date"),
        "PO": inv.get("po") or "",
        "Amount": inv.get("amount"),
        "Result": result,
        "Why": why,
        "KIMCO id": "",
        "Batch": batch_name,
        "Fees and surcharges": format_fees(inv.get("fees")),
        "PPV": "none",
        "Attach status": "no-pdf-on-vm",
    }


def _index_invoices(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        values = item.get("values") or {}
        key = invoice_number_key(values.get("Invoice_Number"))
        if key:
            index[key] = item
    return index


def _vendor_samples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = []
    seen: set[int] = set()
    for item in items:
        values = item.get("values") or {}
        vendor = values.get("Purchase_Order_$_Vendor")
        vendor_id = lookup_id(vendor)
        if vendor_id is None or vendor_id in seen:
            continue
        seen.add(vendor_id)
        samples.append(
            {
                "vendor_id": vendor_id,
                "vendor_text": lookup_text(vendor),
                "invoice_id": item.get("id"),
                "po_text": lookup_text(values.get("Purchase_Order_$_Display_Name")),
            }
        )
    return samples


def _index_purchase_orders(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in lines:
        values = item.get("values") or {}
        po = values.get("Purchase_Order_Number")
        po_id = lookup_id(po)
        text = lookup_text(po) or lookup_text(values.get("Display_Name"))
        number = extract_po_number(text)
        if not po_id or not number:
            continue
        current = index.get(number)
        if current is None:
            index[number] = {"id": po_id, "text": text, "line_ids": [item.get("id")]}
        elif current["id"] == po_id:
            current["line_ids"].append(item.get("id"))
    return index


def _find_or_create_batch(client: KimcoClient, batches: list[dict[str, Any]], batch_name: str) -> dict[str, Any]:
    if batch_name in FORBIDDEN_BATCH_NAMES:
        raise KimcoError(f"Refusing forbidden batch name {batch_name}")
    for item in batches:
        values = item.get("values") or {}
        name = values.get("AP_Invoice_Batch_ID")
        batch_id = item.get("id")
        if batch_id in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_BATCH_NAMES:
            continue
        if name == batch_name:
            if batch_id in FORBIDDEN_BATCH_IDS:
                raise KimcoError("Matched a forbidden batch id")
            LOGGER.info("Found existing batch id=%s name=%s", batch_id, name)
            return {"id": batch_id, "name": name, "created": False}
    LOGGER.info("Creating batch %s", batch_name)
    values = {
        "AP_Invoice_Batch_ID": batch_name,
        "Description": COMMENTS,
        "Batch_Owner": {"id": 173},
        "Status": 0,
    }
    created_id, body, status, error = client.create("ap_batches", values)
    if created_id is None:
        raise KimcoError(f"Batch create failed HTTP {status}: {error}")
    if created_id in FORBIDDEN_BATCH_IDS:
        raise KimcoError("Create returned forbidden batch id; aborting")
    LOGGER.info("Created batch id=%s", created_id)
    return {"id": created_id, "name": batch_name, "created": True, "raw": body}


def _process_invoice(
    client: KimcoClient,
    inv: dict[str, Any],
    *,
    batch: dict[str, Any],
    batch_label: str,
    invoice_by_number: dict[str, dict[str, Any]],
    vendor_samples: list[dict[str, Any]],
    po_index: dict[str, dict[str, Any]],
    pdf_dir: Path | None,
) -> dict[str, Any]:
    vendor = inv.get("vendor") or ""
    number = str(inv.get("invoice_number") or "")
    po = inv.get("po")
    po_display = "" if po is None else str(po)
    amount = inv.get("amount")
    fees = format_fees(inv.get("fees"))
    attach = "no-pdf-on-vm"
    row = {
        "Vendor": vendor,
        "Invoice #": number,
        "date": inv.get("date"),
        "PO": po_display,
        "Amount": amount,
        "Result": "HOLD",
        "Why": "",
        "KIMCO id": "",
        "Batch": batch_label,
        "Fees and surcharges": fees,
        "PPV": "none",
        "Attach status": attach,
    }

    if inv.get("check_stop") or (inv.get("hold_reason") == "CHECK STOP"):
        row["Why"] = "HOLD: CHECK STOP. Do not create a header."
        return row
    if inv.get("action") == "hold" or not po:
        reason = inv.get("hold_reason") or "no-PO"
        row["Why"] = f"HOLD: {reason}. No-PO invoices do not get a header."
        return row

    existing = invoice_by_number.get(invoice_number_key(number))
    if existing:
        existing_id = existing.get("id")
        row["KIMCO id"] = existing_id
        row["Result"] = "Fail"
        if existing_id in FORBIDDEN_INVOICE_IDS:
            row["Why"] = f"Fail/already exists (do not recreate id {existing_id})"
        else:
            row["Why"] = "Fail/already exists"
        return row

    po_info = po_index.get(str(po))
    if not po_info:
        row["Why"] = f"HOLD: PO {po} is not in prototype; do not invent a PO. Lines would also be blocked/405 until Select Receipts."
        return row

    vendor_info = _resolve_vendor(vendor, po_info["text"], vendor_samples)
    if not vendor_info:
        row["Why"] = f"HOLD: vendor/remit/terms not found on prototype for {vendor} (PO {po} exists as {po_info['text']})."
        return row

    try:
        sample = client.get_item("ap_invoices", int(vendor_info["invoice_id"]))
    except KimcoError as exc:
        row["Why"] = f"HOLD: could not load vendor sample invoice: {exc}"
        return row

    sample_values = sample.get("values") or {}
    remit = sample_values.get("Remit_To_Address")
    terms = sample_values.get("Terms_Code")
    if lookup_id(remit) is None or lookup_id(terms) is None:
        row["Why"] = "HOLD: sample invoice missing remit or terms; will not invent them."
        return row

    invoice_day = parse_iso_date(str(inv["date"]))
    due = due_date_from_terms(invoice_day, lookup_text(terms))
    payload = {
        "AP_Invoice_Batch": {"id": batch["id"]},
        "Purchase_Order": {"id": po_info["id"]},
        "Vendor": {"id": vendor_info["vendor_id"]},
        "Invoice_Number": number,
        "Invoice_Type": INVOICE_TYPE_PO,
        "Invoice_Date": kimco_datetime(invoice_day),
        "Invoice_Verification_Amount": float(amount),
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": lookup_id(terms)},
        "Currency": {"id": CURRENCY_USD_ID},
        "Remit_To_Address": {"id": lookup_id(remit)},
        "Transaction_Date": kimco_datetime(invoice_day),
        "Comments": COMMENTS,
    }
    created_id, _body, status, error = client.create("ap_invoices", payload)
    if created_id is None:
        row["Result"] = "Fail"
        row["Why"] = f"Fail: header create HTTP {status}: {error}"
        return row
    if created_id in FORBIDDEN_INVOICE_IDS:
        row["Result"] = "Fail"
        row["KIMCO id"] = created_id
        row["Why"] = "Fail: create returned a forbidden existing invoice id; will not edit it."
        return row

    invoice_by_number[invoice_number_key(number)] = {"id": created_id, "values": {"Invoice_Number": number}}
    pdf_status = _maybe_attach(client, created_id, number, pdf_dir)
    row["Result"] = "Success"
    row["KIMCO id"] = created_id
    row["Attach status"] = pdf_status
    row["Why"] = (
        "Header created. Lines blocked/405: API cannot Select Receipts until Editable is on; "
        "do not type Add Item. Fees recorded in Fees and surcharges (not PPV). "
        f"Attach status={pdf_status}."
    )
    LOGGER.info("Created invoice %s id=%s vendor=%s po=%s", number, created_id, vendor, po)
    return row


def _resolve_vendor(fixture_vendor: str, po_text: str, samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    po_vendor = po_text.split("-", 1)[1] if "-" in po_text else po_text
    for sample in samples:
        if names_match(fixture_vendor, sample["vendor_text"]) or names_match(po_vendor, sample["vendor_text"]):
            return sample
        if names_match(fixture_vendor, sample["po_text"]) or names_match(po_vendor, sample["po_text"]):
            return sample
    return None


def _maybe_attach(client: KimcoClient, invoice_id: int, invoice_number: str, pdf_dir: Path | None) -> str:
    if pdf_dir is None or not pdf_dir.exists():
        return "no-pdf-on-vm"
    matches = list(pdf_dir.glob(f"*{invoice_number}*.pdf")) + list(pdf_dir.glob("*.pdf"))
    # Only attach a PDF that clearly belongs to this invoice number.
    named = [p for p in matches if invoice_number.lower() in p.name.lower()]
    if not named:
        return "no-pdf-on-vm"
    pdf = named[0]
    try:
        content = pdf.read_bytes()
        return client.try_official_attach(
            invoice_id,
            name=pdf.name,
            content_type="application/pdf",
            size=len(content),
            content=content,
        )
    except Exception as exc:  # noqa: BLE001 - attach must not fail the run
        LOGGER.info("Attach attempt failed without raising run: %s", type(exc).__name__)
        return "blocked-405"


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print("\nRun summary (no secrets):", flush=True)
    for row in rows:
        print(
            f"  {row['Vendor']} | {row['Invoice #']} | {row['Result']} | "
            f"id={row['KIMCO id'] or '-'} | {row['Why']}",
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
