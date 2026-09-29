"""Finish Gas & Supply HOLDs after Shawn's 2026-09-21 PO 58948 receipts.

Kyle authorized finish-after-Shawn for batch 720 + Transfer AP Gas bills.
No Mail.Send. invent=false. Do not touch McMaster / O'Neal / Legacy.
NOTE-47: |bill PPV| under $75 only.

Live GET 2026-09-21:
- 24282 PO58948-01 1@173.50 matches 0040423658 / 10134
- 24283 PO58948-12 2@146.50 matches 0040414821 / 10137
- PO 59081 / 59006 still have zero receipts — stay missing_po (no header)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.quality_v12 import apply_exception_category_owner
from ap_clerk.report import write_report
from ap_clerk.rules import (
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    receipt_select_refs,
)
from gas_supply_0917 import (
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    VENDOR_ID_HINT,
    VENDOR_NAME,
    apply_gas_exception_category_owner,
    finish_hold_header,
    gas_proof,
    hydrate_receipts,
    load_list_receipts,
    open_receipts_on_po,
    quality_gas_row,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.gas_shawn_finish")

# NOTE-47 absolute-dollar gate (this Gas branch still has the old 10%/$100
# constants; finish decisions use $75).
NOTE47_PPV_MAX_ABS = 75.00

SUCCESS_LEAVE_ALONE = {
    "0040435122": 10128,
    "0040434973": 10129,
    "0040431060": 10130,
    "0040425657": 10131,
    "0040425612": 10132,
    "0040424382": 10133,
    "0040421569": 10135,
    "0040438494": 10136,
}

FINISHABLE = {
    "0040423658": {
        "kimco_id": 10134,
        "po": "58948",
        "amount": 173.5,
        "date": "2026-09-09",
        "was": "HOLD missing_receipt on 720",
        "expect_receipt_id": 24282,
        "lines": [
            {
                "part": "DEWDCW210B",
                "qty": 1.0,
                "unit_price": 173.5,
                "amount": 173.5,
                "label": 'DEWALT 20V MAX XR 5" CORDLESS',
                "description": 'DEWALT 20V MAX XR 5" CORDLESS',
            }
        ],
    },
    "0040414821": {
        "kimco_id": 10137,
        "po": "58948",
        "amount": 293.0,
        "date": "2026-09-02",
        "was": "HOLD price_variance on Transfer AP 375",
        "expect_receipt_id": 24283,
        "move_home_if_success": True,
        "lines": [
            {
                "part": "EVENVA-20R-250-UF-35QD",
                "qty": 2.0,
                "unit_price": 146.5,
                "amount": 293.0,
                "label": "NOVA 20 FLEX-NECK TIG TORCH",
                "description": "NOVA 20 FLEX-NECK TIG TORCH",
            }
        ],
    },
}

STILL_MISSING_PO = (
    ("0040430010", "59081", 14.0),
    ("0040424839", "59081", 224.94),
    ("0040438057", "59081", 36.12),
    ("0040438056", "59081", 207.53),
    ("0040438055", "59081", 27.82),
    ("0040438053", "59081", 182.5),
    ("0040417672", "59081", 3856.77),
    ("0040414962", "59006", 7840.8),
)

SHEET = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.json"


def ppv_abs_over_note47(ppv_total: Any) -> bool:
    amt = money(ppv_total)
    if amt is None:
        return False
    return abs(amt) >= NOTE47_PPV_MAX_ABS


def parsed_for(invoice_number: str) -> dict[str, Any]:
    spec = FINISHABLE[invoice_number]
    return {
        "invoice_number": invoice_number,
        "po": spec["po"],
        "amount": spec["amount"],
        "date": spec["date"],
        "lines": list(spec["lines"]),
        "fees": [],
        "vendor": VENDOR_NAME,
    }


def match_open_for_bill(
    parsed: dict[str, Any],
    open_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=open_receipts,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    locked = filter_matches_outside_ppv_gate(
        list(match.get("matched") or []),
        invoice_total=parsed.get("amount"),
    )
    selectable = list(locked.get("selectable") or [])
    rec_ids = []
    for hit in selectable:
        rec = hit.get("receipt") if isinstance(hit.get("receipt"), dict) else {}
        if rec.get("id") not in (None, ""):
            rec_ids.append(int(rec["id"]))
    inv_amt = money(parsed.get("amount")) or 0.0
    rec_amt = 0.0
    for hit in selectable:
        rec = hit.get("receipt") if isinstance(hit.get("receipt"), dict) else {}
        qty = money(rec.get("qty"))
        unit = money(rec.get("unit_price"))
        if qty is not None and unit is not None:
            rec_amt = round(rec_amt + qty * unit, 2)
    ppv = round(inv_amt - rec_amt, 2) if selectable else None
    return {
        "match": match,
        "locked": locked,
        "selectable": selectable,
        "select_refs": receipt_select_refs(selectable),
        "receipt_ids": rec_ids,
        "select_zero": bool(locked.get("select_zero")),
        "ppv": ppv,
        "over_note47": ppv_abs_over_note47(ppv) if ppv is not None else bool(locked.get("select_zero")),
    }


def drop_invoiced(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List GETs often omit Invoiced; after hydrate, skip already-invoiced leftovers."""
    open_rows: list[dict[str, Any]] = []
    for rec in receipts:
        raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
        invoiced = raw.get("Invoiced") if raw else rec.get("invoiced")
        if invoiced in {True, "true", 1, "1"}:
            continue
        open_rows.append(rec)
    return open_rows


def usable_open_on_po(
    client: KimcoClient,
    receipts: list[dict[str, Any]],
    po: str,
) -> list[dict[str, Any]]:
    pool = hydrate_receipts(client, open_receipts_on_po(receipts, po))
    cleaned = drop_invoiced(pool)
    return cleaned or drop_invoiced(hydrate_receipts(client, [
        rec
        for rec in receipts
        if invoice_number_key(str(rec.get("po") or rec.get("name") or ""))
        == invoice_number_key(po)
    ]))


def missing_po_still_blocked(po: str, receipts: list[dict[str, Any]]) -> bool:
    wanted = invoice_number_key(po)
    if not wanted:
        return True
    return not any(
        invoice_number_key(str(rec.get("po") or rec.get("name") or "")) == wanted
        for rec in receipts
    )


def move_to_gas_batch(client: KimcoClient, kimco_id: int) -> dict[str, Any]:
    known = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    if not known.get("matches_expected"):
        return {
            "status": "batch-name-mismatch",
            "live_name": known.get("name"),
            "invent": False,
        }
    bid = int(known["id"])
    body, status, error = client.update(
        "ap_invoices",
        int(kimco_id),
        {
            "state": "Modified",
            "id": int(kimco_id),
            "values": {"AP_Invoice_Batch": {"id": bid}},
        },
    )
    after = client.get_item("ap_invoices", int(kimco_id))
    vals = after.get("values") or {}
    live_bid = lookup_id(vals.get("AP_Invoice_Batch"))
    live_name = lookup_text(vals.get("AP_Invoice_Batch"))
    return {
        "status": "moved" if status < 400 and live_bid == bid else f"blocked-{status}",
        "batch_id": live_bid,
        "batch_name": live_name,
        "put": status,
        "error": error,
        "invent": False,
    }


def _sheet_rows() -> list[dict[str, Any]]:
    payload = json.loads(SHEET_JSON.read_text())
    return [dict(row) for row in payload.get("rows") or []]


def _replace_row(rows: list[dict[str, Any]], invoice: str, new_row: dict[str, Any]) -> None:
    for idx, row in enumerate(rows):
        if str(row.get("Invoice #") or "") == invoice:
            rows[idx] = new_row
            return
    rows.append(new_row)


def finish_one(
    client: KimcoClient,
    graph,
    *,
    invoice_number: str,
    receipts: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    spec = FINISHABLE[invoice_number]
    kid = int(spec["kimco_id"])
    parsed = parsed_for(invoice_number)
    pool = usable_open_on_po(client, receipts, spec["po"])
    planned = match_open_for_bill(parsed, pool)
    before = gas_proof(client, kid)
    report = {
        "invoice": invoice_number,
        "kimco_id": kid,
        "was": spec["was"],
        "planned_receipts": planned["receipt_ids"],
        "expect_receipt_id": spec["expect_receipt_id"],
        "select_zero": planned["select_zero"],
        "over_note47": planned["over_note47"],
        "ppv": planned["ppv"],
        "dry_run": dry_run,
        "before_batch": before.get("batch_id") or lookup_id(
            (client.get_item("ap_invoices", kid).get("values") or {}).get("AP_Invoice_Batch")
        ),
    }
    if planned["select_zero"] or planned["over_note47"] or spec["expect_receipt_id"] not in planned["receipt_ids"]:
        report["now"] = "HOLD"
        report["changed"] = False
        report["blocked"] = (
            f"Shawn leftover not uniquely selectable in-gate "
            f"(ids={planned['receipt_ids']} expect={spec['expect_receipt_id']} "
            f"select_zero={planned['select_zero']} over_note47={planned['over_note47']})"
        )
        return report
    if dry_run:
        report["now"] = "would-finish"
        report["changed"] = False
        return report
    finish = finish_hold_header(client, parsed=parsed, kimco_id=kid, receipts=pool)
    proof = gas_proof(client, kid)
    prior = {
        "Vendor": VENDOR_NAME,
        "Invoice #": invoice_number,
        "date": spec["date"],
        "PO": spec["po"],
        "Amount": spec["amount"],
        "Result": "HOLD",
        "KIMCO id": kid,
        "Batch": "",
        "Fees and surcharges": "none",
        "PPV": "none",
        "Attach status": "attached",
        "Flag in Outlook": "Yes",
    }
    row = quality_gas_row(
        graph,
        parsed=parsed,
        enter_row=prior,
        proof=proof,
        finish=finish,
        vendor_id=VENDOR_ID_HINT,
        stamp_outlook=graph is not None,
    )
    move = None
    if row.get("Result") == "Success" and spec.get("move_home_if_success"):
        live_batch = lookup_id(
            (client.get_item("ap_invoices", kid).get("values") or {}).get("AP_Invoice_Batch")
        )
        if live_batch != KNOWN_BATCH_ID:
            move = move_to_gas_batch(client, kid)
            if move.get("status") == "moved":
                row["Batch"] = f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})"
            else:
                row["Notes"] = (
                    f"Success on Transfer AP; batch move {move.get('status')} "
                    f"— left on batch {live_batch}."
                )
                row["Batch"] = f"TRANSFER AP ({live_batch})"
    elif row.get("Result") == "Success":
        row["Batch"] = f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})"
    row = apply_gas_exception_category_owner(apply_exception_category_owner(row))
    after = client.get_item("ap_invoices", kid)
    vals = after.get("values") or {}
    report.update(
        {
            "now": row.get("Result"),
            "changed": row.get("Result") == "Success",
            "finish": {
                "select_status": finish.get("select_status"),
                "wanted": finish.get("wanted"),
                "ppv_status": finish.get("ppv_status"),
                "ppv_amount": finish.get("ppv_amount"),
            },
            "posted_amount": money(vals.get("Invoice_Amount")),
            "verification": money(vals.get("Invoice_Verification_Amount")),
            "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
            "batch": lookup_text(vals.get("AP_Invoice_Batch")),
            "move": move,
            "row": row,
            "blocked": None if row.get("Result") == "Success" else row.get("Why"),
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Required for live GET/finish.")
    parser.add_argument("--dry-run", action="store_true", help="GET + plan only. No writes.")
    parser.add_argument("--write-sheet", action="store_true", help="Refresh the Gas run sheet.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live (Gas Shawn finish).")
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    graph = None if args.dry_run else _optional_graph_client()
    receipts = load_list_receipts(client)
    results = []
    for invoice in FINISHABLE:
        results.append(
            finish_one(
                client,
                graph,
                invoice_number=invoice,
                receipts=receipts,
                dry_run=args.dry_run,
            )
        )
    blocked_po = [
        {
            "invoice": inv,
            "po": po,
            "amount": amt,
            "now": "HOLD",
            "was": "HOLD missing_po (no header)",
            "blocked": f"PO {po} still has zero live receipts — do not enter Type 4.",
            "changed": False,
        }
        for inv, po, amt in STILL_MISSING_PO
        if missing_po_still_blocked(po, receipts)
    ]
    rows = _sheet_rows()
    for item in results:
        if item.get("row"):
            _replace_row(rows, item["invoice"], item["row"])
    if args.write_sheet and not args.dry_run:
        write_report(SHEET, rows)
        payload = json.loads(SHEET_JSON.read_text())
        payload["proof"] = "gas-supply-0917-shawn-finish"
        payload["rows"] = rows
        payload["shawn_finish"] = {
            "invent": False,
            "mail_send": False,
            "note47": NOTE47_PPV_MAX_ABS,
            "results": [
                {k: v for k, v in item.items() if k != "row"} for item in results
            ],
            "still_missing_po": blocked_po,
            "success_leave_alone": SUCCESS_LEAVE_ALONE,
        }
        SHEET_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    summary = {
        "dry_run": args.dry_run,
        "finished": [item for item in results if item.get("now") == "Success"],
        "held": [item for item in results if item.get("now") != "Success"] + blocked_po,
        "leave_alone_success": SUCCESS_LEAVE_ALONE,
        "sheet": str(SHEET) if args.write_sheet and not args.dry_run else None,
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
