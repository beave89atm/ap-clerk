"""Enter 5 more AQPC payment-request bills on batch 711 (20-row sheet).

Skips KIMCO 10007–10021 and Kyle-entered 10917/10918/10920/10921.
Guest Intuit View-details click only. No Intuit login. No Mail.Send.
invent=false.
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

from aqpc_batch711_plus5 import (  # noqa: E402
    BATCH_ID,
    BATCH_NAME,
    VENDOR_NEEDLE,
    format_receipts,
    kimco_aqpc_numbers,
    write_kyle_sheet,
)
from aqpc_plus4 import bill_from_message, live_get_proof, summarize_parse  # noqa: E402
from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.browser_pdf import format_intuit_session_presence  # noqa: E402
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged  # noqa: E402
from ap_clerk.kimco import KimcoClient  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    SHAWN_MCKIBBEN,
    decide_ppv,
    invoice_number_key,
    match_receipts,
    money,
    normalize_receipt,
)

LOGGER = logging.getLogger("ap_clerk.aqpc_batch711_20")

ALREADY_ON_711 = {
    "11002",
    "10999",
    "11003",
    "11004",
    "11005",
    "10998",
    "10991",
    "10984",
    "10969",
    "10968",
    "10967",
    "10964",
    "10962",
    "10958",
    "10956",
}
KYLE_ENTERED = {"10917", "10918", "10920", "10921"}
ALREADY = ALREADY_ON_711 | KYLE_ENTERED
# Next newest unflagged AQPC payment-requests not already on KIMCO (no 11006+
# last time). Discovery fills extras; these are the likely next numbers.
PREFERRED_FIVE = ["10955", "10954", "10953", "10952", "10950"]
KNOWN_FIFTEEN = [
    {"invoice": "11002", "kimco_id": 10007},
    {"invoice": "10999", "kimco_id": 10008},
    {"invoice": "11003", "kimco_id": 10009},
    {"invoice": "11004", "kimco_id": 10010},
    {"invoice": "11005", "kimco_id": 10011},
    {"invoice": "10998", "kimco_id": 10012},
    {"invoice": "10991", "kimco_id": 10013},
    {"invoice": "10984", "kimco_id": 10014},
    {"invoice": "10969", "kimco_id": 10015},
    {"invoice": "10968", "kimco_id": 10016},
    {"invoice": "10967", "kimco_id": 10017},
    {"invoice": "10964", "kimco_id": 10018},
    {"invoice": "10962", "kimco_id": 10019},
    {"invoice": "10958", "kimco_id": 10020},
    {"invoice": "10956", "kimco_id": 10021},
]
PRIOR_SHEET = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-15.json"
HOLD_PDF_AMOUNTS = {
    10009: 10.0,
    10013: 199.0,
    10021: 700.0,
}


def _subject_inv(subject: str) -> str:
    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def _absorb(seen: dict[str, dict[str, Any]], hits: list[dict[str, Any]]) -> None:
    for msg in hits:
        mid = str(msg.get("id") or "")
        subject = str(msg.get("subject") or "")
        if VENDOR_NEEDLE not in subject.upper():
            continue
        if "payment request" not in subject.lower() and "invoice" not in subject.lower():
            continue
        if mid:
            seen[mid] = msg


def find_aqpc_payment_requests(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    generics = [
        f"New payment request from {VENDOR_NEEDLE}",
        f"{VENDOR_NEEDLE} - invoice",
        "AMERICAN QUALITY POWDER COATING payment request",
    ]
    for needle in generics:
        try:
            _absorb(seen, graph.search_messages(ALLOWED_MAILBOX, needle, top=50))
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
    found = {_subject_inv(str(m.get("subject") or "")) for m in seen.values()}
    extras = [
        n
        for n in [
            *PREFERRED_FIVE,
            *ALREADY_ON_711,
            *[str(n) for n in range(11006, 11016)],
            *[str(n) for n in range(10940, 10956)],
        ]
        if n not in found
    ]
    for n in extras:
        try:
            _absorb(
                seen,
                graph.search_messages(
                    ALLOWED_MAILBOX,
                    f"New payment request from {VENDOR_NEEDLE} - invoice {n}",
                    top=5,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - discovery continues
            LOGGER.info("Graph search invoice %s failed: %s", n, type(exc).__name__)
    return list(seen.values())


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
    extras.sort(key=lambda pair: (0 if pair[0] in PREFERRED_FIVE else 1, -int(pair[0] or 0)))
    chosen: list[dict[str, Any]] = []
    for inv, msg in extras:
        if len(chosen) >= 5:
            break
        msg["_wanted_invoice"] = inv
        chosen.append(msg)
    return chosen


def _receipt_ids(proof: dict[str, Any]) -> set[int]:
    out: set[int] = set()
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        if rid not in (None, ""):
            out.add(int(rid))
    return out


def _open_receipts_on_po(receipts: list[dict[str, Any]], po: str | None) -> list[dict[str, Any]]:
    wanted = invoice_number_key(po or "")
    if not wanted:
        return []
    open_rows: list[dict[str, Any]] = []
    for rec in receipts:
        if invoice_number_key(str(rec.get("po") or rec.get("name") or "")) != wanted:
            continue
        raw = rec.get("raw") if isinstance(rec.get("raw"), dict) else {}
        invoiced = raw.get("Invoiced") or raw.get("invoiced")
        if invoiced in {True, "true", 1, "1"}:
            continue
        open_rows.append(rec)
    return open_rows


def _hydrate_receipts(client: KimcoClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List-view receipts often omit qty/unit. Record GET fills those fields."""
    out: list[dict[str, Any]] = []
    for rec in rows:
        if rec.get("qty") is not None and rec.get("unit_price") is not None:
            out.append(rec)
            continue
        rid = rec.get("id")
        if rid in (None, ""):
            out.append(rec)
            continue
        item = client.get_item("receipts", int(rid))
        filled = normalize_receipt(item)
        merged = dict(rec)
        for key in ("qty", "unit_price", "amount", "part", "po", "name"):
            if merged.get(key) in (None, "") and filled.get(key) not in (None, ""):
                merged[key] = filled.get(key)
        out.append(merged)
    return out


def _price_hold_why(parsed: dict[str, Any], proof: dict[str, Any]) -> str:
    pdf_amt = money(parsed.get("amount"))
    posted = money(proof.get("invoice_amount"))
    rec_lines = proof.get("receipt_lines") or []
    pdf_lines = parsed.get("lines") or []
    inv = parsed.get("invoice_number")
    kid = proof.get("id")
    bits: list[str] = []
    if pdf_lines and rec_lines and len(pdf_lines) == 1 and len(rec_lines) == 1:
        pdf_qty = money(pdf_lines[0].get("qty"))
        pdf_unit = money(pdf_lines[0].get("unit_price"))
        rec_qty = money(rec_lines[0].get("qty"))
        rec_unit = money(rec_lines[0].get("unit"))
        if pdf_qty is not None and rec_qty is not None and pdf_qty == rec_qty:
            if pdf_unit is not None and rec_unit is not None and pdf_unit != rec_unit:
                decision = decide_ppv(
                    invoice_line_amount=pdf_amt or 0.0,
                    po_line_amount=money(rec_qty * rec_unit) if rec_qty is not None else (posted or 0.0),
                    invoice_total=pdf_amt or 0.0,
                    po_unit_price=rec_unit,
                )
                if decision.get("hold"):
                    gap = abs((pdf_amt or 0) - (posted or 0))
                    pct = (gap / pdf_amt * 100) if pdf_amt else 0
                    bits.append(
                        f"HOLD (price-does-not-match): PDF invoice {inv} is qty {pdf_qty:g} "
                        f"@ ${pdf_unit:.2f} = ${pdf_amt:.2f}. Receipt {rec_lines[0].get('receipt')} "
                        f"is qty {rec_qty:g} @ ${rec_unit} = ${posted}. "
                        f"({pct:.1f}% of invoice total / ${gap:.2f}). Do not post PPV. "
                        f"{SHAWN_MCKIBBEN}: purchasing must unreceive, change the PO price, "
                        "and re-receive. Do not alter receipt unit price in GI. "
                        f"Header {kid} + PDF attached + receipt selected "
                        f"(KIMCO Invoice_Amount is now {posted} from the receipt). "
                        "Treyce would still rework the price. Outlook Entered with issues. "
                        "Flag status=entered-with-issues."
                    )
    if not bits and pdf_amt is not None and posted is not None and abs(pdf_amt - posted) > 0.02:
        decision = decide_ppv(
            invoice_line_amount=pdf_amt,
            po_line_amount=posted,
            invoice_total=pdf_amt,
        )
        if decision.get("hold"):
            gap = abs(pdf_amt - posted)
            pct = (gap / pdf_amt * 100) if pdf_amt else 0
            bits.append(
                f"HOLD (price-does-not-match): PDF invoice {inv} amount ${pdf_amt:.2f} vs "
                f"KIMCO posted ${posted:.2f} ({pct:.1f}% / ${gap:.2f}). Do not post PPV. "
                f"{SHAWN_MCKIBBEN}. Header {kid} + PDF attached. "
                "Treyce would still rework the price. Outlook Entered with issues. "
                "Flag status=entered-with-issues."
            )
    return bits[0] if bits else ""


def _qty_hold_why(
    parsed: dict[str, Any],
    proof: dict[str, Any],
    *,
    open_on_po: list[dict[str, Any]] | None = None,
) -> str:
    inv = parsed.get("invoice_number")
    kid = proof.get("id")
    pdf_amt = money(parsed.get("amount"))
    pdf_lines = list(parsed.get("lines") or [])
    recs = list(proof.get("receipt_lines") or [])
    bits = [
        f"HOLD (qty-does-not-match): PDF invoice {inv} has {len(pdf_lines)} "
        f"merchandise line(s) totaling ${pdf_amt:.2f}."
        if pdf_amt is not None
        else f"HOLD (qty-does-not-match): PDF invoice {inv} has {len(pdf_lines)} merchandise line(s)."
    ]
    selected_ids = _receipt_ids(proof)
    for idx, line in enumerate(pdf_lines, start=1):
        part = str(line.get("part") or line.get("label") or f"line {idx}")
        desc = str(line.get("description") or line.get("label") or "")[:70]
        iq = money(line.get("qty"))
        iu = money(line.get("unit_price"))
        ia = money(line.get("amount"))
        rec = recs[idx - 1] if idx - 1 < len(recs) else None
        if rec is None:
            leftover = ""
            if open_on_po:
                leftover_bits = []
                for rec_open in open_on_po:
                    if rec_open.get("id") in selected_ids:
                        continue
                    leftover_bits.append(
                        f"{rec_open.get('id')} qty {money(rec_open.get('qty'))} "
                        f"@ ${money(rec_open.get('unit_price') or rec_open.get('amount'))}"
                    )
                if leftover_bits:
                    leftover = " Open leftover receipt(s): " + "; ".join(leftover_bits) + "."
            bits.append(
                f" Line {idx} {part} {desc} invoice qty {iq:g} @ ${iu:.2f} = ${ia:.2f} "
                f"has no matching selected receipt.{leftover}"
                if iq is not None and iu is not None and ia is not None
                else f" Line {idx} {part} has no matching selected receipt.{leftover}"
            )
        else:
            rq = money(rec.get("qty"))
            ru = money(rec.get("unit"))
            if iq is not None and rq is not None and iq != rq:
                bits.append(
                    f" Line {idx} {part} invoice qty {iq:g} @ ${iu} vs receipt "
                    f"{rec.get('receipt')} qty {rq:g} @ ${ru}."
                )
            else:
                bits.append(
                    f" Line {idx} {part} selected receipt {rec.get('receipt')} "
                    f"qty {rq:g} @ ${ru}."
                )
    bits.append(
        f" Partial Select Receipts posted on matching line(s) (NOTE-23). "
        f"Do not invent Success. Header {kid} + PDF attached. "
        "Treyce would still rework leftover qty. Outlook Entered with issues. "
        "Flag status=entered-with-issues."
    )
    return "".join(bits)


def _success_why(parsed: dict[str, Any], proof: dict[str, Any], extra: str = "") -> str:
    recs = format_receipts(proof)
    return (
        f"Finished bill (Invoice_Type 3). Invoice # {parsed.get('invoice_number')} from PDF. "
        f"PO {parsed.get('po')} set. Select Receipts posted {recs}. "
        f"Live GET Invoice_Amount {proof.get('invoice_amount')} matches PDF "
        f"{parsed.get('amount')}. Vendor 22 AMERICAN QUALITY POWDERCOATING matches parsed. "
        "No fees. Attach status=attached. Guest browser View-details click, no Intuit login. "
        f"{extra}Treyce would not rework. Flag status=entered-in-ai."
    )


def try_finish_receipts(
    client: KimcoClient,
    *,
    kimco_id: int,
    parsed: dict[str, Any],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    proof = live_get_proof(client, kimco_id)
    have = _receipt_ids(proof)
    match = match_receipts(
        invoice_number=str(parsed.get("invoice_number") or ""),
        invoice_lines=list(parsed.get("lines") or []),
        receipts=receipts,
        po_number=str(parsed.get("po") or ""),
        invoice_amount=parsed.get("amount"),
    )
    wanted: list[int] = []
    for hit in match.get("matched") or []:
        rec = hit.get("receipt") if isinstance(hit, dict) else None
        rid = (rec or {}).get("id") if isinstance(rec, dict) else None
        if rid not in (None, "") and int(rid) not in have:
            wanted.append(int(rid))
    leftover_lines = list(parsed.get("lines") or [])
    pool = _hydrate_receipts(
        client,
        [
            r
            for r in _open_receipts_on_po(receipts, str(parsed.get("po") or ""))
            if r.get("id") not in have
        ],
    )
    if not wanted and leftover_lines and len(pool) == len(leftover_lines):
        line_keys = sorted(
            (
                money(ln.get("qty")),
                money(ln.get("unit_price") or ln.get("amount")),
            )
            for ln in leftover_lines
        )
        rec_keys = sorted((money(r.get("qty")), money(r.get("unit_price") or r.get("amount"))) for r in pool)
        if line_keys == rec_keys:
            wanted = [int(r["id"]) for r in pool if r.get("id") not in (None, "")]
    # Partial: unique leftover qty+cost matches even when line counts differ.
    if leftover_lines and pool:
        used: set[int] = {int(x) for x in wanted}
        for ln in leftover_lines:
            key = (money(ln.get("qty")), money(ln.get("unit_price")))
            hits = [
                r
                for r in pool
                if r.get("id") not in used
                and (money(r.get("qty")), money(r.get("unit_price"))) == key
            ]
            if len(hits) == 1:
                used.add(int(hits[0]["id"]))
                wanted.append(int(hits[0]["id"]))
        # Invoice qty N across leftover same-unit receipts (10939: 3 x qty-1 @ $15).
        for ln in leftover_lines:
            iq = money(ln.get("qty"))
            iu = money(ln.get("unit_price"))
            if iq is None or iu is None:
                continue
            if any(
                r.get("id") in used
                and (money(r.get("qty")), money(r.get("unit_price"))) == (iq, iu)
                for r in pool
            ):
                continue
            candidates = [
                r
                for r in pool
                if r.get("id") not in used and money(r.get("unit_price")) == iu
            ]
            acc = 0.0
            pick: list[dict[str, Any]] = []
            for rec in sorted(candidates, key=lambda r: (money(r.get("qty")) or 0, int(r.get("id") or 0))):
                q = money(rec.get("qty")) or 0.0
                if acc + q <= iq + 0.001:
                    pick.append(rec)
                    acc += q
                    if abs(acc - iq) <= 0.001:
                        break
            if pick and abs(acc - iq) <= 0.001:
                for rec in pick:
                    used.add(int(rec["id"]))
                    wanted.append(int(rec["id"]))
    status = "already-selected"
    if wanted:
        status = client.try_select_receipts(kimco_id, wanted)
    after = live_get_proof(client, kimco_id)
    return {
        "wanted": wanted,
        "status": status,
        "match_how": match.get("hows") or match.get("how"),
        "after": after,
        "open_on_po": [
            {
                "id": r.get("id"),
                "qty": r.get("qty"),
                "unit_price": r.get("unit_price"),
                "amount": r.get("amount"),
                "part": r.get("part"),
            }
            for r in pool
        ],
    }


def quality_row(
    graph,
    *,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    proof: dict[str, Any],
    finish: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(enter_row)
    kid = proof.get("id") or enter_row.get("KIMCO id")
    pdf_amt = money(parsed.get("amount"))
    posted = money(proof.get("invoice_amount"))
    recs = proof.get("receipt_lines") or []
    attach_ok = bool(proof.get("attachments"))
    message_id = str(parsed.get("graph_message_id") or "")
    extra = ""
    if finish and finish.get("wanted"):
        extra = f"Post-enter Select Receipts {finish.get('status')} ids={finish.get('wanted')}. "

    qty_hold = False
    qty_why = ""
    pdf_lines = parsed.get("lines") or []
    price_why = _price_hold_why(parsed, proof) if recs else ""
    if pdf_lines and recs and len(pdf_lines) == len(recs):
        for inv_line, rec in zip(pdf_lines, recs, strict=False):
            iq = money(inv_line.get("qty"))
            rq = money(rec.get("qty"))
            if iq is not None and rq is not None and iq != rq:
                qty_hold = True
                qty_why = _qty_hold_why(
                    parsed, proof, open_on_po=(finish or {}).get("open_on_po")
                )
                break
    elif pdf_lines and len(recs) < len(pdf_lines):
        qty_hold = True
        qty_why = _qty_hold_why(parsed, proof, open_on_po=(finish or {}).get("open_on_po"))

    finished = (
        attach_ok
        and recs
        and pdf_amt is not None
        and posted is not None
        and abs(pdf_amt - posted) <= 0.02
        and proof.get("invoice_type") == 3
        and proof.get("vendor_id") == 22
        and not qty_hold
        and not price_why
    )
    if finished:
        out["Result"] = "Success"
        out["Why"] = _success_why(parsed, proof, extra)
        out["Flag status"] = "entered-in-ai"
        out["Amount"] = pdf_amt
        if graph is not None and message_id:
            out["outlook"] = graph.flag_matched(ALLOWED_MAILBOX, message_id)
    elif qty_hold:
        out["Result"] = "HOLD"
        out["Why"] = qty_why
        out["Flag status"] = "entered-with-issues"
        out["Amount"] = pdf_amt
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    elif price_why:
        out["Result"] = "HOLD"
        out["Why"] = price_why
        out["Flag status"] = "entered-with-issues"
        out["Amount"] = pdf_amt
        if graph is not None and message_id:
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    else:
        out["Result"] = "HOLD"
        out["Why"] = (
            f"HOLD after live GET of {kid}: PDF amount={pdf_amt} posted={posted} "
            f"receipts={format_receipts(proof)} attach={attach_ok} type={proof.get('invoice_type')}. "
            "Do not invent Success. Never AI Skipped for AQPC. "
            f"{extra}Outlook Entered with issues. Flag status=entered-with-issues."
        )
        out["Flag status"] = "entered-with-issues"
        out["Amount"] = pdf_amt
        if graph is not None and message_id and kid not in (None, ""):
            out["outlook"] = graph.flag_issues(ALLOWED_MAILBOX, message_id)
    out["Receipts"] = format_receipts(proof)
    out["Fees"] = enter_row.get("Fees and surcharges") or "none"
    out["Attach"] = "attached" if attach_ok else enter_row.get("Attach status") or ""
    out["Fees and surcharges"] = out["Fees"]
    out["Attach status"] = out["Attach"]
    out["Flag in Outlook"] = "Yes"
    out["Notes"] = ""
    return out


def prior_rows_from_sheet() -> dict[str, dict[str, Any]]:
    if not PRIOR_SHEET.exists():
        return {}
    payload = json.loads(PRIOR_SHEET.read_text())
    return {str(r.get("Invoice #")): r for r in payload.get("rows") or []}


def catalog_messages(messages: list[dict[str, Any]], entered: dict[str, int]) -> list[dict[str, Any]]:
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
                "already_skip": inv in ALREADY,
                "subject": str(msg.get("subject") or "")[:120],
            }
        )
    catalog.sort(key=lambda r: (r.get("invoice") or "", str(r.get("received") or "")), reverse=True)
    return catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter 5 more AQPC invoices on batch 711 (20-row sheet)")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--skip-enter", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-20.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(format_intuit_session_presence(), flush=True)
    print("Target: live. No Intuit login / no storage-state. No Mail.Send. invent=false.", flush=True)
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
    catalog = catalog_messages(messages, entered)
    print(json.dumps({"aqpc_mail": catalog}, indent=2, default=str), flush=True)

    already = set(ALREADY) | set(entered)
    picked = pick_five(messages, already=already)
    print("Picked five:", [m.get("_wanted_invoice") for m in picked], flush=True)
    for msg in picked:
        print(
            f"  {msg.get('_wanted_invoice')} recv={msg.get('receivedDateTime')} "
            f"flagged={is_already_flagged(msg)} subj={str(msg.get('subject') or '')[:90]}",
            flush=True,
        )
    if args.discover_only:
        return 0 if len(picked) == 5 else 2

    enter_rows: list[dict[str, Any]] = []
    parsed: list[dict[str, Any]] = []
    invoices: list[dict[str, Any]] = []
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

    receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
    prior = prior_rows_from_sheet()
    known_rows: list[dict[str, Any]] = []
    known_gets: dict[str, Any] = {}
    for spec in KNOWN_FIFTEEN:
        kid = spec["kimco_id"]
        inv = spec["invoice"]
        got = live_get_proof(client, kid)
        known_gets[str(kid)] = got
        old = prior.get(inv) or {}
        row = dict(old) if old else {
            "Vendor": "American Quality Powder Coating",
            "Invoice #": inv,
            "KIMCO id": kid,
            "Batch": f"{BATCH_NAME} ({BATCH_ID})",
        }
        row["Receipts"] = format_receipts(got) or old.get("Receipts") or ""
        row["Attach"] = "attached" if got.get("attachments") else old.get("Attach") or old.get("Attach status") or ""
        row["Attach status"] = row["Attach"]
        row["Fees"] = old.get("Fees") or old.get("Fees and surcharges") or "none"
        row["Fees and surcharges"] = row["Fees"]
        row["PPV"] = old.get("PPV") or "none"
        row["Flag in Outlook"] = "Yes"
        row["Notes"] = ""
        if kid in HOLD_PDF_AMOUNTS:
            row["Amount"] = HOLD_PDF_AMOUNTS[kid]
            row["Result"] = "HOLD"
            row["Flag status"] = "entered-with-issues"
        known_rows.append(row)

    new_gets: dict[str, Any] = {}
    finishes: dict[str, Any] = {}
    sheet_new: list[dict[str, Any]] = []
    parsed_by_inv = {str(p.get("invoice_number")): p for p in parsed}
    invoice_by_num = {str(inv.get("invoice_number")): inv for inv in invoices}
    for row in enter_rows:
        inv_no = str(row.get("Invoice #") or "")
        kid = row.get("KIMCO id")
        parsed_row = invoice_by_num.get(inv_no) or parsed_by_inv.get(inv_no) or {}
        finish = None
        if kid not in (None, ""):
            proof = live_get_proof(client, int(kid))
            if not proof.get("receipt_lines") or len(proof.get("receipt_lines") or []) < len(
                parsed_row.get("lines") or []
            ):
                finish = try_finish_receipts(
                    client,
                    kimco_id=int(kid),
                    parsed=parsed_row,
                    receipts=receipts,
                )
                proof = finish.get("after") or live_get_proof(client, int(kid))
                finishes[inv_no] = {k: v for k, v in finish.items() if k != "after"}
            new_gets[str(kid)] = proof
            quality = quality_row(
                graph,
                parsed=parsed_row,
                enter_row=row,
                proof=proof,
                finish=finish,
            )
            sheet_new.append(quality)
        else:
            out = dict(row)
            out["Fees"] = row.get("Fees and surcharges") or "none"
            out["Attach"] = row.get("Attach status") or ""
            out["Receipts"] = ""
            if out.get("Result") == "Skipped":
                out["Result"] = "HOLD"
                out["Why"] = (
                    f"HOLD (pdf-behind-link or enter-failed) for AQPC {inv_no}. "
                    "Never AI Skipped for AQPC. " + str(row.get("Why") or "")
                )
                out["Flag status"] = "ai-hold"
            sheet_new.append(out)

    all_rows = known_rows + sheet_new
    report_path = Path(args.report)
    write_kyle_sheet(report_path, all_rows)
    also = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.xlsx"
    if report_path.resolve() != also.resolve():
        write_kyle_sheet(also, all_rows)
    print(f"Wrote {report_path}", flush=True)

    sidecar = {
        "proof": "aqpc-batch711-20",
        "invent": False,
        "mail_send": False,
        "storage_state": False,
        "intuit_login": False,
        "batch_name": BATCH_NAME,
        "batch_id": BATCH_ID,
        "skipped_already_on_711": {inv: entered.get(inv) for inv in sorted(ALREADY_ON_711)},
        "skipped_kyle_entered": {inv: entered.get(inv) for inv in sorted(KYLE_ENTERED)},
        "chosen": [m.get("_wanted_invoice") for m in picked],
        "parsed": parsed,
        "enter_rows": enter_rows,
        "finishes": finishes,
        "known_gets": known_gets,
        "new_gets": new_gets,
        "rows": all_rows,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar_path = report_path.with_suffix(".json")
    sidecar_path.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    also_json = also.with_suffix(".json")
    if sidecar_path.resolve() != also_json.resolve():
        also_json.write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar_path}", flush=True)
    success = sum(1 for r in sheet_new if r.get("Result") == "Success")
    hold = sum(1 for r in sheet_new if r.get("Result") == "HOLD")
    all_success = sum(1 for r in all_rows if r.get("Result") == "Success")
    all_hold = sum(1 for r in all_rows if r.get("Result") == "HOLD")
    print(f"NEW five: Success={success} HOLD={hold}", flush=True)
    print(f"ALL 20: Success={all_success} HOLD={all_hold}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
