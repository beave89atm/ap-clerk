"""Tag Shawn on unposted AFT bills that have no KIMCO PO. Do not post.

Kyle: AFT invoices tag Shawn. One live login as API Agent user 175.
Bills 10398 (invoice 52004, $190.30) and 10399 (invoice 52005, $363.00)
stay in TRANSFER AP (375). If Shawn (mention id 104) is already on the
bill, do not add another note. A new note starts with "AP Clerk:" and
mentions Shawn id 104 and Kyle Cleaver id 26. Amounts, lines, and the
batch are not edited.
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
from ap_clerk.kimco import (
    KimcoClient,
    KimcoError,
    added_comment_payload,
    comment_author_from_access_token,
    visible_comment_text,
)
from ap_clerk.rules import (
    KYLE_CLEAVER_MENTION_HTML,
    KYLE_CLEAVER_MENTION_ID,
    SHAWN_MENTION_HTML,
    lookup_id,
    lookup_text,
    money,
)

HOST = "https://live.kimcoerp.com"
AFT_VENDOR_ID = 331
BATCH_ID = 375
SHAWN_ID = 104
API_AGENT_ID = 175
OUT_JSON = ROOT / "artifacts" / "aft-no-po-shawn-notes-2026-09-30.json"
BILLS = (
    {"id": 10398, "invoice": "52004", "amount": 190.30},
    {"id": 10399, "invoice": "52005", "amount": 363.00},
)


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "auth failed: live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("auth failed: refusing non-live host")
    try:
        client = KimcoClient.authenticate(
            creds.instance_url, creds.key or "", creds.password or "", target="live"
        )
    except KimcoError as exc:
        raise SystemExit(f"auth failed: {exc}") from exc
    author = comment_author_from_access_token(client.access_token)
    if author.get("id") != API_AGENT_ID or author.get("name") != "API Agent":
        raise SystemExit(
            f"auth failed: login is {author.get('name')} ({author.get('id')}), not API Agent 175"
        )
    return client


def note_html(invoice: str) -> str:
    html = (
        f"<p>AP Clerk: {SHAWN_MENTION_HTML} {KYLE_CLEAVER_MENTION_HTML} "
        f"Invoice {invoice} has no KIMCO PO and needs Shawn.</p>"
    )
    visible = visible_comment_text(html)
    if not visible.startswith("AP Clerk:"):
        raise SystemExit("note does not start with AP Clerk:")
    if html.count(f'data-mention-id="{SHAWN_ID}"') != 1:
        raise SystemExit("note is missing Shawn mention id 104")
    if html.count(f'data-mention-id="{KYLE_CLEAVER_MENTION_ID}"') != 1:
        raise SystemExit("note is missing Kyle Cleaver mention id 26")
    if "no KIMCO PO" not in visible or "needs Shawn" not in visible:
        raise SystemExit("note does not say the invoice has no KIMCO PO and needs Shawn")
    return html


def shawn_mentioned(html: str) -> bool:
    text = html or ""
    if f'data-mention-id="{SHAWN_ID}"' in text or f"data-mention-id='{SHAWN_ID}'" in text:
        return True
    visible = visible_comment_text(text).casefold()
    return "shawn" in visible


def comments_of(record: dict[str, Any]) -> list[dict[str, Any]]:
    lists = record.get("lists")
    if not isinstance(lists, dict) or "Comments_1" not in lists:
        raise KimcoError("record GET did not include Comments_1")
    rows = lists.get("Comments_1") or []
    if not isinstance(rows, list):
        raise KimcoError("Comments_1 was not a list")
    parsed: list[dict[str, Any]] = []
    for comment in rows:
        if not isinstance(comment, dict):
            continue
        values = comment.get("values") if isinstance(comment.get("values"), dict) else {}
        creator = values.get("CreatorId")
        try:
            comment_id = int(comment.get("id"))
        except (TypeError, ValueError):
            comment_id = comment.get("id")
        parsed.append(
            {
                "id": comment_id,
                "html": str(values.get("HtmlValue") or ""),
                "creator_id": lookup_id(creator),
                "creator_name": lookup_text(creator),
                "shawn_mentioned": shawn_mentioned(str(values.get("HtmlValue") or "")),
            }
        )
    return parsed


def line_snapshot(record: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        values = line.get("values") if isinstance(line.get("values"), dict) else {}
        lines.append(
            {
                "id": line.get("id"),
                "receipt": lookup_id(values.get("Receipt")),
                "po": lookup_id(values.get("Purchase_Order_Number"))
                or lookup_text(values.get("Purchase_Order_Number")),
                "qty": money(values.get("Quantity")),
                "price": money(values.get("Unit_Price")),
                "ext": money(values.get("Extended_Amount")),
            }
        )
    return lines


def po_present(record: dict[str, Any], lines: list[dict[str, Any]]) -> bool:
    values = record.get("values") if isinstance(record.get("values"), dict) else {}
    header_po = values.get("Purchase_Order") or values.get("Purchase_Order_Number") or values.get("PO")
    if lookup_id(header_po) not in (None, "") or lookup_text(header_po):
        return True
    return any(line.get("po") not in (None, "") for line in lines)


def invoice_matches(live: Any, expected: str) -> bool:
    text = str(live or "").strip()
    if text == expected:
        return True
    return re.sub(r"\D", "", text) == expected and expected in text


def fingerprint(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") if isinstance(record.get("values"), dict) else {}
    vendor = values.get("Vendor")
    lines = line_snapshot(record)
    return {
        "invoice": values.get("Invoice_Number"),
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "vendor_id": lookup_id(vendor),
        "vendor": lookup_text(vendor),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "posted": values.get("Posted"),
        "status": values.get("Status"),
        "po_present": po_present(record, lines),
        "lines": lines,
    }


def identity_problems(spec: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    """The named dollars are Invoice_Verification_Amount.

    Unposted AFT headers with no lines keep Invoice_Amount at 0. That field
    is not the PDF total and must not be edited to match it.
    """
    problems = []
    if not invoice_matches(snap.get("invoice"), spec["invoice"]):
        problems.append(f"invoice {snap.get('invoice')!r} != {spec['invoice']}")
    if snap.get("verification") != spec["amount"]:
        problems.append(f"verification {snap.get('verification')} != {spec['amount']}")
    vendor_name = str(snap.get("vendor") or "").casefold()
    if snap.get("vendor_id") != AFT_VENDOR_ID and "aft" not in vendor_name:
        problems.append(f"vendor {snap.get('vendor')!r} ({snap.get('vendor_id')}) is not AFT")
    if snap.get("batch_id") != BATCH_ID or "transfer ap" not in str(snap.get("batch") or "").casefold():
        problems.append(f"batch {snap.get('batch')!r} ({snap.get('batch_id')}) is not TRANSFER AP (375)")
    if snap.get("posted") not in (None, "", False):
        problems.append(f"bill is posted ({snap.get('posted')!r})")
    status = str(snap.get("status") or "").casefold()
    if "posted" in status:
        problems.append(f"status is {snap.get('status')!r}")
    if snap.get("po_present"):
        problems.append("bill already has a KIMCO PO")
    return problems


def plan_action(spec: dict[str, Any], snap: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Add one note only when the bill matches and Shawn is not already mentioned."""
    problems = identity_problems(spec, snap)
    if problems:
        return "refused", problems
    if any(row.get("shawn_mentioned") for row in rows):
        return "skipped", []
    return "add", []


def unchanged(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    problems = []
    for key in ("invoice", "amount", "verification", "vendor_id", "batch_id", "posted", "lines"):
        if before.get(key) != after.get(key):
            problems.append(f"{key} changed")
    return problems


def public_comments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "creator_id": row["creator_id"],
            "creator_name": row["creator_name"],
            "shawn_mentioned": row["shawn_mentioned"],
        }
        for row in rows
    ]


def main() -> None:
    client = load_live()
    records = {}
    for spec in BILLS:
        try:
            records[spec["id"]] = client.get_item("ap_invoices", spec["id"])
        except KimcoError as exc:
            raise SystemExit(f"GET bill {spec['id']} failed: {exc}") from exc

    plans: list[dict[str, Any]] = []
    for spec in BILLS:
        record = records[spec["id"]]
        snap = fingerprint(record)
        rows = comments_of(record)
        action, problems = plan_action(spec, snap, rows)
        plans.append(
            {
                "bill_id": spec["id"],
                "invoice": spec["invoice"],
                "action": action,
                "problems": problems,
                "before": snap,
                "comments_before": rows,
            }
        )

    results: list[dict[str, Any]] = []
    for plan in plans:
        if plan["action"] != "add":
            results.append(plan)
            continue
        html = note_html(plan["invoice"])
        payload = added_comment_payload(plan["bill_id"], html)
        _body, status, error = client.update("ap_invoices", plan["bill_id"], payload)
        plan["write_status"] = status
        plan["write_error"] = error
        if status >= 400 or error:
            plan["action"] = "write_failed"
            results.append(plan)
            if status in {401, 403}:
                for remaining in plans[plans.index(plan) + 1 :]:
                    if remaining not in results:
                        remaining["action"] = "not_attempted"
                        remaining["problems"] = ["stopped after auth failure on an earlier bill"]
                        results.append(remaining)
                break
            continue
        results.append(plan)

    for plan in results:
        if plan["action"] in {"write_failed", "not_attempted"} and plan.get("write_status") in {401, 403}:
            plan["comments_after"] = plan["comments_before"]
            plan["added_comment_ids"] = []
            plan["added_creator_ids"] = []
            plan["unchanged_problems"] = []
            continue
        try:
            after_record = client.get_item("ap_invoices", plan["bill_id"])
        except KimcoError as exc:
            plan["action"] = "verify_failed"
            plan["problems"] = [f"GET after write failed: {exc}"]
            plan["comments_after"] = plan["comments_before"]
            plan["added_comment_ids"] = []
            plan["added_creator_ids"] = []
            plan["unchanged_problems"] = []
            continue
        after = fingerprint(after_record)
        rows = comments_of(after_record)
        plan["after"] = after
        plan["comments_after"] = rows
        plan["unchanged_problems"] = unchanged(plan["before"], after)
        before_ids = {row["id"] for row in plan["comments_before"]}
        added = [row for row in rows if row["id"] not in before_ids]
        plan["added_comment_ids"] = [row["id"] for row in added]
        plan["added_creator_ids"] = [row["creator_id"] for row in added]
        if plan["action"] == "add":
            if len(added) != 1:
                plan["action"] = "verify_failed"
                plan["problems"] = [f"expected 1 new comment, found {len(added)}"]
            elif added[0]["creator_id"] != API_AGENT_ID:
                plan["action"] = "verify_failed"
                plan["problems"] = [f"new comment author is {added[0]['creator_id']}, not 175"]
            elif not added[0]["shawn_mentioned"]:
                plan["action"] = "verify_failed"
                plan["problems"] = ["new comment does not mention Shawn"]
            elif f'data-mention-id="{KYLE_CLEAVER_MENTION_ID}"' not in added[0]["html"]:
                plan["action"] = "verify_failed"
                plan["problems"] = ["new comment does not mention Kyle Cleaver"]
            elif not visible_comment_text(added[0]["html"]).startswith("AP Clerk:"):
                plan["action"] = "verify_failed"
                plan["problems"] = ["new comment does not start with AP Clerk:"]
        if plan["unchanged_problems"]:
            plan["action"] = "verify_failed"

    report = []
    for plan in results:
        report.append(
            {
                "bill_id": plan["bill_id"],
                "invoice": plan["invoice"],
                "action": plan["action"],
                "problems": plan.get("problems") or [],
                "unchanged_problems": plan.get("unchanged_problems") or [],
                "amount": plan["before"].get("amount"),
                "verification": plan["before"].get("verification"),
                "batch_id": plan["before"].get("batch_id"),
                "batch": plan["before"].get("batch"),
                "posted": plan["before"].get("posted"),
                "vendor_id": plan["before"].get("vendor_id"),
                "vendor": plan["before"].get("vendor"),
                "comment_ids": [row["id"] for row in plan.get("comments_after") or plan["comments_before"]],
                "added_comment_ids": plan.get("added_comment_ids") or [],
                "added_creator_ids": plan.get("added_creator_ids") or [],
                "comments": public_comments(plan.get("comments_after") or plan["comments_before"]),
                "write_status": plan.get("write_status"),
            }
        )
    OUT_JSON.write_text(json.dumps({"author_id": API_AGENT_ID, "bills": report}, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if any(row["action"] in {"refused", "write_failed", "verify_failed"} for row in report):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
