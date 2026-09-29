"""Keep A1 Image bill 10367 sales tax on the Taxes tab. Do not post the bill.

Kyle 2026-09-28, corrected the same day after Treyce. Invoice 67067,
PO 59295, receipt 24712 is 1 @ 508.67. The PDF total is 550.64. The
41.97 line is Tax Code Sales Tax on APInvoiceTaxCodes, not an additional
charge and not PPV. Receipt selection stays as it is. The batch is not
changed. A re-run does not add a second tax row or a second note.
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
    AP_INVOICE_TAX_LIST,
    PPV_CHARGE_LOOKUP_ID,
    SALES_TAX_CODE,
    SALES_TAX_CODE_LOOKUP_ID,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    sales_tax_line_payload,
)
from ap_clerk.rules import (
    KYLE_CLEAVER_MENTION_HTML,
    KYLE_CLEAVER_MENTION_ID,
    TREYCE_MENTION_HTML,
    TREYCE_MENTION_ID,
    lookup_id,
    lookup_text,
    money,
)

BILL_ID = 10367
INVOICE = "67067"
RECEIPT_ID = 24712
PDF_TOTAL = 550.64
RECEIPT_AMOUNT = 508.67
SALES_TAX = 41.97
RATE_PERCENT = 8.25
HOST = "https://live.kimcoerp.com"
OUT_JSON = ROOT / "artifacts" / "a1-67067-sales-tax-2026-09-28.json"
NOTE = (
    f"<p>AP Clerk: {TREYCE_MENTION_HTML} {KYLE_CLEAVER_MENTION_HTML} "
    "The sales tax was moved from Additional Charges to the Taxes tab "
    f"(Sales Tax, {SALES_TAX:.2f}). The bill still matches ${PDF_TOTAL:.2f}.</p>"
)


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
            }
        )
    taxes = []
    for row in lists.get(AP_INVOICE_TAX_LIST) or []:
        tv = row.get("values") or {}
        code = tv.get("Tax_Code")
        taxes.append(
            {
                "id": row.get("id"),
                "code_id": lookup_id(code),
                "code": lookup_text(code),
                "manual": tv.get("Manual_Calculation"),
                "taxable": money(tv.get("Taxable_Amount")),
                "rate": tv.get("Tax_Rate"),
                "amount": money(tv.get("Tax_Amount")),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        comments.append({"id": comment.get("id"), "html": (comment.get("values") or {}).get("HtmlValue") or ""})
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": money(values.get("Invoice_Amount")),
        "net": money(values.get("Invoice_Net_Amount")),
        "balance": money(values.get("Invoice_Balance")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "tax_amount": money(values.get("Tax_Amount")),
        "additional": money(values.get("Total_Additional_Charges")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "invoice_type": values.get("Invoice_Type"),
        "posted": values.get("Posted"),
        "status": values.get("Status"),
        "lines": lines,
        "charges": charges,
        "taxes": taxes,
        "comments": comments,
    }


def guard(before: dict[str, Any]) -> list[str]:
    problems = []
    if before["invoice"] != INVOICE:
        problems.append(f"invoice {before['invoice']} != {INVOICE}")
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
        and str(row.get("name") or "") == "Sales tax"
    ]
    return hits[0] if len(hits) == 1 else None


def sales_tax_row(taxes: list[dict[str, Any]]) -> dict[str, Any] | None:
    hits = [
        row
        for row in taxes
        if row.get("code_id") == SALES_TAX_CODE_LOOKUP_ID
        and row.get("code") == SALES_TAX_CODE
        and row.get("amount") == SALES_TAX
        and row.get("taxable") == RECEIPT_AMOUNT
    ]
    return hits[0] if len(hits) == 1 else None


def note_present(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in comments:
        html = str(row.get("html") or "")
        if (
            "AP Clerk:" in html
            and f'data-mention-id="{TREYCE_MENTION_ID}"' in html
            and f'data-mention-id="{KYLE_CLEAVER_MENTION_ID}"' in html
            and "Taxes tab" in html
            and "41.97" in html
            and "550.64" in html
        ):
            return row
    return None


def main() -> None:
    client = load_live()
    before = snapshot(client.get_item("ap_invoices", BILL_ID))
    problems = guard(before)
    if problems:
        raise SystemExit("refusing to edit: " + "; ".join(problems))
    if NOTE.count(f'data-mention-id="{KYLE_CLEAVER_MENTION_ID}"') != 1:
        raise SystemExit("refusing comment")
    charge = sales_tax_charge(before["charges"])
    tax = sales_tax_row(before["taxes"])
    charge_status = "already-removed" if charge is None else "remove"
    tax_status = "already" if tax is not None else "add"
    if charge is not None or tax is None:
        if any(row.get("lookup_id") == PPV_CHARGE_LOOKUP_ID for row in before["charges"]):
            raise SystemExit("bill already has a PPV charge")
        lists: dict[str, Any] = {}
        if charge is not None:
            lists["InvoiceAdditionalCharges"] = [{"id": charge["id"], "state": "Removed"}]
        if tax is None:
            added = sales_tax_line_payload(
                SALES_TAX,
                taxable_amount=RECEIPT_AMOUNT,
                rate_percent=RATE_PERCENT,
            )
            lists[AP_INVOICE_TAX_LIST] = added["lists"][AP_INVOICE_TAX_LIST]
        payload = {"state": "Modified", "id": BILL_ID, "lists": lists}
        _body, status, error = client.update("ap_invoices", BILL_ID, payload)
        if status >= 400:
            raise SystemExit(f"tax put failed {status} {error[:300]}")
        charge_status = "removed" if charge is not None else charge_status
        tax_status = "posted" if tax is None else tax_status
    after = snapshot(client.get_item("ap_invoices", BILL_ID))
    drift = guard(after)
    row = sales_tax_row(after["taxes"])
    if row is None:
        drift.append("sales tax row missing")
    elif row["rate"] != 0.0825 or row["manual"] is not True:
        drift.append(f"tax row values {row}")
    if sales_tax_charge(after["charges"]) is not None:
        drift.append("sales tax additional charge still present")
    if after["net"] != PDF_TOTAL or after["balance"] != PDF_TOTAL or after["verification"] != PDF_TOTAL:
        drift.append(f"payable {after['net']} balance {after['balance']} verification {after['verification']}")
    if after["tax_amount"] != SALES_TAX:
        drift.append(f"header tax {after['tax_amount']} != {SALES_TAX}")
    if after["amount"] != RECEIPT_AMOUNT:
        drift.append(f"pre-tax amount {after['amount']} != {RECEIPT_AMOUNT}")
    if after["posted"] not in (None, "", False):
        drift.append("bill became posted")
    if after["lines"] != before["lines"]:
        drift.append("receipt line changed")
    if after["batch_id"] != before["batch_id"]:
        drift.append("batch changed")
    if drift:
        raise SystemExit("readback failed: " + "; ".join(drift))
    prior_ids = {item["id"] for item in before["comments"]}
    existing_note = note_present(after["comments"])
    comment_status = "already"
    comment_id = existing_note.get("id") if existing_note else None
    if existing_note is None:
        _body, status, error = client.update("ap_invoices", BILL_ID, added_comment_payload(BILL_ID, NOTE))
        comment_status = "posted" if status < 400 else f"blocked-{status}"
        if status >= 400:
            raise SystemExit(f"comment put failed {status} {error[:300]}")
    final = snapshot(client.get_item("ap_invoices", BILL_ID))
    if comment_id is None:
        new_rows = [item for item in final["comments"] if item["id"] not in prior_ids and note_present([item])]
        if len(new_rows) != 1:
            raise SystemExit(f"comment readback count {len(new_rows)}")
        comment_id = new_rows[0]["id"]
    final_problems = guard(final)
    if final["net"] != PDF_TOTAL or final["posted"] not in (None, "", False):
        final_problems.append("final payable or posted flag drifted")
    if final["lines"] != before["lines"] or final["batch_id"] != before["batch_id"]:
        final_problems.append("receipt or batch changed after comment")
    if final_problems:
        raise SystemExit("final readback failed: " + "; ".join(final_problems))
    out = {
        "bill_id": BILL_ID,
        "invoice": INVOICE,
        "vendor": "A1 Image Office Systems",
        "amount_before": before["amount"],
        "amount_after": final["amount"],
        "net": final["net"],
        "balance": final["balance"],
        "verification": final["verification"],
        "pdf_total": PDF_TOTAL,
        "sales_tax": SALES_TAX,
        "charge_status": charge_status,
        "removed_charge": None if charge is None else charge["id"],
        "tax_status": tax_status,
        "tax_row": sales_tax_row(final["taxes"]),
        "comment_id": comment_id,
        "comment_status": comment_status,
        "status": "Success" if final["net"] == PDF_TOTAL else "HOLD",
        "posted": final["posted"],
        "batch_id": final["batch_id"],
        "batch": final["batch"],
        "receipt": final["lines"],
        "rule": "Explicit PDF sales tax goes on the Taxes tab as Sales Tax, never Additional Charges or PPV.",
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
