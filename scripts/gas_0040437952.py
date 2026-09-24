"""Kyle 2026-09-22: enter Gas & Supply 0040437952 on batch 720.

9/17 multi-invoice PDF leftover (pages 4–5, PO 59006, $241.46).
Sibling invoices already entered; parent Outlook stays Entered with
issues until ALL siblings are Success (NOTE-51).

Type 3 + PO 59006. Attach this invoice's page(s) only. Select Receipts
if leftovers remain after 10172 (0040414962 / 880@8.91 LINEDS30778).
Fees id 11. PPV |total| under $75. Lines-K only if Misc Type 4.
Gas receiving owner is blank → do NOT @tag missing_receipt.

No Mail.Send. invent=false.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
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
from ap_clerk.outlook_finish import promote_ap_outlook_after_success
from ap_clerk.report import write_report
from ap_clerk.rules import SHAWN_MCKIBBEN, lookup_id, lookup_text, money
from gas_0040438052 import (
    SHAWN_MENTION_ID,
    _bill_summary,
    _blank_gas_missing_receipt,
    _comments_1_shawn,
    _is_0917_statement,
    _received_iso,
    _sheet_row_for,
)
from gas_shawn_finish import NOTE47_PPV_MAX_ABS, ppv_abs_over_note47
from gas_supply_0917 import (
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    VENDOR_ID_HINT,
    apply_over_ppv_transfer_ap,
    bills_from_message,
    exact_invoice_number,
    find_gas_messages,
    finish_entered_rows,
    kimco_gas_numbers,
    load_list_receipts,
    merge_sheet_rows,
    over_ppv_hold_comment,
    prior_rows_from_sidecar,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.gas_0040437952")

TARGET = "0040437952"
EXPECTED_PO = "59006"
EXPECTED_AMOUNT = 241.46
SHEET = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.json"
PROOF = ROOT / "runs" / "gas-0040437952-2026-09-22.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"


def _po_59006_leftovers(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for rec in receipts or []:
        blob = " ".join(
            str(rec.get(k) or "")
            for k in ("po", "po_number", "po_text", "part", "name", "display")
        )
        if "59006" not in blob:
            continue
        hits.append(
            {
                "id": rec.get("id"),
                "part": rec.get("part"),
                "qty": rec.get("qty"),
                "unit_price": rec.get("unit_price"),
                "po": rec.get("po") or rec.get("po_number"),
            }
        )
    return hits


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
        raise SystemExit("Graph required. No Mail.Send.")
    verified = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    if not verified.get("matches_expected"):
        raise SystemExit(f"Batch 720 name mismatch: {verified}")

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    entered = kimco_gas_numbers(client, VENDOR_ID_HINT)
    prior = prior_rows_from_sidecar(SHEET_JSON)
    target_bill: dict[str, Any] | None = None
    target_mid = ""
    sibling_bills: list[dict[str, Any]] = []
    parent_cats: list[str] = []

    for msg in find_gas_messages(graph):
        if not _is_0917_statement(msg):
            continue
        mid = str(msg.get("id") or "")
        bills = bills_from_message(graph, msg, PDF_DIR)
        for bill in bills:
            inv = exact_invoice_number(bill.get("invoice_number"))
            if inv != TARGET:
                continue
            target_bill = bill
            target_mid = mid
            sibling_bills = bills
            parent_cats = list(msg.get("categories") or [])

    target_kimco = entered.get(TARGET)
    proof: dict[str, Any] = {
        "proof": "gas-0040437952-2026-09-22",
        "invent": False,
        "mail_send": False,
        "note51": True,
        "note47_ppv_max": NOTE47_PPV_MAX_ABS,
        "batch": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "target": TARGET,
        "expected_po": EXPECTED_PO,
        "expected_amount": EXPECTED_AMOUNT,
        "target_kimco_id": target_kimco,
        "target_already_entered": target_kimco not in (None, ""),
        "parent_categories_before": parent_cats,
        "dry_run": args.dry_run,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    if target_bill is None:
        proof["blocker"] = "0040437952 not found in 9/17–9/18 Gas Invoice/Statement PDFs"
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 2

    sibling_invs = [
        exact_invoice_number(b.get("invoice_number"))
        for b in sibling_bills
        if exact_invoice_number(b.get("invoice_number"))
    ]
    proof["target_bill"] = _bill_summary(target_bill)
    proof["siblings"] = [
        {
            "invoice": inv,
            "kimco_id": entered.get(inv),
            "sheet": _sheet_row_for(prior, inv),
        }
        for inv in sibling_invs
    ]

    if target_kimco not in (None, ""):
        rec = client.get_item("ap_invoices", int(target_kimco))
        vals = rec.get("values") or {}
        proof["already_entered"] = {
            "kimco_id": int(target_kimco),
            "invoice": exact_invoice_number(vals.get("Invoice_Number")),
            "amount": money(vals.get("Invoice_Amount")),
            "po": lookup_text(vals.get("PO") or vals.get("Purchase_Order")),
            "type": vals.get("Invoice_Type"),
            "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
            "batch": lookup_text(vals.get("AP_Invoice_Batch")),
            "sheet": _sheet_row_for(prior, TARGET),
        }
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 0

    if args.dry_run:
        proof["would"] = "enter-0040437952-on-720"
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 0

    enter_rows = run_enter(
        client,
        [target_bill],
        batch_name=PREFERRED_BATCH_NAME,
        pdf_dir=PDF_DIR,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=False,
    )
    receipts = load_list_receipts(client)
    proof["po_59006_leftovers"] = _po_59006_leftovers(receipts)
    new_rows, finishes, gets = finish_entered_rows(
        client,
        graph,
        parsed_bills=[target_bill],
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=VENDOR_ID_HINT,
    )
    transfer = None
    comments = None
    for row in new_rows:
        kid = row.get("KIMCO id")
        if kid in (None, ""):
            continue
        finish = finishes.get(TARGET) or {}
        ppv_amt = money(finish.get("ppv_amount")) if isinstance(finish, dict) else None
        if (
            str(row.get("Result") or "") != "Success"
            and (finish.get("select_zero") or ppv_abs_over_note47(ppv_amt))
        ):
            comment = over_ppv_hold_comment(
                invoice_number=TARGET,
                po=str(target_bill.get("po") or EXPECTED_PO),
                pdf_amount=target_bill.get("amount"),
            )
            transfer = apply_over_ppv_transfer_ap(client, kimco_id=int(kid), comment=comment)
            comments = _comments_1_shawn(client, int(kid), comment)
            row["Result"] = "HOLD"
            row["Why"] = (
                f"HOLD (price-does-not-match): leftover vs invoice line is over the "
                f"NOTE-47 ${NOTE47_PPV_MAX_ABS:.0f} PPV gate. Receipts were NOT selected. "
                f"{SHAWN_MCKIBBEN} mention-id {SHAWN_MENTION_ID}."
            )
        row.update(_blank_gas_missing_receipt(row))

    sibling_results: list[str] = []
    any_header = False
    for inv in sibling_invs:
        if inv == TARGET:
            this = next((r.get("Result") for r in new_rows), "")
            sibling_results.append(str(this or ""))
            if any(r.get("KIMCO id") not in (None, "") for r in new_rows):
                any_header = True
            continue
        sheet = _sheet_row_for(prior, inv)
        if sheet and sheet.get("KIMCO id") not in (None, ""):
            any_header = True
            sibling_results.append(str(sheet.get("Result") or ""))
        elif entered.get(inv):
            any_header = True
            sibling_results.append("Success")
        else:
            sibling_results.append("HOLD")
    outlook = promote_ap_outlook_after_success(
        graph, target_mid, sibling_results, any_header=any_header
    )
    for row in new_rows:
        row["outlook"] = outlook.get("status")
        row["Flag in Outlook"] = "Yes"
        row["Flag status"] = outlook.get("flag")

    rows = merge_sheet_rows(prior, new_rows)
    write_report(SHEET, rows)
    sidecar = json.loads(SHEET_JSON.read_text())
    sidecar["proof"] = "gas-supply-0917-0040437952"
    sidecar["rows"] = rows
    sidecar["gas_0040437952"] = {
        "target": TARGET,
        "enter_rows": new_rows,
        "finishes": finishes,
        "gets": {k: {"id": v.get("id"), "amount": v.get("invoice_amount")} for k, v in gets.items()},
        "outlook": outlook,
        "transfer_ap": transfer,
        "comments_1": comments,
        "po_59006_leftovers": proof.get("po_59006_leftovers"),
    }
    SHEET_JSON.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    proof.update(
        {
            "entered": [
                {
                    "invoice": r.get("Invoice #"),
                    "kimco_id": r.get("KIMCO id"),
                    "result": r.get("Result"),
                    "po": r.get("PO"),
                    "amount": r.get("Amount"),
                    "why": (r.get("Why") or "")[:240],
                    "exception_category": r.get("Exception category"),
                    "exception_owner": r.get("Exception owner"),
                    "batch": r.get("Batch"),
                    "outlook": r.get("outlook"),
                    "fees": r.get("Fees and surcharges"),
                    "ppv": r.get("PPV"),
                    "receipts": r.get("Receipts"),
                }
                for r in new_rows
            ],
            "outlook": outlook,
            "sibling_results": sibling_results,
            "transfer_ap": transfer,
            "comments_1": comments,
            "sheet": str(SHEET),
        }
    )
    PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    print(json.dumps(proof, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
