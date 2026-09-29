"""Kyle 2026-09-22: check/enter Gas & Supply 0040438052 from the 9/17 multi-invoice PDF.

Find the 2026-09-17 Gas & Supply Invoice/Statement email (received
2026-09-18T04:46Z). List every invoice # in that PDF (NOTE-36 split).
If 0040438052 has no KIMCO header, enter it on batch 720 with that
invoice's page(s) only.

Select Receipts if leftovers exist. Fees id 11. Type 4 Misc → Lines-K
(NOTE-42). PPV |total| under $75 → Success; $75+ → Transfer AP + @Shawn
104, no Select. Success only Treyce-ready.

Gas receiving owner is blank on Kyle's map → do NOT @tag missing_receipt.

NOTE-51: if this finish is Success, update accountspayable@ Outlook.
Parent Entered in AI only when ALL siblings from that PDF are Success.

No Mail.Send. invent=false. Leave McMaster / O'Neal / Legacy alone.
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
from ap_clerk.quality_v12 import (
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
)
from ap_clerk.report import write_report
from ap_clerk.rules import SHAWN_MCKIBBEN, lookup_id, lookup_text, money
from gas_shawn_finish import NOTE47_PPV_MAX_ABS, ppv_abs_over_note47
from gas_supply_0917 import (
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    VENDOR_ID_HINT,
    VENDOR_NAME,
    apply_gas_exception_category_owner,
    apply_over_ppv_transfer_ap,
    bills_from_message,
    exact_invoice_number,
    find_gas_messages,
    finish_entered_rows,
    is_gas_invoice_email,
    kimco_gas_numbers,
    load_list_receipts,
    merge_sheet_rows,
    over_ppv_hold_comment,
    prior_rows_from_sidecar,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.gas_0040438052")

TARGET = "0040438052"
PARENT_RECEIVED_PREFIXES = ("2026-09-17", "2026-09-18")
SHEET = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-17-gas-supply.json"
PROOF = ROOT / "runs" / "gas-0040438052-2026-09-22.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"
SHAWN_MENTION_ID = 104


def _received_iso(msg: dict[str, Any]) -> str:
    return str(msg.get("receivedDateTime") or "")


def _is_0917_statement(msg: dict[str, Any]) -> bool:
    if not is_gas_invoice_email(msg):
        return False
    if not msg.get("hasAttachments"):
        return False
    received = _received_iso(msg)
    if not received.startswith(PARENT_RECEIVED_PREFIXES):
        return False
    subject = str(msg.get("subject") or "").lower()
    return "invoice/statement" in subject or "invoice" in subject


def _bill_summary(bill: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_number": exact_invoice_number(bill.get("invoice_number")),
        "date": bill.get("date"),
        "po": bill.get("po"),
        "amount": money(bill.get("amount")),
        "pages": [
            bill.get("multi_invoice_page_start"),
            bill.get("multi_invoice_page_end"),
        ],
        "pdf_path": bill.get("pdf_path"),
        "pdf_split": bill.get("pdf_split"),
        "fees": [
            {"name": f.get("name"), "amount": f.get("amount")}
            for f in (bill.get("fees") or [])
        ],
        "lines": [
            {
                "part": ln.get("part") or ln.get("label"),
                "qty": ln.get("qty"),
                "unit_price": ln.get("unit_price"),
                "amount": ln.get("amount"),
            }
            for ln in (bill.get("lines") or [])
        ],
        "type4_candidate": not bool(str(bill.get("po") or "").strip()),
    }


def _sheet_row_for(rows: list[dict[str, Any]], invoice: str) -> dict[str, Any] | None:
    for row in rows:
        if exact_invoice_number(row.get("Invoice #")) == invoice:
            return {
                "Invoice #": row.get("Invoice #"),
                "KIMCO id": row.get("KIMCO id"),
                "Result": row.get("Result"),
                "PO": row.get("PO"),
                "Amount": row.get("Amount"),
                "Exception category": row.get("Exception category"),
            }
    return None


def _blank_gas_missing_receipt(row: dict[str, Any]) -> dict[str, Any]:
    """Kyle map: Gas receiving owner is blank → do not @tag."""
    out = apply_gas_exception_category_owner(apply_exception_category_owner(dict(row)))
    if str(out.get(COL_EXCEPTION_CATEGORY) or "") != "missing_receipt":
        return out
    out[COL_EXCEPTION_OWNER] = ""
    why = str(out.get("Why") or "")
    why = why.replace("@Ruben Perez", "no receiving @tag (Gas owner blank)")
    why = why.replace("owner=Ruben Perez", "owner=none (Gas receiving owner blank)")
    if "Gas receiving owner blank" not in why:
        why = f"{why} Gas receiving owner blank — do not @tag missing_receipt."
    out["Why"] = why
    return out


def _comments_1_shawn(client: KimcoClient, kimco_id: int, html: str) -> dict[str, Any]:
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {
            "Comments_1": [
                {
                    "state": "Added",
                    "values": {
                        "HtmlValue": (
                            f'<span data-mention-id="{SHAWN_MENTION_ID}" '
                            f'data-mention-name="Shawn McKibben">{SHAWN_MCKIBBEN}</span> '
                            f"{html}"
                        ),
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
    return {"status": "put", "put": status, "error": error, "invent": False}


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
    parents: list[dict[str, Any]] = []
    target_bill: dict[str, Any] | None = None
    target_mid = ""
    sibling_bills: list[dict[str, Any]] = []

    for msg in find_gas_messages(graph):
        if not _is_0917_statement(msg):
            continue
        mid = str(msg.get("id") or "")
        bills = bills_from_message(graph, msg, PDF_DIR)
        invoices = []
        for bill in bills:
            inv = exact_invoice_number(bill.get("invoice_number"))
            if not inv:
                continue
            invoices.append(
                {
                    **_bill_summary(bill),
                    "kimco_id": entered.get(inv),
                    "sheet": _sheet_row_for(prior, inv),
                }
            )
            if inv == TARGET:
                target_bill = bill
                target_mid = mid
                sibling_bills = bills
        parents.append(
            {
                "message_id": mid,
                "received": _received_iso(msg),
                "subject": str(msg.get("subject") or "")[:140],
                "from": (msg.get("from") or {}).get("emailAddress")
                if isinstance(msg.get("from"), dict)
                else msg.get("from"),
                "categories": msg.get("categories") or [],
                "invoice_count": len(invoices),
                "invoices": invoices,
            }
        )

    parents.sort(key=lambda p: str(p.get("received") or ""), reverse=True)
    target_kimco = entered.get(TARGET)
    proof: dict[str, Any] = {
        "proof": "gas-0040438052-2026-09-22",
        "invent": False,
        "mail_send": False,
        "note51": True,
        "note47_ppv_max": NOTE47_PPV_MAX_ABS,
        "batch": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "target": TARGET,
        "target_kimco_id": target_kimco,
        "target_already_entered": target_kimco not in (None, ""),
        "parents": parents,
        "dry_run": args.dry_run,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    if target_bill is None:
        proof["blocker"] = "0040438052 not found in 9/17–9/18 Gas Invoice/Statement PDFs"
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 2

    sibling_invs = [
        exact_invoice_number(b.get("invoice_number"))
        for b in sibling_bills
        if exact_invoice_number(b.get("invoice_number"))
    ]
    sibling_status = [
        {
            "invoice": inv,
            "kimco_id": entered.get(inv),
            "sheet": _sheet_row_for(prior, inv),
        }
        for inv in sibling_invs
    ]
    proof["target_bill"] = _bill_summary(target_bill)
    proof["siblings"] = sibling_status

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
        proof["would"] = "enter-0040438052-on-720"
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
                po=str(target_bill.get("po") or ""),
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

    # NOTE-51: parent Outlook from ALL siblings on this PDF, including this enter.
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
    sidecar["proof"] = "gas-supply-0917-0040438052"
    sidecar["rows"] = rows
    sidecar["gas_0040438052"] = {
        "target": TARGET,
        "enter_rows": new_rows,
        "finishes": finishes,
        "gets": {k: {"id": v.get("id"), "amount": v.get("invoice_amount")} for k, v in gets.items()},
        "outlook": outlook,
        "transfer_ap": transfer,
        "comments_1": comments,
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
                }
                for r in new_rows
            ],
            "outlook": outlook,
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
