"""GET-only: Kyle leftover on Legacy Wire 10116 / PO 58807.

invent=false. No KIMCO writes. No Mail.Send. No Graph.

Record ids below came from live list+GET on 2026-09-17. List-view
omits Invoiced/Selected/Locked — always record-GET leftovers.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.kimco import (  # noqa: E402
    ADDITIONAL_CHARGE_LISTS,
    FEE_CHARGE_LOOKUP_ID,
    PPV_CHARGE_LOOKUP_ID,
    KimcoClient,
)
from ap_clerk.rules import (  # noqa: E402
    PPV_MAX_ABS_ON_BILL,
    PPV_MAX_PCT_OF_INVOICE,
    decide_ppv,
    filter_matches_outside_ppv_gate,
    lookup_id,
    lookup_text,
    money,
    normalize_receipt,
)

LOGGER = logging.getLogger("ap_clerk.kyle_10116")

INVOICE_ID = 10116
PO = "58807"
PDF_INVOICE = "PS-INV104017"
PDF_AMOUNT = 114.28
PDF_LINE = {
    "part": "A-05480-001",
    "qty": 1.0,
    "unit_price": 100.0,
    "amount": 100.0,
    "description": 'ASSEMBLY, BIFOLD GATE- 77"(2"TUBE)',
}
# Live list 2026-09-17: only these two receipts name PO58807.
KNOWN_RECEIPT_IDS = (23746, 23747)
# Live purchase_lines on PO 58807.
KNOWN_PO_LINE_IDS = (16864, 16865, 16866)
ALREADY_ON_9995 = 9995
REPORT = ROOT / "runs" / "kyle-10116-leftover-2026-09-17.json"


def _truthy(value: Any) -> bool:
    return value in {True, "true", "True", 1, "1"}


def _flag(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw and raw.get(name) is not None:
            return raw.get(name)
    lower = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        key = name.lower()
        if key in lower:
            return lower[key]
    return None


def leftover_row(rec: dict[str, Any]) -> dict[str, Any]:
    raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
    invoiced = _flag(raw, "Invoiced", "invoiced")
    selected = _flag(raw, "Selected_for_Receipt", "Selected")
    locked = _flag(raw, "Locked_PO", "Locked", "IsLocked")
    return {
        "id": rec.get("id"),
        "part": rec.get("part") or rec.get("name"),
        "name": rec.get("name"),
        "po": rec.get("po") or PO,
        "qty": rec.get("qty"),
        "unit_price": rec.get("unit_price"),
        "amount": rec.get("amount"),
        "Invoiced": invoiced,
        "Selected": selected,
        "Locked": locked,
        "Invoiced_bool": _truthy(invoiced),
        "Selected_bool": _truthy(selected),
        "Locked_bool": _truthy(locked),
        "AP_Invoice_Number": _flag(raw, "AP_Invoice_Number"),
        "Quantity_Remaining": raw.get("Quantity_Remaining"),
    }


def charges_from_item(item: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    out: list[dict[str, Any]] = []
    lists = item.get("lists") if isinstance(item.get("lists"), dict) else {}
    for key in ADDITIONAL_CHARGE_LISTS:
        for raw in lists.get(key) or []:
            if not isinstance(raw, dict):
                continue
            values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
            kind = (
                values.get("Additional_Charges")
                or values.get("Additional_Charge")
                or values.get("Name")
                or ""
            )
            lookup = lookup_id(kind) if isinstance(kind, dict) else None
            text = str(lookup_text(kind) or "") if isinstance(kind, dict) else str(kind or "")
            out.append(
                {
                    "lookup_id": lookup,
                    "text": text,
                    "amount": money(values.get("Amount") or values.get("Charge_Amount")),
                    "description": str(values.get("Description") or text),
                }
            )
    return out


def charge_kind(charge: dict[str, Any]) -> str:
    lookup = charge.get("lookup_id")
    if lookup == FEE_CHARGE_LOOKUP_ID:
        return "Fees"
    if lookup == PPV_CHARGE_LOOKUP_ID:
        return "PPV"
    text = str(charge.get("text") or "")
    if "ppv" in text.lower() or "purchase price" in text.lower():
        return "PPV"
    if "fee" in text.lower():
        return "Fees"
    return text or "other"


def live_invoice_proof(client: KimcoClient, invoice_id: int) -> dict[str, Any]:
    item = client.get_item("ap_invoices", invoice_id)
    vals = item.get("values") or {}
    attachments = []
    try:
        attachments = [
            str(a.get("name") or a.get("fileName") or "")
            for a in client.list_attachments(invoice_id)
        ]
    except Exception:  # noqa: BLE001 - proof only
        attachments = []
    lines = []
    for line in item.get("lists", {}).get("APInvoiceLine") or []:
        lv = (line.get("values") if isinstance(line, dict) else None) or {}
        receipt = lv.get("Receipt") or {}
        lines.append(
            {
                "qty": lv.get("Quantity"),
                "unit": lv.get("Unit_Price"),
                "receipt": lookup_id(receipt) or receipt,
                "po_line": lookup_text(lv.get("PO_Item")) or lv.get("PO_Item"),
            }
        )
    charges = charges_from_item(item)
    return {
        "id": item.get("id"),
        "invoice_number": vals.get("Invoice_Number"),
        "vendor_text": lookup_text(vals.get("Vendor")),
        "po": lookup_text(vals.get("Purchase_Order")) or vals.get("Purchase_Order"),
        "invoice_type": vals.get("Invoice_Type"),
        "invoice_amount": vals.get("Invoice_Amount"),
        "verification": vals.get("Invoice_Verification_Amount")
        or vals.get("Verification_Total")
        or vals.get("Invoice_Balance"),
        "posted": vals.get("Posted"),
        "void": vals.get("Void"),
        "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
        "batch_text": lookup_text(vals.get("AP_Invoice_Batch")),
        "receipt_lines": lines,
        "charges": charges,
        "attachments": attachments,
        "comments": vals.get("Comments"),
        "_item": item,
    }


def po_line_row(item: dict[str, Any]) -> dict[str, Any]:
    vals = item.get("values") or {}
    return {
        "id": item.get("id"),
        "display": vals.get("Display_Name"),
        "line": vals.get("Purchase_Line_Number") or vals.get("PO_Item_Number"),
        "description": vals.get("PO_Item_Description"),
        "qty": vals.get("Quantity"),
        "received": vals.get("Received_Quantity"),
        "remaining": vals.get("Remaining_Quantity"),
        "unit_price": vals.get("Unit_Price"),
        "amount": vals.get("Total_Item_Price"),
        "closed": vals.get("Item_Closed"),
        "wo": lookup_text(vals.get("Work_Order_Number")),
        "po": lookup_text(vals.get("Purchase_Order_Number")),
    }


def one_at_41_vs_one_at_100() -> dict[str, Any]:
    """Kyle lock math. Invoice 1@$100 vs leftover 1@$41 on a $114.28 bill."""
    line = dict(PDF_LINE)
    rec = {"id": 23747, "qty": 1.0, "unit_price": 41.0, "amount": 41.0, "part": "PO58807-03"}
    decision = decide_ppv(
        invoice_line_amount=100.0,
        po_line_amount=41.0,
        invoice_total=PDF_AMOUNT,
        invoice_unit_price=100.0,
        po_unit_price=41.0,
        qty=1.0,
        label="A-05480-001",
    )
    locked = filter_matches_outside_ppv_gate(
        [{"line": line, "receipt": rec}],
        invoice_total=PDF_AMOUNT,
    )
    return {
        "invoice": "1@$100=$100",
        "leftover": "23747 1@$41=$41",
        "variance": 59.0,
        "pct_of_invoice": 51.6,
        "pct_limit": round(PDF_AMOUNT * PPV_MAX_PCT_OF_INVOICE, 2),
        "abs_limit": PPV_MAX_ABS_ON_BILL,
        "decision": decision,
        "select_zero": bool(locked.get("select_zero")),
        "bill_over_ppv": bool(locked.get("bill_over_ppv")),
        "kyle_lock_rule": True,
    }


def kyle_plain_text(*, invoice: dict[str, Any], leftovers: list[dict[str, Any]], po_lines: list[dict[str, Any]]) -> str:
    return (
        "Kyle: you searched IV-PS-INV104017. The live bill is PS-INV104017 / KIMCO 10116 "
        f"on batch {invoice.get('Batch')} ({invoice.get('Batch_id')}). "
        "No sales receipts are selected on purpose (NOTE-29 / Kyle lock). "
        "The only qty-1 leftover on PO 58807 is receipt 23747 1@$41, already "
        "Selected/Invoiced/Locked on posted 9995 PS-INV103979. "
        "Invoice 104017 is 1@$100 for the bifold. $59 gap is 51.6% of the $114.28 bill "
        "(gate ≤10% / $11.43). Selecting 23747 would lock a receipt Shawn cannot "
        "unreceive, and it is already on another bill. "
        "PO58807-02 is 3@$100 for A-05480-002 and Received_Quantity=0. "
        "Shawn: receive the $100 bifold line, then AP can Select Receipts. "
        "Do not invent PPV. Do not steal 23747 from 103979."
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("Target: live GET-only. invent=false. No writes. No Mail.Send.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    def _refuse_write(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("GET-only: refusing KIMCO write")

    client.create = _refuse_write  # type: ignore[method-assign]
    client.update = _refuse_write  # type: ignore[method-assign]
    client.add_invoice_lines = _refuse_write  # type: ignore[method-assign]
    client.try_select_receipts = _refuse_write  # type: ignore[method-assign]
    client.try_deselect_receipts = _refuse_write  # type: ignore[method-assign]
    client.try_post_fees = _refuse_write  # type: ignore[method-assign]
    client.try_post_ppv = _refuse_write  # type: ignore[method-assign]

    proof = live_invoice_proof(client, INVOICE_ID)
    charges = [{**c, "kind": charge_kind(c)} for c in (proof.get("charges") or [])]
    invoice_summary = {
        "id": proof.get("id"),
        "Invoice_Number": proof.get("invoice_number"),
        "Amount": proof.get("invoice_amount"),
        "Verification": proof.get("verification"),
        "Batch": proof.get("batch_text"),
        "Batch_id": proof.get("batch_id"),
        "receipt_lines_count": len(proof.get("receipt_lines") or []),
        "receipt_lines": proof.get("receipt_lines") or [],
        "Additional_Charges": charges,
        "Fees": [c for c in charges if c.get("kind") == "Fees"],
        "PPV": [c for c in charges if c.get("kind") == "PPV"],
        "Void": proof.get("void"),
        "Posted": proof.get("posted"),
        "Open": not bool(proof.get("void") or proof.get("posted")),
        "po": proof.get("po"),
        "vendor": proof.get("vendor_text"),
        "invoice_type": proof.get("invoice_type"),
        "attachments": proof.get("attachments") or [],
        "comments": proof.get("comments"),
    }

    rec_rows = []
    for rid in KNOWN_RECEIPT_IDS:
        rec_rows.append(leftover_row(normalize_receipt(client.get_item("receipts", rid))))
    open_rows = [r for r in rec_rows if not r.get("Invoiced_bool")]
    po_lines = [po_line_row(client.get_item("purchase_lines", lid)) for lid in KNOWN_PO_LINE_IDS]
    other = live_invoice_proof(client, ALREADY_ON_9995)
    other_summary = {
        "id": other.get("id"),
        "Invoice_Number": other.get("invoice_number"),
        "Amount": other.get("invoice_amount"),
        "Void": other.get("void"),
        "Posted": other.get("posted"),
        "receipt_lines": other.get("receipt_lines") or [],
    }
    gate = one_at_41_vs_one_at_100()
    report = {
        "proof": "kyle-10116-leftover",
        "invent": False,
        "writes": False,
        "mail_send": False,
        "invoice_id": INVOICE_ID,
        "po": PO,
        "kyle_typed": "IV-PS-INV104017",
        "live_invoice_number": PDF_INVOICE,
        "pdf": {"invoice_number": PDF_INVOICE, "amount": PDF_AMOUNT, "line": PDF_LINE},
        "live_get_10116": invoice_summary,
        "po_58807_receipts": rec_rows,
        "po_58807_open_leftovers": open_rows,
        "po_58807_lines": po_lines,
        "already_on_9995": other_summary,
        "ppv_gate": gate,
        "deliberately_did_not_select": True,
        "why_no_select": (
            "NOTE-29 Kyle lock: leftover 1@$41 vs invoice 1@$100 is 51.6% of $114.28 "
            "(over ≤10% / $11.43). Select Receipts would lock 23747. Live record GET "
            "also shows 23747 already Invoiced/Selected/Locked on 9995. PO58807-02 "
            "3@$100 was never received."
        ),
        "kyle_plain": kyle_plain_text(
            invoice=invoice_summary, leftovers=rec_rows, po_lines=po_lines
        ),
    }
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"Wrote {REPORT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
