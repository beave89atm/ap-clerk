"""Select receipt 25182 on O'Neal bill 10512 with a one-cent PPV.

Does not post the bill, change the batch, or send mail.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import added_comment_payload
from ap_clerk.rules import SHAWN_MENTION_HTML, TREYCE_MENTION_HTML, money
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("oneal-10512")

BILL_ID = 10512
INVOICE = "15487245"
BATCH_ID = 747
RECEIPT_ID = 25182
PDF_TOTAL = 81.52
LINE_EXT = 81.53
PPV = -0.01
OUT = ROOT / "runs" / "qc2530-missing3" / "result.json"
NOTE = (
    "AP Clerk: @Shawn McKibben please disregard my earlier note on 15487245. "
    "1 piece is the 240 in bar on receipt 25182; I selected it with a $0.01 PPV. "
    "No action needed. @Treyce Hodges ready to process, not posted."
)
MENTION_RE = re.compile(
    r'data-mention-id="(\d+)"[^>]*data-mention-name="([^"]*)"',
    flags=re.I,
)


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def note_html() -> str:
    body = NOTE.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    body = body.replace("@Treyce Hodges", TREYCE_MENTION_HTML, 1)
    if body.count('data-mention-id="104"') != 1 or body.count('data-mention-id="33"') != 1:
        raise SystemExit("Note HTML is missing Shawn or Treyce. Not writing.")
    return f"<p>{body}</p>"


def unposted(value: Any) -> bool:
    return value in (None, "", False)


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    batch = values.get("AP_Invoice_Batch") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        receipt = vals.get("Receipt")
        lines.append(
            {
                "id": line.get("id"),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": money(vals.get("Extended_Amount")),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        kind = vals.get("Additional_Charges")
        charges.append(
            {
                "id": charge.get("id"),
                "kind_id": kind.get("id") if isinstance(kind, dict) else None,
                "kind": (kind.get("text") or kind.get("name")) if isinstance(kind, dict) else None,
                "amount": money(vals.get("Amount")),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        vals = comment.get("values") or {}
        html = str(vals.get("HtmlValue") or "")
        creator = vals.get("CreatorId") if isinstance(vals.get("CreatorId"), dict) else {}
        comments.append(
            {
                "id": comment.get("id"),
                "text": plain(html),
                "mentions": [
                    {"id": int(match.group(1)), "name": match.group(2)}
                    for match in MENTION_RE.finditer(html)
                ],
                "created_on": vals.get("CreatedOn"),
                "creator_id": creator.get("id"),
                "creator_name": creator.get("text") or creator.get("name"),
            }
        )
    line_sum = round(sum(float(row["extended"] or 0) for row in lines), 2)
    charge_sum = round(sum(float(row["amount"] or 0) for row in charges), 2)
    return {
        "id": record.get("id"),
        "invoice": values.get("Invoice_Number"),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": (batch.get("text") or batch.get("name")) if isinstance(batch, dict) else None,
        "invoice_amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "net": money(values.get("Invoice_Net_Amount")),
        "lines": lines,
        "charges": charges,
        "taxes": len(lists.get("APInvoiceTaxCodes") or []),
        "comments": comments,
        "lines_plus_charges": round(line_sum + charge_sum, 2),
    }


def confirm_open(row: dict[str, Any], stage: str) -> None:
    if int(row["id"]) != BILL_ID or str(row["invoice"]) != INVOICE:
        raise SystemExit(f"{stage}: bill is not invoice {INVOICE}. Aborting.")
    if not unposted(row.get("posted")) or row.get("void") not in (None, "", False):
        raise SystemExit(f"{stage}: bill is posted or void. Aborting.")
    if int(row.get("batch_id") or 0) != BATCH_ID:
        raise SystemExit(f"{stage}: bill is on batch {row.get('batch_id')}, not 747. Aborting.")


def receipt_ok(record: dict[str, Any]) -> None:
    values = record.get("values") or {}
    qty = values.get("Quantity_Received")
    unit = values.get("PO_Item_Number_$_Unit_Price") or values.get("Purchase_Cost") or values.get("Unit_Cost")
    ext = money(values.get("Extended_Purchase_Cost"))
    if ext is None and qty not in (None, "") and unit not in (None, ""):
        ext = round(float(qty) * float(unit), 2)
    invoiced = values.get("Quantity_Invoiced") or values.get("Quantity_Billed")
    if float(qty or 0) != 240 or abs(float(unit or 0) - 0.3397) > 0.00001 or ext != LINE_EXT:
        raise SystemExit(
            f"Receipt {RECEIPT_ID} is qty {qty} at {unit}, extended {ext}. Expected 240 at 0.3397, $81.53. Aborting."
        )
    if invoiced not in (None, "", 0, 0.0) and float(invoiced) >= 240:
        raise SystemExit(f"Receipt {RECEIPT_ID} is already invoiced for {invoiced}. Aborting.")


def has_receipt(row: dict[str, Any]) -> bool:
    return any(int(line.get("receipt_id") or 0) == RECEIPT_ID for line in row["lines"])


def ppv_amount(row: dict[str, Any]) -> float:
    return round(
        sum(float(charge["amount"] or 0) for charge in row["charges"] if int(charge.get("kind_id") or 0) == 13),
        2,
    )


def matching_note(row: dict[str, Any]) -> dict[str, Any] | None:
    for comment in row["comments"]:
        if comment["text"] == NOTE:
            return comment
    return None


def main() -> None:
    client = login()
    install_401_guard(client)
    before = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_open(before, "before")
    receipt_ok(client.get_item("receipts", RECEIPT_ID))

    if not has_receipt(before):
        status = client.try_select_receipts(BILL_ID, [RECEIPT_ID])
        LOGGER.info("Select receipt %s %s", RECEIPT_ID, status)
        if status != "selected":
            raise SystemExit(f"Select Receipts returned {status}. No PPV and no note.")
    selected = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_open(selected, "after select")
    if not has_receipt(selected):
        raise SystemExit("Receipt 25182 is not on the bill. No PPV and no note.")
    extensions = [float(line["extended"] or 0) for line in selected["lines"] if int(line.get("receipt_id") or 0) == RECEIPT_ID]
    if extensions != [LINE_EXT]:
        raise SystemExit(f"Selected line extended {extensions}, not {LINE_EXT}. No PPV and no note.")

    if ppv_amount(selected) != PPV:
        if selected["charges"]:
            raise SystemExit(f"Bill already has charges {selected['charges']}. Not adding another PPV.")
        status = client.try_post_ppv(BILL_ID, PPV)
        LOGGER.info("PPV %s", status)
        if status != "posted":
            raise SystemExit(f"PPV post returned {status}. No note.")
    priced = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_open(priced, "after ppv")
    if priced["lines_plus_charges"] != PDF_TOTAL or ppv_amount(priced) != PPV:
        raise SystemExit(
            f"Lines plus charges are {priced['lines_plus_charges']} with PPV {ppv_amount(priced)}. No note."
        )

    if matching_note(priced) is None:
        _body, status, error = client.update("ap_invoices", BILL_ID, added_comment_payload(BILL_ID, note_html()))
        LOGGER.info("Note HTTP %s", status)
        if status >= 400:
            raise SystemExit(f"Note was not saved HTTP {status}. {error[:180]}")
    after = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_open(after, "final")
    saved = matching_note(after)
    if saved is None:
        raise SystemExit("The new note was not found on readback.")
    mention_ids = {item["id"] for item in saved["mentions"]}
    if mention_ids != {33, 104} or int(saved.get("creator_id") or 0) != 175:
        raise SystemExit("The note is missing a mention or was not authored by API Agent 175.")
    if after["lines_plus_charges"] != PDF_TOTAL or not has_receipt(after) or ppv_amount(after) != PPV:
        raise SystemExit("Final total or receipt does not match.")

    payload = json.loads(OUT.read_text())
    for bill in payload.get("bills") or []:
        if int(bill.get("bill_id") or 0) != BILL_ID:
            continue
        bill["prior_hold"] = {
            "result": bill.get("result"),
            "reason": bill.get("reason"),
            "note": bill.get("note"),
            "lines": bill.get("lines"),
            "charges": bill.get("charges"),
            "lines_charges_tax": bill.get("lines_charges_tax"),
            "decision": bill.get("decision"),
        }
        bill["batch_id"] = after["batch_id"]
        bill["batch"] = after["batch"]
        bill["posted"] = after["posted"]
        bill["Invoice_Amount"] = after["invoice_amount"]
        bill["verification"] = after["verification"]
        bill["net"] = after["net"]
        bill["lines"] = after["lines"]
        bill["charges"] = after["charges"]
        bill["receipts"] = [RECEIPT_ID]
        bill["lines_charges_tax"] = after["lines_plus_charges"]
        bill["notes"] = [{"id": row["id"], "text": row["text"], "mentions": row["mentions"]} for row in after["comments"]]
        bill["result"] = "PASS"
        bill["reason"] = "1 piece is the 240 in bar on receipt 25182. PPV -$0.01 makes the total $81.52."
        bill["note"] = NOTE
        bill["decision"] = "ppv"
    payload["oneal_10512_ppv"] = {"before": before, "after_select": selected, "after": after}
    payload["posted"] = False
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    LOGGER.info(
        "Bill %s receipt %s total %s batch %s note %s",
        BILL_ID,
        RECEIPT_ID,
        after["lines_plus_charges"],
        after["batch_id"],
        saved["id"],
    )


if __name__ == "__main__":
    main()
