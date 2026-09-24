"""Enter the next 10 O'Neal invoices on dedicated batch 722.

Kyle 2026-09-22: after packing-slip attach + Gas 0040438052.
Reuse `API Agent - 9/21/26 O'Neal` (722). Do not recreate 10160–10164.

Prefer leftovers 15460544 / 15460995 / 15461007 / 15461157, then next
unprocessed Aug 1+. Parse flagged parent emails so leftovers are not
skipped the way Gas 0040438052 was.

missing_receipt stays on 722. Comments_1 names Anthony (O'Neal sheet
owner). Anthony mention-id is unproven — plain text, do not invent.
Do not Transfer AP for missing receipts.

Over-PPV |total| $75+ → Transfer AP + @Shawn 104, no Select.
Fees id 11. PDF truth. Attach vendor invoice PDF; packing slip if a
match already exists — do not block entry waiting on slips.

NOTE-51: Success finish updates accountspayable@ to Entered in AI.
Multi-invoice parent flips only when every sibling from that PDF is
Success.

No Mail.Send. invent=false. Leave Gas / McMaster / Legacy / Transfer AP
source batches alone.
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
from ap_clerk.comments_tab import (
    EXCEPTION_MAIL_SEND,
    apply_missing_receipt_comment_tab,
)
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.outlook_finish import promote_ap_outlook_after_success
from ap_clerk.quality_v12 import (
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
)
from ap_clerk.report import write_report
from ap_clerk.rules import (
    SHAWN_MCKIBBEN,
    filter_matches_outside_ppv_gate,
    match_receipts,
    money,
)
from jpsteel_0916 import load_list_receipts, ppv_total_from_selectable
from mcmaster_0918 import (
    apply_over_ppv_transfer_ap,
    finish_hold_header,
    is_missing_receipt_hold,
    is_over_ppv_price_hold,
    mcmaster_proof,
)
from oneal_0921 import (
    CREATED_HEADERS,
    DO_NOT_MUTATE_IDS,
    LEAVE_ALONE_HOLD_IDS,
    MIN_INVOICE_DATE,
    _parse_date,
    bill_from_message,
    confirm_oneal_vendor,
    exact_invoice_number,
    find_oneal_messages,
    is_credit_bill,
    is_oneal_invoice_email,
    leftover_from_catalog,
    missing_receipt_hold_comment,
    over_ppv_hold_comment,
    quality_oneal_row,
    sanitize_oneal_fees,
    verify_batch,
)

LOGGER = logging.getLogger("ap_clerk.oneal_plus10")

KNOWN_BATCH_ID = 722
PREFERRED_BATCH_NAME = "API Agent - 9/21/26 O'Neal"
CAP = 10
PPV_MAX_ABS = 75.00
PREFERRED_LEFTOVERS = (
    "15460544",
    "15460995",
    "15461007",
    "15461157",
)
ANTHONY_MENTION_NAME = "Anthony"
ANTHONY_MENTION = {
    "id": None,
    "name": ANTHONY_MENTION_NAME,
    "email": None,
    "tag": "@Anthony",
}
SHEET = ROOT / "runs" / "AP-run-2026-09-21-oneal.xlsx"
SHEET_JSON = ROOT / "runs" / "AP-run-2026-09-21-oneal.json"
PROOF = ROOT / "runs" / "oneal-plus10-2026-09-22.json"
PDF_DIR = ROOT / "runs" / "inbox-pdfs"


def ppv_abs_over_gate(ppv_total: Any) -> bool:
    amt = money(ppv_total)
    if amt is None:
        return False
    return abs(amt) >= PPV_MAX_ABS


def pick_plus10(
    parsed_bills: list[dict[str, Any]],
    *,
    already: set[str],
    cap: int = CAP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prefer the four 9/21 leftovers, then newest Aug 1+ unprocessed."""
    by_inv: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for bill in parsed_bills:
        inv = exact_invoice_number(bill.get("invoice_number"))
        if is_credit_bill(bill):
            skipped.append(bill)
            continue
        if not inv or inv in already or inv in CREATED_HEADERS:
            continue
        inv_date = _parse_date(bill.get("date"))
        if inv_date is None or inv_date < MIN_INVOICE_DATE:
            skipped.append(bill)
            continue
        by_inv[inv] = bill
    chosen: list[dict[str, Any]] = []
    for inv in PREFERRED_LEFTOVERS:
        bill = by_inv.pop(inv, None)
        if bill is None:
            continue
        chosen.append(bill)
        if len(chosen) >= cap:
            break
    rest = sorted(
        by_inv.values(),
        key=lambda b: (str(b.get("date") or ""), str(b.get("receivedDateTime") or "")),
        reverse=True,
    )
    for bill in rest:
        if len(chosen) >= cap:
            skipped.append(bill)
            continue
        chosen.append(bill)
    return chosen, skipped


def anthony_missing_receipt_comment(*, invoice_number: str, po: str | None, pdf_amount: Any) -> str:
    amt = money(pdf_amount)
    amt_txt = f"{amt:.2f}" if amt is not None else "unknown"
    inv = exact_invoice_number(invoice_number) or str(invoice_number or "")
    return (
        f"{ANTHONY_MENTION_NAME} HOLD (receipt) on O'Neal Steel {inv} "
        f"PO {po or 'n/a'} PDF ${amt_txt}. Open leftover merch is not received "
        "or does not match PDF lines. Header stays on API Agent - 9/21/26 "
        "O'Neal (722) — do not Transfer AP. O'Neal receiving owner is Anthony "
        "(Kyle sheet). Mention-id for Anthony is not proven (invent=false). "
        "No email."
    )


def apply_anthony_missing_receipt(row: dict[str, Any]) -> dict[str, Any]:
    out = apply_exception_category_owner(dict(row))
    if str(out.get(COL_EXCEPTION_CATEGORY) or "") != "missing_receipt":
        return out
    out[COL_EXCEPTION_OWNER] = ANTHONY_MENTION_NAME
    why = str(out.get("Why") or "")
    why = why.replace("Ruben Perez", ANTHONY_MENTION_NAME)
    why = why.replace("owner=Ruben Perez", f"owner={ANTHONY_MENTION_NAME}")
    if "Anthony" not in why:
        why = f"category=missing_receipt; owner={ANTHONY_MENTION_NAME}. {why}"
    out["Why"] = why
    return out


def merge_sheet_rows(
    prior: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    locked = {exact_invoice_number(k) for k in CREATED_HEADERS}
    out = list(prior)
    seen = {exact_invoice_number(r.get("Invoice #")) for r in prior}
    for row in new_rows:
        inv = exact_invoice_number(row.get("Invoice #"))
        if not inv or inv in locked:
            continue
        if inv in seen:
            out = [
                row if exact_invoice_number(x.get("Invoice #")) == inv else x for x in out
            ]
        else:
            out.append(row)
            seen.add(inv)
    return out


def prior_rows_from_sidecar(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    rows = data.get("rows") or []
    return [r for r in rows if isinstance(r, dict)]


def _bill_ppv(parsed: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=receipts,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    locked = filter_matches_outside_ppv_gate(
        list(match.get("matched") or []), invoice_total=parsed.get("amount")
    )
    ppv = ppv_total_from_selectable(
        list(locked.get("selectable") or []), invoice_total=parsed.get("amount")
    )
    return {
        "ppv": ppv,
        "over_75": ppv_abs_over_gate(ppv) or bool(locked.get("select_zero")),
        "select_zero": bool(locked.get("select_zero")),
        "matched": list(match.get("matched") or []),
    }


def finish_plus10_rows(
    client: KimcoClient,
    graph,
    *,
    parsed_bills: list[dict[str, Any]],
    enter_rows: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    vendor_id: int | None,
    batch_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    finishes: dict[str, Any] = {}
    by_inv = {exact_invoice_number(b.get("invoice_number")): b for b in parsed_bills}
    for enter_row in enter_rows:
        inv = exact_invoice_number(enter_row.get("Invoice #"))
        kid = enter_row.get("KIMCO id")
        parsed = by_inv.get(inv) or {}
        if kid in (None, "") or int(kid) in DO_NOT_MUTATE_IDS | LEAVE_ALONE_HOLD_IDS:
            rows.append(dict(enter_row))
            continue
        planned = _bill_ppv(parsed, receipts)
        if planned["over_75"]:
            finish = {
                "wanted": [],
                "select_status": "ppv-lock-select-zero",
                "fee_status": "held-no-select",
                "ppv_status": "held-no-select",
                "ppv_amount": planned["ppv"],
                "select_zero": True,
                "skipped_over_ppv": True,
                "matched": planned["matched"],
                "after": mcmaster_proof(client, int(kid)),
            }
        else:
            finish = finish_hold_header(
                client, parsed=parsed, kimco_id=int(kid), receipts=receipts
            )
        finishes[inv] = {k: v for k, v in finish.items() if k != "after"}
        proof = finish.get("after") or mcmaster_proof(client, int(kid))
        row = quality_oneal_row(
            None,
            parsed=parsed,
            enter_row=enter_row,
            proof=proof,
            finish=finish,
            vendor_id=vendor_id,
            batch_label=batch_label,
        )
        if is_over_ppv_price_hold(row, finish) or planned["over_75"]:
            comment = over_ppv_hold_comment(
                invoice_number=inv,
                po=str(parsed.get("po") or enter_row.get("PO") or ""),
                pdf_amount=parsed.get("amount") or enter_row.get("Amount"),
            )
            transfer = apply_over_ppv_transfer_ap(client, kimco_id=int(kid), comment=comment)
            finishes[inv]["transfer_ap"] = transfer
            mention = transfer.get("mention_notify") or {}
            if transfer.get("status") == "moved":
                row["Batch"] = f"{transfer.get('batch_name')} ({transfer.get('batch_id')})"
            row["Result"] = "HOLD"
            row["Why"] = (
                f"HOLD (price-does-not-match): leftover vs invoice line is over the "
                f"${PPV_MAX_ABS:.0f} PPV gate. Receipts were NOT selected. "
                f"{SHAWN_MCKIBBEN} mention-id 104. "
                f"Transfer AP status={transfer.get('status')} "
                f"@mention={mention.get('report') or mention}."
            )
            row = apply_exception_category_owner(row)
        elif is_missing_receipt_hold(row):
            comment = anthony_missing_receipt_comment(
                invoice_number=inv,
                po=str(parsed.get("po") or enter_row.get("PO") or ""),
                pdf_amount=parsed.get("amount") or enter_row.get("Amount"),
            )
            notify = apply_missing_receipt_comment_tab(
                client,
                invoice_id=int(kid),
                body=comment,
                mention=ANTHONY_MENTION,
            )
            finishes[inv]["missing_receipt_comment"] = notify
            row["Why"] = (
                f"{row.get('Why')} Comments_1 missing_receipt "
                f"status={notify.get('status')} tab={notify.get('report')} "
                f"transfer_ap=False mention_id={ANTHONY_MENTION['id']!r} "
                f"owner={ANTHONY_MENTION_NAME} mail_send={EXCEPTION_MAIL_SEND}."
            )
            row = apply_anthony_missing_receipt(row)
        else:
            row = apply_exception_category_owner(row)
        rows.append(row)
        try:
            receipts = load_list_receipts(client)
        except KimcoError:
            pass
    return rows, finishes


def stamp_parents(
    graph,
    rows: list[dict[str, Any]],
    parsed_bills: list[dict[str, Any]],
    prior: list[dict[str, Any]],
) -> dict[str, Any]:
    """NOTE-51: one stamp per parent after sibling results are known."""
    if graph is None:
        return {}
    mid_for_inv = {
        exact_invoice_number(b.get("invoice_number")): str(b.get("graph_message_id") or "")
        for b in parsed_bills
    }
    by_mid: dict[str, list[str]] = {}
    kids_on_mid: dict[str, set[str]] = {}
    for bill in parsed_bills:
        mid = str(bill.get("graph_message_id") or "")
        inv = exact_invoice_number(bill.get("invoice_number"))
        if mid and inv:
            kids_on_mid.setdefault(mid, set()).add(inv)
            by_mid.setdefault(mid, [])
    result_by_inv = {
        exact_invoice_number(r.get("Invoice #")): str(r.get("Result") or "")
        for r in prior + rows
        if exact_invoice_number(r.get("Invoice #"))
    }
    header_by_inv = {
        exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id") not in (None, "")
        for r in prior + rows
        if exact_invoice_number(r.get("Invoice #"))
    }
    stamped: dict[str, Any] = {}
    for mid, invs in kids_on_mid.items():
        results = [result_by_inv.get(inv) or "" for inv in sorted(invs)]
        any_header = any(header_by_inv.get(inv) for inv in invs)
        if not any(results):
            continue
        promo = promote_ap_outlook_after_success(
            graph, mid, results, any_header=any_header
        )
        stamped[mid] = promo
        for row in rows:
            if mid_for_inv.get(exact_invoice_number(row.get("Invoice #"))) == mid:
                row["outlook"] = promo.get("status")
                row["Flag in Outlook"] = "Yes"
                row["Flag status"] = promo.get("flag")
    return stamped


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
    if not verified.get("matches_expected") or verified.get("is_forbidden"):
        raise SystemExit(f"Batch 722 mismatch: {verified}")
    vendor_info = confirm_oneal_vendor(client)
    vendor_id = vendor_info.get("vendor_id")
    entered = dict(vendor_info.get("entered") or {})
    if vendor_id in (None, ""):
        raise SystemExit("O'Neal vendor id not confirmed. invent=false.")
    already = set(entered) | set(CREATED_HEADERS)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    parsed_bills: list[dict[str, Any]] = []
    for msg in find_oneal_messages(graph):
        if not is_oneal_invoice_email(msg):
            continue
        for bill in bill_from_message(graph, msg, PDF_DIR):
            parsed_bills.append(sanitize_oneal_fees(bill))
    parsed_bills = [
        b
        for b in parsed_bills
        if exact_invoice_number(b.get("invoice_number")) not in already
    ]
    chosen, leftover = pick_plus10(parsed_bills, already=already, cap=CAP)
    prior = prior_rows_from_sidecar(SHEET_JSON)
    proof: dict[str, Any] = {
        "proof": "oneal-plus10-2026-09-22",
        "invent": False,
        "mail_send": False,
        "note51": True,
        "ppv_max": PPV_MAX_ABS,
        "batch": PREFERRED_BATCH_NAME,
        "batch_id": KNOWN_BATCH_ID,
        "locked_headers": dict(CREATED_HEADERS),
        "preferred": list(PREFERRED_LEFTOVERS),
        "chosen": [
            {
                "invoice": exact_invoice_number(b.get("invoice_number")),
                "po": b.get("po"),
                "amount": b.get("amount"),
                "date": b.get("date"),
            }
            for b in chosen
        ],
        "leftover": leftover_from_catalog(
            leftover,
            chosen={exact_invoice_number(b.get("invoice_number")) for b in chosen},
            older=leftover,
            credits=[],
        ),
        "dry_run": args.dry_run,
        "anthony_mention_id": None,
    }
    if args.dry_run or not chosen:
        proof["would"] = "enter-plus10-on-722" if chosen else "none-to-enter"
        PROOF.write_text(json.dumps(proof, indent=2, default=str) + "\n")
        print(json.dumps(proof, indent=2, default=str))
        return 0 if chosen or args.dry_run else 2

    try:
        receipts = load_list_receipts(client)
    except KimcoError:
        receipts = []
    enter_rows = run_enter(
        client,
        chosen,
        batch_name=PREFERRED_BATCH_NAME,
        pdf_dir=PDF_DIR,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=False,
    )
    for row in enter_rows:
        kid = row.get("KIMCO id")
        if kid in (None, ""):
            continue
        if int(kid) in DO_NOT_MUTATE_IDS | LEAVE_ALONE_HOLD_IDS:
            raise SystemExit(f"Refusing to mutate locked header {kid}")
    rows, finishes = finish_plus10_rows(
        client,
        graph,
        parsed_bills=chosen,
        enter_rows=enter_rows,
        receipts=receipts,
        vendor_id=vendor_id,
        batch_label=f"{PREFERRED_BATCH_NAME} ({KNOWN_BATCH_ID})",
    )
    outlook = stamp_parents(graph, rows, chosen, prior)
    merged = merge_sheet_rows(prior, rows)
    write_report(SHEET, merged)
    sidecar = json.loads(SHEET_JSON.read_text()) if SHEET_JSON.is_file() else {}
    sidecar["proof"] = "oneal-0921-plus10"
    sidecar["rows"] = merged
    sidecar["plus10_headers"] = {
        exact_invoice_number(r.get("Invoice #")): r.get("KIMCO id") for r in rows
    }
    sidecar["plus10"] = {
        "enter_rows": rows,
        "finishes": finishes,
        "outlook": outlook,
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
                    "exception_category": r.get("Exception category"),
                    "exception_owner": r.get("Exception owner"),
                    "batch": r.get("Batch"),
                    "outlook": r.get("outlook"),
                    "why": (r.get("Why") or "")[:220],
                }
                for r in rows
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
