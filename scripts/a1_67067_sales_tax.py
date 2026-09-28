"""Add Texas sales tax on A1 Image bill 10367. Do not post the bill.

Kyle 2026-09-28. Invoice 67067, PO 59295, receipt 24712 is 1 @ 508.67.
The PDF total is 550.64. The 41.97 gap is state sales tax at 8.25%.
The charge is an Additional Charges line named Sales tax, not PPV.
The receipt selection and Transfer AP batch 375 stay as they are.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import (
    FEE_CHARGE_LOOKUP_ID,
    PPV_CHARGE_LOOKUP_ID,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    sales_tax_charge_payload,
)
from ap_clerk.rules import (
    SALES_TAX_CHARGE_DESCRIPTION,
    lookup_id,
    lookup_text,
    money,
    state_sales_tax_comment,
    state_sales_tax_gap_decision,
)

BILL_ID = 10367
INVOICE = "67067"
RECEIPT_ID = 24712
BATCH_ID = 375
PDF_TOTAL = 550.64
RECEIPT_AMOUNT = 508.67
SALES_TAX = 41.97
HOST = "https://live.kimcoerp.com"
OUT_JSON = ROOT / "artifacts" / "a1-67067-sales-tax-2026-09-28.json"


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("refusing non-live host")
    return KimcoClient.authenticate(creds.instance_url, creds.key or "", creds.password or "", target="live")


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        lines.append(
            {
                "id": line.get("id"),
                "receipt": lookup_id(lv.get("Receipt")),
                "qty": money(lv.get("Quantity")),
                "price": money(lv.get("Unit_Price")),
                "ext": money(lv.get("Extended_Amount")),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        cv = charge.get("values") or {}
        kind = cv.get("Additional_Charges")
        charges.append(
            {
                "id": charge.get("id"),
                "lookup_id": lookup_id(kind),
                "lookup_text": lookup_text(kind),
                "name": cv.get("Name"),
                "amount": money(cv.get("Amount")),
                "qty": cv.get("Quantity"),
                "price": money(cv.get("Price")),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        comments.append(
            {
                "id": comment.get("id"),
                "html": (comment.get("values") or {}).get("HtmlValue") or "",
            }
        )
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "tax_amount": money(values.get("Tax_Amount")),
        "additional": money(values.get("Total_Additional_Charges")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "po": lookup_text(values.get("Purchase_Order")),
        "invoice_type": values.get("Invoice_Type"),
        "posted": values.get("Posted"),
        "status": values.get("Status"),
        "lines": lines,
        "charges": charges,
        "comments": comments,
    }


def guard(before: dict[str, Any]) -> list[str]:
    problems = []
    if before["invoice"] != INVOICE:
        problems.append(f"invoice {before['invoice']} != {INVOICE}")
    if before["batch_id"] != BATCH_ID:
        problems.append(f"batch {before['batch_id']} != {BATCH_ID}")
    if before["invoice_type"] != 3:
        problems.append(f"invoice type {before['invoice_type']} != 3")
    if before["posted"] not in (None, "", False):
        problems.append("bill is posted")
    if before["verification"] != PDF_TOTAL:
        problems.append(f"verification {before['verification']} != {PDF_TOTAL}")
    if len(before["lines"]) != 1:
        problems.append(f"line count {len(before['lines'])} != 1")
    else:
        line = before["lines"][0]
        if line["receipt"] != RECEIPT_ID or line["qty"] != 1 or line["ext"] != RECEIPT_AMOUNT:
            problems.append(f"receipt line changed: {line}")
    return problems


def sales_tax_charge(charges: list[dict[str, Any]]) -> dict[str, Any] | None:
    hits = [
        row
        for row in charges
        if row.get("amount") == SALES_TAX
        and row.get("lookup_id") != PPV_CHARGE_LOOKUP_ID
        and str(row.get("name") or "") == SALES_TAX_CHARGE_DESCRIPTION
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def main() -> None:
    client = load_live()
    before = snapshot(client.get_item("ap_invoices", BILL_ID))
    problems = guard(before)
    if problems:
        raise SystemExit("refusing to edit: " + "; ".join(problems))
    decision = state_sales_tax_gap_decision(
        invoice_total=PDF_TOTAL,
        receipt_amount=RECEIPT_AMOUNT,
        sales_tax=SALES_TAX,
        other_charges=[row["amount"] for row in before["charges"] if row.get("amount") not in (None, SALES_TAX)],
        receipts_matched=True,
    )
    note = state_sales_tax_comment(sales_tax=SALES_TAX, invoice_total=PDF_TOTAL)
    if not note.startswith("AP Clerk:") or "@" in note:
        raise SystemExit("refusing comment")
    existing = sales_tax_charge(before["charges"])
    charge_status = "already"
    if existing is None:
        if decision["action"] != "sales_tax":
            raise SystemExit(f"sales tax rule did not apply: {decision['reason']}")
        if any(row.get("lookup_id") == PPV_CHARGE_LOOKUP_ID for row in before["charges"]):
            raise SystemExit("bill already has a PPV charge")
        payload = sales_tax_charge_payload(SALES_TAX, invoice_id=BILL_ID)
        _body, status, error = client.update("ap_invoices", BILL_ID, payload)
        charge_status = "posted" if status < 400 else f"blocked-{status}"
        if status >= 400:
            raise SystemExit(f"charge put failed {status} {error[:300]}")
    after_charge = snapshot(client.get_item("ap_invoices", BILL_ID))
    drift = guard(after_charge)
    charge = sales_tax_charge(after_charge["charges"])
    if charge is None:
        drift.append("sales tax charge missing")
    elif charge["lookup_id"] != FEE_CHARGE_LOOKUP_ID:
        drift.append(f"charge lookup {charge['lookup_id']} is not the fee type")
    if after_charge["amount"] != PDF_TOTAL:
        drift.append(f"amount {after_charge['amount']} != {PDF_TOTAL}")
    if after_charge["posted"] not in (None, "", False):
        drift.append("bill became posted")
    if after_charge["lines"] != before["lines"]:
        drift.append("receipt line changed")
    if after_charge["batch_id"] != before["batch_id"]:
        drift.append("batch changed")
    if drift:
        raise SystemExit("readback failed: " + "; ".join(drift))
    prior_ids = {row["id"] for row in before["comments"]}
    already_note = next((row for row in after_charge["comments"] if note in str(row.get("html") or "")), None)
    comment_status = "already"
    comment_id = already_note.get("id") if already_note else None
    if already_note is None:
        _body, status, error = client.update("ap_invoices", BILL_ID, added_comment_payload(BILL_ID, note))
        comment_status = "posted" if status < 400 else f"blocked-{status}"
        if status >= 400:
            raise SystemExit(f"comment put failed {status} {error[:300]}")
    final = snapshot(client.get_item("ap_invoices", BILL_ID))
    if comment_id is None:
        new_rows = [row for row in final["comments"] if row["id"] not in prior_ids and note in str(row.get("html") or "")]
        if len(new_rows) != 1:
            raise SystemExit(f"comment readback count {len(new_rows)}")
        comment_id = new_rows[0]["id"]
    final_problems = guard(final)
    if final["amount"] != PDF_TOTAL:
        final_problems.append(f"final amount {final['amount']} != {PDF_TOTAL}")
    if final["lines"] != before["lines"]:
        final_problems.append("receipt changed after comment")
    if final["posted"] not in (None, "", False):
        final_problems.append("posted after comment")
    if final_problems:
        raise SystemExit("final readback failed: " + "; ".join(final_problems))
    result_status = "Success" if final["amount"] == PDF_TOTAL else "HOLD"
    out = {
        "bill_id": BILL_ID,
        "invoice": INVOICE,
        "vendor": "A1 Image Office Systems",
        "amount_before": before["amount"],
        "amount_after": final["amount"],
        "verification": final["verification"],
        "pdf_total": PDF_TOTAL,
        "sales_tax": SALES_TAX,
        "charge_status": charge_status,
        "charge": sales_tax_charge(final["charges"]),
        "comment_id": comment_id,
        "comment_status": comment_status,
        "comment": note,
        "status": result_status,
        "kimco_status": final["status"],
        "posted": final["posted"],
        "batch_id": final["batch_id"],
        "batch": final["batch"],
        "receipt": final["lines"],
        "rule": decision["reason"],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
