"""Enter Gas HOLDs now that PO 59081 / 59006 exist on live purchase lines.

Kyle 2026-09-21: 59081 should be in KIMCO. Prior missing_po was receipt-only
search. Purchase-line GET finds PO59081-GAS AND SUPPLY (lookup 7083, 23 lines)
and PO59006-GAS AND SUPPLY (lookup 7008, 1 line). Received_Quantity is 0 on
every line. Receipts list (24,311) has zero rows citing 59081 / 59006.

Enter Type 3 + PDF on batch 720. Do not Success (no leftovers). Stay on 720.
HOLD missing_receipt. No Mail.Send. No Type 4 (PO exists).
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
from ap_clerk.cli import _optional_graph_client, run_enter
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.quality_v12 import apply_exception_category_owner
from ap_clerk.report import write_report
from ap_clerk.rules import extract_po_number, lookup_id, lookup_text, money
from gas_supply_0917 import (
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    VENDOR_ID_HINT,
    VENDOR_NAME,
    apply_gas_exception_category_owner,
    bills_from_message,
    exact_invoice_number,
    find_gas_messages,
    finish_entered_rows,
    load_list_receipts,
    merge_sheet_rows,
    prior_rows_from_sidecar,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.gas_po_59081_enter")

WANTED = {
    "0040430010": {"po": "59081", "amount": 14.0},
    "0040424839": {"po": "59081", "amount": 224.94},
    "0040438057": {"po": "59081", "amount": 36.12},
    "0040438056": {"po": "59081", "amount": 207.53},
    "0040438055": {"po": "59081", "amount": 27.82},
    "0040438053": {"po": "59081", "amount": 182.5},
    "0040417672": {"po": "59081", "amount": 3856.77},
    "0040414962": {"po": "59006", "amount": 7840.8},
}

# Live GET 2026-09-21 purchase_lines. Received_Quantity 0 on every line.
PO_59081_LINES = (
    (17890, "PO59081-01", 1.0, 14.0, "MLW49-56-7240"),
    (17891, "PO59081-02", 3.0, 11.3, "GHS4000A"),
    (17892, "PO59081-03", 4.0, 47.76, "STY57-533A"),
    (17893, "PO59081-04", 1.0, 36.12, "PFXPXTW1425R"),
    (17894, "PO59081-05", 1.0, 108.82, "MIL269767"),
    (17895, "PO59081-06", 1.0, 98.71, "MIL269771"),
    (17896, "PO59081-07", 2.0, 13.91, "PRTJ7324H"),
    (17897, "PO59081-08", 1.0, 182.5, "ELCWW10204F-DP"),
    (17898, "PO59081-09", 100.0, 3.88, "CGW42304"),
    (17899, "PO59081-10", 200.0, 5.25, "3-M60440227472"),
    (17900, "PO59081-11", 48.0, 5.5, "WSP1801S0020"),
    (17901, "PO59081-12", 60.0, 9.63, "PFXPX23T37"),
    (17902, "PO59081-13", 250.0, 0.87, "PFXPX14H35"),
    (17903, "PO59081-14", 250.0, 0.97, "PFXPX14H45"),
    (17904, "PO59081-15", 12.0, 15.96, "TIL48M"),
    (17905, "PO59081-16", 48.0, 4.68, "PIP34-874/XL"),
    (17906, "PO59081-17", 4.0, 6.44, "MKL96007"),
    (17907, "PO59081-18", 1.0, 184.04, "MIL269765"),
    (17908, "PO59081-19", 1.0, 180.18, "MIL301253"),
    (17909, "PO59081-20", 1.0, 36.12, "PFXPXTW1425R"),
    (17910, "PO59081-21", 3.0, 18.83, "STY54-024"),
    (17911, "PO59081-22", 50.0, 1.24, "ANC30SS"),
    (17912, "PO59081-23", 2.0, 78.37, "VIC0323-0252"),
)
PO_59006_LINES = ((17889, "PO59006-01", 880.0, 8.91, "LINEDS30778"),)

INVOICE_PO_COVER = {
    "0040430010": ("PO59081-01",),
    "0040424839": ("PO59081-02", "PO59081-03"),
    "0040438057": ("PO59081-04",),
    "0040438056": ("PO59081-05", "PO59081-06"),
    "0040438055": ("PO59081-07",),
    "0040438053": ("PO59081-08",),
    "0040417672": tuple(f"PO59081-{i:02d}" for i in range(9, 24)),
    "0040414962": ("PO59006-01",),
}

SHEET = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"


def line_ext(qty: float, unit: float) -> float:
    return round(qty * unit, 2)


def po_cover_amount(invoice: str) -> float:
    by_name = {row[1]: row for row in (*PO_59081_LINES, *PO_59006_LINES)}
    total = 0.0
    for name in INVOICE_PO_COVER[invoice]:
        _lid, _n, qty, unit, _part = by_name[name]
        total = round(total + line_ext(qty, unit), 2)
    return total


def received_qty_blocks_success(received: Any) -> bool:
    amt = money(received)
    return amt is None or amt == 0.0


def comments_1_missing_receipt(client: KimcoClient, kimco_id: int, *, invoice: str, po: str) -> dict[str, Any]:
    """Plain-text Comments tab. Ruben mention-id is not proven — do not invent."""
    html = (
        f"HOLD missing_receipt on Gas and Supply {invoice} PO {po}. "
        f"PO header/lines exist on live. Received_Quantity is 0. "
        f"No GI leftovers to Select Receipts. Stay on batch 720. "
        f"Receiving must receive the PO. Do not Transfer AP."
    )
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
    try:
        _body, status, error = client.update("ap_invoices", int(kimco_id), payload)
    except KimcoError as exc:
        return {"status": "blocked", "error": str(exc)[:200], "invent": False}
    after = {}
    try:
        after = client.get_item("ap_invoices", int(kimco_id))
    except KimcoError:
        after = {}
    rows = (after.get("lists") or {}).get("Comments_1") or []
    persisted = bool(rows)
    return {
        "status": "persisted" if status < 400 and persisted else f"put-{status}",
        "put": status,
        "error": error,
        "tab_rows": len(rows) if isinstance(rows, list) else 0,
        "invent": False,
    }


def _collect_wanted_bills(graph, pdf_dir: Path) -> list[dict[str, Any]]:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, dict[str, Any]] = {}
    for msg in find_gas_messages(graph):
        if not msg.get("hasAttachments"):
            continue
        for bill in bills_from_message(graph, msg, pdf_dir):
            inv = exact_invoice_number(bill.get("invoice_number"))
            if inv not in WANTED:
                continue
            if bill.get("pdf_unavailable") or bill.get("amount") in (None, ""):
                continue
            wanted[inv] = bill
    return [wanted[k] for k in WANTED if k in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live.")
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph required to attach vendor PDFs. No Mail.Send.")
    verified = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    if not verified.get("matches_expected"):
        raise SystemExit(f"Batch 720 name mismatch: {verified.get('name')}")
    bills = _collect_wanted_bills(graph, PDF_DIR)
    found_inv = [exact_invoice_number(b.get("invoice_number")) for b in bills]
    missing_pdf = [inv for inv in WANTED if inv not in found_inv]
    cover = {inv: po_cover_amount(inv) for inv in WANTED}
    summary: dict[str, Any] = {
        "invent": False,
        "mail_send": False,
        "po_59081": {
            "exists": True,
            "lookup_id": 7083,
            "text": "PO59081-GAS AND SUPPLY",
            "line_count": 23,
            "received_qty_all_zero": True,
            "open_receipts": 0,
        },
        "po_59006": {
            "exists": True,
            "lookup_id": 7008,
            "text": "PO59006-GAS AND SUPPLY",
            "line_count": 1,
            "received_qty_all_zero": True,
            "open_receipts": 0,
        },
        "pdf_found": found_inv,
        "pdf_missing": missing_pdf,
        "po_cover_amounts": cover,
        "dry_run": args.dry_run,
    }
    if args.dry_run:
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if not bills:
        print(json.dumps({**summary, "blocker": "no PDFs for wanted invoices"}, indent=2))
        return 2
    enter_rows = run_enter(
        client,
        bills,
        batch_name=PREFERRED_BATCH_NAME,
        pdf_dir=PDF_DIR,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=False,
    )
    receipts = load_list_receipts(client)
    new_rows, finishes, gets = finish_entered_rows(
        client,
        graph,
        parsed_bills=bills,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=VENDOR_ID_HINT,
    )
    comments = {}
    for row in new_rows:
        kid = row.get("KIMCO id")
        inv = exact_invoice_number(row.get("Invoice #"))
        if kid in (None, "") or inv not in WANTED:
            continue
        comments[inv] = comments_1_missing_receipt(
            client, int(kid), invoice=inv, po=WANTED[inv]["po"]
        )
    prior = prior_rows_from_sidecar(SHEET)
    rows = merge_sheet_rows(prior, new_rows)
    write_report(SHEET, rows)
    sidecar = json.loads(SHEET_JSON.read_text())
    sidecar["proof"] = "gas-supply-0917-po-59081-enter"
    sidecar["rows"] = rows
    sidecar["po_59081_enter"] = {
        **summary,
        "enter_rows": new_rows,
        "finishes": finishes,
        "comments_1": comments,
        "gets": {k: {"id": v.get("id"), "amount": v.get("invoice_amount")} for k, v in gets.items()},
    }
    SHEET_JSON.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "sheet": str(SHEET),
                "entered": [
                    {
                        "invoice": r.get("Invoice #"),
                        "kimco_id": r.get("KIMCO id"),
                        "result": r.get("Result"),
                        "batch": r.get("Batch"),
                        "why": (r.get("Why") or "")[:180],
                    }
                    for r in new_rows
                ],
                "pdf_missing": missing_pdf,
                "comments_1": comments,
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
