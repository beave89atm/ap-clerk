"""Scan the AP mailbox and enter the next 10 unentered vendor invoices.

Window: mail received on or after 2026-09-04, every folder.
Oldest first. Never posts a bill. Batch: API Agent - 9/25/26 Batch 10.
Quantity_Received only. Packing-slip gate is suspended.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.pdf_invoice import (
    classify_pdf_page,
    extract_pdf_text,
    parse_invoice_pdf,
    parse_invoice_text,
)
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text, names_match

BATCH_NAME = "API Agent - 9/25/26 Batch 10"
OUT_JSON = ROOT / "artifacts" / "batch10-2026-09-25.json"
CACHE = Path("/tmp/batch10")
CHICAGO = ZoneInfo("America/Chicago")
WINDOW_START = datetime(2026, 9, 4, tzinfo=CHICAGO)
WINDOW_END = datetime(2026, 9, 26, tzinfo=CHICAGO)

_REMINDER = re.compile(
    r"friendly payment reminder|payment reminder|past due invoice|past-due invoice|"
    r"your account with us now appears as past due",
    re.I,
)
_STATEMENT_SUBJECT = re.compile(
    r"statement of account|account statement|^\s*statement\b|statement from\b",
    re.I,
)
_CREDIT = re.compile(r"\bcredit\s+memo\b|\bcredit\s+number\b|\bcredit\s+no\b|\bcredit note\b", re.I)
_NOISE_SUBJECT = re.compile(
    r"\b(out of office|automatic reply|undeliverable|delivery status notification|"
    r"read:\s|accepted:\s|declined:\s)\b",
    re.I,
)


def _utc(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chicago_day(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(CHICAGO).date().isoformat()


def scan_mailbox(graph: Any) -> dict[str, Any]:
    folders: dict[str, dict[str, Any]] = {}

    def walk(url: str, parent_name: str | None) -> None:
        first = True
        while url:
            kwargs: dict[str, Any] = {}
            if first:
                kwargs["params"] = {
                    "$select": "id,displayName,parentFolderId,childFolderCount",
                    "$top": 100,
                }
                first = False
            response = graph.request("GET", url, **kwargs)
            if response.status_code != 200:
                raise RuntimeError(f"folder list HTTP {response.status_code}")
            payload = response.json() or {}
            for item in payload.get("value") or []:
                fid = str(item.get("id") or "")
                name = str(item.get("displayName") or "")
                folders[fid] = {"name": name, "parent": parent_name}
                if int(item.get("childFolderCount") or 0) > 0 and fid:
                    walk(graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{fid}/childFolders"), name)
            url = payload.get("@odata.nextLink")

    walk(graph._user_url(ALLOWED_MAILBOX, "mailFolders"), None)
    filt = f"receivedDateTime ge {_utc(WINDOW_START)} and receivedDateTime lt {_utc(WINDOW_END)}"
    params = {
        "$select": "id,subject,from,receivedDateTime,hasAttachments,parentFolderId,categories,bodyPreview",
        "$filter": filt,
        "$orderby": "receivedDateTime asc",
        "$top": 100,
    }
    messages: list[dict[str, Any]] = []
    url: str | None = graph._messages_url(ALLOWED_MAILBOX)
    first = True
    while url:
        response = graph.request("GET", url, **({"params": params} if first else {}))
        first = False
        if response.status_code != 200:
            raise RuntimeError(f"message list HTTP {response.status_code}")
        payload = response.json() or {}
        messages.extend(payload.get("value") or [])
        url = payload.get("@odata.nextLink")
        print(f"listed messages {len(messages)}", flush=True)
    by_folder: dict[str, int] = {}
    rows = []
    for msg in messages:
        folder = folders.get(str(msg.get("parentFolderId") or ""), {"name": "unknown", "parent": None})
        key = f"{folder.get('parent') or ''}/{folder.get('name')}"
        by_folder[key] = by_folder.get(key, 0) + 1
        frm = ((msg.get("from") or {}).get("emailAddress") or {})
        rows.append(
            {
                "id": msg.get("id"),
                "subject": str(msg.get("subject") or ""),
                "from": str(frm.get("address") or ""),
                "from_name": str(frm.get("name") or ""),
                "received": msg.get("receivedDateTime"),
                "received_chicago": _chicago_day(str(msg.get("receivedDateTime") or "")),
                "folder": folder.get("name"),
                "parent": folder.get("parent"),
                "has_attachments": bool(msg.get("hasAttachments")),
                "categories": list(msg.get("categories") or []),
                "preview": str(msg.get("bodyPreview") or "")[:400],
            }
        )
    return {
        "folder_count": len(folders),
        "messages_in_window": len(messages),
        "by_folder": by_folder,
        "messages": rows,
    }


def _page_invoice_number(text: str) -> str:
    parsed = parse_invoice_text(text or "", filename="page.pdf")
    return str(parsed.get("invoice_number") or "").strip()


def _is_credit_page(text: str) -> bool:
    if not _CREDIT.search(text or ""):
        return False
    # A credit page that is also a payable invoice face is still a credit memo.
    return True


def _slim_bill(parsed: dict[str, Any], *, page_indexes: list[int]) -> dict[str, Any] | None:
    number = str(parsed.get("invoice_number") or "").strip()
    if not number:
        return None
    if parsed.get("is_statement_doc") and not parsed.get("note54_invoice_pages_only"):
        return None
    if parsed.get("is_purchase_order_doc") or parsed.get("is_receipt_scan_doc"):
        return None
    kind = str(parsed.get("attachment_class") or "")
    if kind in {"packing_slip", "receipt_scan", "pod", "statement", "past_due", "po", "check_stop"}:
        return None
    amount = parsed.get("amount")
    lines = []
    for line in parsed.get("lines") or []:
        if not isinstance(line, dict):
            continue
        lines.append(
            {
                "part": line.get("part") or line.get("item") or line.get("description"),
                "description": line.get("description") or line.get("label"),
                "qty": line.get("qty") if line.get("qty") is not None else line.get("quantity"),
                "unit_price": line.get("unit_price") or line.get("price") or line.get("rate"),
                "amount": line.get("amount") if line.get("amount") is not None else line.get("line_amount"),
                "po": line.get("po"),
                "po_line": line.get("po_line") or line.get("line"),
                "wo": line.get("wo") or line.get("work_order"),
                "fee": bool(line.get("fee")),
            }
        )
    fees = []
    for fee in parsed.get("fees") or []:
        if isinstance(fee, dict):
            fees.append({"name": fee.get("name"), "amount": fee.get("amount"), "fee": True})
    return {
        "vendor": parsed.get("vendor") or "",
        "invoice_number": number,
        "date": parsed.get("date"),
        "po": parsed.get("po"),
        "pos": list(parsed.get("pos") or []),
        "total": amount,
        "fees": fees,
        "lines": lines,
        "page_indexes": page_indexes,
        "hold_reason": parsed.get("hold_reason") or "",
        "parse_verified": bool(parsed.get("parse_verified")),
        "multi_po": bool(parsed.get("multi_po")),
        "gas_misc": bool(parsed.get("gas_misc")),
        "gas_split": bool(parsed.get("gas_split")),
        "note54_uncertain": bool(parsed.get("note54_uncertain")),
        "check_stop": bool(parsed.get("check_stop")),
    }


def bills_from_pdf(path: Path, *, subject: str, from_name: str, from_address: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (invoice bills, skip records) after a page-by-page read."""
    text = extract_pdf_text(path)
    pages = re.split(r"\f", text or "")
    if not pages:
        pages = [""]
    skips: list[dict[str, Any]] = []
    classes = [classify_pdf_page(page) for page in pages]
    credit_pages = [index for index, page in enumerate(pages) if _is_credit_page(page)]
    for index in credit_pages:
        number = _page_invoice_number(pages[index])
        skips.append(
            {
                "kind": "credit_memo",
                "invoice_number": number,
                "page": index + 1,
                "reason": "Credit memo. Treyce handles credit memos.",
            }
        )

    invoice_pages = [
        index
        for index, klass in enumerate(classes)
        if klass == "invoice" and index not in credit_pages
    ]
    # One parser pass on the full file covers Gas / O'Neal / Fastenal packs.
    full = parse_invoice_pdf(path, subject=subject, from_name=from_name, from_address=from_address)
    pack = [full, *(full.get("siblings") or [])]
    bills: list[dict[str, Any]] = []
    seen: set[str] = set()
    if len(pack) > 1 or (full.get("invoice_number") and not full.get("is_statement_doc")):
        for parsed in pack:
            bill = _slim_bill(parsed, page_indexes=list(parsed.get("note54_invoice_pages") or []))
            if not bill:
                continue
            key = invoice_number_key(bill["invoice_number"])
            if key in seen:
                continue
            # Drop a bill whose only pages are credit-memo pages.
            if bill["page_indexes"] and all((i - 1 if i else i) in credit_pages for i in bill["page_indexes"]):
                continue
            seen.add(key)
            bills.append(bill)
    # Page-by-page when the full parse found nothing, or pages name extra invoices.
    page_numbers: dict[str, list[int]] = {}
    page_parsed: dict[str, dict[str, Any]] = {}
    for index in invoice_pages:
        parsed = parse_invoice_text(
            pages[index],
            subject=subject,
            from_name=from_name,
            from_address=from_address,
            filename=path.name,
        )
        number = str(parsed.get("invoice_number") or "").strip()
        if not number or _is_credit_page(pages[index]):
            continue
        key = invoice_number_key(number)
        page_numbers.setdefault(key, []).append(index)
        page_parsed.setdefault(key, parsed)
    for key, indexes in page_numbers.items():
        if key in seen:
            existing = next(row for row in bills if invoice_number_key(row["invoice_number"]) == key)
            for index in indexes:
                if index not in existing["page_indexes"]:
                    existing["page_indexes"].append(index)
            continue
        parsed = page_parsed[key]
        bill = _slim_bill(parsed, page_indexes=indexes)
        if bill:
            seen.add(key)
            bills.append(bill)
    if not bills and not skips:
        if full.get("is_statement_doc") or all(klass == "statement" for klass in classes if klass != "other"):
            skips.append({"kind": "statement", "reason": "Account statement is not an invoice."})
        elif any(klass == "uncertain" for klass in classes):
            skips.append(
                {
                    "kind": "pdf_capture",
                    "reason": "Invoice and statement share a page, so the pack was not entered.",
                }
            )
    return bills, skips


def collect(graph: Any, scan: dict[str, Any]) -> dict[str, Any]:
    pdf_dir = CACHE / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    invoices: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    messages = scan["messages"]
    for index, msg in enumerate(messages, start=1):
        subject = msg["subject"]
        blob = f"{subject}\n{msg.get('preview') or ''}"
        if _NOISE_SUBJECT.search(subject):
            skipped.append(
                {
                    "kind": "noise",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "Mailbox noise is not a vendor invoice.",
                }
            )
            continue
        reminder = bool(_REMINDER.search(blob))
        if reminder and not msg["has_attachments"]:
            skipped.append(
                {
                    "kind": "reminder",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "Payment reminder is not a new invoice.",
                }
            )
            continue
        if _STATEMENT_SUBJECT.search(subject) and not msg["has_attachments"]:
            skipped.append(
                {
                    "kind": "statement",
                    "subject": subject,
                    "email_received": msg["received_chicago"],
                    "email_received_utc": msg["received"],
                    "reason": "Account statement is not an invoice.",
                }
            )
            continue
        if not msg["has_attachments"]:
            continue
        if index % 25 == 0:
            print(f"pdf pass {index}/{len(messages)} invoices {len(invoices)}", flush=True)
        attachments = graph.download_pdf_attachments(ALLOWED_MAILBOX, msg["id"])
        if not attachments:
            if reminder:
                skipped.append(
                    {
                        "kind": "reminder",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "reason": "Payment reminder is not a new invoice.",
                    }
                )
            continue
        digest = hashlib.sha1(str(msg["id"]).encode()).hexdigest()[:12]
        for att_index, (name, content) in enumerate(attachments):
            path = pdf_dir / f"{digest}-{att_index}.pdf"
            if not path.exists() or path.stat().st_size != len(content):
                path.write_bytes(content)
            if reminder and _STATEMENT_SUBJECT.search(name):
                skipped.append(
                    {
                        "kind": "reminder",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "reason": "Payment reminder is not a new invoice.",
                    }
                )
                continue
            try:
                bills, skips = bills_from_pdf(
                    path,
                    subject=subject,
                    from_name=msg["from_name"],
                    from_address=msg["from"],
                )
            except Exception as exc:  # noqa: BLE001 - keep scanning
                skipped.append(
                    {
                        "kind": "unparsed",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "reason": f"PDF could not be read ({type(exc).__name__}).",
                        "pdf_name": name,
                    }
                )
                continue
            for skip in skips:
                skipped.append(
                    {
                        **skip,
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "message_id": msg["id"],
                        "pdf_name": name,
                    }
                )
            if reminder and not bills:
                skipped.append(
                    {
                        "kind": "reminder",
                        "subject": subject,
                        "email_received": msg["received_chicago"],
                        "email_received_utc": msg["received"],
                        "reason": "Payment reminder is not a new invoice.",
                    }
                )
                continue
            for bill in bills:
                if _CREDIT.search(f"{subject} {name}") and not bill.get("total"):
                    skipped.append(
                        {
                            "kind": "credit_memo",
                            "invoice_number": bill["invoice_number"],
                            "subject": subject,
                            "email_received": msg["received_chicago"],
                            "reason": "Credit memo. Treyce handles credit memos.",
                        }
                    )
                    continue
                key = f"{invoice_number_key(bill['invoice_number'])}|{(bill.get('vendor') or '').strip().lower()}"
                bill["message_id"] = msg["id"]
                bill["subject"] = subject
                bill["from"] = msg["from"]
                bill["from_name"] = msg["from_name"]
                bill["email_received"] = msg["received_chicago"]
                bill["email_received_utc"] = msg["received"]
                bill["folder"] = msg["folder"]
                bill["pdf_name"] = name
                bill["pdf_path"] = str(path)
                prior = seen.get(key)
                if prior is None:
                    # Same number from a weaker vendor string still dedupes later.
                    bill["email_received_dates"] = [bill["email_received"]]
                    bill["message_ids"] = [msg["id"]]
                    seen[key] = bill
                    invoices.append(bill)
                    continue
                dates = prior.setdefault("email_received_dates", [prior["email_received"]])
                if bill["email_received"] not in dates:
                    dates.append(bill["email_received"])
                ids = prior.setdefault("message_ids", [prior["message_id"]])
                if msg["id"] not in ids:
                    ids.append(msg["id"])
                if (not prior.get("lines")) and bill.get("lines"):
                    prior["lines"] = bill["lines"]
                    prior["pdf_path"] = bill["pdf_path"]
                    prior["message_id"] = msg["id"]
    invoices.sort(key=lambda row: (row.get("email_received") or "", row.get("invoice_number") or ""))
    return {"invoices": invoices, "skipped": skipped}


def load_kimco_index(client: Any) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    offset = 0
    total = None
    url = client._url("ap_invoices")
    while total is None or offset < total:
        response = client.request(
            "GET",
            url,
            params={
                "pageSize": 2000,
                "offset": offset,
                "fields": "Invoice_Number,Vendor,Posted,Void,AP_Invoice_Batch,Invoice_Amount,Invoice_Verification_Amount",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"invoice list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            values = item.get("values") or {}
            number = invoice_number_key(str(values.get("Invoice_Number") or ""))
            if not number or item.get("id") in (None, ""):
                continue
            index.setdefault(number, []).append(
                {
                    "id": int(item["id"]),
                    "vendor_id": lookup_id(values.get("Vendor")),
                    "vendor_text": lookup_text(values.get("Vendor")) or "",
                    "posted": bool(values.get("Posted")),
                    "void": values.get("Void") is True,
                    "batch": lookup_text(values.get("AP_Invoice_Batch")) or None,
                    "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
                    "invoice_amount": values.get("Invoice_Amount"),
                    "verification_amount": values.get("Invoice_Verification_Amount"),
                }
            )
        print(f"kimco invoices offset {offset} total {total}", flush=True)
        if not items:
            break
        offset += len(items)
    return index


def match_existing(index: dict[str, list[dict[str, Any]]], bill: dict[str, Any]) -> dict[str, Any] | None:
    from ap_clerk.cli import _is_clearly_other_vendor
    from ap_clerk.rules import vendor_match_score

    hits = index.get(invoice_number_key(bill.get("invoice_number"))) or []
    vendor = str(bill.get("vendor") or "")
    kept = []
    for hit in hits:
        if hit.get("void"):
            continue
        text = str(hit.get("vendor_text") or "")
        posted_id = hit.get("vendor_id")
        if names_match(vendor, text) or vendor_match_score(vendor, text):
            kept.append(hit)
        elif _is_clearly_other_vendor(vendor, text, posted_id):
            continue
        else:
            kept.append(hit)
    if not kept:
        return None
    kept.sort(key=lambda row: int(row["id"]))
    return kept[-1]


def discover() -> dict[str, Any]:
    from ap_clerk.auth import load_credentials, resolve_target
    from ap_clerk.cli import _optional_graph_client
    from ap_clerk.kimco import KimcoClient

    CACHE.mkdir(parents=True, exist_ok=True)
    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph credentials missing")
    print("Scanning mailbox", flush=True)
    scan = scan_mailbox(graph)
    print(
        f"Window messages {scan['messages_in_window']} folders {scan['folder_count']}",
        flush=True,
    )
    (CACHE / "scan-meta.json").write_text(
        json.dumps(
            {
                "folder_count": scan["folder_count"],
                "messages_in_window": scan["messages_in_window"],
                "by_folder": scan["by_folder"],
            },
            indent=2,
        )
    )
    collected = collect(graph, scan)
    print(f"Parsed invoices {len(collected['invoices'])} skips {len(collected['skipped'])}", flush=True)
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error)
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    index = load_kimco_index(client)
    already = []
    pending = []
    for bill in collected["invoices"]:
        hit = match_existing(index, bill)
        if hit:
            already.append(
                {
                    "invoice_number": bill["invoice_number"],
                    "vendor": bill.get("vendor"),
                    "kimco_bill_id": hit["id"],
                    "posted": hit["posted"],
                    "batch": hit["batch"],
                    "batch_id": hit["batch_id"],
                    "invoice_amount": hit["invoice_amount"],
                    "verification_amount": hit["verification_amount"],
                    "left_alone": True,
                    "email_received": bill.get("email_received_dates") or [bill.get("email_received")],
                    "pdf_total": bill.get("total"),
                    "po": bill.get("po"),
                }
            )
            continue
        pending.append(bill)
    pending.sort(key=lambda row: (row.get("email_received") or "", row.get("invoice_number") or ""))
    catalog = {
        "mailbox_scan": {
            "messages_in_window": scan["messages_in_window"],
            "folder_count": scan["folder_count"],
            "by_folder": scan["by_folder"],
        },
        "skipped": collected["skipped"],
        "already_in_kimco": already,
        "pending_count": len(pending),
        "pending_next": [
            {
                "vendor": row.get("vendor"),
                "invoice_number": row.get("invoice_number"),
                "email_received": row.get("email_received"),
                "date": row.get("date"),
                "po": row.get("po"),
                "pos": row.get("pos"),
                "total": row.get("total"),
                "lines": len(row.get("lines") or []),
                "fees": row.get("fees"),
                "subject": row.get("subject"),
                "folder": row.get("folder"),
                "pdf_path": row.get("pdf_path"),
                "message_id": row.get("message_id"),
                "parse_verified": row.get("parse_verified"),
                "hold_reason": row.get("hold_reason"),
            }
            for row in pending[:40]
        ],
    }
    (CACHE / "catalog.json").write_text(json.dumps(catalog, indent=2, default=str))
    (CACHE / "pending.json").write_text(json.dumps(pending, indent=2, default=str))
    print(f"already {len(already)} pending {len(pending)}", flush=True)
    for row in pending[:15]:
        print(
            f"NEXT {row.get('email_received')} {row.get('vendor')} #{row.get('invoice_number')} "
            f"PO {row.get('po') or '-'} ${row.get('total')} lines {len(row.get('lines') or [])}",
            flush=True,
        )
    return catalog


if __name__ == "__main__":
    discover()
