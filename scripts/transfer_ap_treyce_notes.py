"""Tag Treyce on Transfer AP edits. Do not post any bill.

Kyle 2026-09-28. Mention id 33 is the data-mention-id on existing
Comments_1 spans addressed to @Treyce Hodges. Comment 1139 (A1 67067)
is overwritten in place so that sales-tax note tags him. Poller comments
1134–1138 get a new AP Clerk follow-up when they have no real Treyce span.
Receipts, amounts, batches, and Posted stay as they are.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient, KimcoError, added_comment_payload, replace_comment_payload
from ap_clerk.rules import (
    TREYCE_MENTION_HTML,
    TREYCE_MENTION_ID,
    ap_clerk_edit_note,
    lookup_id,
    lookup_text,
    money,
    state_sales_tax_comment,
)

HOST = "https://live.kimcoerp.com"
BATCH_ID = 375
OUT_JSON = ROOT / "artifacts" / "transfer-ap-treyce-notes-2026-09-28.json"

FOLLOWUPS = (
    {
        "comment_id": 1134,
        "bill_id": 10322,
        "invoice": "TXFT4100503",
        "text": (
            "AP Clerk: Follow-up for comment 1134. Selected receipt 24710 on PO 59190 "
            "line 01 (qty 250 at 0.624 = 156.00) and receipt 24709 line 02 "
            "(qty 50 at 1.80 = 90.00). Shipping and Handling 22.14 was added as a fee. "
            "Lines 246.00 plus the fee match the invoice at 268.14. "
            "The bill stays in Transfer AP and is not posted."
        ),
    },
    {
        "comment_id": 1135,
        "bill_id": 10376,
        "invoice": "1473030",
        "text": (
            "AP Clerk: Follow-up for comment 1135. Selected receipt 24715 on PO 59296, "
            "qty 50 at 0.3919 (19.60), which matches the PDF. The bill amount is 19.60. "
            "The bill stays in Transfer AP and is not posted."
        ),
    },
    {
        "comment_id": 1136,
        "bill_id": 10367,
        "invoice": "67067",
        "text": (
            "AP Clerk: Follow-up for comment 1136. Sales tax of 41.97 shown on the vendor "
            "invoice was added as an additional charge per Kyle. Receipt 24712 is unchanged. "
            "The bill now matches the invoice at 550.64. The bill stays in Transfer AP and is not posted."
        ),
    },
    {
        "comment_id": 1137,
        "bill_id": 10317,
        "invoice": "15455478",
        "text": (
            "AP Clerk: Follow-up for comment 1137. Selected new receipt 24720 on PO 59059 "
            "line 02, qty 1200 at 0.1665 (199.80). Receipt 23563 was not selected. "
            "Lines 257.52 plus a charge of -0.06 match the invoice at 257.46. "
            "The bill stays in Transfer AP and is not posted."
        ),
    },
    {
        "comment_id": 1138,
        "bill_id": 10323,
        "invoice": "TXFT4100537",
        "text": (
            "AP Clerk: Follow-up for comment 1138. Pricing stays as invoiced and a proof of "
            "delivery was requested. No receipts were selected and the bill was not finished. "
            "It stays in Transfer AP and is not posted."
        ),
    },
)


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("refusing non-live host")
    return KimcoClient.authenticate(creds.instance_url, creds.key or "", creds.password or "", target="live")


def redact(html: str) -> str:
    return re.sub(r'data-mention-email="[^"]*"', 'data-mention-email=""', html or "")


def comments_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        html = str((comment.get("values") or {}).get("HtmlValue") or "")
        raw_id = comment.get("id")
        try:
            comment_id = int(raw_id)
        except (TypeError, ValueError):
            comment_id = raw_id
        rows.append({"id": comment_id, "html": html})
    return rows


def fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lines = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        lines.append(
            {
                "id": line.get("id"),
                "receipt": lookup_id(lv.get("Receipt")),
                "qty": money(lv.get("Quantity")),
                "ext": money(lv.get("Extended_Amount")),
            }
        )
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": money(values.get("Invoice_Amount")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "posted": values.get("Posted"),
        "lines": lines,
    }


def has_treyce_mention(html: str) -> bool:
    return f'data-mention-id="{TREYCE_MENTION_ID}"' in html and "prosemirror-mention-node" in html


def live_treyce_span(records: list[dict[str, Any]]) -> str:
    for record in records:
        for comment in comments_of(record):
            for match in re.finditer(r"<span\b[^>]*>[\s\S]*?</span>", comment["html"], flags=re.I):
                tag = match.group(0)
                if f'data-mention-id="{TREYCE_MENTION_ID}"' not in tag:
                    continue
                if 'data-mention-name="Treyce Hodges"' not in tag:
                    continue
                if "prosemirror-mention-node" not in tag:
                    continue
                return tag
    return TREYCE_MENTION_HTML


def with_live_span(html: str, span: str) -> str:
    if span == TREYCE_MENTION_HTML or TREYCE_MENTION_HTML not in html:
        return html
    return html.replace(TREYCE_MENTION_HTML, span, 1)


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems = []
    for key in ("invoice", "amount", "batch_id", "posted", "lines"):
        if before.get(key) != after.get(key):
            problems.append(f"{key} changed")
    if after.get("posted") not in (None, "", False):
        problems.append("bill is posted")
    if after.get("batch_id") != BATCH_ID:
        problems.append(f"batch {after.get('batch_id')} != {BATCH_ID}")
    return problems


def main() -> None:
    client = load_live()
    bill_ids = sorted({row["bill_id"] for row in FOLLOWUPS})
    records = {bill_id: client.get_item("ap_invoices", bill_id) for bill_id in bill_ids}
    before = {bill_id: fingerprint(records[bill_id]) for bill_id in bill_ids}
    for bill_id, snap in before.items():
        if snap["batch_id"] != BATCH_ID or snap["posted"] not in (None, "", False):
            raise SystemExit(f"refusing bill {bill_id}: batch {snap['batch_id']} posted {snap['posted']!r}")
    span = live_treyce_span(list(records.values()))
    if f'data-mention-id="{TREYCE_MENTION_ID}"' not in span:
        raise SystemExit("Treyce mention span is missing id 33")

    checked: list[dict[str, Any]] = []
    for spec in FOLLOWUPS:
        rows = comments_of(records[spec["bill_id"]])
        target = next((row for row in rows if row["id"] == spec["comment_id"]), None)
        if target is None:
            raise SystemExit(f"comment {spec['comment_id']} is not on bill {spec['bill_id']}")
        if str(before[spec["bill_id"]]["invoice"]) != spec["invoice"]:
            raise SystemExit(
                f"bill {spec['bill_id']} invoice {before[spec['bill_id']]['invoice']} != {spec['invoice']}"
            )
        checked.append(
            {
                "comment_id": spec["comment_id"],
                "bill_id": spec["bill_id"],
                "invoice": spec["invoice"],
                "had_treyce_mention": has_treyce_mention(target["html"]),
            }
        )

    sales_note = with_live_span(
        state_sales_tax_comment(sales_tax=41.97, invoice_total=550.64, batch_id=BATCH_ID),
        span,
    )
    if not has_treyce_mention(sales_note) or "AP Clerk:" not in sales_note:
        raise SystemExit("sales tax note is missing the Treyce mention")
    bill_10367 = comments_of(records[10367])
    note_1139 = next((row for row in bill_10367 if row["id"] == 1139), None)
    if note_1139 is None:
        raise SystemExit("comment 1139 is not on bill 10367")
    replace_status = "already"
    if has_treyce_mention(note_1139["html"]) and "Sales tax of $41.97 was added per Kyle" in note_1139["html"]:
        replace_status = "already"
    else:
        _body, status, error = client.update(
            "ap_invoices",
            10367,
            replace_comment_payload(10367, 1139, sales_note),
        )
        replace_status = "replaced" if status < 400 else f"blocked-{status}"
        # KIMCO accepts a new Comments_1 span and rejects the same span on an
        # overwrite (400 Unexpected Error). The follow-up below carries the mention.
        if status >= 400 and status != 400:
            raise SystemExit(f"replace 1139 failed {status}")

    new_comments: list[dict[str, Any]] = []
    for spec in FOLLOWUPS:
        fresh = client.get_item("ap_invoices", spec["bill_id"])
        drift = unchanged(before[spec["bill_id"]], fingerprint(fresh))
        if drift:
            raise SystemExit(f"bill {spec['bill_id']} drifted before follow-up: {drift}")
        marker = f"Follow-up for comment {spec['comment_id']}."
        existing = next(
            (
                row
                for row in comments_of(fresh)
                if marker in row["html"] and has_treyce_mention(row["html"])
            ),
            None,
        )
        if existing is not None:
            new_comments.append(
                {
                    "poller_comment_id": spec["comment_id"],
                    "bill_id": spec["bill_id"],
                    "comment_id": existing["id"],
                    "status": "already",
                }
            )
            continue
        note = with_live_span(
            ap_clerk_edit_note(spec["text"], batch_id=BATCH_ID, batch_name="Transfer AP"),
            span,
        )
        prior = {row["id"] for row in comments_of(fresh)}
        _body, status, error = client.update(
            "ap_invoices",
            spec["bill_id"],
            added_comment_payload(spec["bill_id"], note),
        )
        if status >= 400:
            raise SystemExit(f"comment on {spec['bill_id']} failed {status} {error[:300]}")
        readback = client.get_item("ap_invoices", spec["bill_id"])
        drift = unchanged(before[spec["bill_id"]], fingerprint(readback))
        if drift:
            raise SystemExit(f"bill {spec['bill_id']} drifted after comment: {drift}")
        added = [
            row
            for row in comments_of(readback)
            if row["id"] not in prior and marker in row["html"] and has_treyce_mention(row["html"])
        ]
        if len(added) != 1:
            raise SystemExit(f"bill {spec['bill_id']} follow-up readback count {len(added)}")
        new_comments.append(
            {
                "poller_comment_id": spec["comment_id"],
                "bill_id": spec["bill_id"],
                "comment_id": added[0]["id"],
                "status": "added",
            }
        )

    final_10367 = client.get_item("ap_invoices", 10367)
    drift = unchanged(before[10367], fingerprint(final_10367))
    if drift:
        raise SystemExit(f"bill 10367 drifted after notes: {drift}")
    tagged_sales = [
        row
        for row in comments_of(final_10367)
        if has_treyce_mention(row["html"]) and "AP Clerk:" in row["html"] and "Sales tax of" in row["html"]
    ]
    if not tagged_sales:
        raise SystemExit("bill 10367 has no Treyce-tagged sales tax note")
    out_1139 = next(row for row in comments_of(final_10367) if row["id"] == 1139)

    out = {
        "treyce_mention_id": TREYCE_MENTION_ID,
        "poller_comments_had_treyce_mention": checked,
        "comment_1139_status": replace_status,
        "comment_1139_has_treyce_mention": has_treyce_mention(out_1139["html"]),
        "comment_1139_id": 1139,
        "tagged_sales_tax_comment_ids": [row["id"] for row in tagged_sales],
        "new_comment_ids": new_comments,
        "posted": {str(bill_id): before[bill_id]["posted"] for bill_id in bill_ids},
        "amounts_unchanged": {
            str(bill_id): fingerprint(client.get_item("ap_invoices", bill_id))["amount"] == before[bill_id]["amount"]
            for bill_id in bill_ids
        },
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
