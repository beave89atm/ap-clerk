"""GET-only live QA for Gas & Supply after Shawn PO updates.

No writes. No Mail.Send. invent=false.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient
from ap_clerk.misc_lines import type4_shop_supplies_lines_ok
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text, money, normalize_receipt
from gas_supply_0917 import (
    CREATED_HEADERS,
    PLUS10_HEADERS,
    PLUS15_HEADERS,
    PLUS5_HEADERS,
    VENDOR_ID_HINT,
    charges_from_item,
    exact_invoice_number,
    gas_proof,
    is_gas_vendor_text,
    open_receipts_on_po,
)

KNOWN_HEADERS = {
    **CREATED_HEADERS,
    **PLUS5_HEADERS,
    **PLUS10_HEADERS,
    **PLUS15_HEADERS,
}
HOLD_NO_HEADER = [
    ("0040430010", "59081", 14.0),
    ("0040424839", "59081", 224.94),
    ("0040438057", "59081", 36.12),
    ("0040438056", "59081", 207.53),
    ("0040438055", "59081", 27.82),
    ("0040438053", "59081", 182.5),
    ("0040417672", "59081", 3856.77),
    ("0040414962", "59006", 7840.8),
]
POS = ("59081", "58948", "59006")
OUT = Path("/tmp/gas-qa/live_probe.json")


def _comments_tab(item: dict[str, Any]) -> list[dict[str, Any]]:
    lists = item.get("lists") if isinstance(item.get("lists"), dict) else {}
    rows = []
    for raw in lists.get("Comments_1") or []:
        if not isinstance(raw, dict):
            continue
        vals = raw.get("values") if isinstance(raw.get("values"), dict) else raw
        rows.append(
            {
                "id": raw.get("id"),
                "html": str(vals.get("HtmlValue") or vals.get("Value") or "")[:400],
                "mention": lookup_id(vals.get("Mention") or vals.get("User") or vals.get("Tagged_User")),
            }
        )
    return rows


def _header_summary(client: KimcoClient, kid: int, invoice: str) -> dict[str, Any]:
    item = client.get_item("ap_invoices", kid)
    vals = item.get("values") or {}
    proof = gas_proof(client, kid)
    inv_type = vals.get("Invoice_Type")
    if isinstance(inv_type, dict):
        inv_type = inv_type.get("id") or inv_type.get("text")
    batch = vals.get("AP_Invoice_Batch") or vals.get("Batch")
    lines_ok = type4_shop_supplies_lines_ok(item) if str(inv_type) in {"4", "4.0"} else None
    return {
        "id": kid,
        "invoice": exact_invoice_number(vals.get("Invoice_Number")) or invoice,
        "vendor_id": lookup_id(vals.get("Vendor")),
        "vendor": lookup_text(vals.get("Vendor")),
        "amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Invoice_Verification_Amount")),
        "po": lookup_text(vals.get("Purchase_Order") or vals.get("PO"))
        or vals.get("Purchase_Order_Number"),
        "invoice_type": inv_type,
        "batch_id": lookup_id(batch),
        "batch": lookup_text(batch),
        "header_comments": str(vals.get("Comments") or ""),
        "comments_1": _comments_tab(item),
        "misc_lines": proof.get("misc_lines"),
        "type4_lines_ok": lines_ok,
        "receipt_lines": proof.get("receipt_lines"),
        "fee_amounts": proof.get("fee_amounts"),
        "ppv_amounts": proof.get("ppv_amounts"),
        "charges": charges_from_item(item),
        "attach_count": len(client.list_attachments(kid) or []),
    }


def main() -> None:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    headers = []
    for inv, kid in sorted(KNOWN_HEADERS.items(), key=lambda kv: kv[1]):
        try:
            headers.append(_header_summary(client, int(kid), inv))
        except Exception as exc:  # noqa: BLE001 — report GET failure
            headers.append({"id": kid, "invoice": inv, "error": str(exc)[:300]})

    receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
    po_receipts = {}
    for po in POS:
        open_rows = open_receipts_on_po(receipts, po)
        all_rows = [
            r
            for r in receipts
            if invoice_number_key(str(r.get("po") or r.get("name") or ""))
            == invoice_number_key(po)
        ]
        po_receipts[po] = {
            "open": [
                {
                    "id": r.get("id"),
                    "part": r.get("part"),
                    "qty": r.get("qty"),
                    "unit_price": r.get("unit_price"),
                    "amount": r.get("amount"),
                    "invoiced": (r.get("raw") or {}).get("Invoiced")
                    if isinstance(r.get("raw"), dict)
                    else None,
                    "name": r.get("name"),
                }
                for r in open_rows
            ],
            "all_count": len(all_rows),
            "all": [
                {
                    "id": r.get("id"),
                    "part": r.get("part"),
                    "qty": r.get("qty"),
                    "unit_price": r.get("unit_price"),
                    "amount": r.get("amount"),
                    "invoiced": (r.get("raw") or {}).get("Invoiced")
                    if isinstance(r.get("raw"), dict)
                    else None,
                    "name": r.get("name"),
                }
                for r in all_rows
            ],
        }

    found_by_number: dict[str, list[dict[str, Any]]] = {inv: [] for inv, _, _ in HOLD_NO_HEADER}
    gas_on_720: list[dict[str, Any]] = []
    gas_on_transfer: list[dict[str, Any]] = []
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        vendor_txt = str(lookup_text(vals.get("Vendor") or vals.get("Vendor_$_Display_Name")) or "")
        vendor_id = lookup_id(vals.get("Vendor"))
        number = exact_invoice_number(vals.get("Invoice_Number"))
        batch = vals.get("AP_Invoice_Batch") or vals.get("Batch")
        batch_id = lookup_id(batch)
        batch_name = str(lookup_text(batch) or "")
        is_gas = vendor_id == VENDOR_ID_HINT or is_gas_vendor_text(vendor_txt)
        if number in found_by_number:
            found_by_number[number].append(
                {
                    "id": item.get("id"),
                    "vendor": vendor_txt,
                    "vendor_id": vendor_id,
                    "amount": money(vals.get("Invoice_Amount")),
                    "batch_id": batch_id,
                    "batch": batch_name,
                }
            )
        if not is_gas:
            continue
        row = {
            "id": item.get("id"),
            "invoice": number,
            "amount": money(vals.get("Invoice_Amount")),
            "batch_id": batch_id,
            "batch": batch_name,
            "vendor_id": vendor_id,
        }
        if batch_id == 720 or "gas" in batch_name.lower():
            gas_on_720.append(row)
        if batch_id == 375 or "transfer" in batch_name.lower():
            gas_on_transfer.append(row)

    payload = {
        "invent": False,
        "writes": False,
        "headers": headers,
        "hold_no_header_found": found_by_number,
        "po_receipts": po_receipts,
        "gas_on_720": gas_on_720,
        "gas_on_transfer": gas_on_transfer,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps({
        "wrote": str(OUT),
        "headers": len(headers),
        "gas_on_720": len(gas_on_720),
        "gas_on_transfer": len(gas_on_transfer),
        "po_open": {po: len(po_receipts[po]["open"]) for po in POS},
        "hold_found": {k: [x["id"] for x in v] for k, v in found_by_number.items()},
    }, indent=2))


if __name__ == "__main__":
    main()
