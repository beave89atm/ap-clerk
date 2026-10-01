"""Link AFT bill 10398 to Customer P.O. No. 59097 from invoice 52004.

The priced line is 346 lb at 0.55 = 190.30. A receipt is selected only when
quantity and price match that line to the penny. Does not change the vendor,
does not post, and does not close Transfer AP.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.aft_customer_po import (
    assign_receipts,
    build_treyce_note,
    customer_po_from_layout,
    payable_amount,
)
from ap_clerk.kimco import API_AGENT_COMMENT_AUTHOR_IDS, KimcoError, comment_author_from_access_token
from scripts.aft_customer_po_10398_10399 import (
    BATCH_ID,
    _po_token,
    _public_snap,
    _save_note,
    _scan,
    batch_status,
    download_pdfs,
    header_matches,
    link_po,
    load_live,
    po_header,
    receipt_row,
    restore_vendor,
    snapshot,
)

BILL_ID = 10398
INVOICE = "52004"
PDF_TOTAL = 190.30
CUSTOMER_PO = "59097"
# 346 lb x $0.55/lb = $190.30. 49 each is the piece count, not the priced quantity.
PRICED_LINE = {"qty": 346.0, "unit_price": 0.55, "ext": 190.30}
OUT_JSON = ROOT / "artifacts" / "aft-52004-customer-po.json"
READBACK_10399 = 10399


def layout_text(content: bytes) -> str:
    path = Path("/tmp/aft-52004.pdf")
    path.write_bytes(content)
    return subprocess.check_output(["pdftotext", "-layout", str(path), "-"], text=True)


def open_receipts_for(client: Any, items: list[dict[str, Any]], number: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for raw in items:
        if raw.get("id") in (None, "") or raw.get("id") in seen:
            continue
        blob = json.dumps(raw.get("values") or raw)
        if not _po_token(blob, number):
            continue
        seen.add(raw.get("id"))
        full = raw if receipt_row(raw, number) else client.get_item("receipts", int(raw["id"]))
        row = receipt_row(full, number)
        if row:
            rows.append(row)
    return rows


def main() -> None:
    client = load_live()
    author = comment_author_from_access_token(client.access_token)
    if author["id"] != API_AGENT_COMMENT_AUTHOR_IDS["live"]:
        raise SystemExit("stopping: login is not API Agent 175")
    batch_before = batch_status(client)
    readback = snapshot(client.get_item("ap_invoices", READBACK_10399))
    record = client.get_item("ap_invoices", BILL_ID)
    before = snapshot(record)
    problems = header_matches(before, {"id": BILL_ID, "invoice": INVOICE, "amount": PDF_TOTAL})
    if problems:
        raise SystemExit("refusing 10398: " + "; ".join(problems))
    if before["vendor_id"] != 385:
        raise SystemExit(f"refusing vendor change; vendor is {before['vendor_id']} {before['vendor']}")
    pdfs = download_pdfs(client, BILL_ID, client.list_attachments(BILL_ID))
    if len(pdfs) != 1:
        raise SystemExit(f"expected one PDF, found {len(pdfs)}")
    text = layout_text(pdfs[0][1])
    po = customer_po_from_layout(text, invoice_number=INVOICE)
    if po != CUSTOMER_PO:
        raise SystemExit(f"Customer P.O. No. parsed as {po}; stopping without a write")
    po_items = _scan(client, "purchase_lines", [CUSTOMER_PO])
    receipt_items = _scan(client, "receipts", [CUSTOMER_PO])
    header = po_header(po_items, CUSTOMER_PO)
    if header is None:
        candidates: dict[int, dict[str, Any]] = {}
        for raw in po_items:
            if raw.get("id") in (None, ""):
                continue
            full = client.get_item("purchase_lines", int(raw["id"]))
            one = po_header([full], CUSTOMER_PO)
            if one:
                candidates[int(one["id"])] = one
        header = next(iter(candidates.values())) if len(candidates) == 1 else None
    if not header:
        raise SystemExit("PO 59097 was not found; stopping without a vendor change")
    receipts = open_receipts_for(client, receipt_items, CUSTOMER_PO)
    chosen = assign_receipts([PRICED_LINE], receipts)
    link_status = link_po(client, BILL_ID, before, int(header["id"]))
    after_link = snapshot(client.get_item("ap_invoices", BILL_ID))
    if after_link["vendor_id"] != before["vendor_id"]:
        restore_vendor(client, BILL_ID, int(before["vendor_id"]))
        after_link = snapshot(client.get_item("ap_invoices", BILL_ID))
    if after_link["vendor_id"] != 385 or after_link["batch_id"] != BATCH_ID:
        raise SystemExit("vendor or batch changed while linking the PO")
    if after_link["posted"] not in (None, "", False):
        raise SystemExit("bill became posted")
    linked = after_link["po_id"] == int(header["id"])
    select_status = "not-selected"
    if chosen and linked:
        select_status = client.try_select_receipts(BILL_ID, [{"id": row["id"]} for row in chosen])
    after_select = snapshot(client.get_item("ap_invoices", BILL_ID))
    if after_select["vendor_id"] != 385:
        restore_vendor(client, BILL_ID, 385)
        after_select = snapshot(client.get_item("ap_invoices", BILL_ID))
    if after_select["vendor_id"] != 385 or after_select["batch_id"] != BATCH_ID:
        raise SystemExit("vendor or batch changed during receipt selection")
    if after_select["posted"] not in (None, "", False):
        raise SystemExit("bill became posted")
    selected_ids = {line["receipt"] for line in after_select["lines"]}
    selected = [row for row in chosen if row["id"] in selected_ids]
    payable, payable_field = payable_amount(after_select)
    if payable is None:
        raise SystemExit("payable total missing")
    html = build_treyce_note(
        po=CUSTOMER_PO,
        invoice=INVOICE,
        linked=linked,
        selected=selected,
        payable=float(payable),
        match_reason="selected" if selected else "matched-none",
    )
    note = _save_note(client, BILL_ID, before, html)
    final = note["final"]
    batch_after = batch_status(client)
    comment = next((row for row in final["comments"] if row["id"] == note["comment_id"]), None)
    prior = next((row for row in readback["comments"] if row["id"] == 1330), None)
    out = {
        "login_user_id": author["id"],
        "login_user_name": author["name"],
        "batch_before": batch_before,
        "batch_after": batch_after,
        "batch_closed": batch_before.get("status") != batch_after.get("status"),
        "bill_10399": {
            "bill_id": READBACK_10399,
            "customer_po": "59106",
            "po_linked": readback["po_id"] == 7108,
            "po_text": readback["po"],
            "receipts_selected": bool(readback["lines"]),
            "selected_receipt_ids": [line["receipt"] for line in readback["lines"]],
            "payable": payable_amount(readback)[0],
            "comment_id": 1330,
            "comment_html": None if prior is None else prior.get("html"),
            "vendor_id": readback["vendor_id"],
            "posted": readback["posted"],
            "batch_id": readback["batch_id"],
        },
        "bill_10398": {
            "bill_id": BILL_ID,
            "invoice": INVOICE,
            "customer_po": CUSTOMER_PO,
            "po_header": header,
            "priced_line": PRICED_LINE,
            "open_receipts": receipts,
            "link_status": "linked" if linked else link_status,
            "po_linked": linked,
            "po_text": final["po"],
            "select_status": select_status,
            "receipts_selected": bool(selected),
            "selected_receipt_ids": [row["id"] for row in selected],
            "payable": payable_amount(final)[0],
            "payable_field": payable_field,
            "comment_id": note["comment_id"],
            "comment_status": note["comment_status"],
            "comment_html": None if comment is None else comment.get("html"),
            "vendor_id": final["vendor_id"],
            "vendor": final["vendor"],
            "posted": final["posted"],
            "batch_id": final["batch_id"],
            "batch": final["batch"],
            "invoice_type": final["invoice_type"],
            "after": _public_snap(final),
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    if batch_before.get("status") != batch_after.get("status"):
        raise SystemExit("batch status changed")


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
