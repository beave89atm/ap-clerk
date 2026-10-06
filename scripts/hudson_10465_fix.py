"""Code Hudson Energy 10465 like the posted Hudson misc bills.

Posted bills 9284, 9285, and 9286 are Invoice_Type 4 with one
Maintenance/Service line for the full invoice total, no extra charges,
and no Taxes-tab row. This script follows that pattern. It does not post,
does not close a batch, and does not send mail.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import KimcoError
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.rules import ap_clerk_edit_note, money
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login, slim_bill

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("hudson-10465")

BILL_ID = 10465
BATCH_ID = 747
PDF_TOTAL = 2448.46
MISC_ITEM = {"id": 2, "text": "Maintenance/Service-."}
GL_ACCOUNT = {"id": 75, "text": "6445100 - Facility Maint. & Storage"}
PRIOR_IDS = (9284, 9285, 9286)
PATTERN_BILL = 9284
DESCRIPTION = "August Electric Bill"
SOURCE_PDF = Path("/tmp/hudson-10465/2026-09-01_300067525_2609000773_20260901.pdf")
BEFORE_PATH = Path("/tmp/hudson-10465-discover.json")
OUT_JSON = ROOT / "runs" / "hudson-10465-fix.json"
OUT_PDF = ROOT / "runs" / "hudson-10465-2609000773.pdf"


def put(client, payload: dict[str, Any]) -> int:
    if "Posted" in (payload.get("values") or {}):
        raise SystemExit("Refusing a payload that sets Posted.")
    response = client.request("PUT", client._record_url("ap_invoices", BILL_ID), json=payload)
    LOGGER.info("PUT HTTP %s", response.status_code)
    if response.status_code >= 400:
        raise SystemExit(f"PUT HTTP {response.status_code}. Stopping.")
    return response.status_code


def fresh(client) -> dict[str, Any]:
    return slim_bill(client.get_item("ap_invoices", BILL_ID))


def guard(record: dict[str, Any], *, stage: str) -> None:
    if record.get("posted") not in (None, "", False):
        raise SystemExit(f"Bill became posted during {stage}. Stopping.")
    batch_id = (record.get("batch") or {}).get("id")
    if int(batch_id or 0) != BATCH_ID:
        raise SystemExit(f"Batch changed during {stage}. Stopping.")


def covered(record: dict[str, Any]) -> float:
    line_total = round(sum(float(money(line.get("extended")) or 0) for line in record["lines"]), 2)
    charge_total = round(sum(float(money(charge.get("amount")) or 0) for charge in record["charges"]), 2)
    tax_total = round(sum(float(money(tax.get("amount")) or 0) for tax in record["taxes"]), 2)
    return round(line_total + charge_total + tax_total, 2)


def main() -> None:
    discovered = json.loads(BEFORE_PATH.read_text())
    before = discovered["before"]
    priors = [row for row in discovered["hudson"] if int(row["id"]) in PRIOR_IDS]
    if [int(row["id"]) for row in priors] != list(PRIOR_IDS):
        raise SystemExit("Posted Hudson bills were not the expected three. No write.")
    items = {((row["lines"] or [{}])[0].get("item") or {}).get("id") for row in priors}
    if items != {MISC_ITEM["id"]}:
        raise SystemExit("Posted Hudson misc items disagree. No write.")
    if any(row["charges"] or row["taxes"] or len(row["lines"]) != 1 for row in priors):
        raise SystemExit("Posted Hudson tax or line pattern disagrees. No write.")
    if any(row.get("posted") is not True or row.get("type") != 4 for row in priors):
        raise SystemExit("A comparison bill is not a posted type 4 bill. No write.")

    client = login()
    install_401_guard(client)
    live = fresh(client)
    guard(live, stage="recheck")
    if live.get("invoice") != "2609000773" or live.get("type") != 4:
        raise SystemExit("Bill 10465 is not the Hudson type 4 header. No write.")
    if [charge["id"] for charge in live["charges"]] != [5678, 5679, 5680, 5681, 5682]:
        raise SystemExit("Charges changed since the before snapshot. No write.")
    if live["lines"] or live["taxes"]:
        raise SystemExit("Lines or taxes changed since the before snapshot. No write.")
    if money(live.get("verification")) != PDF_TOTAL:
        raise SystemExit("Verification is not the PDF total. No write.")

    put(
        client,
        {
            "id": BILL_ID,
            "state": "Modified",
            "lists": {
                "InvoiceAdditionalCharges": [
                    {"id": int(charge["id"]), "state": "Removed"} for charge in live["charges"]
                ]
            },
        },
    )
    cleared = fresh(client)
    guard(cleared, stage="remove charges")
    if cleared["charges"]:
        raise SystemExit("Fee charges are still on the bill. No line was added.")

    payload = misc_add_item_payload(
        [{"description": DESCRIPTION, "qty": 1, "unit_price": PDF_TOTAL}],
        invoice_id=BILL_ID,
        vendor_id=88,
        misc_item=MISC_ITEM,
        gl_account=GL_ACCOUNT,
    )
    payload["values"] = {"Invoice_Type": 4}
    try:
        put(client, payload)
    except SystemExit:
        retry = misc_add_item_payload(
            [{"description": DESCRIPTION, "qty": 1, "unit_price": PDF_TOTAL}],
            invoice_id=BILL_ID,
            vendor_id=88,
            misc_item=MISC_ITEM,
        )
        retry["values"] = {"Invoice_Type": 4}
        put(client, retry)

    coded = fresh(client)
    guard(coded, stage="add line")
    if coded["charges"] or coded["taxes"] or len(coded["lines"]) != 1:
        raise SystemExit("Line result does not match the posted Hudson pattern. No note added.")
    line = coded["lines"][0]
    if (line.get("item") or {}).get("id") != MISC_ITEM["id"]:
        raise SystemExit("Misc item is not Maintenance/Service. No note added.")
    if line.get("receipt") not in (None, "", {}):
        raise SystemExit("A receipt was linked. No note added.")
    if coded.get("po") not in (None, "", {}):
        raise SystemExit("A purchase order is still linked. No note added.")
    if covered(coded) != PDF_TOTAL or money(coded.get("invoice_amount")) != PDF_TOTAL:
        raise SystemExit(
            f"Total is {covered(coded)} invoice amount {coded.get('invoice_amount')}. No note added."
        )

    note = (
        "AP Clerk: @Treyce Hodges per Kyle, Hudson Energy 2609000773 is miscellaneous, no PO. "
        "Coded to Maintenance/Service like posted bill 9284. "
        f"Total matches PDF ${PDF_TOTAL:.2f}. Not posted."
    )
    html = ap_clerk_edit_note(note, action="treyce")
    from ap_clerk.kimco import added_comment_payload

    put(client, added_comment_payload(BILL_ID, html))
    after = fresh(client)
    guard(after, stage="comment")
    new_notes = [row for row in after["comments"] if row["id"] != 1527]
    if len(new_notes) != 1 or not str(new_notes[0]["text"]).startswith("AP Clerk:"):
        raise SystemExit("Comment count after the write was not one new note.")
    if covered(after) != PDF_TOTAL:
        raise SystemExit("Total changed after the note.")

    if not SOURCE_PDF.is_file():
        raise SystemExit("Invoice PDF was not saved from the KIMCO download.")
    shutil.copyfile(SOURCE_PDF, OUT_PDF)
    result = {
        "before": before,
        "after": after,
        "prior_bills_used": priors,
        "pdf_total": PDF_TOTAL,
        "invoice_pdf": "runs/hudson-10465-2609000773.pdf",
        "pdf_amount_due": PDF_TOTAL,
        "pdf_current_charges": 2444.62,
        "pdf_ercot_adjustment": 3.84,
        "attachment": {
            "id": discovered["attachments"][0]["id"],
            "filename": discovered["attachments"][0]["name"],
            "page_count": 2,
            "invoice_numbers": ["2609000773", "2609000773"],
            "action": "kept; both pages are invoice 2609000773",
        },
        "pattern": {
            "misc_item": MISC_ITEM,
            "gl_on_posted_bills": GL_ACCOUNT,
            "description": DESCRIPTION,
            "tax_handling": "Posted Hudson bills put the full invoice total on one misc line and leave the Taxes tab empty.",
            "pattern_bill": PATTERN_BILL,
        },
        "note": new_notes[0]["text"],
    }
    OUT_JSON.write_text(json.dumps(result, indent=2, default=str))
    LOGGER.info("Coded 10465 total %s notes %s", covered(after), len(after["comments"]))


if __name__ == "__main__":
    main()
