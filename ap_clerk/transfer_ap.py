"""NOTE-53: missing_receipt HOLD → Transfer AP (same move path as over-PPV).

Supersedes the 2026-09-21 rule that left missing_receipt on the current
API Agent batch. Over-PPV / price_variance still Transfer AP + @Shawn
(unchanged). NOTE-58 uses this same move for a quantity/UOM disconnect
whose dollar gap is over the PPV limit. invent=false. Never invent batch
id 375 — lookup by name. The missing_receipt helper does not rewrite
Comments / Comments_1. The quantity/UOM helper adds one @Shawn Comments_1
note. Neither posts the bill. Outlook categories are unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from ap_clerk.gates import GATE_PRICE, GATE_RECEIPT, RESULT_HOLD
from ap_clerk.kimco import KimcoError
from ap_clerk.quality_v12 import classify_exception
from ap_clerk.rules import (
    SHAWN_USER_ID,
    TRANSFER_AP_BATCH_NAME,
    comments_1_html,
    lookup_id,
)

LOGGER = logging.getLogger("ap_clerk.transfer_ap")

# Live GET fact: Transfer AP is id 375. Hint only — never invent this id.
TRANSFER_AP_PRIOR_ID_HINT = 375

MOVE_MOVED = "moved"
MOVE_ALREADY = "already-on-transfer-ap"
MOVE_BATCH_NOT_FOUND = "batch-not-found"
MOVE_BATCH_LIST_FAILED = "batch-list-failed"
MOVE_SKIPPED = "skipped"


def find_transfer_ap_batch(batches: list[dict[str, Any]]) -> dict[str, Any]:
    """Lookup Transfer AP by name. Never invent id 375."""
    hits: list[dict[str, Any]] = []
    wanted = TRANSFER_AP_BATCH_NAME.casefold()
    for item in batches or []:
        vals = item.get("values") if isinstance(item.get("values"), dict) else {}
        name = str(
            (vals or {}).get("AP_Invoice_Batch_ID")
            or (vals or {}).get("Name")
            or item.get("name")
            or ""
        ).strip()
        if name.casefold() != wanted:
            continue
        if item.get("id") in (None, ""):
            continue
        hits.append({"id": int(item["id"]), "name": name})
    if len(hits) == 1:
        return {"found": True, "id": hits[0]["id"], "name": hits[0]["name"], "invent": False}
    if len(hits) > 1:
        return {"found": False, "ambiguous": True, "hits": hits, "invent": False}
    return {
        "found": False,
        "id": None,
        "name": None,
        "invent": False,
        "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
    }


def should_transfer_ap_missing_receipt(
    *,
    result: str | None = None,
    category: str | None = None,
    why: str | None = None,
    issue_gate: str | None = None,
) -> bool:
    """NOTE-53: HOLD missing_receipt → Transfer AP. Not over-PPV. Not Success."""
    cat = str(category or "").strip().lower()
    classified = classify_exception(result=result or RESULT_HOLD, why=str(why or ""))
    # NOTE-55: open-receipt qty / already-billed is not a missing_receipt move.
    if classified and classified[0] in {"quantity_variance", "already_entered"}:
        return False
    if cat in {
        "price_variance",
        "missing_po",
        "partial_match",
        "quantity_variance",
        "already_entered",
    }:
        return False
    if issue_gate == GATE_PRICE:
        return False
    if cat == "missing_receipt":
        return True
    if classified and classified[0] == "missing_receipt":
        return True
    if issue_gate == GATE_RECEIPT:
        why_l = str(why or "").lower()
        if "multiple open receipts" in why_l or "will not guess" in why_l:
            return False
        return (
            "no receipts" in why_l
            or "no open receipt" in why_l
            or "parts not received" in why_l
        )
    return False


def apply_transfer_ap_batch_move(
    client: Any,
    *,
    kimco_id: int,
    comment: str | None = None,
) -> dict[str, Any]:
    """PUT AP_Invoice_Batch to Transfer AP. Lookup by name. invent=false.

    Same move path as over-PPV. Comment is optional — omit to keep existing
    header Comments / Comments_1 child items unchanged.
    """
    try:
        batches = client.list_items("ap_batches")
    except (KimcoError, AttributeError, TypeError) as exc:
        return {
            "status": MOVE_BATCH_LIST_FAILED,
            "error": str(exc)[:240],
            "invent": False,
            "kimco_id": int(kimco_id),
        }
    found = find_transfer_ap_batch(list(batches or []))
    if not found.get("found"):
        return {
            "status": MOVE_BATCH_NOT_FOUND,
            "lookup": found,
            "invent": False,
            "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
            "kimco_id": int(kimco_id),
        }
    bid = int(found["id"])
    try:
        before = client.get_item("ap_invoices", int(kimco_id))
    except (KimcoError, AttributeError, TypeError):
        before = {}
    vals = before.get("values") if isinstance(before.get("values"), dict) else {}
    live_bid = lookup_id((vals or {}).get("AP_Invoice_Batch"))
    if live_bid == bid:
        return {
            "status": MOVE_ALREADY,
            "batch_id": bid,
            "batch_name": found["name"],
            "invent": False,
            "kimco_id": int(kimco_id),
        }
    values: dict[str, Any] = {"AP_Invoice_Batch": {"id": bid}}
    if comment:
        values["Comments"] = comment
    try:
        _body, status, error = client.update(
            "ap_invoices",
            int(kimco_id),
            {"state": "Modified", "id": int(kimco_id), "values": values},
        )
    except (KimcoError, AttributeError, TypeError) as exc:
        return {
            "status": "put-failed",
            "error": str(exc)[:240],
            "invent": False,
            "kimco_id": int(kimco_id),
        }
    try:
        after = client.get_item("ap_invoices", int(kimco_id))
    except (KimcoError, AttributeError, TypeError):
        after = {}
    after_vals = after.get("values") if isinstance(after.get("values"), dict) else {}
    after_bid = lookup_id((after_vals or {}).get("AP_Invoice_Batch"))
    ok = after_bid == bid or (status is not None and int(status) < 400)
    return {
        "status": MOVE_MOVED if ok else f"put-{status}",
        "batch_id": bid,
        "batch_name": found["name"],
        "http": status,
        "error": error,
        "invent": False,
        "kimco_id": int(kimco_id),
        "comment_written": bool(comment),
    }


def should_transfer_ap_qty_uom(decision: dict[str, Any] | None) -> bool:
    """NOTE-58: qty/UOM disconnect over the PPV limit → Transfer AP."""
    if not isinstance(decision, dict):
        return False
    return bool(decision.get("hold") and decision.get("transfer_ap"))


def post_shawn_comments_1(client: Any, kimco_id: int, note: str) -> dict[str, Any]:
    """Add one Comments_1 note with the existing @Shawn mention span (user id 104)."""
    html = comments_1_html(note, mention_id=SHAWN_USER_ID)
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {
            "Comments_1": [
                {
                    "state": "Added",
                    "values": {
                        "HtmlValue": html,
                        "Entity": {"id": 203},
                        "ObjectId": int(kimco_id),
                        "FormId": 218,
                    },
                }
            ]
        },
    }
    updater = getattr(client, "update", None)
    if updater is None:
        return {"status": "skipped", "invent": False, "kimco_id": int(kimco_id)}
    try:
        _body, status, error = updater("ap_invoices", int(kimco_id), payload)
    except (KimcoError, AttributeError, TypeError) as exc:
        return {
            "status": "blocked",
            "error": str(exc)[:240],
            "invent": False,
            "kimco_id": int(kimco_id),
        }
    return {
        "status": "persisted" if status is not None and int(status) < 400 else f"put-{status}",
        "put": status,
        "error": error,
        "invent": False,
        "kimco_id": int(kimco_id),
        "mention_id": SHAWN_USER_ID,
    }


def apply_qty_uom_transfer_ap(
    client: Any,
    *,
    kimco_id: int,
    note: str,
) -> dict[str, Any]:
    """NOTE-58: same Transfer AP PUT as missing_receipt, plus one @Shawn note.

    Does not post the bill. Does not select receipts.
    """
    moved = apply_transfer_ap_batch_move(client, kimco_id=int(kimco_id))
    comment = post_shawn_comments_1(client, int(kimco_id), note)
    LOGGER.info(
        "NOTE-58 qty/UOM Transfer AP kimco_id=%s move=%s comment=%s",
        kimco_id,
        moved.get("status"),
        comment.get("status"),
    )
    return {"move": moved, "comment": comment}


def apply_missing_receipt_transfer_ap(
    client: Any,
    *,
    kimco_id: int,
) -> dict[str, Any]:
    """NOTE-53 wrapper: move missing_receipt HOLD to Transfer AP.

    Does not rewrite Comments or Outlook. Does not Select leftovers.
    Does not invent Success.
    """
    out = apply_transfer_ap_batch_move(client, kimco_id=int(kimco_id))
    LOGGER.info(
        "NOTE-53 missing_receipt Transfer AP kimco_id=%s status=%s batch=%s",
        kimco_id,
        out.get("status"),
        out.get("batch_id"),
    )
    return out
