"""Re-read receipts on PO 59081. Read-only. Does not change the attachment audit.

The first pass treated purchase-order id 7083 as a match on any PO-like
lookup, which collided with an unrelated PO line id. This pass keeps a
receipt only when its PO_Number is 59081.
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

from scripts.sept25_30_attachment_audit import PO_JSON, is_kit, receipt_fact, stamp_text
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("po59081-receipts")

PO_ID = 7083
PO_TEXT = re.compile(r"\b59081\b")


def on_po(item: dict[str, Any]) -> bool:
    values = item.get("values") if isinstance(item.get("values"), dict) else {}
    po = values.get("PO_Number") or values.get("Purchase_Order")
    if isinstance(po, dict):
        if str(po.get("id") or "") == str(PO_ID):
            return True
        if PO_TEXT.search(str(po.get("text") or "")):
            return True
    elif PO_TEXT.search(stamp_text(po)):
        return True
    for key in ("Name", "Display_Name"):
        if PO_TEXT.search(stamp_text(values.get(key) or item.get(key))):
            return True
    return False


def child_receipt_ids(record: dict[str, Any]) -> list[int]:
    found: list[int] = []
    for row in (record.get("lists") or {}).get("poLineReceiptsForCounting") or []:
        if not isinstance(row, dict):
            continue
        for candidate in (row.get("id"), (row.get("values") or {}).get("Receipt")):
            if isinstance(candidate, dict):
                candidate = candidate.get("id")
            if candidate in (None, ""):
                continue
            try:
                found.append(int(candidate))
            except (TypeError, ValueError):
                continue
    return found


def main() -> None:
    client = login()
    install_401_guard(client)
    payload = json.loads(PO_JSON.read_text())
    line_ids = [int(line["id"]) for line in payload["lines"]]
    sample = client.get_item("purchase_lines", line_ids[0])
    sample_rows = (sample.get("lists") or {}).get("poLineReceiptsForCounting") or []
    LOGGER.info(
        "Line %s receipt-count rows %s keys %s",
        line_ids[0],
        len(sample_rows),
        sorted(sample_rows[0]) if sample_rows and isinstance(sample_rows[0], dict) else [],
    )
    if sample_rows and isinstance(sample_rows[0], dict):
        LOGGER.info("First receipt-count value keys: %s", sorted((sample_rows[0].get("values") or {})))
    ids: list[int] = []
    ids.extend(child_receipt_ids(sample))
    for line_id in line_ids[1:]:
        ids.extend(child_receipt_ids(client.get_item("purchase_lines", line_id)))
    if not ids:
        rows = client.list_items("receipts")
        wanted = {str(line_id) for line_id in line_ids}
        for row in rows:
            values = row.get("values") if isinstance(row.get("values"), dict) else {}
            pointer = values.get("PO_Item_Number")
            pointer_id = pointer.get("id") if isinstance(pointer, dict) else pointer
            if str(pointer_id or "") in wanted and row.get("id") not in (None, ""):
                ids.append(int(row["id"]))
        LOGGER.info("Receipt list hits by PO line id: %s", len(ids))
    ids = list(dict.fromkeys(ids))
    LOGGER.info("Receipt ids to read: %s", len(ids))
    receipts = []
    for receipt_id in ids:
        try:
            record = client.get_item("receipts", receipt_id)
        except Exception as exc:
            LOGGER.info("Receipt GET %s failed: %s", receipt_id, type(exc).__name__)
            continue
        fact = receipt_fact(record)
        po = (fact.get("fields") or {}).get("PO_Number") or {}
        text = po.get("text") if isinstance(po, dict) else stamp_text(po)
        po_match = (isinstance(po, dict) and str(po.get("id") or "") == str(PO_ID)) or bool(PO_TEXT.search(str(text or "")))
        if not po_match:
            LOGGER.info("Skipped receipt %s PO %s", receipt_id, text)
            continue
        fact["kit"] = is_kit(fact.get("part"), fact.get("description"), json.dumps(fact.get("fields"), default=str))
        receipts.append(fact)
    payload = json.loads(PO_JSON.read_text())
    kit_receipts = [row for row in receipts if row["kit"]]
    amount_hits = [row for row in receipts if row.get("extended") == 213.93]
    payload["receipt_count"] = len(receipts)
    payload["receipts"] = receipts
    payload["kit_on_receipt"] = bool(kit_receipts)
    payload["kit_receipts"] = [
        {"id": row["id"], "part": row["part"], "description": row["description"], "extended": row["extended"]}
        for row in kit_receipts
    ]
    payload["receipts_at_213_93"] = [
        {"id": row["id"], "part": row["part"], "description": row["description"], "extended": row["extended"]}
        for row in amount_hits
    ]
    payload["receipt_list_note"] = (
        "Receipts are included only when PO_Number is PO 59081 (id 7083). "
        "A PO line id that happens to equal 7083 is not a match."
    )
    PO_JSON.write_text(json.dumps(payload, indent=2, default=str))
    LOGGER.info("Kept %s receipts. Kit receipts %s. Amount 213.93 %s.", len(receipts), len(kit_receipts), len(amount_hits))


if __name__ == "__main__":
    main()
