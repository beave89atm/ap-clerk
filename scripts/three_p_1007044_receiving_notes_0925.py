"""Append one plain receiving-check Comments_1 note where qty fields differ.

Re-reads live 1007044-1 receipts on bills 10306, 10308, 10312, and 10314.
A bill gets one new Comments_1 note when Quantity_Remaining does not equal
Quantity_Received. One sentence per mismatched receipt. No @mention.
Existing notes stay. Receipts, prices, amounts, batches, and status stay.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ap_clerk.kimco import KimcoError
from ap_clerk.rules import lookup_id, lookup_text, money
from three_p_1007044_notes_0925 import (
    BILLS,
    PART,
    comment_payload,
    fingerprint,
    load_live,
    part_is_1007044,
    qty_eq,
    saved_note,
    unchanged_except_new_comment,
)

OUT_JSON = ROOT / "artifacts" / "3p-1007044-receiving-notes-2026-09-25.json"
HOST = "https://live.kimcoerp.com"


def qty_text(qty: float | None) -> str:
    if qty is None:
        return "blank"
    if abs(qty - round(qty)) < 0.001:
        return str(int(round(qty)))
    return f"{qty:.2f}"


def po_number(text: str | None) -> str:
    match = re.search(r"(\d{4,})", str(text or ""))
    return match.group(1) if match else ""


def receipt_sentence(row: dict[str, Any]) -> str:
    return (
        "AP Clerk: Receiving check on part 1007044-1: "
        f"receipt {row['id']} shows Quantity_Received {qty_text(row['quantity_received'])} "
        f"but Quantity_Remaining {qty_text(row['quantity_remaining'])} on PO {row['po']}. "
        "These should line up. Please verify the receiving record for this line "
        "when unreceiving/re-receiving at the corrected price."
    )


def note_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        raise RuntimeError("receiving note requires at least one receipt")
    text = " ".join(receipt_sentence(row) for row in rows)
    if not text.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if "@" in text or "data-mention" in text or "Shawn" in text:
        raise RuntimeError("note must not mention or notify")
    return text


def mismatches(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in receipts if not qty_eq(row.get("quantity_received"), row.get("quantity_remaining"))]


def append_comment(client: Any, kimco_id: int, text: str) -> dict[str, Any]:
    payload = comment_payload(kimco_id, text)
    if "values" in payload or "Mention" in json.dumps(payload):
        raise RuntimeError("refusing payload that is not a plain comment add")
    response = client.request("PUT", client._record_url("ap_invoices", kimco_id), json=payload)
    return {
        "http": response.status_code,
        "error": "" if response.status_code < 400 else response.text[:400],
    }


def live_receipts(client: Any, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    rows = []
    problems = []
    for po_row in spec["pos"]:
        for item in po_row["receipts"]:
            rid = int(item["id"])
            receipt = client.get_item("receipts", rid)
            values = receipt.get("values") or {}
            po_line_id = lookup_id(values.get("PO_Item_Number"))
            purchase_line = client.get_item("purchase_lines", int(po_line_id)) if po_line_id not in (None, "") else {}
            if not part_is_1007044(purchase_line):
                problems.append(f"receipt {rid} PO line is not {PART}")
            po = po_number(lookup_text(values.get("PO_Number")))
            if po != str(po_row["po"]):
                problems.append(f"receipt {rid} PO {po} != {po_row['po']}")
            rows.append(
                {
                    "id": rid,
                    "po": po,
                    "quantity_received": money(values.get("Quantity_Received")),
                    "quantity_remaining": money(values.get("Quantity_Remaining")),
                    "unit_cost": money(values.get("Unit_Cost")),
                }
            )
    return rows, problems


def run() -> dict[str, Any]:
    client = load_live()
    bills = []
    for spec in BILLS:
        record = client.get_item("ap_invoices", int(spec["bill_id"]))
        before = fingerprint(record)
        rows, problems = live_receipts(client, spec)
        bad = mismatches(rows)
        entry: dict[str, Any] = {
            "bill_id": spec["bill_id"],
            "invoice": spec["invoice"],
            "receipts": rows,
            "mismatches": bad,
            "noted": False,
            "left_alone": not bad and not problems,
            "new_comment_id": None,
            "saved_text": None,
            "existing_comment_ids": [item["id"] for item in before["comments"]],
            "problems": problems,
        }
        if problems or not bad:
            if problems:
                entry["discrepancy"] = "; ".join(problems)
            bills.append(entry)
            continue
        text = note_text(bad)
        prior_ids = {item["id"] for item in before["comments"]}
        already = [item for item in before["comments"] if text in str(item.get("html") or "")]
        if already:
            entry["noted"] = True
            entry["left_alone"] = True
            entry["new_comment_id"] = already[0].get("id")
            entry["saved_text"] = already[0].get("html")
            entry["note"] = "already present; no second note written"
            bills.append(entry)
            continue
        put = append_comment(client, int(spec["bill_id"]), text)
        after_record = client.get_item("ap_invoices", int(spec["bill_id"]))
        after = fingerprint(after_record)
        saved = saved_note(after["comments"], text, prior_ids)
        saved_html = None if saved is None else saved.get("html")
        new_id = None if saved is None else saved.get("id")
        drift = unchanged_except_new_comment(before, after, new_id)
        if put["http"] >= 400:
            drift.append(f"comment PUT HTTP {put['http']} {put['error']}")
        fresh, _fresh_problems = live_receipts(client, spec)
        for old, new in zip(rows, fresh):
            if old["quantity_received"] != new["quantity_received"] or old["quantity_remaining"] != new["quantity_remaining"]:
                drift.append(f"receipt {old['id']} qty changed after the note")
            if old["unit_cost"] != new["unit_cost"]:
                drift.append(f"receipt {old['id']} unit cost changed after the note")
        if saved_html is None:
            drift.append("saved Comments_1 text was not found on readback")
        elif "@" in str(saved_html) or "data-mention-id" in str(saved_html):
            drift.append("saved comment contains a mention")
        elif str(saved_html) != text and str(saved_html) != f"<p>{text}</p>":
            drift.append("saved text differs from the plain note")
        entry["noted"] = saved_html is not None and not drift
        entry["left_alone"] = False
        entry["put_http"] = put["http"]
        entry["new_comment_id"] = new_id
        entry["saved_text"] = saved_html
        entry["existing_comments_kept"] = not any(item.startswith("existing") for item in drift)
        entry["readback_comment_ids"] = [item["id"] for item in after["comments"]]
        if drift:
            entry["discrepancy"] = "; ".join(drift)
        bills.append(entry)
        if drift:
            break
    artifact = {
        "run": "3p-1007044-receiving-notes-2026-09-25",
        "host": HOST,
        "posted": False,
        "emails_sent": False,
        "mentions_added": False,
        "bills": bills,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(artifact, indent=2) + "\n")
    return artifact


def main() -> None:
    artifact = run()
    for row in artifact["bills"]:
        print(
            f"{row['bill_id']} {row['invoice']} noted={row['noted']} "
            f"comment={row.get('new_comment_id')} left_alone={row['left_alone']} "
            f"discrepancy={row.get('discrepancy')}"
        )


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
