"""Correct UniFirst tax to the printed $69.22, then read every new bill back."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.ap_run import purchase_gl_hold, qc_printed_total, unifirst_tax_gap
from ap_clerk.kimco import KimcoClient
from ap_clerk.rules import lookup_id, lookup_text
from scripts.ap_run_2026_10_08_enter import BATCH_NAME, OUT, move_batch
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_missed_entry_2026_10_07 import put, totals

RUN_BATCH = 750


def tax_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for tax in (record.get("lists") or {}).get("APInvoiceTaxCodes") or []:
        values = tax.get("values") or {}
        rows.append(
            {
                "id": tax.get("id"),
                "amount": values.get("Tax_Amount"),
                "taxable": values.get("Taxable_Amount"),
                "rate": values.get("Tax_Rate"),
                "manual": values.get("Manual_Calculation"),
                "code": lookup_id(values.get("Tax_Code")),
            }
        )
    return rows


def note_mentions(record: dict[str, Any]) -> list[dict[str, Any]]:
    notes = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        html = str((comment.get("values") or {}).get("HtmlValue") or "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        notes.append(
            {
                "id": comment.get("id"),
                "text": plain,
                "mentions": re.findall(r'data-mention-id="(\d+)"', html),
            }
        )
    return notes


def main() -> None:
    client = login()
    install_401_guard(client)
    record = client.get_item("ap_invoices", 10562)
    before = tax_rows(record)
    print("tax before", before)
    if len(before) == 1:
        payload = {
            "id": 10562,
            "state": "Modified",
            "lists": {
                "APInvoiceTaxCodes": [
                    {
                        "id": before[0]["id"],
                        "state": "Modified",
                        "values": {
                            "Manual_Calculation": True,
                            "Taxable_Amount": 859.32,
                            "Tax_Rate": 0.08055,
                            "Tax_Amount": 69.22,
                            "Tax_Code": {"id": 2},
                        },
                    }
                ]
            },
        }
        status = put(client, 10562, payload)
        print("tax put", status)
    after_record = client.get_item("ap_invoices", 10562)
    after = totals(after_record)
    print("covered", after["covered"], "tax", tax_rows(after_record))
    fixed = abs(float(after["covered"]) - 928.54) < 0.001
    if fixed and int(after.get("batch_id") or 0) != RUN_BATCH:
        move_batch(client, 10562, RUN_BATCH)
        after = totals(client.get_item("ap_invoices", 10562))
        print("moved", after.get("batch"), after.get("batch_id"), after["covered"])
    progress = json.loads((OUT / "progress.json").read_text())
    for row in progress:
        if row.get("invoice") == "2810822429" and fixed:
            row["result"] = "PASS"
            row["reason"] = ""
            row["batch"] = after.get("batch")
            row["batch_id"] = after.get("batch_id")
            row["covered"] = after["covered"]
            row["header_total"] = after.get("verification")
    (OUT / "progress.json").write_text(json.dumps(progress, indent=2))
    readback = []
    for invoice_id in range(10541, 10566):
        record = client.get_item("ap_invoices", invoice_id)
        info = totals(record)
        values = record.get("values") or {}
        vendor = lookup_text(values.get("Vendor"))
        page_path = OUT / "qc" / f"{invoice_id}.txt"
        page = page_path.read_text() if page_path.exists() else ""
        typed = info.get("verification") if info.get("verification") not in (None, "") else 0
        total_qc = qc_printed_total(page_text=page, typed_amount=float(typed or 0))
        gl_holds = [purchase_gl_hold(line.get("gl")) for line in info.get("lines") or []]
        tax_gap = round(float(typed or 0) - float(info.get("covered") or 0), 2)
        readback.append(
            {
                "bill": invoice_id,
                "invoice": values.get("Invoice_Number"),
                "vendor": vendor,
                "vendor_id": lookup_id(values.get("Vendor")),
                "posted": values.get("Posted"),
                "comments": values.get("Comments"),
                "header": info.get("verification"),
                "printed_total": total_qc["printed_total"],
                "printed_total_qc": "pass" if total_qc["ok"] else "fail",
                "printed_total_reason": total_qc["reason"],
                "covered": info.get("covered"),
                "batch": info.get("batch"),
                "batch_id": info.get("batch_id"),
                "lines": info.get("lines"),
                "charges": info.get("charges"),
                "taxes": info.get("taxes"),
                "notes": note_mentions(record),
                "po": lookup_text(values.get("Purchase_Order")),
                "gl_hold": any(item["hold"] for item in gl_holds),
                "tax_gap": unifirst_tax_gap(
                    vendor=str(vendor or ""),
                    printed_total=float(typed or 0),
                    covered=float(info.get("covered") or 0),
                    tax_gap=tax_gap,
                ),
            }
        )
    # AVEX history item, for the report.
    history = client.get_item("ap_invoices", 9254)
    hvals = history.get("values") or {}
    (OUT / "readback.json").write_text(json.dumps({"bills": readback, "unifirst_fixed": fixed, "avex_sample": {
        "id": 9254,
        "number": hvals.get("Invoice_Number"),
        "vendor": lookup_text(hvals.get("Vendor")),
        "posted": hvals.get("Posted"),
        "lines": totals(history)["lines"],
    }}, indent=2, default=str))
    batch = client.get_item("ap_batches", RUN_BATCH)
    print("batch", (batch.get("values") or {}).get("Status"), (batch.get("values") or {}).get("AP_Invoice_Batch_ID") or (batch.get("values") or {}).get("Name"))
    print("unifirst_fixed", fixed)


if __name__ == "__main__":
    main()
