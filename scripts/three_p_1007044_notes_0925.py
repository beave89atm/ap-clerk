"""Append one plain Comments_1 note on four live 3P HOLD bills.

Bills: 10306 / 142179, 10308 / 142188, 10312 / 142216, 10314 / 142280.
All are HOLD price_variance on Transfer AP (375). The note is appended.
Existing Comments_1 rows are not edited. No @mention. No email. No change
to receipts, prices, amounts, batches, lines, or status.

A bill is skipped when a live 1007044-1 receipt qty or unit cost differs
from the expected open receipt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.rules import lookup_id, lookup_text, money

OUT_JSON = ROOT / "artifacts" / "3p-1007044-notes-2026-09-25.json"
HOST = "https://live.kimcoerp.com"
PO_UNIT = 361.46
INVOICE_UNIT = 97.50
GAP_EACH = 263.96
TRANSFER_AP_BATCH = 375
PART = "1007044-1"

PREFIX = (
    "AP Clerk: Line not matching on cost: part 1007044-1 only. "
    "Quantities match receipts on every line; all other lines are matched and selected. "
)
SUFFIX = (
    "Fix: unreceive, set PO price for 1007044-1 to $97.50 (or confirm correct price / "
    "unit of measure with 3P), re-receive same qty, then AP will select receipts and finish. "
    "Pattern: the same $263.96/ea gap on 1007044-1 appears on 4 invoices "
    "(142179, 142188, 142216, 142280), POs 58925, 58952, 58972, 59002, 59031, "
    "21 pcs, $5,543.16 total. Please update the standard/PO price for 1007044-1 going forward."
)

BILLS: list[dict[str, Any]] = [
    {
        "bill_id": 10306,
        "invoice": "142179",
        "pos": [
            {
                "po": "58925",
                "qty": 4,
                "gap": 1055.84,
                "receipts": [{"id": 23414, "qty": 4}],
            }
        ],
    },
    {
        "bill_id": 10308,
        "invoice": "142188",
        "pos": [
            {
                "po": "58925",
                "qty": 2,
                "gap": 527.92,
                "receipts": [{"id": 23543, "qty": 2}],
            }
        ],
    },
    {
        "bill_id": 10312,
        "invoice": "142216",
        "pos": [
            {
                "po": "58925",
                "qty": 1,
                "gap": 263.96,
                "receipts": [{"id": 23528, "qty": 1}],
            },
            {
                "po": "58952",
                "qty": 3,
                "gap": 791.88,
                "receipts": [{"id": 23529, "qty": 3}],
            },
            {
                "po": "58972",
                "qty": 2,
                "gap": 527.92,
                "receipts": [{"id": 23527, "qty": 2}],
            },
            {
                "po": "59002",
                "qty": 4,
                "gap": 1055.84,
                "receipts": [{"id": 23530, "qty": 4}],
            },
        ],
    },
    {
        "bill_id": 10314,
        "invoice": "142280",
        "pos": [
            {
                "po": "59002",
                "qty": 2,
                "gap": 527.92,
                "receipts": [{"id": 23917, "qty": 2}],
            },
            {
                "po": "59031",
                "qty": 3,
                "gap": 791.88,
                "receipts": [
                    {"id": 23920, "qty": 2},
                    {"id": 23921, "qty": 1},
                ],
            },
        ],
    },
]


def money_eq(left: Any, right: float) -> bool:
    value = money(left)
    return value is not None and abs(value - right) < 0.001


def qty_eq(left: Any, right: float) -> bool:
    value = money(left)
    return value is not None and abs(value - right) < 0.001


def dollar(amount: float) -> str:
    return f"${amount:,.2f}"


def qty_text(qty: float) -> str:
    if abs(qty - round(qty)) < 0.001:
        return str(int(round(qty)))
    return f"{qty:.2f}"


def po_phrase(row: dict[str, Any]) -> str:
    receipts = row["receipts"]
    if len(receipts) == 1 and qty_eq(receipts[0]["qty"], row["qty"]):
        receipt_bit = f"receipt {receipts[0]['id']}"
    else:
        receipt_bit = " + ".join(
            f"receipt {item['id']} qty {qty_text(float(item['qty']))}" for item in receipts
        )
    return (
        f"PO {row['po']} qty {qty_text(float(row['qty']))}, {receipt_bit}, "
        f"PO price {dollar(PO_UNIT)} vs invoice {dollar(INVOICE_UNIT)}, gap {dollar(float(row['gap']))}"
    )


def note_text(spec: dict[str, Any]) -> str:
    detail = ". ".join(po_phrase(row) for row in spec["pos"])
    text = f"{PREFIX}{detail}. {SUFFIX}"
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if "@" in text or "data-mention" in text or "Shawn" in text:
        raise RuntimeError("note must not mention or notify")
    return text


def expected_gap(qty: float) -> float:
    return round(qty * GAP_EACH, 2)


def comment_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        values = comment.get("values") or {}
        rows.append(
            {
                "id": comment.get("id"),
                "html": values.get("HtmlValue") or "",
                "mention": values.get("Mention"),
            }
        )
    return rows


def line_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        values = line.get("values") or {}
        rows.append(
            {
                "id": line.get("id"),
                "qty": values.get("Quantity"),
                "price": values.get("Unit_Price"),
                "ext": values.get("Extended_Amount"),
                "receipt": lookup_id(values.get("Receipt")),
            }
        )
    return rows


def charge_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for charge in (record.get("lists") or {}).get("InvoiceAdditionalCharges") or []:
        values = charge.get("values") or {}
        rows.append({"id": charge.get("id"), "amount": values.get("Amount")})
    return rows


def fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": values.get("Invoice_Amount"),
        "verification": values.get("Invoice_Verification_Amount"),
        "batch": lookup_id(values.get("AP_Invoice_Batch")),
        "batch_text": lookup_text(values.get("AP_Invoice_Batch")),
        "status": values.get("Status"),
        "posted": values.get("Posted"),
        "posting_hold": values.get("Posting_Hold"),
        "vendor": lookup_text(values.get("Vendor")),
        "comments_header": values.get("Comments"),
        "lines": line_rows(record),
        "charges": charge_rows(record),
        "comments": comment_rows(record),
    }


def receipt_open(values: dict[str, Any]) -> bool:
    invoiced = values.get("Invoiced")
    ap = values.get("AP_Invoice_Number")
    ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "").strip()
    return invoiced is not True and not ap_text


def part_is_1007044(line: dict[str, Any]) -> bool:
    blob = json.dumps(line.get("values") or {}, default=str)
    return PART in blob


def verify_receipt(
    receipt: dict[str, Any],
    purchase_line: dict[str, Any],
    *,
    expected_qty: float,
    expected_po: str,
) -> list[str]:
    values = receipt.get("values") or {}
    diffs: list[str] = []
    rid = receipt.get("id")
    qty = money(values.get("Quantity_Received"))
    if not qty_eq(qty, expected_qty):
        diffs.append(f"receipt {rid} Quantity_Received {qty} != invoice/open qty {expected_qty}")
    for field in ("Unit_Cost", "Purchase_Cost", "PO_Item_Number_$_Unit_Price"):
        cost = money(values.get(field))
        if not money_eq(cost, PO_UNIT):
            diffs.append(f"receipt {rid} {field} {cost} != {PO_UNIT}")
    if not receipt_open(values):
        diffs.append(f"receipt {rid} is not open (Invoiced={values.get('Invoiced')}, AP={values.get('AP_Invoice_Number')})")
    po_text = lookup_text(values.get("PO_Number")) or ""
    if expected_po not in po_text:
        diffs.append(f"receipt {rid} PO {po_text} != {expected_po}")
    if not part_is_1007044(purchase_line):
        diffs.append(f"receipt {rid} PO line is not {PART}")
    po_price = money((purchase_line.get("values") or {}).get("Unit_Price"))
    if not money_eq(po_price, PO_UNIT):
        diffs.append(f"receipt {rid} PO line unit price {po_price} != {PO_UNIT}")
    return diffs


def verify_spec_math(spec: dict[str, Any]) -> list[str]:
    diffs = []
    total_qty = 0.0
    for row in spec["pos"]:
        receipt_qty = round(sum(float(item["qty"]) for item in row["receipts"]), 2)
        if not qty_eq(receipt_qty, row["qty"]):
            diffs.append(f"PO {row['po']} receipt qtys {receipt_qty} != PO qty {row['qty']}")
        gap = expected_gap(float(row["qty"]))
        if not money_eq(gap, float(row["gap"])):
            diffs.append(f"PO {row['po']} gap {row['gap']} != {gap}")
        total_qty += float(row["qty"])
    return diffs


def bill_context_diffs(record: dict[str, Any], spec: dict[str, Any], receipt_ids: set[int]) -> list[str]:
    values = record.get("values") or {}
    diffs = []
    invoice = str(values.get("Invoice_Number") or "").strip()
    if invoice != spec["invoice"]:
        diffs.append(f"bill {spec['bill_id']} invoice {invoice} != {spec['invoice']}")
    vendor = lookup_text(values.get("Vendor")) or ""
    if "3P" not in vendor.upper():
        diffs.append(f"bill {spec['bill_id']} vendor {vendor} is not 3P")
    batch = lookup_id(values.get("AP_Invoice_Batch"))
    if batch != TRANSFER_AP_BATCH:
        diffs.append(f"bill {spec['bill_id']} batch {batch} != Transfer AP {TRANSFER_AP_BATCH}")
    if values.get("Posted") not in (None, "", False):
        diffs.append(f"bill {spec['bill_id']} is posted")
    selected = {row["receipt"] for row in line_rows(record)}
    already = sorted(receipt_ids & {int(item) for item in selected if item not in (None, "")})
    if already:
        diffs.append(f"bill {spec['bill_id']} already selected 1007044 receipts {already}")
    return diffs


def comment_payload(kimco_id: int, text: str) -> dict[str, Any]:
    if "@" in text or "mention" in text.lower() or not text.startswith("AP Clerk:"):
        raise RuntimeError("refusing comment payload")
    return {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {
            "Comments_1": [
                {
                    "state": "Added",
                    "values": {
                        "HtmlValue": text,
                        "Entity": {"id": 203},
                        "ObjectId": int(kimco_id),
                        "FormId": 218,
                    },
                }
            ]
        },
    }


def saved_note(rows: list[dict[str, Any]], text: str, prior_ids: set[Any]) -> dict[str, Any] | None:
    hits = []
    for row in rows:
        html = str(row.get("html") or "")
        if row.get("id") in prior_ids:
            continue
        if text in html or html.strip() == text:
            hits.append(row)
    if len(hits) != 1:
        return None
    return hits[0]


def unchanged_except_new_comment(before: dict[str, Any], after: dict[str, Any], new_id: Any) -> list[str]:
    diffs = []
    for key in (
        "invoice",
        "amount",
        "verification",
        "batch",
        "batch_text",
        "status",
        "posted",
        "posting_hold",
        "vendor",
        "comments_header",
        "lines",
        "charges",
    ):
        if before.get(key) != after.get(key):
            diffs.append(f"{key} changed")
    before_comments = {row["id"]: row["html"] for row in before.get("comments") or []}
    after_comments = list(after.get("comments") or [])
    after_ids = [row["id"] for row in after_comments]
    if sorted(before_comments) != sorted(row["id"] for row in after_comments if row["id"] != new_id):
        diffs.append("existing Comments_1 ids changed")
    for row in after_comments:
        if row["id"] == new_id:
            continue
        if before_comments.get(row["id"]) != row["html"]:
            diffs.append(f"existing comment {row['id']} text changed")
    if after_ids.count(new_id) != 1:
        diffs.append("new comment id missing")
    if len(after_comments) != len(before_comments) + 1:
        diffs.append(f"comment count {len(before_comments)} -> {len(after_comments)}")
    return diffs


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("refusing non-live host")
    return KimcoClient.authenticate(creds.instance_url, creds.key, creds.password, target="live")


def gather(client: KimcoClient, spec: dict[str, Any]) -> dict[str, Any]:
    record = client.get_item("ap_invoices", int(spec["bill_id"]))
    receipts = []
    diffs = verify_spec_math(spec)
    receipt_ids: set[int] = set()
    for row in spec["pos"]:
        for item in row["receipts"]:
            rid = int(item["id"])
            receipt_ids.add(rid)
            receipt = client.get_item("receipts", rid)
            po_line_id = lookup_id((receipt.get("values") or {}).get("PO_Item_Number"))
            if po_line_id in (None, ""):
                diffs.append(f"receipt {rid} has no PO line")
                purchase_line = {}
            else:
                purchase_line = client.get_item("purchase_lines", int(po_line_id))
            diffs.extend(
                verify_receipt(
                    receipt,
                    purchase_line,
                    expected_qty=float(item["qty"]),
                    expected_po=str(row["po"]),
                )
            )
            values = receipt.get("values") or {}
            receipts.append(
                {
                    "id": rid,
                    "po": row["po"],
                    "expected_qty": item["qty"],
                    "quantity_received": money(values.get("Quantity_Received")),
                    "quantity_remaining": money(values.get("Quantity_Remaining")),
                    "unit_cost": money(values.get("Unit_Cost")),
                    "purchase_cost": money(values.get("Purchase_Cost")),
                    "po_unit_price": money(values.get("PO_Item_Number_$_Unit_Price")),
                    "open": receipt_open(values),
                    "po_line": lookup_text(values.get("PO_Item_Number")),
                }
            )
    diffs.extend(bill_context_diffs(record, spec, receipt_ids))
    text = note_text(spec)
    prior = comment_rows(record)
    already = [row for row in prior if text in str(row.get("html") or "")]
    return {
        "record": record,
        "fingerprint": fingerprint(record),
        "receipts": receipts,
        "diffs": diffs,
        "text": text,
        "already": already,
    }


def append_comment(client: KimcoClient, kimco_id: int, text: str) -> dict[str, Any]:
    payload = comment_payload(kimco_id, text)
    if "values" in payload or "Mention" in json.dumps(payload):
        raise RuntimeError("refusing payload that is not a plain comment add")
    response = client.request("PUT", client._record_url("ap_invoices", kimco_id), json=payload)
    return {
        "http": response.status_code,
        "error": "" if response.status_code < 400 else response.text[:400],
    }


def run() -> dict[str, Any]:
    client = load_live()
    bills = []
    for spec in BILLS:
        gathered = gather(client, spec)
        row: dict[str, Any] = {
            "bill_id": spec["bill_id"],
            "invoice": spec["invoice"],
            "qty_verification": "no" if gathered["diffs"] else "yes",
            "verified": not gathered["diffs"],
            "receipts": gathered["receipts"],
            "differences": gathered["diffs"],
            "new_comment_id": None,
            "saved_text": None,
            "existing_comment_ids": [item["id"] for item in gathered["fingerprint"]["comments"]],
            "existing_comments_kept": None,
            "amount_unchanged": None,
            "batch_unchanged": None,
            "status_unchanged": None,
        }
        if gathered["diffs"]:
            row["discrepancy"] = "; ".join(gathered["diffs"])
            bills.append(row)
            continue
        if gathered["already"]:
            saved = gathered["already"][0]
            row["new_comment_id"] = saved.get("id")
            row["saved_text"] = saved.get("html")
            row["existing_comments_kept"] = True
            row["amount_unchanged"] = True
            row["batch_unchanged"] = True
            row["status_unchanged"] = True
            row["note"] = "already present; no second note written"
            bills.append(row)
            continue
        before = gathered["fingerprint"]
        prior_ids = {item["id"] for item in before["comments"]}
        put = append_comment(client, int(spec["bill_id"]), gathered["text"])
        after_record = client.get_item("ap_invoices", int(spec["bill_id"]))
        after = fingerprint(after_record)
        saved = saved_note(after["comments"], gathered["text"], prior_ids)
        saved_html = None if saved is None else saved.get("html")
        new_id = None if saved is None else saved.get("id")
        drift = unchanged_except_new_comment(before, after, new_id)
        if put["http"] >= 400:
            drift.append(f"comment PUT HTTP {put['http']} {put['error']}")
        for item in gathered["receipts"]:
            receipt = client.get_item("receipts", int(item["id"]))
            values = receipt.get("values") or {}
            if not qty_eq(values.get("Quantity_Received"), item["quantity_received"]):
                drift.append(f"receipt {item['id']} qty changed after the note")
            if not money_eq(values.get("Unit_Cost"), item["unit_cost"]):
                drift.append(f"receipt {item['id']} unit cost changed after the note")
        if saved_html is None:
            drift.append("saved Comments_1 text was not found on readback")
        elif "@" in str(saved_html) or "data-mention-id" in str(saved_html):
            drift.append("saved comment contains a mention")
        elif str(saved_html) != gathered["text"] and str(saved_html) != f"<p>{gathered['text']}</p>":
            drift.append("saved text differs from the plain note")
        row["put_http"] = put["http"]
        row["new_comment_id"] = new_id
        row["saved_text"] = saved_html
        row["existing_comments_kept"] = not any(item.startswith("existing") for item in drift)
        row["amount_unchanged"] = before["amount"] == after["amount"] and before["verification"] == after["verification"]
        row["batch_unchanged"] = before["batch"] == after["batch"]
        row["status_unchanged"] = before["status"] == after["status"] and before["posted"] == after["posted"]
        row["readback_comment_ids"] = [item["id"] for item in after["comments"]]
        if drift:
            row["verified"] = True
            row["qty_verification"] = "yes"
            row["discrepancy"] = "; ".join(drift)
        bills.append(row)
        if drift:
            break
    artifact = {
        "run": "3p-1007044-notes-2026-09-25",
        "host": HOST,
        "vendor": "999-3P INDUSTRIES",
        "posted": False,
        "emails_sent": False,
        "mentions_added": False,
        "receipts_changed": False,
        "prices_changed": False,
        "amounts_changed": False,
        "batches_changed": False,
        "status_changed": False,
        "bills": bills,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(artifact, indent=2) + "\n")
    return artifact


def main() -> None:
    artifact = run()
    for row in artifact["bills"]:
        print(
            f"{row['bill_id']} {row['invoice']} verified={row['qty_verification']} "
            f"comment={row.get('new_comment_id')} discrepancy={row.get('discrepancy')}"
        )


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
