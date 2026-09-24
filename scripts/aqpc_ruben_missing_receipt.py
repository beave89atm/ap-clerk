"""Kyle lock: AQPC missing_receipt HOLDs → Comments_1 plain @Ruben Perez.

Live KIMCO only. Scan open AP batches for vendor 22 (American Quality
Powder Coating). Tag HOLD missing_receipt headers that have no @Ruben Perez
note. Plain text only — Ruben mention-id is unproven (invent=false).

Must tag: invoice 11020 / header 10263 / PO 59227 / Transfer AP 375.
Do not touch 11021 / 10264 (price_variance @Shawn McKibben).
No Select Receipts, no batch moves, no email, no finished/posted bills.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import Workbook

from ap_clerk.auth import load_credentials
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.rules import is_aqpc_vendor, lookup_id, lookup_text, money

ARTIFACT_JSON = Path("/opt/cursor/artifacts/aqpc-ruben-tag-2026-09-24.json")
ARTIFACT_XLSX = Path("/opt/cursor/artifacts/aqpc-ruben-tag-2026-09-24.xlsx")
PROOF = ROOT / "runs" / "aqpc-ruben-tag-2026-09-24.json"

RUBEN_TAG = "@Ruben Perez"
DO_NOT_TOUCH_IDS = frozenset({10264})
DO_NOT_TOUCH_INVOICES = frozenset({"11021"})
MUST_TAG_ID = 10263
TRANSFER_AP_BATCH_ID = 375
AQPC_BATCH_ID = 727
ENTITY_ID = 203
FORM_ID = 218

COLUMNS = (
    "Invoice #",
    "KIMCO id",
    "Tagged",
    "Batch",
    "Batch id",
    "PO",
    "Amount",
    "Verification",
    "Vendor",
    "Receipt lines",
    "Before comment",
    "After comment",
    "Skip reason",
    "Notes",
    "Verified @Ruben Perez",
)


def po_number(text: Any) -> str:
    raw = str(text or "")
    match = re.search(r"PO\s*(\d{4,6})", raw, flags=re.I)
    if match:
        return match.group(1)
    digits = re.sub(r"\D", "", raw)
    return digits or "n/a"


def amount_label(amount: Any, verification: Any) -> str:
    amt = money(amount)
    ver = money(verification)
    if amt not in (None, 0, 0.0):
        return f"${float(amt):.2f}"
    if ver not in (None, 0, 0.0):
        return f"${float(ver):.2f}"
    return "n/a"


def ruben_comment_html(*, invoice: str, po: str, amount: Any, verification: Any) -> str:
    """Plain @Ruben Perez. Never a data-mention-id node."""
    po_n = po_number(po)
    dollars = amount_label(amount, verification)
    html = (
        f"<p>{RUBEN_TAG} HOLD missing_receipt on AQPC invoice {invoice} "
        f"PO {po_n} {dollars}. Dock receive is needed so AP can Select Receipts. "
        "Mention-id for Ruben Perez is not proven (invent=false). No email.</p>"
    )
    if "data-mention-id" in html:
        raise RuntimeError("refusing invented Ruben mention node")
    return html


def _html_of(comment: dict[str, Any]) -> str:
    return str((comment.get("values") or {}).get("HtmlValue") or comment.get("html") or "")


def comment_snippet(comments: list[dict[str, Any]] | None, limit: int = 240) -> str:
    parts = []
    for row in comments or []:
        html = _html_of(row).strip()
        if html:
            parts.append(html)
    text = " | ".join(parts)
    return text[:limit]


def has_ruben_tag(comments: list[dict[str, Any]] | None) -> bool:
    return any(RUBEN_TAG in _html_of(row) for row in (comments or []))


def _is_shawn_price_note(html: str) -> bool:
    low = html.lower()
    if "shawn" not in low and "data-mention-id=\"104\"" not in low:
        return False
    needles = (
        "price_variance",
        "price-does-not-match",
        "price variance",
        "charging",
        "ppv",
    )
    return any(needle in low for needle in needles)


def blank_comment_id(comments: list[dict[str, Any]] | None) -> int | None:
    """First Comments_1 row with no text. Used to refresh a blank note."""
    for row in comments or []:
        if _html_of(row).strip():
            continue
        cid = row.get("id")
        if cid not in (None, ""):
            return int(cid)
    return None


def classify_header(snap: dict[str, Any]) -> dict[str, Any]:
    """Decide tag vs skip from a live GET snapshot. invent=false."""
    kid = int(snap.get("kimco_id") or 0)
    invoice = str(snap.get("invoice") or "")
    vendor_id = snap.get("vendor_id")
    vendor = str(snap.get("vendor") or "")
    comments = list(snap.get("comments") or [])
    receipt_n = int(snap.get("receipt_n") or 0)
    po = str(snap.get("po") or "")
    reason = ""
    action = "skip"

    if kid in DO_NOT_TOUCH_IDS or invoice in DO_NOT_TOUCH_INVOICES:
        reason = "do-not-touch price_variance @Shawn McKibben"
    elif not is_aqpc_vendor(vendor, vendor_id):
        reason = "not-aqpc"
    elif snap.get("void") is True:
        reason = "void"
    elif snap.get("posted") is True or snap.get("posted_date"):
        reason = "posted"
    elif has_ruben_tag(comments):
        reason = "already-tagged-ruben"
    elif any(_is_shawn_price_note(_html_of(row)) for row in comments):
        reason = "existing-shawn-price-note"
    elif receipt_n > 0:
        reason = "receipts-already-selected"
    elif not po_number(po) or po_number(po) == "n/a":
        reason = "no-po-not-dock-hold"
    elif amount_label(snap.get("amount"), snap.get("verification")) == "n/a":
        reason = "zero-amount-not-dock-hold"
    else:
        action = "tag"
        reason = "missing_receipt-no-open-receipt-no-ruben-tag"

    return {
        "action": action,
        "reason": reason,
        "invent": False,
        "mention_id": None,
    }


def snapshot(client: KimcoClient, kid: int) -> dict[str, Any]:
    rec = client.get_item("ap_invoices", int(kid))
    vals = rec.get("values") or {}
    lists = rec.get("lists") or {}
    lines = lists.get("APInvoiceLine") or []
    comments_raw = lists.get("Comments_1") or []
    comments: list[dict[str, Any]] = []
    if isinstance(comments_raw, list):
        for row in comments_raw:
            if not isinstance(row, dict):
                continue
            comments.append(
                {
                    "id": row.get("id"),
                    "html": str((row.get("values") or {}).get("HtmlValue") or ""),
                }
            )
    receipt_n = 0
    if isinstance(lines, list):
        for line in lines:
            lv = line.get("values") if isinstance(line, dict) else {}
            if (lv or {}).get("Receipt") not in (None, "", {}):
                receipt_n += 1
    return {
        "kimco_id": int(kid),
        "invoice": str(vals.get("Invoice_Number") or ""),
        "vendor_id": lookup_id(vals.get("Vendor")),
        "vendor": lookup_text(vals.get("Vendor")),
        "po": lookup_text(vals.get("Purchase_Order")),
        "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
        "batch": lookup_text(vals.get("AP_Invoice_Batch")),
        "amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Invoice_Verification_Amount")),
        "posted": vals.get("Posted"),
        "posted_date": vals.get("Posted_Date"),
        "void": vals.get("Void"),
        "status": vals.get("Status"),
        "invoice_type": vals.get("Invoice_Type"),
        "unposted": vals.get("Unposted_Count"),
        "receipt_n": receipt_n,
        "comments": comments,
    }


def open_batch_aqpc_ids(client: KimcoClient) -> tuple[list[dict[str, Any]], list[int]]:
    """Vendor 22 headers sitting on Status=0 batches. Deduped by header id."""
    batches = client.list_items("ap_batches", page_size=1000)
    open_rows: list[dict[str, Any]] = []
    ids: list[int] = []
    seen: set[int] = set()
    for batch in batches:
        vals = batch.get("values") or {}
        if vals.get("Status") != 0:
            continue
        bid = int(batch["id"])
        name = str(vals.get("AP_Invoice_Batch_ID") or "")
        rec = client.get_item("ap_batches", bid)
        displays = (rec.get("lists") or {}).get("APInvoiceDisplay") or []
        aqpc_n = 0
        if isinstance(displays, list):
            for row in displays:
                vvals = row.get("values") or {}
                vendor = vvals.get("Vendor") or {}
                vid = vendor.get("id") if isinstance(vendor, dict) else None
                vtext = vendor.get("text") if isinstance(vendor, dict) else str(vendor or "")
                if not is_aqpc_vendor(vtext, vid):
                    continue
                aqpc_n += 1
                kid = int(row.get("id"))
                if kid not in seen:
                    seen.add(kid)
                    ids.append(kid)
        open_rows.append(
            {
                "batch_id": bid,
                "batch": name,
                "invoice_count": len(displays) if isinstance(displays, list) else 0,
                "aqpc_count": aqpc_n,
            }
        )
    if MUST_TAG_ID not in seen:
        ids.append(MUST_TAG_ID)
    return open_rows, ids


def _comment_values(kimco_id: int, html: str) -> dict[str, Any]:
    return {
        "HtmlValue": html,
        "Entity": {"id": ENTITY_ID},
        "ObjectId": int(kimco_id),
        "FormId": FORM_ID,
    }


def put_comments_1(
    client: KimcoClient,
    kimco_id: int,
    html: str,
    *,
    replace_id: int | None = None,
) -> dict[str, Any]:
    """PUT lists.Comments_1. Added, or Modified when refreshing a blank row."""
    if "data-mention-id" in html:
        return {"status": "refused-invented-mention", "invent": False}
    child: dict[str, Any] = {"state": "Added", "values": _comment_values(kimco_id, html)}
    if replace_id is not None:
        child = {
            "state": "Modified",
            "id": int(replace_id),
            "values": {"HtmlValue": html},
        }
    payload = {
        "state": "Modified",
        "id": int(kimco_id),
        "lists": {"Comments_1": [child]},
    }
    try:
        _body, status, error = client.update("ap_invoices", int(kimco_id), payload)
    except KimcoError as exc:
        return {"status": "blocked", "error": str(exc)[:200], "invent": False}
    return {
        "status": "persisted" if status < 400 else f"put-{status}",
        "put": status,
        "error": error,
        "replace_id": replace_id,
        "invent": False,
    }


def apply_tag(client: KimcoClient, before: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    decision = classify_header(before)
    html = ruben_comment_html(
        invoice=str(before.get("invoice") or ""),
        po=str(before.get("po") or ""),
        amount=before.get("amount"),
        verification=before.get("verification"),
    )
    row: dict[str, Any] = {
        "invoice": before.get("invoice"),
        "kimco_id": before.get("kimco_id"),
        "po": po_number(before.get("po")),
        "po_text": before.get("po"),
        "batch": before.get("batch"),
        "batch_id": before.get("batch_id"),
        "vendor": before.get("vendor"),
        "vendor_id": before.get("vendor_id"),
        "amount": before.get("amount"),
        "verification": before.get("verification"),
        "receipt_n": before.get("receipt_n"),
        "posted": before.get("posted"),
        "void": before.get("void"),
        "before_comment": comment_snippet(before.get("comments")),
        "action": decision["action"],
        "skip_reason": "" if decision["action"] == "tag" else decision["reason"],
        "classify_reason": decision["reason"],
        "tagged": False,
        "mention_id": None,
        "invent": False,
        "mail_send": False,
        "select_receipts": False,
        "batch_moved": False,
        "html": html if decision["action"] == "tag" else "",
    }
    if decision["action"] != "tag":
        row["after_comment"] = row["before_comment"]
        row["verified_ruben"] = RUBEN_TAG in str(row["after_comment"])
        row["notes"] = decision["reason"]
        return row
    if dry_run:
        row["notes"] = "dry-run"
        row["after_comment"] = row["before_comment"]
        row["verified_ruben"] = False
        return row
    replace_id = blank_comment_id(before.get("comments"))
    put = put_comments_1(
        client,
        int(before["kimco_id"]),
        html,
        replace_id=replace_id,
    )
    after = snapshot(client, int(before["kimco_id"]))
    verified = has_ruben_tag(after.get("comments"))
    if not verified and replace_id is not None:
        put_add = put_comments_1(client, int(before["kimco_id"]), html, replace_id=None)
        after = snapshot(client, int(before["kimco_id"]))
        verified = has_ruben_tag(after.get("comments"))
        put = {"replace": put, "add": put_add}
    row["put"] = put
    row["after_comment"] = comment_snippet(after.get("comments"))
    row["after_batch_id"] = after.get("batch_id")
    row["after_receipt_n"] = after.get("receipt_n")
    row["verified_ruben"] = verified
    row["tagged"] = bool(verified)
    row["batch_unchanged"] = after.get("batch_id") == before.get("batch_id")
    row["receipts_unchanged"] = after.get("receipt_n") == before.get("receipt_n")
    if verified:
        row["notes"] = "Comments_1 plain @Ruben Perez. mention-id unknown. No email."
        row["skip_reason"] = ""
    else:
        row["notes"] = "PUT did not read back @Ruben Perez"
        row["skip_reason"] = "verify-failed"
    return row


def sheet_row(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "Invoice #": result.get("invoice"),
        "KIMCO id": result.get("kimco_id"),
        "Tagged": "yes" if result.get("tagged") else "no",
        "Batch": result.get("batch"),
        "Batch id": result.get("batch_id"),
        "PO": result.get("po"),
        "Amount": result.get("amount"),
        "Verification": result.get("verification"),
        "Vendor": result.get("vendor"),
        "Receipt lines": result.get("receipt_n"),
        "Before comment": result.get("before_comment"),
        "After comment": result.get("after_comment"),
        "Skip reason": result.get("skip_reason") or "",
        "Notes": result.get("notes") or "",
        "Verified @Ruben Perez": "yes" if result.get("verified_ruben") else "no",
    }


def write_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "AQPC Ruben tags"
    sheet.append(list(COLUMNS))
    for row in rows:
        sheet.append([row.get(col) for col in COLUMNS])
    book.save(path)


def write_outputs(payload: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    text = json.dumps(payload, indent=2, default=str) + "\n"
    ARTIFACT_JSON.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_JSON.write_text(text)
    PROOF.parent.mkdir(parents=True, exist_ok=True)
    PROOF.write_text(text)
    write_xlsx(ARTIFACT_XLSX, rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.live:
        raise SystemExit("Refusing: pass --live. invent=false. Live KIMCO only.")
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials not ready")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    open_batches, ids = open_batch_aqpc_ids(client)
    results: list[dict[str, Any]] = []
    for kid in ids:
        before = snapshot(client, kid)
        results.append(apply_tag(client, before, dry_run=args.dry_run))
    rows = [sheet_row(r) for r in results]
    payload = {
        "when": datetime.now(timezone.utc).isoformat(),
        "invent": False,
        "mention_id": None,
        "mention_note": (
            "Ruben Perez mention-id is None in ap_clerk/receiving_owners.py. "
            "No verified data-id in repo notes. Plain text @Ruben Perez only."
        ),
        "mail_send": False,
        "select_receipts": False,
        "batch_moved": False,
        "do_not_touch": sorted(DO_NOT_TOUCH_IDS),
        "open_batches": open_batches,
        "headers_checked": len(results),
        "tagged_ids": [r["kimco_id"] for r in results if r.get("tagged")],
        "rows": rows,
        "detail": results,
    }
    write_outputs(payload, rows)
    tagged = payload["tagged_ids"]
    print(
        json.dumps(
            {
                "checked": len(results),
                "tagged_ids": tagged,
                "must_tag_10263": 10263 in tagged,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
