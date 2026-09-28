"""Correct 2026-09-24 Comments_1 notes that show under Treyce Hodges.

The 10:15 CT poller wrote those notes through the form login, so KIMCO
authored them as user 33. This adds one API Agent note per misattributed
comment. Probe comments 1043-1046 are deleted only when they are still on
bill 10181, authored by 33, and the visible text is exactly "probe".

Does not post a bill. Does not change amounts, receipts, or batches.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import (
    TREYCE_COMMENT_AUTHOR_ID,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    comment_author_from_access_token,
    visible_comment_text,
)
from ap_clerk.rules import SHAWN_MENTION_HTML, TREYCE_MENTION_HTML, money

HOST = "https://live.kimcoerp.com"
OUT_JSON = ROOT / "artifacts" / "kimco-comments1-treyce-cleanup-2026-09-28.json"
PROBE_IDS = (1043, 1044, 1045, 1046)
PROBE_BILL_ID = 10181

NOTES = (
    {
        "bill_id": 10181,
        "invoice": "0040437952",
        "comment_id": 1048,
        "expected_amount": 241.46,
        "needle": "Selected receipt 24596",
        "receipt": (
            "AP Clerk selected receipt 24596 (qty 3 at 80.486 = 241.46) "
            "and left receipt 24597 unselected."
        ),
    },
    {
        "bill_id": 10294,
        "invoice": "0040388258",
        "comment_id": 1049,
        "expected_amount": 867.72,
        "needle": "Selected receipts 24602",
        "receipt": (
            "AP Clerk selected receipts 24602, 24603, 24604, 24605, 24606, 24607, and 24608."
        ),
    },
    {
        "bill_id": 10295,
        "invoice": "0040387888",
        "comment_id": 1050,
        "expected_amount": 2998.96,
        "needle": "Selected receipts 24610",
        "receipt": (
            "AP Clerk selected receipts 24610 through 24615 and did not bill the backordered lines."
        ),
    },
    {
        "bill_id": 10297,
        "invoice": "0040385309",
        "comment_id": 1051,
        "expected_amount": 294.28,
        "needle": "Removed receipt 24332",
        "receipt": "AP Clerk removed receipt 24332 and selected receipts 24599, 24600, and 24601.",
    },
    {
        "bill_id": 10146,
        "invoice": "72013304",
        "comment_id": 1052,
        "expected_amount": 443.49,
        "needle": "Selected receipt 24255",
        "receipt": (
            "AP Clerk changed the purchase order from PO58221 to PO59221 and selected "
            "receipt 24255 (qty 24 at 17.71 = 425.04), keeping the fee of 18.45."
        ),
    },
    {
        "bill_id": 10143,
        "invoice": "72068812",
        "comment_id": 1053,
        "expected_amount": 336.49,
        "needle": "no receipt was selected",
        "receipt": (
            "AP Clerk did not select a receipt. Receipt 24255 is 24 at 17.71 and is on "
            "invoice 72013304, not this bill."
        ),
    },
)


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("refusing non-live host")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    author = comment_author_from_access_token(client.access_token)
    if author["id"] == TREYCE_COMMENT_AUTHOR_ID or author["name"] == "Treyce Hodges":
        raise SystemExit("refusing to write Comments_1 as Treyce Hodges (33)")
    if author["id"] != 175 or author["name"] != "API Agent":
        raise SystemExit("refusing to write Comments_1: login is not API Agent (175)")
    return client


def comments_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        values = comment.get("values") or {}
        creator = values.get("CreatorId")
        creator_id = creator.get("id") if isinstance(creator, dict) else creator
        creator_name = creator.get("text") if isinstance(creator, dict) else ""
        try:
            comment_id = int(comment.get("id"))
        except (TypeError, ValueError):
            comment_id = comment.get("id")
        rows.append(
            {
                "id": comment_id,
                "html": str(values.get("HtmlValue") or ""),
                "creator_id": creator_id,
                "creator_name": creator_name,
                "created": str(values.get("CreatedOn") or ""),
            }
        )
    return rows


def receipt_ids(record: dict[str, Any]) -> list[Any]:
    ids = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        receipt = (line.get("values") or {}).get("Receipt")
        if isinstance(receipt, dict):
            ids.append(receipt.get("id"))
        else:
            ids.append(receipt)
    return ids


def charge_amounts(record: dict[str, Any]) -> list[Any]:
    amounts = []
    for charge in (record.get("lists") or {}).get("InvoiceAdditionalCharges") or []:
        amounts.append(money((charge.get("values") or {}).get("Amount")))
    return amounts


def fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    batch = values.get("AP_Invoice_Batch")
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "batch_id": batch.get("id") if isinstance(batch, dict) else batch,
        "posted": values.get("Posted"),
        "receipts": receipt_ids(record),
        "charges": charge_amounts(record),
    }


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems = []
    for key in ("invoice", "amount", "verification", "batch_id", "posted", "receipts", "charges"):
        if before.get(key) != after.get(key):
            problems.append(f"{key} changed")
    return problems


def amounts_match(live_amount: float | None, verification: float | None, expected: float) -> bool:
    if live_amount is None or verification is None:
        return False
    return abs(live_amount - expected) < 0.005 and abs(live_amount - verification) < 0.005


def gap_sentence(live_amount: float | None, verification: float | None) -> str:
    left = 0.0 if live_amount is None else live_amount
    right = 0.0 if verification is None else verification
    return (
        f"The live bill amount is {left:.2f} and the invoice verification amount is {right:.2f}, "
        f"a gap of {abs(right - left):.2f}."
    )


def note_html(spec: dict[str, Any], snap: dict[str, Any]) -> tuple[str, str]:
    if amounts_match(snap["amount"], snap["verification"], float(spec["expected_amount"])):
        matched = True
        closing = (
            f"The bill matches the invoice at {snap['amount']:.2f} "
            "and nothing further is needed from Shawn."
        )
    else:
        matched = False
        closing = gap_sentence(snap["amount"], snap["verification"])
    plain = (
        "AP Clerk: @Shawn McKibben @Treyce Hodges "
        f"The 9/24 note above (comment {spec['comment_id']}) showing under Treyce Hodges's name "
        "was written by AP Clerk, not Treyce. "
        f"{spec['receipt']} {closing}"
    )
    if not plain.startswith("AP Clerk:"):
        raise RuntimeError("note must start with AP Clerk:")
    if matched and "nothing further is needed from Shawn" not in plain:
        raise RuntimeError("matching note must tell Shawn nothing further is needed")
    if not matched and "matches the invoice" in plain:
        raise RuntimeError("a gap must not be described as a match")
    html = plain.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1).replace(
        "@Treyce Hodges", TREYCE_MENTION_HTML, 1
    )
    if html.count('data-mention-id="104"') != 1 or html.count('data-mention-id="33"') != 1:
        raise RuntimeError("note must mention Shawn 104 and Treyce 33 once each")
    if not html.startswith("AP Clerk:"):
        raise RuntimeError("mention replacement moved the AP Clerk prefix")
    return f"<p>{html}</p>", "matches" if matched else closing


def exact_probe(html: str) -> bool:
    return visible_comment_text(html) == "probe"


def removed_comment_payload(invoice_id: int, comment_id: int) -> dict[str, Any]:
    return {
        "state": "Modified",
        "id": int(invoice_id),
        "lists": {"Comments_1": [{"id": int(comment_id), "state": "Removed"}]},
    }


def delete_probes(client: KimcoClient, record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = comments_of(record)
    by_id = {row["id"]: row for row in rows}
    results = []
    for comment_id in PROBE_IDS:
        row = by_id.get(comment_id)
        if row is None:
            results.append({"id": comment_id, "status": "already_absent"})
            continue
        if row["creator_id"] != TREYCE_COMMENT_AUTHOR_ID or not exact_probe(row["html"]):
            results.append(
                {
                    "id": comment_id,
                    "status": "left_alone",
                    "creator_id": row["creator_id"],
                    "text": visible_comment_text(row["html"])[:80],
                }
            )
            continue
        before = fingerprint(record)
        _body, status, error = client.update(
            "ap_invoices",
            PROBE_BILL_ID,
            removed_comment_payload(PROBE_BILL_ID, comment_id),
        )
        if status >= 400:
            raise SystemExit(f"delete probe {comment_id} failed {status} {error[:200]}")
        readback = client.get_item("ap_invoices", PROBE_BILL_ID)
        drift = unchanged(before, fingerprint(readback))
        if drift:
            raise SystemExit(f"bill {PROBE_BILL_ID} drifted after deleting {comment_id}: {drift}")
        if any(item["id"] == comment_id for item in comments_of(readback)):
            raise SystemExit(f"probe {comment_id} is still on bill {PROBE_BILL_ID}")
        record = readback
        results.append({"id": comment_id, "status": "deleted"})
    return results


def main() -> None:
    client = load_live()
    bill_ids = sorted({int(row["bill_id"]) for row in NOTES})
    records = {bill_id: client.get_item("ap_invoices", bill_id) for bill_id in bill_ids}
    before = {bill_id: fingerprint(records[bill_id]) for bill_id in bill_ids}
    probes = delete_probes(client, records[PROBE_BILL_ID])
    if any(row["status"] == "deleted" for row in probes):
        records[PROBE_BILL_ID] = client.get_item("ap_invoices", PROBE_BILL_ID)
        drift = unchanged(before[PROBE_BILL_ID], fingerprint(records[PROBE_BILL_ID]))
        if drift:
            raise SystemExit(f"bill {PROBE_BILL_ID} drifted after probe cleanup: {drift}")

    written: list[dict[str, Any]] = []
    for spec in NOTES:
        bill_id = int(spec["bill_id"])
        fresh = client.get_item("ap_invoices", bill_id)
        drift = unchanged(before[bill_id], fingerprint(fresh))
        if drift:
            raise SystemExit(f"bill {bill_id} drifted before the correction note: {drift}")
        snap = fingerprint(fresh)
        if str(snap["invoice"]) != spec["invoice"] and spec["comment_id"] != 1053:
            raise SystemExit(f"bill {bill_id} invoice {snap['invoice']!r} != {spec['invoice']}")
        if spec["comment_id"] == 1053 and str(snap["invoice"]) not in {spec["invoice"], "58221"}:
            raise SystemExit(f"bill 10143 invoice number changed to {snap['invoice']!r}")
        rows = comments_of(fresh)
        target = next((row for row in rows if row["id"] == spec["comment_id"]), None)
        if target is None:
            raise SystemExit(f"comment {spec['comment_id']} is not on bill {bill_id}")
        text = visible_comment_text(target["html"])
        if target["creator_id"] != TREYCE_COMMENT_AUTHOR_ID:
            raise SystemExit(f"comment {spec['comment_id']} creator is {target['creator_id']}, not 33")
        if not str(target["created"]).startswith("2026-09-24"):
            raise SystemExit(f"comment {spec['comment_id']} was not created on 2026-09-24")
        if spec["needle"] not in text:
            raise SystemExit(f"comment {spec['comment_id']} does not match the poller wording")
        marker = (
            f"comment {spec['comment_id']}) showing under Treyce Hodges's name "
            "was written by AP Clerk"
        )
        existing = next((row for row in rows if marker in visible_comment_text(row["html"])), None)
        html, verdict = note_html(spec, snap)
        if existing is not None:
            written.append(
                {
                    "bill_id": bill_id,
                    "invoice": snap["invoice"],
                    "pdf_invoice": spec["invoice"],
                    "misattributed_comment_id": spec["comment_id"],
                    "new_comment_id": existing["id"],
                    "status": "already",
                    "verification": verdict,
                    "live_amount": snap["amount"],
                    "live_verification": snap["verification"],
                }
            )
            continue
        prior = {row["id"] for row in rows}
        _body, status, error = client.update("ap_invoices", bill_id, added_comment_payload(bill_id, html))
        if status >= 400:
            raise SystemExit(f"comment on {bill_id} failed {status} {error[:300]}")
        readback = client.get_item("ap_invoices", bill_id)
        drift = unchanged(before[bill_id], fingerprint(readback))
        if drift:
            raise SystemExit(f"bill {bill_id} drifted after the correction note: {drift}")
        added = [
            row
            for row in comments_of(readback)
            if row["id"] not in prior and marker in visible_comment_text(row["html"])
        ]
        if len(added) != 1:
            raise SystemExit(f"bill {bill_id} correction readback count {len(added)}")
        added_row = added[0]
        if added_row["creator_id"] != 175 or added_row["creator_name"] != "API Agent":
            raise SystemExit(
                f"bill {bill_id} new comment authored as {added_row['creator_id']} {added_row['creator_name']}"
            )
        if 'data-mention-id="104"' not in added_row["html"] or 'data-mention-id="33"' not in added_row["html"]:
            raise SystemExit(f"bill {bill_id} new comment is missing Shawn or Treyce")
        if not visible_comment_text(added_row["html"]).startswith("AP Clerk:"):
            raise SystemExit(f"bill {bill_id} new comment does not start with AP Clerk:")
        written.append(
            {
                "bill_id": bill_id,
                "invoice": fingerprint(readback)["invoice"],
                "pdf_invoice": spec["invoice"],
                "misattributed_comment_id": spec["comment_id"],
                "new_comment_id": added_row["id"],
                "new_comment_creator_id": added_row["creator_id"],
                "status": "added",
                "verification": verdict,
                "live_amount": snap["amount"],
                "live_verification": snap["verification"],
            }
        )

    uncertain = [
        {
            "bill_id": 10143,
            "invoice": before[10143]["invoice"],
            "comment_id": 1105,
            "reason": (
                "Authored by Treyce Hodges on 2026-09-25, text asks Shawn which parts to reject. "
                "Not the 9/24 poller wording. Left alone."
            ),
        }
    ]
    out = {
        "author": "API Agent",
        "author_id": 175,
        "probes": probes,
        "notes": written,
        "left_alone": uncertain,
        "amounts_unchanged": True,
        "posted_unchanged": True,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)) from exc
