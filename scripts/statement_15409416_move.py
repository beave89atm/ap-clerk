"""Note then move O'Neal statement bill 10475 to TRANSFER AP.

Kyle's later wording is the note that gets saved. The note is confirmed
before the batch change. Does not post, close a batch, or send mail.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import added_comment_payload
from ap_clerk.rules import SHAWN_MENTION_HTML, TREYCE_MENTION_HTML
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("statement-move")

BILL_ID = 10475
INVOICE = "15409416"
TRANSFER_ID = 375
TRANSFER_NAME = "TRANSFER AP"
OUT = ROOT / "runs" / "statement-15409416-check.json"
NOTE = (
    "AP Clerk: @Treyce Hodges this was my mistake. "
    "O'Neal 15409416 is a statement, not an invoice, and I entered it as a bill in error. "
    "Please delete this bill. "
    "@Shawn McKibben please disregard my earlier note on this one; no receiving action needed."
)
MENTION_RE = re.compile(
    r'data-mention-id="(\d+)"[^>]*data-mention-name="([^"]*)"',
    flags=re.I,
)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def note_html() -> str:
    body = NOTE.replace("@Treyce Hodges", TREYCE_MENTION_HTML, 1)
    body = body.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    if body.count('data-mention-id="33"') != 1 or body.count('data-mention-id="104"') != 1:
        raise SystemExit("Note HTML is missing Treyce or Shawn. Not writing.")
    return f"<p>{body}</p>"


def comment_fact(comment: dict[str, Any]) -> dict[str, Any]:
    values = comment.get("values") if isinstance(comment.get("values"), dict) else {}
    html = str(values.get("HtmlValue") or "")
    creator = values.get("CreatorId")
    modifier = values.get("ModifierId")
    return {
        "id": comment.get("id"),
        "text": plain(html),
        "mentions": [
            {"id": int(match.group(1)), "name": match.group(2)}
            for match in MENTION_RE.finditer(html)
        ],
        "created_on": values.get("CreatedOn"),
        "creator_id": creator.get("id") if isinstance(creator, dict) else creator,
        "creator_name": (creator.get("text") or creator.get("name")) if isinstance(creator, dict) else None,
        "modified_on": values.get("ModifiedOn"),
        "modifier_id": modifier.get("id") if isinstance(modifier, dict) else modifier,
    }


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    batch = values.get("AP_Invoice_Batch") or {}
    lists = record.get("lists") or {}
    return {
        "id": record.get("id"),
        "invoice": values.get("Invoice_Number"),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "type": values.get("Invoice_Type"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": batch.get("text") or batch.get("name") if isinstance(batch, dict) else None,
        "lines": len(lists.get("APInvoiceLine") or []),
        "charges": len(lists.get("InvoiceAdditionalCharges") or []),
        "taxes": len(lists.get("APInvoiceTaxCodes") or []),
        "comments": [comment_fact(row) for row in lists.get("Comments_1") or [] if isinstance(row, dict)],
        "read_at": now(),
    }


def unposted(value: Any) -> bool:
    return value in (None, "", False)


def put(client, payload: dict[str, Any]) -> int:
    if payload.get("Posted") not in (None, "", False):
        raise SystemExit("Refusing a payload that posts the bill.")
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if values.get("Posted") not in (None, "", False):
        raise SystemExit("Refusing a payload that posts the bill.")
    _body, status, error = client.update("ap_invoices", BILL_ID, payload)
    LOGGER.info("PUT bill %s HTTP %s", BILL_ID, status)
    if status >= 400:
        LOGGER.info("PUT failed HTTP %s", status)
        raise SystemExit(f"PUT failed HTTP {status}. {error[:180]}")
    return int(status)


def confirm_ready(row: dict[str, Any]) -> None:
    if int(row["id"]) != BILL_ID or str(row["invoice"]) != INVOICE:
        raise SystemExit(f"Bill {row.get('id')} invoice {row.get('invoice')} is not {INVOICE}. Aborting.")
    if not unposted(row.get("posted")) or row.get("void") not in (None, "", False):
        raise SystemExit("Bill is posted or void. Aborting.")


def matching_note(row: dict[str, Any]) -> dict[str, Any] | None:
    for comment in row["comments"]:
        if comment["text"] == NOTE:
            return comment
    return None


def confirm_note(comment: dict[str, Any]) -> None:
    mention_ids = {item["id"] for item in comment["mentions"]}
    if mention_ids != {33, 104}:
        raise SystemExit(f"Note mentions are {sorted(mention_ids)}, not Treyce 33 and Shawn 104. Not moving.")
    if int(comment.get("creator_id") or 0) != 175:
        raise SystemExit("Note author is not API Agent 175. Not moving.")


def main() -> None:
    client = login()
    install_401_guard(client)
    transfer = client.get_item("ap_batches", TRANSFER_ID)
    transfer_name = str((transfer.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if transfer_name != TRANSFER_NAME:
        raise SystemExit(f"Batch {TRANSFER_ID} name is {transfer_name!r}, not {TRANSFER_NAME}. Aborting.")

    before = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_ready(before)
    existing = matching_note(before)
    note_status = "already-present" if existing else "added"
    if existing is None:
        put(client, added_comment_payload(BILL_ID, note_html()))
    after_note = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_ready(after_note)
    if int(after_note["batch_id"] or 0) != int(before["batch_id"] or 0):
        raise SystemExit("Batch changed while the note was saved. Stopping.")
    saved = matching_note(after_note)
    if saved is None:
        raise SystemExit("The new note was not found on readback. Not moving the bill.")
    confirm_note(saved)
    if note_status == "added" and len(after_note["comments"]) != len(before["comments"]) + 1:
        raise SystemExit("Comment count did not increase by one. Not moving the bill.")

    move_status = "already"
    if int(after_note["batch_id"] or 0) != TRANSFER_ID:
        put(
            client,
            {
                "id": BILL_ID,
                "state": "Modified",
                "values": {"AP_Invoice_Batch": {"id": TRANSFER_ID}},
            },
        )
        move_status = "moved"
    after = snapshot(client.get_item("ap_invoices", BILL_ID))
    confirm_ready(after)
    if int(after["batch_id"] or 0) != TRANSFER_ID or after["batch"] != TRANSFER_NAME:
        raise SystemExit(f"Bill is on batch {after['batch_id']} {after['batch']}. Expected 375 TRANSFER AP.")
    final_note = matching_note(after)
    if final_note is None:
        raise SystemExit("The note disappeared after the move.")
    confirm_note(final_note)

    payload = json.loads(OUT.read_text())
    payload["kyle_approval"] = {
        "bill_id": BILL_ID,
        "only_this_bill": True,
        "note_saved_before_move": True,
        "note_wording": (
            "Kyle's follow-up replaced the earlier sentence before any write. "
            "The saved note says this was AP Clerk's mistake."
        ),
        "note_text": NOTE,
        "note_status": note_status,
        "move_status": move_status,
        "transfer_batch": {"id": TRANSFER_ID, "name": transfer_name},
        "before": before,
        "after_note": after_note,
        "after": after,
    }
    target = payload.get("invoice_15409416")
    if isinstance(target, dict) and target.get("id") == BILL_ID:
        target["batch"] = {"id": TRANSFER_ID, "text": TRANSFER_NAME}
        target["posted"] = after["posted"]
        target["comments_after_approval"] = after["comments"]
    notes = payload.get("classification_notes")
    if isinstance(notes, list):
        notes.append(
            "After the read-only check, bill 10475 was noted and then moved to TRANSFER AP batch 375. Bill 10477 was not changed."
        )
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    LOGGER.info(
        "Bill %s note %s then %s to batch %s. Unposted. Comments %s.",
        BILL_ID,
        saved["id"],
        move_status,
        TRANSFER_ID,
        [row["id"] for row in after["comments"]],
    )


if __name__ == "__main__":
    main()
