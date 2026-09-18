"""NOTE-40 Transfer AP + @Shawn payload helpers.

Pure builders. invent=false. dry_run never PUT/PATCH/POST an invoice.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ap_clerk.rules import SHAWN_MCKIBBEN, TRANSFER_AP_BATCH_NAME, money

LOGGER = logging.getLogger("ap_clerk.transfer_ap")

# Prior live fact only. Name lookup wins. Never a hard fallback.
TRANSFER_AP_PRIOR_ID_HINT = 375

_SELECT_RECEIPTS_KEYS = frozenset(
    {
        "receipt",
        "receipts",
        "receipt_id",
        "receipt_ids",
        "apinvoiceline",
        "lists",
        "select_receipts",
        "wanted",
        "matched",
    }
)


def over_ppv_hold_comment(
    *,
    invoice_number: str,
    po: str | None,
    pdf_amount: Any,
    vendor: str = "",
) -> str:
    """KIMCO Comments text for a new over-PPV HOLD. Always @tags Shawn."""
    amt = money(pdf_amount)
    amt_txt = f"{amt:.2f}" if amt is not None else "unknown"
    inv = str(invoice_number or "").strip()
    vendor_bit = f" {vendor.strip()}" if (vendor or "").strip() else ""
    return (
        f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on{vendor_bit} {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Leftover vs invoice line is over the "
        "PPV gate. Receipts were NOT selected so purchasing can unreceive, "
        "change the PO price, and re-receive. Do not alter receipt unit price in GI."
    )


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
        return {
            "found": True,
            "id": hits[0]["id"],
            "name": hits[0]["name"],
            "invent": False,
        }
    if len(hits) > 1:
        return {"found": False, "ambiguous": True, "hits": hits, "invent": False}
    return {
        "found": False,
        "id": None,
        "name": None,
        "invent": False,
        "hint_ignored": TRANSFER_AP_PRIOR_ID_HINT,
    }


def build_over_ppv_transfer_ap_payload(
    *,
    kimco_id: int,
    batch_id: int,
    comment: str,
) -> dict[str, Any]:
    """Exact PUT body for NOTE-40. Does not write. No Select Receipts keys."""
    if not comment or SHAWN_MCKIBBEN not in comment:
        raise ValueError("NOTE-40 Comments must include @Shawn McKibben")
    if "not selected" not in comment.lower():
        raise ValueError("NOTE-40 Comments must say receipts were NOT selected")
    return {
        "state": "Modified",
        "id": int(kimco_id),
        "values": {
            "AP_Invoice_Batch": {"id": int(batch_id)},
            "Comments": comment,
        },
    }


def payload_select_receipts_keys(payload: dict[str, Any]) -> list[str]:
    """Return any Select Receipts–like keys. Empty means payload is header-only."""
    found: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_s = str(key)
                here = f"{path}.{key_s}" if path else key_s
                if key_s.casefold() in _SELECT_RECEIPTS_KEYS:
                    found.append(here)
                _walk(value, here)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                _walk(value, f"{path}[{idx}]")

    _walk(payload, "")
    return found


def log_dry_run_payload(payload: dict[str, Any]) -> None:
    LOGGER.info(
        "NOTE-40 dry-run PUT body (not sent): %s",
        json.dumps(payload, indent=2, default=str),
    )
