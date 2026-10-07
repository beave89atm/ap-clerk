"""Add one Anthony/Shawn note on O'Neal bills 10505, 10508, and 10509.

Searches KIMCO user, employee, and mention sources first. Uses an Anthony
mention id only when exactly one internal Kannon user named Anthony is found.
Does not post, send mail, or change a batch. One API Agent sign-in.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import added_comment_payload
from ap_clerk.rules import SHAWN_MENTION_HTML, money
from scripts import sept25_30_finish_2026_10_06 as finish

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("anthony-tag")

OUT = ROOT / "runs" / "anthony-tag-2026-10-06.json"
SHAWN_ID = 104
PROBE_PATHS = (
    "/api/v2/users",
    "/api/v2/users?pageSize=500&search=Anthony",
    "/api/users",
    "/api/users?search=Anthony",
    "/api/v2/employees",
    "/api/v2/employees?pageSize=500&search=Anthony",
    "/api/v2/employee",
    "/api/v2/mentions?query=Anthony",
    "/api/v2/mentions?search=Anthony",
    "/api/mentions?q=Anthony",
    "/api/v2/mention?search=Anthony",
    "/api/v2/lists",
    "/api/v2/services",
    "/api/services",
    "/api/v2/system/users",
    "/api/v2/security/users",
    "/api/v2/search?q=Anthony",
)
BILLS = {
    10505: {
        "invoice": "15486598",
        "po": "59251",
        "total": 3012.19,
        "note": (
            "AP Clerk: @Anthony @Shawn McKibben O'Neal 15486598 on PO 59251 ($3,012.19) "
            "has no receipt. Please receive it. On hold in Transfer AP, not posted."
        ),
    },
    10508: {
        "invoice": "15486823",
        "po": "59289",
        "total": 10713.89,
        "note": (
            "AP Clerk: @Anthony @Shawn McKibben O'Neal 15486823 on PO 59289 ($10,713.89) "
            "has no receipt. Please receive it. On hold in Transfer AP, not posted."
        ),
    },
    10509: {
        "invoice": "15486842",
        "po": "59293",
        "total": 5328.46,
        "note": (
            "AP Clerk: @Anthony @Shawn McKibben O'Neal 15486842 ($5,328.46): no open receipt "
            "on PO 59293 matches the invoice lines within $75. Please receive or correct it. "
            "On hold in Transfer AP, not posted."
        ),
    },
}
MENTION_TAG_RE = re.compile(r"<span\b(?=[^>]*data-mention-id)[^>]*>", flags=re.I)
ATTR_RE = {
    "id": re.compile(r'data-mention-id="(\d+)"', flags=re.I),
    "name": re.compile(r'data-mention-name="([^"]*)"', flags=re.I),
    "email": re.compile(r'data-mention-email="([^"]*)"', flags=re.I),
}
GUID_RE = re.compile(r"\b[0-9a-f]{32}\b", flags=re.I)
ANTHONY_RE = re.compile(r"\banthony\b", flags=re.I)
CATALOG_RE = re.compile(r"user|employee|mention|staff|security", flags=re.I)


class BillAbort(RuntimeError):
    def __init__(self, message: str, before: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.before = before


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def unposted(value: Any) -> bool:
    return value in (None, "", False)


def spans(value: str) -> list[dict[str, Any]]:
    found = []
    for tag in MENTION_TAG_RE.findall(value or ""):
        row = {}
        for key, pattern in ATTR_RE.items():
            match = pattern.search(tag)
            row[key] = match.group(1) if match else ""
        if row.get("id"):
            row["id"] = int(row["id"])
            found.append(row)
    return found


def named_anthony(value: str) -> bool:
    return bool(ANTHONY_RE.search(value or ""))


def email_of(value: str) -> str:
    return str(value or "").strip()


def internal_kannon(email: str, record: dict[str, Any]) -> bool | None:
    domain = email_of(email).split("@")[-1].lower() if "@" in email_of(email) else ""
    if domain == "kannonmfg.com":
        return True
    if domain:
        return False
    blob = json.dumps(record, default=str).lower()
    if "kannonmfg.com" in blob:
        return True
    if re.search(r"\b(external|vendor|customer|supplier)\b", blob):
        return False
    return None


def person_name(record: dict[str, Any]) -> str:
    for key in (
        "name",
        "Name",
        "Full_Name",
        "FullName",
        "Display_Name",
        "displayName",
        "User_Name",
        "Employee_Name",
        "text",
    ):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            text = value.get("text") or value.get("name")
            if text:
                return str(text).strip()
    first = str(record.get("First_Name") or record.get("firstName") or "").strip()
    last = str(record.get("Last_Name") or record.get("lastName") or "").strip()
    return " ".join(part for part in (first, last) if part)


def person_email(record: dict[str, Any]) -> str:
    for key in ("email", "Email", "Email_Address", "User_Email", "EMail", "Mail"):
        value = record.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return ""


def person_id(record: dict[str, Any]) -> int | None:
    for key in ("id", "Id", "ID", "User_ID", "userId", "Employee_ID"):
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        try:
            if value not in (None, ""):
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def add_match(matches: list[dict[str, Any]], row: dict[str, Any]) -> None:
    key = (row.get("source"), row.get("id"), row.get("name"), row.get("email"))
    if any((item.get("source"), item.get("id"), item.get("name"), item.get("email")) == key for item in matches):
        return
    matches.append(row)


def walk(node: Any, source: str, matches: list[dict[str, Any]]) -> int:
    seen = 0
    if isinstance(node, dict):
        name = person_name(node)
        email = person_email(node)
        if named_anthony(name) or named_anthony(email):
            add_match(
                matches,
                {
                    "source": source,
                    "id": person_id(node),
                    "name": name,
                    "email": email,
                    "internal_kannon": internal_kannon(email, node),
                },
            )
        for value in node.values():
            seen += walk(value, source, matches)
        return seen + (1 if name or email or person_id(node) else 0)
    if isinstance(node, list):
        for item in node:
            seen += walk(item, source, matches)
    elif isinstance(node, str) and "data-mention-id" in node:
        for span in spans(node):
            if named_anthony(span["name"]) or named_anthony(span["email"]):
                add_match(
                    matches,
                    {
                        "source": source,
                        "id": span["id"],
                        "name": span["name"],
                        "email": span["email"],
                        "internal_kannon": internal_kannon(span["email"], span),
                    },
                )
    return seen


def parse_json(response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def catalog_targets(payload: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(node: Any, label: str) -> None:
        if isinstance(node, dict):
            texts = [str(value) for value in node.values() if isinstance(value, str)]
            name = next((text for text in texts if CATALOG_RE.search(text)), label)
            guids = GUID_RE.findall(json.dumps(node, default=str))
            if name and CATALOG_RE.search(name):
                for guid in guids:
                    found.append({"name": name[:80], "guid": guid.lower()})
            for value in node.values():
                visit(value, name)
        elif isinstance(node, list):
            for item in node:
                visit(item, label)

    visit(payload, "")
    unique = []
    seen = set()
    for row in found:
        if row["guid"] in seen:
            continue
        seen.add(row["guid"])
        unique.append(row)
    return unique[:8]


def mention_lookup(client, matches: list[dict[str, Any]]) -> dict[str, Any]:
    """UI mention search: POST /comment/users/ with {text}. Needs a web session."""
    url = client.base_url + "/comment/users/"
    attempts = []
    for label, text in (("Anthony", "Anthony"), ("Ant", "Ant")):
        response = client.request(
            "POST",
            url,
            data={"text": text},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            allow_redirects=False,
        )
        payload = parse_json(response)
        if payload is not None:
            walk(payload, f"POST /comment/users/ text={label}", matches)
        attempts.append(
            {
                "text": text,
                "http": response.status_code,
                "location": (response.headers.get("location") or "")[:160],
                "json": payload is not None,
            }
        )
        LOGGER.info("POST /comment/users/ %s HTTP %s", label, response.status_code)
    return {
        "ui_endpoint": "POST /comment/users/",
        "source": "KIMCO client getSuggestions posts ~/comment/users/ with {text}. Objects use id, name, and email.",
        "attempts": attempts,
        "web_password_attempted": False,
    }


def probe(client, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reports = []
    followed: set[str] = set()
    queue = list(PROBE_PATHS)
    while queue and len(reports) < 40:
        path = queue.pop(0)
        response = client.request("GET", client.base_url + path)
        payload = parse_json(response)
        count = walk(payload, path, matches) if payload is not None else 0
        reports.append(
            {
                "path": path,
                "http": response.status_code,
                "records_seen": count,
                "anthony_matches": len([row for row in matches if row["source"] == path]),
            }
        )
        LOGGER.info("GET %s HTTP %s", path, response.status_code)
        if response.status_code == 200 and payload is not None and path in {"/api/v2/lists", "/api/v2/services", "/api/services"}:
            for target in catalog_targets(payload):
                if target["guid"] in followed:
                    continue
                followed.add(target["guid"])
                queue.append(f"/api/v2/{target['guid']}?pageSize=500&search=Anthony")
    return reports


def scan_comments(client, matches: list[dict[str, Any]]) -> dict[str, Any]:
    mention_rows = []
    scanned = 0
    for bill_id in range(10465, 10513):
        try:
            record = client.get_item("ap_invoices", bill_id)
        except Exception:
            continue
        scanned += 1
        for comment in (record.get("lists") or {}).get("Comments_1") or []:
            if not isinstance(comment, dict):
                continue
            html_value = str((comment.get("values") or {}).get("HtmlValue") or "")
            for span in spans(html_value):
                mention_rows.append({"bill_id": bill_id, "comment_id": comment.get("id"), **span})
                if named_anthony(span["name"]) or named_anthony(span["email"]):
                    add_match(
                        matches,
                        {
                            "source": f"comments_1 bill {bill_id} comment {comment.get('id')}",
                            "id": span["id"],
                            "name": span["name"],
                            "email": span["email"],
                            "internal_kannon": internal_kannon(span["email"], span),
                        },
                    )
    names = sorted({row["name"] for row in mention_rows})
    return {"bills_scanned": scanned, "mention_objects": len(mention_rows), "mention_names": names}


def choose(matches: list[dict[str, Any]]) -> dict[str, Any]:
    internal = [
        row
        for row in matches
        if row.get("internal_kannon") is True and named_anthony(str(row.get("name") or "")) and row.get("id")
    ]
    unique = []
    seen = set()
    for row in internal:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        unique.append(row)
    if len(unique) == 1:
        person = unique[0]
        return {
            "use_anthony_mention": True,
            "anthony_id": person["id"],
            "anthony_name": person["name"],
            "anthony_email": person["email"],
            "reason": "Exactly one internal Kannon user named Anthony.",
        }
    return {
        "use_anthony_mention": False,
        "anthony_id": None,
        "reason": (
            f"{len(unique)} internal Kannon users named Anthony. "
            "Shawn mention 104 is used and Anthony stays plain text."
        ),
    }


def note_html(text: str, decision: dict[str, Any]) -> str:
    body = text
    if decision["use_anthony_mention"]:
        name = html.escape(str(decision["anthony_name"]), quote=True)
        email = html.escape(str(decision.get("anthony_email") or ""), quote=True)
        span = (
            f'<span data-mention-id="{int(decision["anthony_id"])}" data-mention-name="{name}" '
            f'data-mention-email="{email}" class="prosemirror-mention-node">@{html.escape(str(decision["anthony_name"]))}</span>'
        )
        body = body.replace("@Anthony", span, 1)
    body = body.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    if body.count('data-mention-id="104"') != 1:
        raise BillAbort("Shawn mention is missing. Not writing.")
    anthony_count = body.count(f'data-mention-id="{int(decision["anthony_id"])}"') if decision["use_anthony_mention"] else 0
    if decision["use_anthony_mention"] and anthony_count != 1:
        raise BillAbort("Anthony mention was not inserted once. Not writing.")
    if not decision["use_anthony_mention"] and "data-mention-id" in body.replace(SHAWN_MENTION_HTML, ""):
        raise BillAbort("Refusing an invented Anthony mention.")
    return f"<p>{body}</p>"


def lookup_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value or "")


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    batch = values.get("AP_Invoice_Batch") or {}
    comments = []
    for comment in lists.get("Comments_1") or []:
        if not isinstance(comment, dict):
            continue
        vals = comment.get("values") or {}
        html_value = str(vals.get("HtmlValue") or "")
        creator = vals.get("CreatorId")
        comments.append(
            {
                "id": comment.get("id"),
                "text": plain(html_value),
                "html": html_value,
                "mentions": spans(html_value),
                "created_on": vals.get("CreatedOn"),
                "creator_id": creator.get("id") if isinstance(creator, dict) else creator,
                "creator_name": (creator.get("text") or creator.get("name")) if isinstance(creator, dict) else None,
            }
        )
    return {
        "id": int(record["id"]),
        "invoice": str(values.get("Invoice_Number") or ""),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": (batch.get("text") or batch.get("name")) if isinstance(batch, dict) else None,
        "po": lookup_text(values.get("Purchase_Order") or values.get("PO_Number")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "invoice_amount": money(values.get("Invoice_Amount")),
        "comments": comments,
        "read_at": now(),
    }


def confirm_open(row: dict[str, Any], bill_id: int, spec: dict[str, Any]) -> None:
    if int(row["id"]) != bill_id or row["invoice"] != spec["invoice"]:
        raise BillAbort(f"Bill {row.get('id')} invoice {row.get('invoice')!r} is not {spec['invoice']}.", row)
    if not unposted(row.get("posted")) or row.get("void") not in (None, "", False):
        raise BillAbort("Bill is posted or void.", row)
    if row.get("verification") != spec["total"]:
        raise BillAbort(f"Verification is {row.get('verification')}, not {spec['total']}.", row)
    if spec["po"] not in str(row.get("po") or ""):
        raise BillAbort(f"PO field {row.get('po')!r} does not contain {spec['po']}.", row)


def put_note(client, bill_id: int, payload: dict[str, Any]) -> None:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if "AP_Invoice_Batch" in values or payload.get("Posted") not in (None, "", False):
        raise SystemExit("Refusing a payload that posts the bill or changes the batch.")
    response = client.request("PUT", client._record_url("ap_invoices", bill_id), json=payload)
    LOGGER.info("PUT bill %s HTTP %s", bill_id, response.status_code)
    if response.status_code >= 400:
        raise BillAbort(f"PUT bill {bill_id} HTTP {response.status_code}.")


def expected_plain(spec: dict[str, Any], decision: dict[str, Any]) -> str:
    text = spec["note"]
    if decision["use_anthony_mention"]:
        text = text.replace("@Anthony", f"@{decision['anthony_name']}", 1)
    return text


def apply_bill(client, bill_id: int, spec: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    before = snapshot(client.get_item("ap_invoices", bill_id))
    confirm_open(before, bill_id, spec)
    wanted = expected_plain(spec, decision)
    existing = next((comment for comment in before["comments"] if comment["text"] == wanted), None)
    status = "already-present"
    if existing is None:
        put_note(client, bill_id, added_comment_payload(bill_id, note_html(spec["note"], decision)))
        status = "added"
    after = snapshot(client.get_item("ap_invoices", bill_id))
    confirm_open(after, bill_id, spec)
    if int(after.get("batch_id") or 0) != int(before.get("batch_id") or 0):
        raise BillAbort("Batch changed. The note may have saved; the batch must stay put.", after)
    saved = next((comment for comment in after["comments"] if comment["text"] == wanted), None)
    if saved is None:
        raise BillAbort("The note was not found on readback.", after)
    mention_ids = [int(item["id"]) for item in saved["mentions"]]
    if SHAWN_ID not in mention_ids or int(saved.get("creator_id") or 0) != 175:
        raise BillAbort("Readback is missing Shawn or was not authored by API Agent 175.", after)
    if decision["use_anthony_mention"]:
        if mention_ids.count(int(decision["anthony_id"])) != 1:
            raise BillAbort("Readback is missing the Anthony mention.", after)
    elif any(named_anthony(item.get("name") or "") for item in saved["mentions"]):
        raise BillAbort("Readback invented an Anthony mention.", after)
    elif "Anthony" not in saved["text"]:
        raise BillAbort("Readback is missing plain-text Anthony.", after)
    if status == "added" and len(after["comments"]) != len(before["comments"]) + 1:
        raise BillAbort("Comment count did not increase by one.", after)
    public = dict(saved)
    return {"status": status, "before": before, "after": after, "saved_note": public}


def main() -> None:
    client = finish.login()
    finish.install_401_guard(client)
    matches: list[dict[str, Any]] = []
    probes = probe(client, matches)
    lookup = mention_lookup(client, matches)
    comments = scan_comments(client, matches)
    decision = choose(matches)
    LOGGER.info("Anthony decision: %s", decision["reason"])
    results: dict[str, Any] = {}
    try:
        for bill_id, spec in BILLS.items():
            try:
                results[str(bill_id)] = apply_bill(client, bill_id, spec, decision)
                LOGGER.info("Bill %s %s", bill_id, results[str(bill_id)]["status"])
            except BillAbort as exc:
                results[str(bill_id)] = {"status": "aborted", "reason": str(exc), "before": exc.before}
                LOGGER.info("Bill %s aborted: %s", bill_id, exc)
    finally:
        OUT.write_text(
            json.dumps(
                {
                    "written_at": now(),
                    "sign_ins": finish.SIGN_INS,
                    "probes": probes,
                    "mention_lookup": lookup,
                    "comment_scan": comments,
                    "anthony_matches": matches,
                    "decision": decision,
                    "bills": results,
                    "constraints": {"author": "API Agent 175", "posted": False, "email_sent": False, "batch_changed": False},
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
    failed = [key for key, row in results.items() if row.get("status") not in {"added", "already-present"}]
    if failed:
        raise SystemExit(f"Bills not noted: {', '.join(failed)}")


if __name__ == "__main__":
    main()
