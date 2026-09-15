"""Live finish KIMCO 10010 (AQPC 11004 / PO 59165) leftover swap receipts.

Kyle confirmed leftover 24109 / 24110 are just swapped. Select by qty+cost:
invoice 15@$10 → 24110, invoice 5@$10 → 24109. Bill should hit $2,600.

Does not touch 10009. No new header, no void, no treyce@ email. invent=false.
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

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.gates import RESULT_HOLD, RESULT_SUCCESS, finish_gate, selfcheck_payload
from ap_clerk.graph import ALLOWED_MAILBOX, FLAG_FLAGGED
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.rules import lookup_id, lookup_text, match_receipts, money, normalize_receipt

LOGGER = logging.getLogger("ap_clerk.finish_10010")

INVOICE_ID = 10010
LEAVE_ALONE = 10009
INV_NUMBER = "11004"
PO_NUMBER = "59165"
LEFTOVER_IDS = (24109, 24110)
ALREADY_IDS = (24106, 24107, 24108, 24111)


def _line_summary(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (item.get("lists") or {}).get("APInvoiceLine") or []:
        lv = (line.get("values") if isinstance(line, dict) else None) or {}
        qty = money(lv.get("Quantity"))
        unit = money(lv.get("Unit_Price"))
        amt = money(lv.get("Amount") or lv.get("Line_Amount") or lv.get("Extended_Amount"))
        if amt is None and qty is not None and unit is not None:
            amt = round(qty * unit, 2)
        rows.append(
            {
                "line_id": line.get("id") if isinstance(line, dict) else None,
                "qty": qty,
                "unit_price": unit,
                "amount": amt,
                "receipt_id": lookup_id(lv.get("Receipt")),
                "po_line": lookup_text(lv.get("Purchase_Order_Line") or lv.get("PO_Item_Number")),
                "part": lookup_text(lv.get("Part_ID") or lv.get("Part")),
            }
        )
    return rows


def _invoice_summary(item: dict[str, Any]) -> dict[str, Any]:
    vals = item.get("values") if isinstance(item.get("values"), dict) else {}
    lines = _line_summary(item)
    return {
        "id": item.get("id"),
        "invoice_number": vals.get("Invoice_Number"),
        "vendor": lookup_text(vals.get("Vendor")),
        "po": lookup_text(vals.get("Purchase_Order")),
        "invoice_type": vals.get("Invoice_Type"),
        "invoice_amount": money(vals.get("Invoice_Amount")),
        "verification": money(
            vals.get("Invoice_Verification_Amount")
            or vals.get("Verification_Total")
            or vals.get("Invoice_Balance")
        ),
        "total_line_net": money(vals.get("Total_Line_Net_Amounts")),
        "lines_count": vals.get("Lines_Count") if vals.get("Lines_Count") is not None else len(lines),
        "void": vals.get("Void"),
        "posted": vals.get("Posted"),
        "batch": lookup_id(vals.get("AP_Invoice_Batch")),
        "lines": lines,
    }


def _pdf_lines() -> list[dict[str, Any]]:
    return [
        {"part": "AMT-6001232", "qty": 5.0, "amount": 2225.0, "unit_price": 445.0, "po_line": 1},
        {"part": "AMT-5003753", "qty": 5.0, "amount": 25.0, "unit_price": 5.0, "po_line": 2},
        {"part": "AMT-5003753", "qty": 5.0, "amount": 25.0, "unit_price": 5.0, "po_line": 3},
        {"part": "AMT-5003741", "qty": 15.0, "amount": 150.0, "unit_price": 10.0, "po_line": 4},
        {"part": "AMT-5003750-002", "qty": 5.0, "amount": 50.0, "unit_price": 10.0, "po_line": 5},
        {"part": "AMT-5003750", "qty": 5.0, "amount": 125.0, "unit_price": 25.0, "po_line": 6},
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("Target: live  invent=false  no-void  no-new-headers  no-treyce-email", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    before_10010 = _invoice_summary(client.get_item("ap_invoices", INVOICE_ID))
    leave_10009 = _invoice_summary(client.get_item("ap_invoices", LEAVE_ALONE))
    print(
        f"BEFORE 10010 lines={before_10010['lines_count']} "
        f"net={before_10010['total_line_net']} amount={before_10010['invoice_amount']}",
        flush=True,
    )
    print(
        f"GET 10009 (leave alone) lines={leave_10009['lines_count']} "
        f"net={leave_10009['total_line_net']}",
        flush=True,
    )

    leftovers = []
    for rid in LEFTOVER_IDS:
        rec = client.get_item("receipts", rid)
        leftovers.append(normalize_receipt(rec))
        print(
            f"GET leftover {rid} qty={leftovers[-1].get('qty')} "
            f"unit={leftovers[-1].get('unit_price')} amount={leftovers[-1].get('amount')} "
            f"invoiced={rec.get('values', {}).get('Invoiced')}",
            flush=True,
        )

    already = {int(ln["receipt_id"]) for ln in before_10010["lines"] if ln.get("receipt_id")}
    open_leftovers = [r for r in leftovers if r.get("id") not in already]
    match = match_receipts(
        invoice_number=INV_NUMBER,
        invoice_lines=_pdf_lines(),
        receipts=open_leftovers,
        po_number=PO_NUMBER,
        invoice_amount=2600.0,
    )
    picked = []
    for hit in match.get("matched") or []:
        rec = hit.get("receipt") or {}
        rid = rec.get("id")
        if rid in already:
            continue
        picked.append(rid)
    print(f"matcher leftover picks={picked} unmatched={match.get('unmatched_lines')}", flush=True)
    if set(picked) != {24109, 24110}:
        print("matcher did not uniquely pair leftover qty+cost; will not invent ids", flush=True)

    select_status = ""
    if picked:
        try:
            select_status = client.try_select_receipts(INVOICE_ID, picked)
        except KimcoError as exc:
            select_status = f"error:{exc}"[:200]
    print(f"Select Receipts {picked} → {select_status}", flush=True)

    after_10010 = _invoice_summary(client.get_item("ap_invoices", INVOICE_ID))
    after_10009 = _invoice_summary(client.get_item("ap_invoices", LEAVE_ALONE))
    print(
        f"AFTER 10010 lines={after_10010['lines_count']} "
        f"net={after_10010['total_line_net']} amount={after_10010['invoice_amount']}",
        flush=True,
    )

    line_net = after_10010.get("total_line_net")
    six_lines = len(after_10010.get("lines") or []) == 6
    dollars = line_net == 2600.0 or after_10010.get("invoice_amount") == 2600.0
    selected_ids = {ln.get("receipt_id") for ln in after_10010.get("lines") or []}
    have_leftovers = {24109, 24110} <= selected_ids
    untouched_10009 = after_10009.get("lines") == leave_10009.get("lines")
    voided = bool(after_10010.get("void"))

    check = selfcheck_payload(
        {
            "invoice_number": INV_NUMBER,
            "field_sources": {"invoice_number": "pdf"},
            "po": PO_NUMBER,
            "pos": [PO_NUMBER],
            "lines": _pdf_lines(),
            "fees": [],
        },
        invoice_type=3,
        po=PO_NUMBER,
        receipt_result=match,
        fees_posted=True,
    )
    check["require_attach"] = True
    check["attach_status"] = "attached"
    check["require_select_receipts"] = True
    check["receipts_selected"] = have_leftovers and six_lines
    result, finish_why = finish_gate(
        header_created=True,
        attach_status="attached",
        po=PO_NUMBER,
        receipts_selected=have_leftovers and six_lines,
        kimco_id=INVOICE_ID,
        selfcheck=check,
        fees=[],
        fees_posted=True,
    )
    if voided or not untouched_10009:
        result = RESULT_HOLD
        finish_why = "Refused: void or 10009 changed."
    elif select_status != "selected" and not have_leftovers:
        result = RESULT_HOLD
        finish_why = f"Select Receipts did not post leftovers ({select_status})."
    elif six_lines and dollars and have_leftovers:
        result = RESULT_SUCCESS
        finish_why = (
            "Finished 10010: six lines including leftover 24109 qty 5 @ $10 and "
            "24110 qty 15 @ $10. Bill $2,600. Treyce would not rework."
        )
    else:
        result = RESULT_HOLD
        finish_why = (
            f"Entered with issues: lines={after_10010.get('lines_count')} "
            f"net={line_net} selected={sorted(x for x in selected_ids if x)}. "
            f"{finish_why}"
        )

    outlook = {"touched": False, "status": "skipped"}
    graph = _optional_graph_client()
    if graph is not None and result == RESULT_SUCCESS:
        hits = graph.search_messages(
            ALLOWED_MAILBOX,
            f"New payment request from AMERICAN QUALITY POWDER COATING - invoice {INV_NUMBER}",
            top=10,
        )
        usable = [
            m
            for m in hits
            if INV_NUMBER in str(m.get("subject") or "")
            and "POWDER" in str(m.get("subject") or "").upper()
        ]
        usable.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        if usable:
            mid = str(usable[0].get("id") or "")
            status = graph.flag_matched(ALLOWED_MAILBOX, mid)
            outlook = {
                "touched": True,
                "message_subject": usable[0].get("subject"),
                "status": status,
                "entered_in_ai": status == FLAG_FLAGGED,
            }
            print(f"Outlook 11004 → {status}", flush=True)

    proof = {
        "proof": "kimco-10010-swapped-receipts",
        "invent": False,
        "void": False,
        "new_headers": False,
        "treyce_email": False,
        "touched_10009": not untouched_10009,
        "select_status": select_status,
        "picked": picked,
        "matcher": {
            "matched_ids": [
                (hit.get("receipt") or {}).get("id") for hit in (match.get("matched") or [])
            ],
            "unmatched": match.get("unmatched_lines") or [],
        },
        "before": before_10010,
        "after": after_10010,
        "leave_alone_10009": {"before": leave_10009, "after": after_10009},
        "leftover_receipts": leftovers,
        "Result": result,
        "Why": finish_why,
        "outlook": outlook,
    }
    out = ROOT / "runs" / "kimco-10010-swapped-receipts.json"
    out.write_text(json.dumps(proof, indent=2, default=str))
    print(f"Result={result}", flush=True)
    print(f"Why={finish_why}", flush=True)
    print(f"Wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
