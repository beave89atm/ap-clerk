"""Type 4 Misc Lines-K / Add Item (no Receipt). invent=false.

Select Receipts requires Receipt.id and must not invent typed merchandise.
Kyle 2026-09-18: miscellaneous invoices need Lines-K rows (description,
quantity, cost) plus a live-looked-up category. Proven live Type 4 shape
from GET 9966 / 9970 (Treyce): MFG_Miscellaneous_Item + Misc_Description
+ Quantity + Unit_Price. Fuel surcharge stays Additional Charge Fees.
"""

from __future__ import annotations

import re
from typing import Any

from ap_clerk.kimco import KimcoError

SHOP_SUPPLIES_GS_RE = re.compile(
    r"shop\s*supplies\s*[-./]?\s*g\s*[&+]?\s*s",
    flags=re.I,
)

# Live GET 2026-09-18 on Type 4 Gas bills 9966 / 9970. Hint only — lookup
# by name on live records; never invent this id if the name does not match.
SHOP_SUPPLIES_GS_ITEM_HINT = 31
SHOP_SUPPLIES_GS_GL_HINT = 200


def is_shop_supplies_gs_name(text: Any) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if SHOP_SUPPLIES_GS_RE.search(raw):
        return True
    compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
    return "shopsuppliesgs" in compact


def lookup_id_text(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    iid = raw.get("id")
    if iid in (None, ""):
        return None
    return {"id": int(iid), "text": raw.get("text")}


def pick_unique_lookup(candidates: list[Any], *, label: str) -> dict[str, Any]:
    by_id: dict[int, dict[str, Any]] = {}
    for raw in candidates:
        item = lookup_id_text(raw)
        if item is None:
            continue
        by_id[item["id"]] = item
    if len(by_id) != 1:
        raise KimcoError(f"{label} live name lookup not unique: {list(by_id.values())}")
    return next(iter(by_id.values()))


def collect_shop_supplies_gs_from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Read MFG_Miscellaneous_Item / Purchase_GL_Account from Type 4 lines.

    Only keep lookups whose live text matches shop supplies - g&s.
    """
    items: list[dict[str, Any]] = []
    gls: list[dict[str, Any]] = []
    for record in records or []:
        lists = record.get("lists") if isinstance(record, dict) else {}
        for line in (lists or {}).get("APInvoiceLine") or []:
            values = (line.get("values") if isinstance(line, dict) else None) or {}
            misc = values.get("MFG_Miscellaneous_Item")
            if is_shop_supplies_gs_name((misc or {}).get("text") if isinstance(misc, dict) else ""):
                items.append(misc)
            gl = values.get("Purchase_GL_Account")
            if is_shop_supplies_gs_name((gl or {}).get("text") if isinstance(gl, dict) else ""):
                gls.append(gl)
    item = pick_unique_lookup(items, label="MFG_Miscellaneous_Item shop supplies - g&s")
    gl = pick_unique_lookup(gls, label="Purchase_GL_Account shop supplies - g&s") if gls else None
    return {"misc_item": item, "gl_account": gl}


def misc_line_description(line: dict[str, Any]) -> str:
    part = str(line.get("part") or "").strip()
    desc = str(line.get("description") or line.get("label") or "").strip()
    if part and desc and part.lower() not in desc.lower():
        return f"{part} {desc}"[:160]
    return (desc or part)[:160]


def misc_add_item_payload(
    lines: list[dict[str, Any]],
    *,
    invoice_id: int,
    vendor_id: int,
    misc_item: dict[str, Any],
    gl_account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record PUT body for Type 4 Lines-K Add Item. No Receipt. No fees rows."""
    if invoice_id in (None, ""):
        raise KimcoError("Misc Add Item requires an invoice id")
    item_id = (misc_item or {}).get("id")
    if item_id in (None, ""):
        raise KimcoError("Misc Add Item requires live MFG_Miscellaneous_Item.id")
    children: list[dict[str, Any]] = []
    for raw in lines or []:
        if raw.get("Receipt") not in (None, "", {}):
            raise KimcoError("Misc Add Item must not include Receipt (that is Select Receipts)")
        qty = raw.get("qty") if raw.get("qty") is not None else raw.get("quantity")
        price = raw.get("unit_price") if raw.get("unit_price") is not None else raw.get("Unit_Price")
        if qty in (None, "") or price in (None, ""):
            raise KimcoError("Misc Add Item line needs qty and unit_price")
        values: dict[str, Any] = {
            "MFG_Miscellaneous_Item": {"id": int(item_id)},
            "Misc_Description": misc_line_description(raw),
            "Quantity": qty,
            "Unit_Price": price,
            "Invoice_Number": {"id": int(invoice_id)},
            "Vendor": {"id": int(vendor_id)},
        }
        if gl_account and gl_account.get("id") not in (None, ""):
            values["Purchase_GL_Account"] = {"id": int(gl_account["id"])}
        children.append({"state": "Added", "values": values})
    if not children:
        raise KimcoError("Misc Add Item requires at least one merchandise line")
    return {
        "id": int(invoice_id),
        "state": "Modified",
        "lists": {"APInvoiceLine": children},
    }


def payload_has_receipt(payload: dict[str, Any]) -> bool:
    for child in ((payload.get("lists") or {}).get("APInvoiceLine") or []):
        values = (child.get("values") if isinstance(child, dict) else None) or {}
        if values.get("Receipt") not in (None, "", {}):
            return True
    return False
