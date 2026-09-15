"""Finish 10010 swapped leftovers and enter 5 more AQPC bills on batch 711.

No Intuit login. No Mail.Send. invent=false.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from aqpc_plus4 import bill_from_message, live_get_proof, summarize_parse
from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.browser_pdf import format_intuit_session_presence
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged
from ap_clerk.kimco import KimcoClient
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text, money

LOGGER = logging.getLogger("ap_clerk.aqpc_batch711_10")

BATCH_NAME = "API Agent - 9/15/26"
BATCH_ID = 711
VENDOR_NEEDLE = "AMERICAN QUALITY POWDER COATING"
ALREADY = {
    "10917",
    "10918",
    "10920",
    "10921",
    "11002",
    "10999",
    "11003",
    "11004",
    "11005",
}
SWAP_RECEIPTS = (
    {"id": 24109, "qty": 5.0, "unit": 10.0},
    {"id": 24110, "qty": 15.0, "unit": 10.0},
)
KNOWN_TEN = [
    {"invoice": "11002", "kimco_id": 10007},
    {"invoice": "10999", "kimco_id": 10008},
    {"invoice": "11003", "kimco_id": 10009},
    {"invoice": "11004", "kimco_id": 10010},
    {"invoice": "11005", "kimco_id": 10011},
]
SHEET_COLUMNS = [
    "Vendor",
    "Invoice #",
    "date",
    "PO",
    "Amount",
    "Result",
    "Why",
    "KIMCO id",
    "Batch",
    "Fees",
    "PPV",
    "Attach",
    "Flag status",
    "Receipts",
]


def _subject_inv(subject: str) -> str:
    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def _receipt_ids(proof: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        if rid not in (None, ""):
            out.add(int(rid))
    return out


def format_receipts(proof: dict[str, Any]) -> str:
    bits: list[str] = []
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        qty = line.get("qty")
        unit = line.get("unit")
        po_line = line.get("po_line") or ""
        bits.append(f"{rid} {qty:g}@{unit} {po_line}".strip())
    return "; ".join(bits)


def find_aqpc_payment_requests(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        f"New payment request from {VENDOR_NEEDLE}",
        f"{VENDOR_NEEDLE} - invoice",
    ]
    for n in list(range(10900, 11021)) + [11006, 11007, 11008, 11009, 11010]:
        needles.append(f"New payment request from {VENDOR_NEEDLE} - invoice {n}")
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=50)
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            mid = str(msg.get("id") or "")
            subject = str(msg.get("subject") or "")
            if VENDOR_NEEDLE not in subject.upper():
                continue
            if "payment request" not in subject.lower() and "invoice" not in subject.lower():
                continue
            if mid:
                seen[mid] = msg
    return list(seen.values())


def kimco_aqpc_numbers(client: KimcoClient) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        vendor_id = lookup_id(vals.get("Vendor"))
        vendor_txt = str(lookup_text(vals.get("Vendor")) or "").upper()
        if vendor_id != 22 and "QUALITY POWDER" not in vendor_txt:
            continue
        number = invoice_number_key(vals.get("Invoice_Number"))
        if number and item.get("id") not in (None, ""):
            found[number] = int(item["id"])
    return found


def pick_five(
    messages: list[dict[str, Any]],
    *,
    already: set[str],
) -> list[dict[str, Any]]:
    by_inv: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        if not inv or inv in already:
            continue
        if is_already_flagged(msg):
            continue
        by_inv.setdefault(inv, []).append(msg)
    extras: list[tuple[str, dict[str, Any]]] = []
    for inv, cands in by_inv.items():
        cands.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        extras.append((inv, cands[0]))
    extras.sort(key=lambda pair: -int(pair[0] or 0))
    chosen: list[dict[str, Any]] = []
    for inv, msg in extras:
        if len(chosen) >= 5:
            break
        msg["_wanted_invoice"] = inv
        chosen.append(msg)
    return chosen


def verify_swap_receipt(client: KimcoClient, spec: dict[str, Any]) -> dict[str, Any]:
    item = client.get_item("receipts", int(spec["id"]))
    vals = item.get("values") or {}
    qty = money(vals.get("Quantity_Received") or vals.get("Quantity"))
    unit = money(
        vals.get("PO_Item_Number_$_Unit_Price")
        or vals.get("Unit_Price")
        or vals.get("Purchase_Cost")
        or vals.get("Unit_Cost")
    )
    po = lookup_text(vals.get("Purchase_Order_Number") or vals.get("Purchase_Order"))
    part = lookup_text(vals.get("PO_Item_Number") or vals.get("Part_Number"))
    ok = qty == spec["qty"] and unit == spec["unit"]
    return {
        "id": item.get("id"),
        "qty": qty,
        "unit": unit,
        "po": po,
        "part": part,
        "expected_qty": spec["qty"],
        "expected_unit": spec["unit"],
        "ok": ok,
    }


def finish_10010(client: KimcoClient, graph) -> dict[str, Any]:
    before = live_get_proof(client, 10010)
    have = _receipt_ids(before)
    wanted = {spec["id"] for spec in SWAP_RECEIPTS}
    checks = [verify_swap_receipt(client, spec) for spec in SWAP_RECEIPTS]
    proof: dict[str, Any] = {
        "kimco_id": 10010,
        "invoice": "11004",
        "before": before,
        "receipt_checks": checks,
        "put": None,
        "after": None,
        "outlook": None,
    }
    if not all(c.get("ok") for c in checks):
        proof["result"] = "HOLD"
        proof["why"] = (
            "Did not PUT 24109/24110: live receipt qty/unit did not match Kyle "
            f"spec 24109 qty5@$10 and 24110 qty15@$10. checks={checks}"
        )
        return proof
    missing = [spec["id"] for spec in SWAP_RECEIPTS if spec["id"] not in have]
    if missing:
        status = client.try_select_receipts(10010, missing)
        proof["put"] = {"ids": missing, "status": status}
    else:
        proof["put"] = {"ids": [], "status": "already-selected"}
    after = live_get_proof(client, 10010)
    proof["after"] = after
    after_ids = _receipt_ids(after)
    amount = money(after.get("invoice_amount"))
    if wanted <= after_ids and amount == 2600.0 and len(after.get("receipt_lines") or []) >= 6:
        proof["result"] = "Success"
        proof["why"] = (
            "Finished bill (Invoice_Type 3). Invoice # 11004 from PDF. PO 59165 set. "
            "Select Receipts completed swapped leftovers by qty+cost: 24109 qty 5 @ 10.00 "
            "and 24110 qty 15 @ 10.00 (plus 24106/07/08/11). Live GET Invoice_Amount 2600.00 "
            "matches PDF. Vendor 22 AMERICAN QUALITY POWDERCOATING matches parsed. "
            "No fees. Attach status=attached. Treyce would not rework. "
            "Flag status=entered-in-ai."
        )
        if graph is not None:
            hits = graph.search_messages(
                ALLOWED_MAILBOX,
                "New payment request from AMERICAN QUALITY POWDER COATING - invoice 11004",
                top=10,
            )
            hits.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
            if hits:
                proof["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, str(hits[0].get("id") or ""))
    else:
        proof["result"] = "HOLD"
        proof["why"] = (
            "HOLD (qty-does-not-match) after swap attempt: "
            f"receipts={sorted(after_ids)} amount={amount} "
            f"put={proof['put']}. Do not invent Success."
        )
    return proof


def row_from_known(proof: dict[str, Any], *, result: str, why: str, flag: str) -> dict[str, Any]:
    po = str(proof.get("po") or "")
    po_n = re.search(r"(\d{5,6})", po)
    return {
        "Vendor": "American Quality Powder Coating",
        "Invoice #": proof.get("invoice_number"),
        "date": str(proof.get("invoice_date") or "")[:10],
        "PO": po_n.group(1) if po_n else po,
        "Amount": proof.get("invoice_amount"),
        "Result": result,
        "Why": why,
        "KIMCO id": proof.get("id"),
        "Batch": f"{BATCH_NAME} ({BATCH_ID})",
        "Fees": "none" if not proof.get("fee_count") else str(proof.get("fee_count")),
        "PPV": "none",
        "Attach": "attached" if proof.get("attachments") else "",
        "Flag status": flag,
        "Receipts": format_receipts(proof),
        "Fees and surcharges": "none" if not proof.get("fee_count") else str(proof.get("fee_count")),
        "Attach status": "attached" if proof.get("attachments") else "",
        "Flag in Outlook": "Yes",
        "Notes": "",
    }


def write_kyle_sheet(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AP run"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    for col, name in enumerate(SHEET_COLUMNS, start=1):
        cell = sheet.cell(1, col, name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True)
    fills = {
        "Success": PatternFill("solid", fgColor="C6EFCE"),
        "Incomplete": PatternFill("solid", fgColor="F8CBAD"),
        "Fail": PatternFill("solid", fgColor="FFC7CE"),
        "HOLD": PatternFill("solid", fgColor="FFEB9C"),
    }
    for row_idx, row in enumerate(rows, start=2):
        mapped = {
            "Vendor": row.get("Vendor"),
            "Invoice #": row.get("Invoice #"),
            "date": row.get("date"),
            "PO": row.get("PO"),
            "Amount": row.get("Amount"),
            "Result": row.get("Result"),
            "Why": row.get("Why"),
            "KIMCO id": row.get("KIMCO id"),
            "Batch": row.get("Batch"),
            "Fees": row.get("Fees") or row.get("Fees and surcharges") or "none",
            "PPV": row.get("PPV") or "none",
            "Attach": row.get("Attach") or row.get("Attach status") or "",
            "Flag status": row.get("Flag status"),
            "Receipts": row.get("Receipts") or "",
        }
        for col, name in enumerate(SHEET_COLUMNS, start=1):
            cell = sheet.cell(row_idx, col, mapped.get(name, ""))
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if name == "Result":
                fill = fills.get(str(mapped.get(name)))
                if fill:
                    cell.fill = fill
    widths = [28, 14, 12, 10, 12, 12, 55, 12, 24, 10, 10, 12, 18, 40]
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(idx)].width = width
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(SHEET_COLUMNS))}{max(1, len(rows) + 1)}"
    sheet.freeze_panes = "A2"
    workbook.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finish 10010 and enter 5 AQPC on batch 711")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--skip-10010", action="store_true")
    parser.add_argument("--skip-enter", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-10.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(format_intuit_session_presence(), flush=True)
    print("Target: live. No Intuit login / no storage-state. No Mail.Send.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    graph = _optional_graph_client()
    if graph is None:
        print("Graph authenticate failed", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    batch = client.get_item("ap_batches", BATCH_ID)
    bvals = batch.get("values") or {}
    print(
        f"Batch {BATCH_ID} name={bvals.get('AP_Invoice_Batch_ID')} "
        f"status={bvals.get('Status')} unposted={bvals.get('Unposted_Count')}",
        flush=True,
    )

    entered = kimco_aqpc_numbers(client)
    print(f"KIMCO AQPC invoices already present: {sorted(entered)}", flush=True)
    messages = find_aqpc_payment_requests(graph)
    catalog = []
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        catalog.append(
            {
                "invoice": inv,
                "received": msg.get("receivedDateTime"),
                "flagged": is_already_flagged(msg),
                "categories": msg.get("categories") or [],
                "already_kimco": entered.get(inv),
                "subject": str(msg.get("subject") or "")[:120],
            }
        )
    catalog.sort(key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")), reverse=True)
    print(json.dumps({"aqpc_mail": catalog}, indent=2, default=str), flush=True)

    already = set(ALREADY) | set(entered)
    picked = pick_five(messages, already=already)
    print("Picked five:", [m.get("_wanted_invoice") for m in picked], flush=True)
    if args.discover_only:
        return 0 if len(picked) == 5 else 2

    finish_proof: dict[str, Any] = {}
    if not args.skip_10010:
        finish_proof = finish_10010(client, graph)
        print(json.dumps({"finish_10010": finish_proof}, indent=2, default=str), flush=True)

    enter_rows: list[dict[str, Any]] = []
    parsed: list[dict[str, Any]] = []
    if not args.skip_enter:
        if len(picked) != 5:
            print(f"Need exactly 5 new AQPC emails; found {len(picked)}", flush=True)
            return 2
        pdf_dir = ROOT / "runs" / "inbox-pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        invoices = [bill_from_message(graph, msg, pdf_dir) for msg in picked]
        parsed = [summarize_parse(inv) for inv in invoices]
        print(json.dumps({"parsed": parsed}, indent=2, default=str), flush=True)
        enter_rows = run_enter(
            client,
            invoices,
            batch_name=BATCH_NAME,
            pdf_dir=pdf_dir,
            graph_client=graph,
            mailbox=ALLOWED_MAILBOX,
            flag_outlook=True,
        )
        _print_summary(enter_rows)

    known_rows: list[dict[str, Any]] = []
    known_gets: dict[str, Any] = {}
    plus4 = json.loads((ROOT / "runs" / "AP-run-2026-09-15-aqpc-plus4.json").read_text())
    plus4_by_inv = {str(r.get("Invoice #")): r for r in plus4.get("rows") or []}
    next_row = json.loads((ROOT / "runs" / "AP-run-2026-09-15-aqpc-next.json").read_text())
    next_by_inv = {str(r.get("Invoice #")): r for r in next_row.get("rows") or []}
    for spec in KNOWN_TEN:
        kid = spec["kimco_id"]
        got = live_get_proof(client, kid)
        known_gets[str(kid)] = got
        prior = plus4_by_inv.get(spec["invoice"]) or next_by_inv.get(spec["invoice"]) or {}
        result = str(prior.get("Result") or "HOLD")
        why = str(prior.get("Why") or "")
        flag = str(prior.get("Flag status") or "")
        if kid == 10010 and finish_proof:
            result = str(finish_proof.get("result") or result)
            why = str(finish_proof.get("why") or why)
            flag = "entered-in-ai" if result == "Success" else flag
        if kid == 10009:
            result = "HOLD"
            why = str(prior.get("Why") or why)
            flag = "entered-with-issues"
        if kid == 10007 and not why:
            why = str((next_row.get("rows") or [{}])[0].get("Why") or "")
            result = "Success"
            flag = "entered-in-ai"
        row = row_from_known(got, result=result, why=why, flag=flag)
        if kid == 10009:
            row["Amount"] = 10.0
        if kid == 10010 and result == "Success":
            row["Amount"] = 2600.0
        known_rows.append(row)

    new_gets: dict[str, Any] = {}
    sheet_new: list[dict[str, Any]] = []
    for row in enter_rows:
        kid = row.get("KIMCO id")
        extra = ""
        if kid not in (None, ""):
            new_gets[str(kid)] = live_get_proof(client, kid)
            extra = format_receipts(new_gets[str(kid)])
        out = dict(row)
        out["Fees"] = row.get("Fees and surcharges") or "none"
        out["Attach"] = row.get("Attach status") or ""
        out["Receipts"] = extra
        sheet_new.append(out)

    all_rows = known_rows + sheet_new
    report_path = Path(args.report)
    write_kyle_sheet(report_path, all_rows)
    print(f"Wrote {report_path}", flush=True)

    sidecar = {
        "proof": "aqpc-batch711-10",
        "invent": False,
        "mail_send": False,
        "storage_state": False,
        "intuit_login": False,
        "batch_name": BATCH_NAME,
        "batch_id": BATCH_ID,
        "finish_10010": finish_proof,
        "chosen": [m.get("_wanted_invoice") for m in picked],
        "parsed": parsed,
        "enter_rows": enter_rows,
        "known_gets": known_gets,
        "new_gets": new_gets,
        "rows": all_rows,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
