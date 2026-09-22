"""Finish remaining O'Neal HOLDs on batch 722 and Transfer AP.

Kyle 2026-09-22: after Gas 0040437952. Inventory 10160–10164, 10174–10180,
and any other O'Neal on 722 / Transfer AP. Recheck leftovers. Select
Receipts + Fees + in-gate PPV (<$75) → Success. Over-gate stays for
Treyce/Shawn. missing_receipt Comments_1 Anthony (plain). Move finished
Transfer AP bills back to 722. Enter leftover Aug 1+ if any.

NOTE-51: Success updates accountspayable@ Entered in AI only when every
sibling from that PDF is Success.

Packing slips: this stack cannot GET receiving@ (AP mailbox only). Do not
invent a slip match.

No Mail.Send. invent=false. Leave Gas / McMaster / Legacy alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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
from ap_clerk.comments_tab import apply_missing_receipt_comment_tab
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.quality_v12 import apply_exception_category_owner
from ap_clerk.report import write_report
from ap_clerk.rules import lookup_id, lookup_text, money
from jpsteel_0916 import load_list_receipts, open_receipts_on_po
from mcmaster_0918 import finish_hold_header, is_missing_receipt_hold, is_over_ppv_price_hold, mcmaster_proof
from oneal_0921 import (
    CREATED_HEADERS,
    bill_from_message,
    confirm_oneal_vendor,
    exact_invoice_number,
    find_oneal_messages,
    is_oneal_invoice_email,
    leftover_from_catalog,
    quality_oneal_row,
    sanitize_oneal_fees,
    verify_batch,
)
from oneal_plus10_0922 import (
    ANTHONY_MENTION,
    KNOWN_BATCH_ID,
    PPV_MAX_ABS,
    PREFERRED_BATCH_NAME,
    _bill_ppv,
    anthony_missing_receipt_comment,
    apply_anthony_missing_receipt,
    pick_plus10,
    prior_rows_from_sidecar,
    stamp_parents,
)

LOGGER = logging.getLogger("ap_clerk.oneal_finish")

KNOWN_IDS = tuple(range(10160, 10165)) + tuple(range(10174, 10181))
TRANSFER_AP_ID_HINT = 375
SHEET = ROOT / "runs" / "AP-run-2026-09-21-oneal.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-21-oneal.json"
PROOF = ROOT / "runs" / "oneal-finish-2026-09-22.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"
_PO_NUM = re.compile(r"(5[7-9]\d{3})")


def po_from_text(value: Any) -> str:
    match = _PO_NUM.search(str(value or ""))
    return match.group(1) if match else ""


def snapshot(client: KimcoClient, kid: int) -> dict[str, Any]:
    rec = client.get_item("ap_invoices", int(kid))
    vals = rec.get("values") or {}
    lists = rec.get("lists") or {}
    recs = lists.get("APInvoiceLine") or []
    charges = lists.get("InvoiceAdditionalCharges") or []
    try:
        atts = [a.get("name") for a in client.list_attachments(int(kid))]
    except Exception:  # noqa: BLE001
        atts = []
    return {
        "kimco_id": int(kid),
        "invoice": exact_invoice_number(vals.get("Invoice_Number")),
        "amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Invoice_Verification_Amount")),
        "type": vals.get("Invoice_Type"),
        "po": po_from_text(lookup_text(vals.get("PO") or vals.get("Purchase_Order"))),
        "po_text": lookup_text(vals.get("PO") or vals.get("Purchase_Order")),
        "vendor": lookup_text(vals.get("Vendor")),
        "vendor_id": lookup_id(vals.get("Vendor")),
        "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
        "batch": lookup_text(vals.get("AP_Invoice_Batch")),
        "receipt_n": len(recs) if isinstance(recs, list) else 0,
        "charge_n": len(charges) if isinstance(charges, list) else 0,
        "attachments": atts,
        "comments_n": len(lists.get("Comments_1") or []),
    }


def classify_before(snap: dict[str, Any], sheet: dict[str, Any] | None) -> str:
    if sheet and str(sheet.get("Result") or "") == "Success":
        amt = snap.get("amount")
        ver = snap.get("verification")
        if (
            snap.get("receipt_n")
            and amt is not None
            and ver is not None
            and abs(amt - ver) <= 0.02
        ):
            return "Success"
    if snap.get("receipt_n") and snap.get("amount") not in (None, 0, 0.0):
        amt = snap.get("amount")
        ver = snap.get("verification")
        if amt is not None and ver is not None and abs(amt - ver) <= 0.02:
            return "Success"
        return "HOLD rounding"
    if sheet:
        result = str(sheet.get("Result") or "HOLD")
        cat = str(sheet.get("Exception category") or "").strip()
        if result == "HOLD" and cat:
            return f"HOLD {cat}"
        return result
    if snap.get("receipt_n"):
        return "HOLD"
    return "HOLD"


def merge_finish_rows(
    prior: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Update first-five + plus-10 rows. Do not lock CREATED_HEADERS."""
    out = list(prior)
    seen = {exact_invoice_number(r.get("Invoice #")) for r in prior}
    for row in new_rows:
        inv = exact_invoice_number(row.get("Invoice #"))
        if not inv:
            continue
        if inv in seen:
            out = [
                row if exact_invoice_number(x.get("Invoice #")) == inv else x for x in out
            ]
        else:
            out.append(row)
            seen.add(inv)
    return out


def sheet_row_for(rows: list[dict[str, Any]], invoice: str) -> dict[str, Any] | None:
    for row in rows:
        if exact_invoice_number(row.get("Invoice #")) == invoice:
            return row
    return None


def move_to_722(client: KimcoClient, kid: int) -> dict[str, Any]:
    payload = {
        "state": "Modified",
        "id": int(kid),
        "values": {"AP_Invoice_Batch": {"id": int(KNOWN_BATCH_ID)}},
    }
    try:
        _b, status, err = client.update("ap_invoices", int(kid), payload)
    except KimcoError as exc:
        return {"status": "blocked", "error": str(exc)[:200]}
    return {"status": "moved" if status < 400 else f"put-{status}", "put": status, "error": err}


def rounding_ppv_needed(snap: dict[str, Any]) -> float | None:
    amt = money(snap.get("amount"))
    ver = money(snap.get("verification"))
    if amt is None or ver is None or amt == 0:
        return None
    gap = round(ver - amt, 2)
    if abs(gap) <= 0.02:
        return 0.0
    if abs(gap) >= PPV_MAX_ABS:
        return None
    return gap


def inventory_ids(entered: dict[str, int]) -> list[int]:
    """Known wave ids plus every live O'Neal header id (filter by batch later)."""
    ids = set(KNOWN_IDS)
    for kid in entered.values():
        if kid not in (None, ""):
            ids.add(int(kid))
    return sorted(ids)


def keep_inventory_snap(snap: dict[str, Any]) -> bool:
    kid = snap.get("kimco_id")
    return int(kid) in KNOWN_IDS or snap.get("batch_id") in {
        KNOWN_BATCH_ID,
        TRANSFER_AP_ID_HINT,
    }


def parsed_for(invoice: str, bills: list[dict[str, Any]], snap: dict[str, Any]) -> dict[str, Any]:
    for bill in bills:
        if exact_invoice_number(bill.get("invoice_number")) == invoice:
            return sanitize_oneal_fees(bill)
    return {
        "invoice_number": invoice,
        "po": snap.get("po"),
        "amount": snap.get("verification") or snap.get("amount"),
        "lines": [],
        "fees": [],
        "vendor": "O'Neal Steel - Dallas (GP)",
    }


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
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph required. No Mail.Send.")
    verified = verify_batch(client, KNOWN_BATCH_ID, PREFERRED_BATCH_NAME)
    if not verified.get("matches_expected"):
        raise SystemExit(f"Batch 722 mismatch: {verified}")
    vendor_info = confirm_oneal_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    entered = dict(vendor_info.get("entered") or {})
    if vendor_id in (None, ""):
        raise SystemExit("O'Neal vendor id not confirmed.")
    prior = prior_rows_from_sidecar(SHEET_JSON)
    ids = inventory_ids(entered)
    before: list[dict[str, Any]] = []
    for kid in ids:
        snap = snapshot(client, kid)
        if not keep_inventory_snap(snap):
            continue
        inv = snap.get("invoice") or ""
        sheet = sheet_row_for(prior, inv) if inv else None
        snap["before_result"] = classify_before(snap, sheet)
        snap["sheet"] = {
            "Result": (sheet or {}).get("Result"),
            "Exception category": (sheet or {}).get("Exception category"),
            "Exception owner": (sheet or {}).get("Exception owner"),
        }
        before.append(snap)

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    parsed_bills: list[dict[str, Any]] = []
    for msg in find_oneal_messages(graph):
        if not is_oneal_invoice_email(msg):
            continue
        parsed_bills.extend(bill_from_message(graph, msg, PDF_DIR))

    already = set(entered) | set(CREATED_HEADERS)
    remaining, leftover = pick_plus10(parsed_bills, already=already, cap=20)
    proof: dict[str, Any] = {
        "proof": "oneal-finish-2026-09-22",
        "invent": False,
        "mail_send": False,
        "note51": True,
        "ppv_max": PPV_MAX_ABS,
        "packing_slip": "receiving@ not reachable on this AP-only Graph client",
        "inventory": before,
        "remaining_unprocessed": [
            {
                "invoice": exact_invoice_number(b.get("invoice_number")),
                "po": b.get("po"),
                "amount": b.get("amount"),
                "date": b.get("date"),
            }
            for b in remaining
        ],
        "leftover_mailbox": leftover_from_catalog(
            leftover,
            chosen={exact_invoice_number(b.get("invoice_number")) for b in remaining},
            older=leftover,
            credits=[],
        ),
        "dry_run": args.dry_run,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.dry_run:
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 0

    try:
        receipts = load_list_receipts(client)
    except KimcoError:
        receipts = []

    touched: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    for snap in before:
        kid = int(snap["kimco_id"])
        inv = str(snap.get("invoice") or "")
        before_result = snap.get("before_result")
        if before_result == "Success" and snap.get("batch_id") == KNOWN_BATCH_ID:
            touched.append(
                {
                    "invoice": inv,
                    "kimco_id": kid,
                    "before": "Success",
                    "after": "Success",
                    "before_batch": snap.get("batch"),
                    "action": "leave-success",
                    "po": snap.get("po"),
                }
            )
            continue
        parsed = parsed_for(inv, parsed_bills, snap)
        open_on_po = open_receipts_on_po(receipts, snap.get("po"))
        report: dict[str, Any] = {
            "invoice": inv,
            "kimco_id": kid,
            "before": before_result,
            "before_batch": snap.get("batch"),
            "po": snap.get("po"),
            "open_on_po": [
                {"id": r.get("id"), "part": r.get("part"), "qty": r.get("qty"), "unit": r.get("unit_price")}
                for r in open_on_po[:12]
            ],
        }
        # Already-selected + in-gate rounding (10161 class).
        if snap.get("receipt_n") and before_result in {"HOLD rounding", "HOLD", "Success"}:
            gap = rounding_ppv_needed(snap)
            if gap is None:
                report["after"] = "HOLD over-gate or amount unknown"
                report["left_for"] = "Treyce/Shawn"
                touched.append(report)
                continue
            if gap:
                report["ppv_post"] = client.try_post_ppv(kid, gap)
            if snap.get("batch_id") != KNOWN_BATCH_ID:
                report["move"] = move_to_722(client, kid)
            after = snapshot(client, kid)
            ok = (
                after.get("amount") is not None
                and after.get("verification") is not None
                and abs(after["amount"] - after["verification"]) <= 0.02
            )
            report["after"] = "Success" if ok else "HOLD"
            report["after_snap"] = after
            if ok:
                row = {
                    "Vendor": "O'Neal Steel - Dallas (GP)",
                    "Invoice #": inv,
                    "PO": snap.get("po"),
                    "Amount": after.get("verification"),
                    "Result": "Success",
                    "KIMCO id": kid,
                    "Batch": f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
                    "Exception category": "",
                    "Exception owner": "",
                    "Fees and surcharges": "none",
                    "PPV": f"{gap:.2f}" if gap else "none",
                    "Attach status": "attached" if after.get("attachments") else "",
                    "Why": (
                        f"Finished bill. Rounding/in-gate PPV {gap if gap else 0:.2f} "
                        f"under ${PPV_MAX_ABS:.0f}. Amount={after.get('amount')}. "
                        f"Moved to 722={report.get('move')}."
                    ),
                }
                new_rows.append(apply_exception_category_owner(row))
            touched.append(report)
            continue

        if before_result == "Success":
            if snap.get("batch_id") != KNOWN_BATCH_ID:
                report["move"] = move_to_722(client, kid)
                report["after"] = "Success"
                touched.append(report)
            continue

        planned = _bill_ppv(parsed, receipts)
        if planned["over_75"] and not open_on_po:
            report["after"] = before_result
            report["left_for"] = "Treyce/Shawn — no leftover and prior over-gate"
            touched.append(report)
            continue
        if planned["over_75"] and open_on_po:
            report["after"] = "HOLD price_variance"
            report["ppv"] = planned["ppv"]
            report["left_for"] = "Treyce/Shawn — leftover vs PDF still over $75"
            if snap.get("batch_id") != TRANSFER_AP_ID_HINT:
                report["why"] = "stay Transfer AP / do not Select"
            touched.append(report)
            continue

        finish = finish_hold_header(
            client, parsed=parsed, kimco_id=kid, receipts=receipts
        )
        proof_after = finish.get("after") or mcmaster_proof(client, kid)
        prior_row = sheet_row_for(prior, inv) or {
            "Vendor": "O'Neal Steel - Dallas (GP)",
            "Invoice #": inv,
            "PO": snap.get("po"),
            "Amount": snap.get("verification"),
            "KIMCO id": kid,
            "Batch": f"{snap.get('batch')} ({snap.get('batch_id')})",
        }
        row = quality_oneal_row(
            None,
            parsed=parsed,
            enter_row=prior_row,
            proof=proof_after,
            finish=finish,
            vendor_id=vendor_id,
            batch_label=f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
        )
        if is_over_ppv_price_hold(row, finish) or planned["over_75"]:
            row["Result"] = "HOLD"
            row["Why"] = (
                f"HOLD (price-does-not-match): leftover vs invoice still over "
                f"${PPV_MAX_ABS:.0f}. Receipts not newly selected. Leave for Shawn."
            )
            row = apply_exception_category_owner(row)
            report["after"] = "HOLD price_variance"
            report["left_for"] = "Treyce/Shawn"
        elif is_missing_receipt_hold(row):
            notify = apply_missing_receipt_comment_tab(
                client,
                invoice_id=kid,
                body=anthony_missing_receipt_comment(
                    invoice_number=inv,
                    po=str(parsed.get("po") or snap.get("po") or ""),
                    pdf_amount=parsed.get("amount") or snap.get("verification"),
                ),
                mention=ANTHONY_MENTION,
            )
            row["Why"] = (
                f"{row.get('Why')} Comments_1 Anthony plain "
                f"status={notify.get('status')} mention_id=None."
            )
            row = apply_anthony_missing_receipt(row)
            report["after"] = "HOLD missing_receipt"
            report["anthony"] = notify.get("status")
        else:
            row = apply_exception_category_owner(row)
            if row.get("Result") == "Success" and snap.get("batch_id") != KNOWN_BATCH_ID:
                report["move"] = move_to_722(client, kid)
                row["Batch"] = f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})"
            report["after"] = row.get("Result")
        report["finish"] = {
            "select": finish.get("select_status"),
            "wanted": finish.get("wanted"),
            "ppv": finish.get("ppv_amount"),
        }
        new_rows.append(row)
        touched.append(report)
        try:
            receipts = load_list_receipts(client)
        except KimcoError:
            pass

    entered_new: list[dict[str, Any]] = []
    if remaining:
        enter_rows = run_enter(
            client,
            remaining,
            batch_name=PREFERRED_BATCH_NAME,
            pdf_dir=PDF_DIR,
            graph_client=graph,
            mailbox=ALLOWED_MAILBOX,
            flag_outlook=False,
        )
        from oneal_plus10_0922 import finish_plus10_rows

        rows, finishes = finish_plus10_rows(
            client,
            graph,
            parsed_bills=remaining,
            enter_rows=enter_rows,
            receipts=receipts,
            vendor_id=vendor_id,
            batch_label=f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
        )
        entered_new = rows
        new_rows.extend(rows)
        proof["new_enters"] = finishes

    outlook = stamp_parents(graph, new_rows, parsed_bills, prior)
    # Promote Success parents when all known siblings on sheet are Success.
    merged = merge_finish_rows(prior, new_rows)
    write_report(SHEET, merged)
    sidecar = json.loads(SHEET_JSON.read_text()) if SHEET_JSON.is_file() else {}
    sidecar["proof"] = "oneal-0921-finish"
    sidecar["rows"] = merged
    sidecar["finish_0922"] = {"touched": touched, "outlook": outlook}
    SHEET_JSON.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    proof.update(
        {
            "touched": touched,
            "entered_new": [
                {
                    "invoice": r.get("Invoice #"),
                    "kimco_id": r.get("KIMCO id"),
                    "result": r.get("Result"),
                }
                for r in entered_new
            ],
            "outlook": outlook,
            "sheet": str(SHEET),
        }
    )
    PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    print(json.dumps(proof, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
