"""Second sign-in for the 2026-10-09 fixes the first pass skipped.

Finishes bill 10569, adds the missing Treyce note on posted bill 10491,
and replaces the inaccurate 10472 note in place. Reads PO 7139 on bill
10590 and clears that link only when the live purchase order is not O'Neal.
Does not post, delete, send mail, or touch the archived email.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import KimcoError, replace_comment_payload
from ap_clerk.rules import ap_clerk_edit_note, lookup_id, lookup_text
from scripts.fixes_2026_10_09 import (
    ONEAL_VENDOR,
    OUT,
    ROWS,
    TRANSFER_ID,
    add_note,
    anthony_html,
    brief,
    fix_10569,
    note_rows,
    posted,
    read_bill,
    record_row,
    require_author,
    snapshot,
)
from scripts.sept25_30_attachment_audit import receipt_fact
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.ap_run_2026_10_08_enter import uom_text
from scripts.sept_missed_entry_2026_10_07 import put

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("fixes-1009-finish")

FIELDS = ["bill", "change", "before", "after", "note id + text", "readback ok y/n"]


def safe_fields(values: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.lower()
        if not any(word in lowered for word in ("vendor", "po", "purchase", "display", "name")):
            continue
        if isinstance(value, dict):
            kept[key] = {"id": value.get("id"), "text": value.get("text") or value.get("name")}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            kept[key] = value
    return kept


def clarify_10472(client) -> None:
    record, before = read_bill(client, 10472)
    before_text = brief(before)
    notes = note_rows(record)
    target = next((note for note in notes if int(note.get("id") or 0) == 1818), None)
    if target is None:
        target = next((note for note in notes if "orphan from a crashed run" in note["text"]), None)
    receipts = ",".join(str(line["receipt_id"]) for line in before["lines"] if line.get("receipt_id")) or "none"
    text = (
        "AP Clerk: @Treyce Hodges Xcaliber invoice WB4337861607 is an orphan from a crashed run. "
        "The 10/6 catch-up stopped on this invoice when the response ended prematurely, and no note was saved. "
        f"Live read: the bill is posted, the batch is blank, the header is ${before['verification']}, "
        f"and receipt {receipts} quantity covers ${before['covered']} on {before['po'] or 'PO 59113'}. "
        "It is not an unposted hold. I did not change the coding."
    )
    html = ap_clerk_edit_note(text, action="treyce")
    if target is None:
        saved = add_note(client, 10472, html, "not an unposted hold")
    else:
        status = put(client, 10472, replace_comment_payload(10472, int(target["id"]), html))
        if status >= 400:
            record_row(10472, f"note replace HTTP {status}", before_text, before_text, target["id"], target["text"], False)
            return
        after_notes = note_rows(client.get_item("ap_invoices", 10472))
        saved = next((note for note in after_notes if int(note.get("id") or 0) == int(target["id"])), None)
    after = snapshot(client.get_item("ap_invoices", 10472))
    same = (
        after["invoice"] == before["invoice"]
        and after["batch_id"] == before["batch_id"]
        and after["lines"] == before["lines"]
        and after["posted"] == before["posted"]
        and after["verification"] == before["verification"]
    )
    ok = (
        bool(saved)
        and require_author(saved)
        and 33 in saved["mentions"]
        and "not an unposted hold" in saved["text"]
        and "held because it is posted" not in saved["text"]
        and same
        and posted(after["posted"])
    )
    record_row(10472, "note only; posted orphan from crashed run", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)


def note_10491(client) -> None:
    record, before = read_bill(client, 10491)
    before_text = brief(before)
    receipt = client.get_item("receipts", 24982)
    fact = receipt_fact(receipt)
    fact["uom"] = uom_text(receipt)
    values = receipt.get("values") or {}
    po = lookup_text(values.get("PO_Number") or values.get("Purchase_Order"))
    selected = [line for line in before["lines"] if int(line.get("receipt_id") or 0) == 24982]
    part = str(fact.get("part") or "")
    matches = (
        before["invoice"] == "18564"
        and posted(before["posted"])
        and selected
        and "A-04421-000" in part
        and "58931" in str(po)
        and abs(float(fact.get("qty") or 0) - 66) < 0.02
        and any(abs(float(line.get("qty") or 0) - 4) < 0.02 for line in selected)
    )
    if not matches:
        record_row(
            10491,
            "not noted; live receipt 24982 did not match the approved facts",
            before_text,
            f"invoice={before['invoice']} posted={before['posted']} line_qty={[line.get('qty') for line in selected]} "
            f"receipt_qty={fact.get('qty')} {fact.get('uom')} part={part} po={po}",
            "",
            "",
            False,
        )
        return
    text = (
        "AP Clerk: @Treyce Hodges this posted bill selected receipt 24982 (66 ea A-04421-000, PO 58931) "
        "that belongs to invoice 18664, and its quantity was changed to 4. "
        "It needs a posted-bill adjustment so 18664 (bill 10555) can be matched."
    )
    saved = add_note(client, 10491, ap_clerk_edit_note(text, action="treyce"), "posted-bill adjustment")
    after = snapshot(client.get_item("ap_invoices", 10491))
    same = after["lines"] == before["lines"] and after["batch_id"] == before["batch_id"] and after["invoice"] == before["invoice"]
    ok = (
        bool(saved)
        and require_author(saved)
        and 33 in saved["mentions"]
        and "posted-bill adjustment" in saved["text"]
        and same
        and posted(after["posted"])
    )
    record_row(10491, "note only; posted receipt 24982", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)


def review_po_7139(client) -> None:
    record, before = read_bill(client, 10590)
    before_text = brief(before)
    if before["invoice"] != "15492850" or before["vendor_id"] != ONEAL_VENDOR:
        record_row(10590, "PO link left; bill is not O'Neal 15492850", before_text, before_text, "", "", False)
        return
    details = []
    vendor_ids: set[int] = set()
    names: list[str] = []
    for receipt_id in (24136, 24558):
        receipt = client.get_item("receipts", receipt_id)
        values = receipt.get("values") or {}
        fact = receipt_fact(receipt)
        fact["uom"] = uom_text(receipt)
        fields = safe_fields(values)
        line_id = fact.get("po_line_id")
        line_fields: dict[str, Any] = {}
        if line_id:
            line = client.get_item("purchase_lines", int(line_id))
            line_fields = safe_fields(line.get("values") or {})
            vendor = lookup_id((line.get("values") or {}).get("Vendor"))
            if vendor:
                vendor_ids.add(int(vendor))
            name = lookup_text((line.get("values") or {}).get("Purchase_Order_Number")) or ""
            if name:
                names.append(name)
        vendor = lookup_id(values.get("Vendor"))
        if vendor:
            vendor_ids.add(int(vendor))
        po_name = lookup_text(values.get("PO_Number") or values.get("Purchase_Order")) or ""
        if po_name:
            names.append(po_name)
        details.append(
            f"{receipt_id} qty {fact.get('qty')} {fact.get('uom')} po {po_name} line {line_id} fields {fields} line_fields {line_fields}"
        )
        LOGGER.info("PO review %s", details[-1])
    header_po = str(before.get("po") or "")
    blob = " ".join([header_po, *names]).upper()
    oneal = "ONEAL" in blob or "O'NEAL" in blob
    crosslink = "CROSSLINK" in blob
    foreign = (vendor_ids and ONEAL_VENDOR not in vendor_ids) or (crosslink and not oneal)
    LOGGER.info("PO 7139 vendors=%s names=%s foreign=%s", sorted(vendor_ids), names, foreign)
    if not foreign:
        record_row(10590, "PO link left; live PO was not a different vendor", before_text, before_text + " | " + " || ".join(details), "", "", True)
        return
    if posted(before["posted"]):
        record_row(10590, "PO link left; bill is posted", before_text, before_text, "", "", False)
        return
    if int(before.get("po_id") or 0) != 7139:
        record_row(10590, f"PO link left; header PO id is {before.get('po_id')}", before_text, before_text, "", "", True)
        return
    fresh, live = read_bill(client, 10590)
    if posted(live["posted"]) or int(live.get("po_id") or 0) != 7139 or live["lines"]:
        record_row(10590, "PO link left; bill changed before the clear", before_text, brief(live), "", "", False)
        return
    status = put(client, 10590, {"id": 10590, "state": "Modified", "values": {"Purchase_Order": None}})
    if status >= 400:
        record_row(10590, f"PO clear HTTP {status}", before_text, brief(snapshot(client.get_item("ap_invoices", 10590))), "", "", False)
        return
    vendor_name = "Crosslink Powder Coating" if crosslink else "a different vendor"
    text = (
        "AP Clerk: @Anthony @Shawn McKibben the printed customer PO 59137 on O'Neal invoice 15492850 "
        f"is not an O'Neal purchase order. KIMCO purchase order 59137 is {vendor_name}, "
        "so I removed that purchase-order link. Receipts 24136 and 24558 belong to that other purchase order "
        "and were not selected. No O'Neal receipt is on file for the 8 pieces of 3/8 plate and 8 pieces of 1/4 plate. "
        "The bill stays in Transfer AP and is not posted."
    )
    saved = add_note(client, 10590, anthony_html(text), "removed that purchase-order link")
    after = snapshot(client.get_item("ap_invoices", 10590))
    ok = (
        after["po_id"] in (None, "")
        and not after["lines"]
        and not posted(after["posted"])
        and int(after.get("batch_id") or 0) == TRANSFER_ID
        and after["invoice"] == "15492850"
        and bool(saved)
        and require_author(saved)
        and 104 in saved["mentions"]
        and "@Anthony" in saved["text"]
    )
    record_row(
        10590,
        "cleared Crosslink PO 59137 link; still on hold in Transfer AP",
        before_text,
        brief(after),
        (saved or {}).get("id"),
        (saved or {}).get("text") or "",
        ok,
    )


def merge_csv() -> None:
    path = OUT / "fixes.csv"
    existing = list(csv.DictReader(path.open()))
    updates = {row["bill"]: row for row in ROWS}
    merged = []
    seen: set[str] = set()
    for row in existing:
        bill = row["bill"]
        if bill in updates and bill not in seen:
            merged.append(updates[bill])
            seen.add(bill)
        elif bill in updates:
            continue
        else:
            merged.append(row)
    for bill, row in updates.items():
        if bill not in seen:
            merged.append(row)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(merged)
    (OUT / "fixes.json").write_text(json.dumps(merged, indent=2))


def main() -> None:
    client = login()
    install_401_guard(client)
    fix_10569(client)
    clarify_10472(client)
    note_10491(client)
    review_po_7139(client)
    merge_csv()
    LOGGER.info("Finish rows %s", json.dumps(ROWS))


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KimcoError) as exc:
        LOGGER.info("Stopped: %s", exc)
        if ROWS:
            merge_csv()
        raise
