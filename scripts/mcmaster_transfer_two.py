"""Kyle override: move McMaster 72013304 and 70759737 to Transfer AP.

Usual NOTE-45 keeps missing_receipt on the API Agent batch. Kyle 2026-09-22
explicitly asked these two onto Transfer AP with Comments_1 @Shawn 104.

Do not Select Receipts. Do not invent Success. Do not finish Transfer AP
(Treyce owns follow-through). No Mail.Send. Leave other McMaster HOLDs alone.
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

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.comments_tab import (
    SHAWN_MENTION,
    SHAWN_MENTION_ID,
    add_invoice_comment_tab,
    comment_tab_items,
    transfer_ap_batch_only_payload,
)
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.quality_v12 import apply_exception_category_owner
from ap_clerk.report import write_report
from ap_clerk.rules import SHAWN_MCKIBBEN, lookup_id, lookup_text, money
from mcmaster_0918 import (
    DO_NOT_MUTATE_IDS,
    KNOWN_BATCH_ID,
    PREFERRED_BATCH_NAME,
    TRANSFER_AP_PRIOR_ID_HINT,
    VENDOR_NAME,
    exact_invoice_number,
    find_transfer_ap_batch,
    mcmaster_proof,
    merge_sheet_rows,
    prior_rows_from_sidecar,
)
from mcmaster_receipt_retry import (
    assert_existing_header,
    download_kimco_pdf,
    enter_row_from_sheet,
    find_local_pdf,
    parsed_from_pdf,
)

LOGGER = logging.getLogger("ap_clerk.mcmaster_transfer_two")

TARGETS = {
    "72013304": {
        "kimco_id": 10146,
        "po": "58221",
        "amount": 443.49,
        "from_batch": KNOWN_BATCH_ID,
    },
    "70759737": {
        "kimco_id": 10158,
        "po": "59014",
        "amount": 1037.59,
        "from_batch": KNOWN_BATCH_ID,
    },
}
ALLOWED_WRITE_IDS = {spec["kimco_id"] for spec in TARGETS.values()}
VENDOR_ID = 117
COMMENT_NEEDLE = "Kyle asked AP to move"


def refuse_other_header(kimco_id: int, invoice_number: str) -> None:
    inv = exact_invoice_number(invoice_number) or str(invoice_number or "")
    if inv not in TARGETS:
        raise KimcoError(f"Refuse: this script writes 72013304/70759737 only, not {inv}")
    if int(kimco_id) != int(TARGETS[inv]["kimco_id"]):
        raise KimcoError(
            f"Refuse: {inv} expected KIMCO {TARGETS[inv]['kimco_id']}, not {kimco_id}"
        )
    if int(kimco_id) in DO_NOT_MUTATE_IDS:
        raise KimcoError(f"Refuse: {kimco_id} is GET-only Success")


def merch_summary(parsed: dict[str, Any]) -> str:
    bits = []
    for line in parsed.get("lines") or []:
        bits.append(
            f"{line.get('part')} {line.get('qty')}@{line.get('unit_price')}"
        )
    return "; ".join(bits) if bits else "no merch lines"


def shawn_action_note(*, invoice: str, parsed: dict[str, Any], live: dict[str, Any]) -> str:
    """Actionable Comments_1 body. Mention-id 104 is applied by mention_html."""
    spec = TARGETS[invoice]
    po = str(parsed.get("po") or spec["po"])
    amt = money(parsed.get("amount")) or spec["amount"]
    merch = merch_summary(parsed)
    fees = ", ".join(
        f"{f.get('name')} {f.get('amount')}" for f in (parsed.get("fees") or [])
    ) or "none"
    recs = live.get("receipt_lines") or []
    live_po = str(live.get("po_text") or po)
    if invoice == "72013304":
        wrong = (
            f"Live PO {live_po} is AQPC-titled. Open leftovers on 58221 are not "
            "McMaster 4082T15. Do not Select those AQPC leftovers (would lock "
            "the wrong merch). Receive McMaster 4082T15 qty 24 on the correct "
            "PO, or unreceive/reprice/re-receive if the PO line is wrong."
        )
    else:
        wrong = (
            f"PO {live_po} has zero open merch leftovers matching the PDF. "
            "Receive the PO 59014 lines so AP can Select Receipts."
        )
    return (
        f"{SHAWN_MCKIBBEN} {COMMENT_NEEDLE} McMaster-Carr {invoice} / "
        f"KIMCO {spec['kimco_id']} (PDF ${amt:.2f}, PO {po}) to Transfer AP. "
        f"Override of missing_receipt-stays-on-721. Type 3 header had "
        f"receipts={len(recs)} fees={fees}. PDF merch={merch}. {wrong} "
        "Treyce owns Transfer AP follow-through. Do not alter receipt unit "
        "price in GI. No email."
    )


def move_one(
    client: KimcoClient,
    *,
    invoice: str,
    parsed: dict[str, Any],
    enter_row: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    spec = TARGETS[invoice]
    kid = int(spec["kimco_id"])
    refuse_other_header(kid, invoice)
    header = assert_existing_header(client, kid, invoice)
    vals = header.get("values") or {}
    proof = mcmaster_proof(client, kid)
    from_batch = lookup_id(vals.get("AP_Invoice_Batch"))
    from_name = lookup_text(vals.get("AP_Invoice_Batch"))
    live = {
        "receipt_lines": proof.get("receipt_lines") or [],
        "po_text": lookup_text(vals.get("Purchase_Order")) or vals.get("PO"),
        "amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Invoice_Verification_Amount")),
        "fees": proof.get("fee_amounts"),
    }
    note = shawn_action_note(invoice=invoice, parsed=parsed, live=live)
    report: dict[str, Any] = {
        "invoice": invoice,
        "kimco_id": kid,
        "po": parsed.get("po") or spec["po"],
        "pdf_amount": parsed.get("amount") or spec["amount"],
        "from_batch": from_batch,
        "from_name": from_name,
        "note": note,
        "already_receipts": live["receipt_lines"],
        "dry_run": dry_run,
        "select": False,
        "success": False,
        "mail_send": False,
    }
    if from_batch not in {KNOWN_BATCH_ID, TRANSFER_AP_PRIOR_ID_HINT}:
        report["status"] = "blocked-unexpected-batch"
        report["blocked"] = f"batch {from_batch} is not 721/375"
        return report
    if dry_run:
        report["status"] = "would-move"
        return report
    batches = client.list_items("ap_batches")
    found = find_transfer_ap_batch(batches)
    if not found.get("found"):
        report["status"] = "batch-not-found"
        report["lookup"] = found
        return report
    bid = int(found["id"])
    payload = transfer_ap_batch_only_payload(invoice_id=kid, batch_id=bid)
    if "Comments" in (payload.get("values") or {}):
        report["status"] = "refused-header-comments"
        return report
    _body, status, error = client.update("ap_invoices", kid, payload)
    tab = add_invoice_comment_tab(
        client,
        invoice_id=kid,
        body=note,
        needles=(COMMENT_NEEDLE, invoice),
        mention=SHAWN_MENTION,
    )
    after = client.get_item("ap_invoices", kid)
    after_vals = after.get("values") or {}
    live_bid = lookup_id(after_vals.get("AP_Invoice_Batch"))
    live_name = lookup_text(after_vals.get("AP_Invoice_Batch"))
    moved = status < 400 and live_bid == bid
    items = comment_tab_items(after)
    new_ids = [
        row.get("id")
        for row in items
        if COMMENT_NEEDLE in str((row.get("values") or {}).get("HtmlValue") or "")
        and invoice in str((row.get("values") or {}).get("HtmlValue") or "")
    ]
    mention_ok = any(
        f'data-mention-id="{SHAWN_MENTION_ID}"'
        in str((row.get("values") or {}).get("HtmlValue") or "")
        for row in items
        if row.get("id") in new_ids
    )
    row = dict(enter_row)
    row["Result"] = "HOLD"
    row["Amount"] = parsed.get("amount") or spec["amount"]
    row["PO"] = parsed.get("po") or spec["po"]
    row["KIMCO id"] = kid
    row["Fees and surcharges"] = enter_row.get("Fees and surcharges") or (
        ", ".join(f"{f.get('name')} {f.get('amount')}" for f in (parsed.get("fees") or []))
        or "none"
    )
    row["PPV"] = enter_row.get("PPV") or "none"
    row["Receipts"] = "none"
    row["Attach status"] = "attached"
    row["Flag status"] = "entered-with-issues"
    row["Batch"] = (
        f"{live_name} ({live_bid})" if moved else f"{from_name} ({from_batch})"
    )
    row["Why"] = (
        f"HOLD (receipt): Kyle override — move McMaster {invoice} / {kid} "
        f"from {from_name} ({from_batch}) to Transfer AP. "
        f"PDF merch={merch_summary(parsed)}; selected=none. {note} "
        f"Transfer AP status={'moved' if moved else f'blocked-{status}'} "
        f"batch_id={live_bid} Comments_1 items={new_ids} "
        f"mention_id={SHAWN_MENTION_ID if mention_ok else None} "
        f"tab={tab.get('report') or tab.get('status')}. mail_send=False."
    )
    row = apply_exception_category_owner(row)
    report.update(
        {
            "status": "moved" if moved else f"blocked-{status}",
            "to_batch": live_bid,
            "to_name": live_name,
            "put": status,
            "error": error,
            "comments_1": tab,
            "comment_item_ids": new_ids,
            "mention_id": SHAWN_MENTION_ID if mention_ok else None,
            "row": row,
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-18-mcmaster.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live (McMaster Transfer AP two).")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key, creds.password, target="live"
    )
    report_path = Path(args.report)
    prior_rows = prior_rows_from_sidecar(report_path)
    results = []
    for invoice, spec in TARGETS.items():
        kid = int(spec["kimco_id"])
        pdf = find_local_pdf(invoice) or download_kimco_pdf(client, kid, invoice)
        if pdf is None:
            results.append({"invoice": invoice, "kimco_id": kid, "status": "no-pdf"})
            continue
        parsed = parsed_from_pdf(pdf, invoice, spec["po"])
        enter_row = enter_row_from_sheet(prior_rows, invoice, kid)
        results.append(
            move_one(
                client,
                invoice=invoice,
                parsed=parsed,
                enter_row=enter_row,
                dry_run=args.dry_run,
            )
        )
    rows = list(prior_rows)
    for item in results:
        if item.get("row"):
            rows = merge_sheet_rows(rows, [item["row"]])
    summary = {
        "dry_run": args.dry_run,
        "invent": False,
        "mail_send": False,
        "select": False,
        "results": [{k: v for k, v in item.items() if k != "row"} for item in results],
    }
    print(json.dumps(summary, indent=2, default=str), flush=True)
    if args.dry_run:
        return 0
    write_report(report_path, rows)
    sidecar = {
        "proof": "mcmaster-transfer-two",
        "invent": False,
        "mail_send": False,
        "vendor": VENDOR_NAME,
        "vendor_id": VENDOR_ID,
        "kyle_override": "missing_receipt Transfer AP for 72013304 and 70759737 only",
        "results": summary["results"],
        "rows": rows,
    }
    if report_path.with_suffix(".json").is_file():
        prior = json.loads(report_path.with_suffix(".json").read_text())
        sidecar["prior_proof"] = prior.get("proof")
        sidecar["catalog"] = prior.get("catalog")
        sidecar["leftover_pending"] = prior.get("leftover_pending")
    report_path.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2, default=str) + "\n"
    )
    print(f"Sheet: {report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
