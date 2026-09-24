"""Finish Gas 10165–10172 after Shawn completed receiving PO 59081 / 59006.

Kyle 2026-09-22: leftovers now exist (GI 24332–24355; Received_Quantity full).
Select Receipts by PDF part/qty/cost. Fees id 11. NOTE-47 |PPV| under $75.
Type 3 with PO — not Misc Type 4. Amount = PDF. Stay on batch 720.
|$75+| → Transfer AP + @Shawn mention-id 104, no Select. No Mail.Send.
invent=false. Leave McMaster / O'Neal / Legacy alone.
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
    SHAWN_MCKIBBEN,
    filter_matches_outside_ppv_gate,
    invoice_number_key,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    normalize_receipt,
    receipt_select_refs,
)
from gas_po_59081_enter import (
    INVOICE_PO_COVER,
    PO_59006_LINES,
    PO_59081_LINES,
    WANTED,
    po_cover_amount,
)
from gas_shawn_finish import (
    NOTE47_PPV_MAX_ABS,
    SUCCESS_LEAVE_ALONE,
    drop_invoiced,
    ppv_abs_over_note47,
)
from gas_supply_0917 import (
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    VENDOR_ID_HINT,
    VENDOR_NAME,
    apply_gas_exception_category_owner,
    apply_over_ppv_transfer_ap,
    finish_hold_header,
    gas_proof,
    open_receipts_on_po,
    over_ppv_hold_comment,
    quality_gas_row,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.gas_59081_retry")

# Process 0040438057 before 0040417672 so the two identical
# PFXPXTW1425R 1@36.12 leftovers are not both consumed by 10171.
FINISH_ORDER = (
    "0040430010",
    "0040424839",
    "0040438057",
    "0040438056",
    "0040438055",
    "0040438053",
    "0040414962",
    "0040417672",
)

HEADERS = {
    "0040430010": 10165,
    "0040424839": 10166,
    "0040438057": 10167,
    "0040438056": 10168,
    "0040438055": 10169,
    "0040438053": 10170,
    "0040417672": 10171,
    "0040414962": 10172,
}

INVOICE_DATES = {
    "0040430010": "2026-09-11",
    "0040424839": "2026-09-09",
    "0040438057": "2026-09-17",
    "0040438056": "2026-09-17",
    "0040438055": "2026-09-17",
    "0040438053": "2026-09-17",
    "0040417672": "2026-09-03",
    "0040414962": "2026-09-02",
}

# Shawn-created GI leftovers from the 2026-09-22 morning receive.
KNOWN_RECEIPT_IDS = tuple(range(24332, 24356))
PO_LINE_IDS = tuple(range(17889, 17913))
SHAWN_MENTION_ID = 104

SHEET = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.json"
PROOF_OUT = Path("/tmp/gas-qa/finish_59081_retry.json")

CATALOG_BY_DISPLAY = {
    row[1]: row[4] for row in (*PO_59081_LINES, *PO_59006_LINES)
}
LINE_BY_DISPLAY = {
    row[1]: row for row in (*PO_59081_LINES, *PO_59006_LINES)
}


def merch_lines_for(invoice: str) -> list[dict[str, Any]]:
    """PDF merch = PO cover part/qty/cost (live Why text + purchase lines)."""
    lines: list[dict[str, Any]] = []
    for name in INVOICE_PO_COVER[invoice]:
        _lid, display, qty, unit, part = LINE_BY_DISPLAY[name]
        lines.append(
            {
                "part": part,
                "qty": qty,
                "unit_price": unit,
                "amount": round(qty * unit, 2),
                "po_line": display,
                "label": part,
                "description": part,
            }
        )
    return lines


def parsed_for(invoice_number: str) -> dict[str, Any]:
    spec = WANTED[invoice_number]
    return {
        "invoice_number": invoice_number,
        "po": spec["po"],
        "amount": spec["amount"],
        "date": INVOICE_DATES[invoice_number],
        "lines": merch_lines_for(invoice_number),
        "fees": [],
        "vendor": VENDOR_NAME,
    }


def annotate_catalog_part(rec: dict[str, Any]) -> dict[str, Any]:
    """Join GI leftover PO59081-NN display name → catalog part for match."""
    out = dict(rec)
    blob = " ".join(
        str(out.get(key) or "")
        for key in ("part", "name", "slip", "description", "label")
    )
    for display, catalog in CATALOG_BY_DISPLAY.items():
        if display in blob:
            out["po_line"] = display
            out["part"] = catalog
            out["item"] = catalog
            break
    return out


def match_open_for_bill(
    parsed: dict[str, Any],
    open_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    annotated = [annotate_catalog_part(rec) for rec in open_receipts]
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=annotated,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    locked = filter_matches_outside_ppv_gate(
        list(match.get("matched") or []),
        invoice_total=parsed.get("amount"),
    )
    selectable = list(locked.get("selectable") or [])
    rec_ids: list[int] = []
    rec_amt = 0.0
    for hit in selectable:
        rec = hit.get("receipt") if isinstance(hit.get("receipt"), dict) else {}
        if rec.get("id") not in (None, ""):
            rec_ids.append(int(rec["id"]))
        qty = money(rec.get("qty"))
        unit = money(rec.get("unit_price"))
        if qty is not None and unit is not None:
            rec_amt = round(rec_amt + qty * unit, 2)
    inv_amt = money(parsed.get("amount")) or 0.0
    ppv = round(inv_amt - rec_amt, 2) if selectable else None
    unmatched = list(match.get("unmatched_lines") or match.get("unmatched") or [])
    return {
        "match": match,
        "locked": locked,
        "selectable": selectable,
        "select_refs": receipt_select_refs(selectable),
        "receipt_ids": rec_ids,
        "select_zero": bool(locked.get("select_zero")),
        "ppv": ppv,
        "over_note47": ppv_abs_over_note47(ppv) if ppv is not None else bool(locked.get("select_zero")),
        "unmatched": unmatched,
        "rec_amt": rec_amt,
    }


def usable_open_on_po(
    client: KimcoClient,
    receipts: list[dict[str, Any]],
    po: str,
) -> list[dict[str, Any]]:
    pool = [annotate_catalog_part(rec) for rec in drop_invoiced(open_receipts_on_po(receipts, po))]
    return [rec for rec in pool if rec.get("qty") is not None and rec.get("unit_price") is not None]


def hydrate_known_receipts(client: KimcoClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rid in KNOWN_RECEIPT_IDS:
        try:
            item = client.get_item("receipts", int(rid))
        except KimcoError:
            continue
        rec = annotate_catalog_part(normalize_receipt(item))
        rec["invoiced"] = (item.get("values") or {}).get("Invoiced")
        rec["raw"] = item.get("values") or {}
        rows.append(rec)
    # Newer leftovers past the morning scan — do not assume 24355 is last.
    for rid in range(KNOWN_RECEIPT_IDS[-1] + 1, KNOWN_RECEIPT_IDS[-1] + 31):
        try:
            item = client.get_item("receipts", int(rid))
        except KimcoError:
            break
        rec = annotate_catalog_part(normalize_receipt(item))
        po = invoice_number_key(str(rec.get("po") or rec.get("name") or ""))
        if po not in {"59081", "59006"}:
            continue
        rec["invoiced"] = (item.get("values") or {}).get("Invoiced")
        rec["raw"] = item.get("values") or {}
        rows.append(rec)
    return rows


def po_received_snapshot(client: KimcoClient) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for lid in PO_LINE_IDS:
        item = client.get_item("purchase_lines", int(lid))
        vals = item.get("values") or {}
        gi = (item.get("lists") or {}).get("poLineReceiptsForCounting") or []
        lines.append(
            {
                "id": lid,
                "display": vals.get("Display_Name"),
                "received": money(vals.get("Received_Quantity")),
                "qty": money(vals.get("Quantity")),
                "unit": money(vals.get("Unit_Price")),
                "counter": vals.get("Receipts_Counter"),
                "gi_n": len(gi) if isinstance(gi, list) else 0,
            }
        )
    return lines


def comments_1_shawn(
    client: KimcoClient,
    kimco_id: int,
    html: str,
) -> dict[str, Any]:
    """Comments tab + mention-id 104 (Shawn). Retry without Mention if PUT rejects it."""
    base_values = {
        "HtmlValue": html,
        "Entity": {"id": 203},
        "ObjectId": int(kimco_id),
        "FormId": 218,
    }
    attempts = (
        {**base_values, "Mention": {"id": SHAWN_MENTION_ID}},
        base_values,
    )
    last: dict[str, Any] = {"status": "blocked", "invent": False}
    for values in attempts:
        payload = {
            "state": "Modified",
            "id": int(kimco_id),
            "lists": {"Comments_1": [{"state": "Added", "values": values}]},
        }
        try:
            _body, status, error = client.update("ap_invoices", int(kimco_id), payload)
        except KimcoError as exc:
            last = {"status": "blocked", "error": str(exc)[:200], "invent": False}
            continue
        after = {}
        try:
            after = client.get_item("ap_invoices", int(kimco_id))
        except KimcoError:
            after = {}
        rows = (after.get("lists") or {}).get("Comments_1") or []
        persisted = bool(rows)
        last = {
            "status": "persisted" if status < 400 and persisted else f"put-{status}",
            "put": status,
            "error": error,
            "tab_rows": len(rows) if isinstance(rows, list) else 0,
            "mention_id": SHAWN_MENTION_ID if "Mention" in values else None,
            "invent": False,
        }
        if last["status"] == "persisted":
            return last
    return last


def _sheet_rows() -> list[dict[str, Any]]:
    payload = json.loads(SHEET_JSON.read_text())
    return [dict(row) for row in payload.get("rows") or []]


def _replace_row(rows: list[dict[str, Any]], invoice: str, new_row: dict[str, Any]) -> None:
    for idx, row in enumerate(rows):
        if str(row.get("Invoice #") or "") == invoice:
            rows[idx] = new_row
            return
    rows.append(new_row)


def _prior_row(invoice: str) -> dict[str, Any]:
    for row in _sheet_rows():
        if str(row.get("Invoice #") or "") == invoice:
            return dict(row)
    spec = WANTED[invoice]
    return {
        "Vendor": VENDOR_NAME,
        "Invoice #": invoice,
        "date": INVOICE_DATES[invoice],
        "PO": spec["po"],
        "Amount": spec["amount"],
        "Result": "HOLD",
        "KIMCO id": HEADERS[invoice],
        "Batch": f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
        "Fees and surcharges": "none",
        "PPV": "none",
        "Attach status": "attached",
        "Flag in Outlook": "Yes",
    }


def finish_one(
    client: KimcoClient,
    graph,
    *,
    invoice_number: str,
    receipts: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    kid = int(HEADERS[invoice_number])
    parsed = parsed_for(invoice_number)
    pool = usable_open_on_po(client, receipts, parsed["po"])
    planned = match_open_for_bill(parsed, pool)
    before = gas_proof(client, kid)
    report: dict[str, Any] = {
        "invoice": invoice_number,
        "kimco_id": kid,
        "po": parsed["po"],
        "pdf_amount": parsed["amount"],
        "po_cover": po_cover_amount(invoice_number),
        "planned_receipts": planned["receipt_ids"],
        "select_zero": planned["select_zero"],
        "over_note47": planned["over_note47"],
        "ppv": planned["ppv"],
        "unmatched": [
            {
                "part": (u.get("part") if isinstance(u, dict) else None),
                "qty": (u.get("qty") if isinstance(u, dict) else None),
            }
            for u in planned["unmatched"]
        ],
        "open_on_po": [
            {
                "id": r.get("id"),
                "part": r.get("part"),
                "qty": r.get("qty"),
                "unit": r.get("unit_price"),
            }
            for r in pool
        ],
        "dry_run": dry_run,
        "before": {
            "type": before.get("invoice_type"),
            "amount": before.get("invoice_amount"),
            "verification": before.get("verification_amount"),
            "receipts": before.get("receipt_lines"),
            "batch_id": lookup_id(
                (client.get_item("ap_invoices", kid).get("values") or {}).get("AP_Invoice_Batch")
            ),
        },
    }
    lines_n = len(parsed["lines"])
    selected_n = len(planned["receipt_ids"])
    incomplete = bool(planned["unmatched"]) or selected_n < lines_n
    blocked_over = planned["select_zero"] or planned["over_note47"]
    if blocked_over:
        report["now"] = "HOLD"
        report["changed"] = False
        report["blocked"] = (
            f"NOTE-47 |PPV| over ${NOTE47_PPV_MAX_ABS:.0f} or select-zero "
            f"(ppv={planned['ppv']} ids={planned['receipt_ids']})"
        )
        if dry_run:
            report["would"] = "transfer-ap-shawn-104-no-select"
            return report
        comment = over_ppv_hold_comment(
            invoice_number=invoice_number,
            po=parsed["po"],
            pdf_amount=parsed["amount"],
        )
        transfer = apply_over_ppv_transfer_ap(client, kimco_id=kid, comment=comment)
        tab = comments_1_shawn(client, kid, comment)
        prior = _prior_row(invoice_number)
        prior["Result"] = "HOLD"
        prior["Why"] = (
            f"HOLD (price-does-not-match): leftover vs invoice line is over the "
            f"NOTE-47 ${NOTE47_PPV_MAX_ABS:.0f} PPV gate. Receipts were NOT selected. "
            f"{SHAWN_MCKIBBEN} mention-id {SHAWN_MENTION_ID}. "
            f"Transfer AP status={transfer.get('status')}."
        )
        prior = apply_gas_exception_category_owner(apply_exception_category_owner(prior))
        if transfer.get("status") == "moved":
            prior["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
        report.update(
            {
                "transfer_ap": transfer,
                "comments_1": tab,
                "row": prior,
            }
        )
        return report
    if incomplete or not planned["receipt_ids"]:
        report["now"] = "HOLD"
        report["changed"] = False
        report["blocked"] = (
            f"leftover match incomplete (selected={planned['receipt_ids']} "
            f"lines={lines_n} unmatched={report['unmatched']}). Do not invent Success."
        )
        return report
    if dry_run:
        report["now"] = "would-finish"
        report["changed"] = False
        return report
    finish = finish_hold_header(client, parsed=parsed, kimco_id=kid, receipts=pool)
    proof = finish.get("after") or gas_proof(client, kid)
    prior = _prior_row(invoice_number)
    row = quality_gas_row(
        graph,
        parsed=parsed,
        enter_row=prior,
        proof=proof,
        finish=finish,
        vendor_id=VENDOR_ID_HINT,
        stamp_outlook=False,
    )
    if row.get("Result") == "Success":
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
                "fee_status": finish.get("fee_status"),
                "ppv_status": finish.get("ppv_status"),
                "ppv_amount": finish.get("ppv_amount"),
            },
            "posted_amount": money(vals.get("Invoice_Amount")),
            "verification": money(vals.get("Invoice_Verification_Amount")),
            "invoice_type": vals.get("Invoice_Type"),
            "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
            "batch": lookup_text(vals.get("AP_Invoice_Batch")),
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
        raise SystemExit("Refusing: pass --live (Gas 59081 retry).")
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    graph = None if args.dry_run else _optional_graph_client()
    verified = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    if not verified.get("matches_expected"):
        raise SystemExit(f"Batch 720 name mismatch: {verified.get('name')}")
    known = hydrate_known_receipts(client)
    # Targeted GET 24332+ (plus 30-id probe). Full 24k list GET is sparse and
    # already proved these ids in rescan_59081_retry.py.
    receipts = drop_invoiced([annotate_catalog_part(rec) for rec in known])
    po_lines = po_received_snapshot(client)
    results = []
    used: set[int] = set()
    for invoice in FINISH_ORDER:
        remaining = [rec for rec in receipts if rec.get("id") not in used]
        item = finish_one(
            client,
            graph,
            invoice_number=invoice,
            receipts=remaining,
            dry_run=args.dry_run,
        )
        results.append(item)
        for rid in item.get("planned_receipts") or []:
            if item.get("now") in {"Success", "would-finish"}:
                used.add(int(rid))
        if not args.dry_run and item.get("now") == "Success":
            # Re-GET leftovers so later bills do not re-select invoiced rows.
            refreshed = hydrate_known_receipts(client)
            receipts = drop_invoiced(refreshed)

    rows = _sheet_rows()
    for item in results:
        if item.get("row"):
            _replace_row(rows, item["invoice"], item["row"])
    if args.write_sheet and not args.dry_run:
        write_report(SHEET, rows)
        payload = json.loads(SHEET_JSON.read_text())
        payload["proof"] = "gas-supply-0917-59081-retry"
        payload["rows"] = rows
        payload["gas_59081_retry"] = {
            "invent": False,
            "mail_send": False,
            "note47": NOTE47_PPV_MAX_ABS,
            "fees_id": 11,
            "type": 3,
            "batch": KNOWN_BATCH_ID,
            "po_lines": po_lines,
            "hydrated": [
                {
                    "id": r.get("id"),
                    "po": r.get("po"),
                    "part": r.get("part"),
                    "qty": r.get("qty"),
                    "unit": r.get("unit_price"),
                    "invoiced": r.get("invoiced"),
                }
                for r in known
            ],
            "results": [{k: v for k, v in item.items() if k != "row"} for item in results],
            "success_leave_alone": SUCCESS_LEAVE_ALONE,
        }
        SHEET_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    summary = {
        "dry_run": args.dry_run,
        "hydrated_n": len(known),
        "open_n": len(receipts),
        "recv_59081": sum((r.get("received") or 0) for r in po_lines if str(r.get("display") or "").startswith("PO59081")),
        "recv_59006": sum((r.get("received") or 0) for r in po_lines if str(r.get("display") or "").startswith("PO59006")),
        "finished": [item for item in results if item.get("now") == "Success"],
        "would_finish": [item for item in results if item.get("now") == "would-finish"],
        "held": [item for item in results if item.get("now") not in {"Success", "would-finish"}],
        "leave_alone_success": SUCCESS_LEAVE_ALONE,
        "sheet": str(SHEET) if args.write_sheet and not args.dry_run else None,
        "results": [{k: v for k, v in item.items() if k not in {"row", "open_on_po", "before"}} for item in results],
    }
    PROOF_OUT.parent.mkdir(parents=True, exist_ok=True)
    PROOF_OUT.write_text(json.dumps({"summary": summary, "results": results, "po_lines": po_lines}, indent=2, default=str) + "\n")
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
