"""Read-only check: September API Agent bills vs accountspayable@ filing.

No KIMCO writes. No mail moves, category changes, or sends. One API Agent
sign-in; a failed password is not retried. Header Comments is the creator
stamp (live bills have no CreatorId). The id range 10465-10512 is included
even when the last editor is someone else.
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    GraphClient,
    load_graph_credentials,
)
from ap_clerk.pdf_invoice import _INV_EMJ, _INV_LABEL, _ONEAL_INV, gas_invoice_numbers
from ap_clerk.rules import lookup_text, money
from scripts.sept25_30_attachment_audit import ACCOUNT_NUMBERS, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-archive-check")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)
logging.getLogger("pypdf").setLevel(logging.ERROR)

OUT = ROOT / "runs" / "sept-archive-check-2026-10-07.csv"
CACHE = Path("/tmp/sept-archive-headers.jsonl")
RANGE = range(10465, 10513)
DELETE_PENDING = {10475, 10477}
ARCHIVE = "Inbox/9 - FORT WORTH ARCHIVE"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime,hasAttachments"
WORKERS = 6
COLUMNS = [
    "bill",
    "vendor",
    "invoice",
    "email received",
    "sender",
    "subject",
    "current folder",
    "categories",
    "proposed action",
    "flag",
]

HOLD_BITS = (
    "on hold",
    "does not match",
    "do not match",
    "was not selected",
    "not selected",
    "please receive",
    "no matching receipt",
    "no open matching",
    "needs a purchase order",
    "no purchase order",
    "no receipt",
    "has no receipt",
    "please confirm the unit",
    "quantity does not match",
    "cannot be matched",
    "do not select",
    "missing purchase order",
)
RESOLVE_BITS = (
    "disregard",
    "ready to process",
    "no action needed",
    "selected receipt",
    "selected receipts",
    "selected quantity",
    "please delete this bill",
    "matches the pdf",
    "the bill matches",
    "coded to",
)
PACKET_DOMAINS = ("gasandsupply.com", "emjmetals.com", "onealsteel.com")
VENDOR_HINTS = (
    ("GAS", ("gasandsupply.com", "gas & supply", "gas&supply", "gas and supply")),
    ("EMJ", ("emjmetals.com", "jorgensen")),
    ("ONEAL", ("onealsteel.com", "o'neal", "oneal")),
    ("RYERSON", ("ryerson.com", "ryerson")),
    ("SHOPPA", ("shoppa", "billtrust.com")),
    ("TPI", ("tpitexas.com", "tpi")),
    ("POWDER", ("powder",)),
    ("XCALIBER", ("xcaliber",)),
    ("WILLBANKS", ("willbanks",)),
    ("PRIORITY", ("priority1.com", "priority1")),
    ("HUDSON", ("hudson",)),
    ("TRACE", ("trace metal", "tracemetal")),
    ("TRICOR", ("tricor",)),
    ("LUXOR", ("luxor",)),
    ("CAPITAL", ("capital machine", "capitalmachine")),
    ("CASTLE", ("castle",)),
    ("FASTENAL", ("fastenal",)),
    ("AIR PRODUCTS", ("airproducts", "air products")),
    ("GREENTREE", ("greentree",)),
    ("MORGAN", ("morgan",)),
    ("EASTERN", ("eastern metal", "easternmetal")),
    ("SUPERMARKET", ("supermarket",)),
    ("ARROW", ("arrow",)),
    ("BEARING", ("bearing",)),
    ("EXOTIC", ("exotic",)),
    ("TRICOR", ("tricor",)),
    ("ALTERNATIVE", ("alternative",)),
    ("PRODUCTION METALS", ("production metal",)),
)


def plain(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def date_only(value: Any) -> str:
    return str(value or "")[:10]


def covered_of(record: dict[str, Any]) -> float:
    lists = record.get("lists") or {}
    total = 0.0
    for line in lists.get("APInvoiceLine") or []:
        total += float(money((line.get("values") or {}).get("Extended_Amount")) or 0)
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        total += float(money((charge.get("values") or {}).get("Amount")) or 0)
    for tax in lists.get("APInvoiceTaxCodes") or []:
        total += float(money((tax.get("values") or {}).get("Tax_Amount")) or 0)
    return round(total, 2)


def notes_of(record: dict[str, Any]) -> list[str]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        if not isinstance(comment, dict):
            continue
        values = comment.get("values") or {}
        rows.append((str(values.get("CreatedOn") or ""), int(comment.get("id") or 0), plain(str(values.get("HtmlValue") or ""))[:500]))
    rows.sort()
    return [text for _, _, text in rows if text]


def compact(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    batch = values.get("AP_Invoice_Batch") if isinstance(values.get("AP_Invoice_Batch"), dict) else {}
    modifier = values.get("ModifierId") if isinstance(values.get("ModifierId"), dict) else {}
    return {
        "id": int(record["id"]),
        "invoice": str(values.get("Invoice_Number") or ""),
        "invoice_date": date_only(values.get("Invoice_Date")),
        "comments": str(values.get("Comments") or "").strip(),
        "vendor": lookup_text(values.get("Vendor")),
        "batch_id": batch.get("id"),
        "batch": batch.get("text") or batch.get("name") or "",
        "posted": values.get("Posted"),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "invoice_amount": money(values.get("Invoice_Amount")),
        "covered": covered_of(record),
        "notes": notes_of(record),
        "modifier_id": modifier.get("id"),
    }


def latest_hold(notes: list[str]) -> bool:
    if not notes:
        return False
    text = notes[-1].lower()
    held = any(bit in text for bit in HOLD_BITS)
    resolved = any(bit in text for bit in RESOLVE_BITS)
    if resolved and not held:
        return False
    return held


def disposition(row: dict[str, Any]) -> str:
    bill_id = int(row["id"])
    latest = (row["notes"][-1].lower() if row["notes"] else "")
    if bill_id in DELETE_PENDING or "please delete this bill" in latest or "source document is a statement" in latest or "source document is a letter" in latest:
        return "delete-pending"
    verification = row["verification"]
    covered = row["covered"]
    gap = verification is not None and covered is not None and abs(float(verification) - float(covered)) > 0.05
    if gap or latest_hold(row["notes"]):
        return "HOLD"
    return "PASS"


def agent_created(row: dict[str, Any]) -> bool:
    comments = str(row["comments"] or "")
    if comments == "API Agent" or comments.startswith(("API Agent ", "API Agent.", "API Agent@")):
        return True
    return str(row["batch"] or "").startswith("API Agent")


def include_row(row: dict[str, Any]) -> bool:
    if int(row["id"]) in RANGE:
        return True
    return str(row["invoice_date"]).startswith("2026-09") and agent_created(row)


def refuse_writes(client) -> None:
    guarded = client.request

    def readonly(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Refusing KIMCO {method}. Dry run only.")
        return guarded(method, url, **kwargs)

    client.request = readonly  # type: ignore[method-assign]


def load_cache() -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    if not CACHE.is_file():
        return found
    for line in CACHE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        found[int(row["id"])] = row
    return found


def scan_headers(client) -> tuple[list[dict[str, Any]], set[str], list[int]]:
    listed = client.list_items("ap_invoices")
    by_id = {int(item["id"]): str((item.get("values") or {}).get("Invoice_Number") or "") for item in listed}
    invoice_numbers = {norm(number) for number in by_id.values() if number}
    missing = [bill_id for bill_id in RANGE if bill_id not in by_id]
    cached = load_cache()
    token = client.access_token
    base = f"{client.base_url}/api/v2/{client.services['ap_invoices']}"
    # Id 10000 is an August 2026 invoice and id 10200 is 2026-08-25.
    # September invoice dates in this file start above that neighborhood.
    needed = [bill_id for bill_id in by_id if bill_id not in cached and bill_id >= 9800]
    LOGGER.info("AP list %s cached %s to fetch %s missing-in-range %s", len(by_id), len(cached), len(needed), missing)
    lock = threading.Lock()
    session_local = threading.local()

    def fetch(bill_id: int) -> dict[str, Any]:
        session = getattr(session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
            session_local.session = session
        response = session.get(f"{base}/{bill_id}", timeout=90)
        if response.status_code == 404:
            return {"id": bill_id, "missing": True}
        if response.status_code != 200:
            return {"id": bill_id, "error": response.status_code}
        return compact(response.json())

    done = 0
    with CACHE.open("a") as handle, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(fetch, bill_id) for bill_id in needed]
        for future in as_completed(futures):
            row = future.result()
            done += 1
            if row.get("error"):
                raise SystemExit(f"GET bill {row['id']} HTTP {row['error']}. No writes.")
            if row.get("missing"):
                continue
            cached[int(row["id"])] = row
            with lock:
                handle.write(json.dumps(row) + "\n")
            if done % 500 == 0:
                LOGGER.info("Fetched %s of %s", done, len(needed))
    selected = [row for row in cached.values() if include_row(row)]
    selected.sort(key=lambda row: int(row["id"]))
    LOGGER.info("Selected %s bills", len(selected))
    return selected, invoice_numbers, missing


def sender_of(message: dict[str, Any]) -> str:
    return str((((message.get("from") or {}).get("emailAddress") or {}).get("address") or "")).strip()


def folder_path(graph: GraphClient, folder_id: str, cache: dict[str, str]) -> str:
    if not folder_id:
        return ""
    if folder_id in cache:
        return cache[folder_id]
    parts: list[str] = []
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen and len(parts) < 8:
        seen.add(current)
        response = graph.request(
            "GET",
            graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{current}"),
            params={"$select": "id,displayName,parentFolderId"},
        )
        if response.status_code != 200:
            cache[folder_id] = ""
            return ""
        folder = response.json() or {}
        name = str(folder.get("displayName") or "")
        if name and name.lower() not in {"msgfolderroot", "top of information store"}:
            parts.append(name)
        current = str(folder.get("parentFolderId") or "")
    path = "/".join(reversed(parts))
    cache[folder_id] = path
    return path


def hint_ids() -> dict[str, list[str]]:
    hints: dict[str, list[str]] = {}

    def add(invoice: str, message_id: str) -> None:
        key = norm(invoice)
        if key and message_id and message_id not in hints.setdefault(key, []):
            hints[key].append(message_id)

    for path in (
        ROOT / "runs" / "sept25-30-email-move-execute.csv",
        ROOT / "runs" / "sept25-30-email-move3-execute.csv",
    ):
        old_to_new: dict[str, str] = {}
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                old_to_new[row["old id"]] = row["new id"]
        dry = path.name.replace("-execute.csv", "-dryrun.csv")
        with (ROOT / "runs" / dry).open(newline="") as handle:
            for row in csv.DictReader(handle):
                add(row["invoice #"], old_to_new.get(row["current message id"], row["current message id"]))
    progress = json.loads((ROOT / "runs" / "sept-catchup-progress.json").read_text())
    for row in progress["rows"]:
        add(str(row.get("Invoice #") or ""), str(row.get("graph_message_id") or ""))
    return hints


def progress_received() -> dict[int, str]:
    found: dict[int, str] = {}
    progress = json.loads((ROOT / "runs" / "sept-catchup-progress.json").read_text())
    for row in progress["rows"]:
        raw = row.get("KIMCO id")
        received = str(row.get("receivedDateTime") or "")
        if raw in (None, "") or not received:
            continue
        found[int(raw)] = received
    return found


def vendor_hit(vendor: str, message: dict[str, Any]) -> bool:
    blob = f"{vendor} {sender_of(message)} {message.get('subject') or ''}".lower()
    for needle, tokens in VENDOR_HINTS:
        if needle.lower() in vendor.lower() and any(token in blob for token in tokens):
            return True
    return False


def contains_invoice(text: str, invoice: str) -> bool:
    key = norm(invoice)
    if len(key) < 4:
        return False
    return re.search(rf"(?<![A-Z0-9]){re.escape(invoice.strip())}(?![A-Z0-9])", text or "", flags=re.I) is not None


def attachment_names(graph: GraphClient, message_id: str, cache: dict[str, list[str]]) -> list[str]:
    if message_id not in cache:
        cache[message_id] = [str(item.get("name") or "") for item in graph.list_attachments(ALLOWED_MAILBOX, message_id)]
    return cache[message_id]


def pdf_text(graph: GraphClient, message_id: str, cache: dict[str, str]) -> str:
    if message_id not in cache:
        chunks: list[str] = []
        for _name, content in graph.download_pdf_attachments(ALLOWED_MAILBOX, message_id):
            chunks.extend(page_texts(content))
        cache[message_id] = "\n".join(chunks)
    return cache[message_id]


def search_needles(invoice: str) -> list[str]:
    raw = invoice.strip()
    needles = [raw] if raw else []
    head = raw.split("/")[0].strip()
    if head and norm(head) != norm(raw) and len(norm(head)) >= 6:
        needles.append(head)
    compact = norm(raw)
    if len(compact) > 8:
        needles.append(compact[:8])
    unique: list[str] = []
    for needle in needles:
        if needle and needle not in unique:
            unique.append(needle)
    return unique


def remember(found: dict[str, dict[str, Any]], graph: GraphClient, message: dict[str, Any]) -> None:
    message_id = str(message.get("id") or "")
    if not message_id or message_id in found:
        return
    if message.get("parentFolderId"):
        found[message_id] = message
        return
    try:
        found[message_id] = graph.get_message(ALLOWED_MAILBOX, message_id, select=SELECT)
    except Exception:
        LOGGER.info("Message could not be read")


def candidate_messages(
    graph: GraphClient,
    bill: dict[str, Any],
    hints: dict[str, list[str]],
    days: dict[str, list[dict[str, Any]]],
    received_hint: str,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    invoice = bill["invoice"]
    for message_id in hints.get(norm(invoice), []):
        try:
            found[message_id] = graph.get_message(ALLOWED_MAILBOX, message_id, select=SELECT)
        except Exception:
            LOGGER.info("Hint message for bill %s is gone", bill["id"])
    hits: list[dict[str, Any]] = []
    for needle in search_needles(invoice):
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=15)
        except Exception:
            hits = []
        if hits:
            break
    for message in hits:
        remember(found, graph, message)
    if received_hint and not any(str(item.get("receivedDateTime") or "") == received_hint for item in found.values()):
        day = received_hint[:10]
        if day not in days:
            days[day] = graph.list_messages(received_from=date.fromisoformat(day), received_to=date.fromisoformat(day))
        for message in days[day]:
            if str(message.get("receivedDateTime") or "") == received_hint:
                remember(found, graph, message)
    return list(found.values())


def letterish(subject: str) -> bool:
    return bool(re.search(r"pending payment|please advise|^re:", subject or "", flags=re.I))


def gas_mail(vendor: str, subject: str, sender: str) -> bool:
    blob = f"{vendor} {subject} {sender}".lower()
    return "gas" in blob and "supply" in blob


def pdf_confirms(text: str, invoice: str, vendor: str, subject: str, sender: str, bill_id: int) -> bool:
    if gas_mail(vendor, subject, sender):
        return any(norm(number) == norm(invoice) for number in gas_invoice_numbers(text))
    if letterish(subject) and bill_id not in DELETE_PENDING:
        return False
    if re.search(r"o'?neal|oneal", f"{vendor} {subject} {sender}", flags=re.I):
        numbers = [match.group(1) for match in _ONEAL_INV.finditer(text or "")]
        # A statement or pending-orders letter lists many invoices. Those references
        # are not the source email for the other bills.
        if len(set(numbers)) > 8 and bill_id not in DELETE_PENDING:
            return False
        return any(norm(number) == norm(invoice) for number in numbers)
    if any(norm(match.group(1)) == norm(invoice) for match in _INV_EMJ.finditer(text or "")):
        return True
    if any(norm(match.group(1)) == norm(invoice) for match in _INV_LABEL.finditer(text or "")):
        return True
    return contains_invoice(text, invoice)


def score_message(
    graph: GraphClient,
    bill: dict[str, Any],
    message: dict[str, Any],
    names: dict[str, list[str]],
    pdfs: dict[str, str],
    hints: dict[str, list[str]],
    received_hint: str,
) -> tuple[int, str]:
    invoice = bill["invoice"]
    message_id = str(message.get("id") or "")
    subject = plain(str(message.get("subject") or ""))
    sender = sender_of(message)
    attach = " ".join(attachment_names(graph, message_id, names))
    score = 0
    where = []
    if contains_invoice(subject, invoice):
        score += 5
        where.append("subject")
    if contains_invoice(attach, invoice):
        score += 4
        where.append("attachment")
    if vendor_hit(bill["vendor"], message):
        score += 2
        where.append("vendor")
    if message_id in hints.get(norm(invoice), []):
        score += 6
        where.append("prior-id")
    received = str(message.get("receivedDateTime") or "")
    if received_hint and received == received_hint:
        score += 20
        where.append("received")
    if subject.lower().startswith("re:"):
        score -= 3
    if "subject" not in where and "attachment" not in where and ("vendor" in where or "prior-id" in where or "received" in where):
        text = pdf_text(graph, message_id, pdfs)
        if pdf_confirms(text, invoice, bill["vendor"], subject, sender, int(bill["id"])):
            score += 5
            where.append("pdf")
    if len(norm(invoice)) < 7 and "vendor" not in where and "prior-id" not in where and "pdf" not in where:
        score -= 4
    return score, ",".join(where)


def sibling_invoices(text: str, subject: str, vendor: str, sender: str) -> list[str]:
    found: list[str] = []

    def add(token: str) -> None:
        token = str(token or "").strip().strip(" .,#")
        if token and token not in found and token not in ACCOUNT_NUMBERS:
            found.append(token)

    if gas_mail(vendor, subject, sender):
        for number in gas_invoice_numbers(text):
            add(number)
        return found
    for match in _INV_EMJ.finditer(text or ""):
        add(match.group(1))
    if re.search(r"o'?neal|oneal", f"{vendor} {subject} {text[:500]}", flags=re.I):
        for match in _ONEAL_INV.finditer(text or ""):
            add(match.group(1))
    for match in _INV_LABEL.finditer(text or ""):
        add(match.group(1))
    return found


def choose_messages(graph: GraphClient, bills: list[dict[str, Any]], all_numbers: set[str]) -> dict[int, dict[str, Any]]:
    hints = hint_ids()
    received_by_bill = progress_received()
    names: dict[str, list[str]] = {}
    pdfs: dict[str, str] = {}
    folders: dict[str, str] = {}
    days: dict[str, list[dict[str, Any]]] = {}
    chosen: dict[int, dict[str, Any]] = {}
    for bill in bills:
        received_hint = received_by_bill.get(int(bill["id"]), "")
        ranked = []
        for message in candidate_messages(graph, bill, hints, days, received_hint):
            score, where = score_message(graph, bill, message, names, pdfs, hints, received_hint)
            if score >= 3 and any(part in where for part in ("subject", "attachment", "pdf")):
                ranked.append((score, where, message))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            chosen[int(bill["id"])] = {"message": None, "alternates": 0, "where": ""}
            continue
        best = ranked[0]
        close = [item for item in ranked if item[0] >= best[0] - 1 and item[2].get("id") != best[2].get("id")]
        message = best[2]
        message_id = str(message.get("id") or "")
        sender = sender_of(message)
        subject = plain(str(message.get("subject") or ""))
        attach_blob = " ".join(attachment_names(graph, message_id, names))
        pdf_count = sum(1 for name in attachment_names(graph, message_id, names) if name.lower().endswith(".pdf"))
        need_pdf = (
            pdf_count > 1
            or any(domain in sender.lower() for domain in PACKET_DOMAINS)
            or int(bill["id"]) in DELETE_PENDING
            or (pdf_count == 1 and not contains_invoice(attach_blob, bill["invoice"]))
        )
        text = pdf_text(graph, message_id, pdfs) if need_pdf else ""
        numbers = sibling_invoices(f"{subject}\n{attach_blob}\n{text}", subject, bill["vendor"], sender)
        chosen[int(bill["id"])] = {
            "message": message,
            "alternates": len(close),
            "where": best[1],
            "folder": folder_path(graph, str(message.get("parentFolderId") or ""), folders),
            "sender": sender,
            "subject": subject,
            "numbers": numbers,
        }
        LOGGER.info("Bill %s matched score=%s where=%s alternates=%s", bill["id"], best[0], best[1], len(close))
    return chosen


def expected_category(status: str) -> str:
    if status == "HOLD":
        return ENTERED_WITH_ISSUES_CATEGORY
    return ENTERED_IN_AI_CATEGORY


def proposed(status: str, mail: dict[str, Any], unbilled: list[str]) -> str:
    if mail.get("message") is None:
        return "KEEP (source email not found)"
    if status == "delete-pending":
        if int(mail.get("bill_id") or 0) == 10477:
            reason = "letter awaiting Treyce's deletion"
        else:
            reason = "statement or letter awaiting Treyce's deletion"
        if unbilled:
            return f"KEEP ({reason}; email contains invoice {', '.join(unbilled)} with no bill)"
        return f"KEEP ({reason})"
    if unbilled:
        return f"KEEP (email contains invoice {', '.join(unbilled)} with no bill)"
    categories = list((mail["message"].get("categories") or []))
    expected = expected_category(status)
    other = ENTERED_WITH_ISSUES_CATEGORY if expected == ENTERED_IN_AI_CATEGORY else ENTERED_IN_AI_CATEGORY
    if mail.get("folder") == ARCHIVE and expected in categories and other not in categories:
        return "OK"
    return f"MOVE ({expected})"


def build_rows(
    bills: list[dict[str, Any]],
    chosen: dict[int, dict[str, Any]],
    all_numbers: set[str],
) -> list[dict[str, str]]:
    by_message: dict[str, list[int]] = {}
    for bill in bills:
        mail = chosen[int(bill["id"])]
        message = mail.get("message")
        if message:
            by_message.setdefault(str(message.get("id")), []).append(int(bill["id"]))
    rows = []
    for bill in bills:
        mail = chosen[int(bill["id"])]
        mail["bill_id"] = int(bill["id"])
        status = disposition(bill)
        message = mail.get("message")
        numbers = mail.get("numbers") or []
        unbilled = []
        for number in numbers:
            if norm(number) == norm(bill["invoice"]) or norm(number) in all_numbers or number in ACCOUNT_NUMBERS:
                continue
            if number not in unbilled:
                unbilled.append(number)
        flags = []
        if message is None:
            flags.append("missing email")
        else:
            shared = by_message.get(str(message.get("id")), [])
            if len(shared) > 1:
                flags.append("email matches bills " + "|".join(str(item) for item in shared))
            if mail.get("alternates"):
                flags.append("several emails match this invoice")
        if unbilled:
            flags.append("email contains invoice " + "|".join(unbilled) + " with no bill")
        if int(bill["id"]) not in RANGE and not str(bill["comments"]).startswith("API Agent"):
            flags.append("included from API Agent batch; header comment is not API Agent")
        categories = ""
        if message:
            categories = " | ".join(str(item) for item in (message.get("categories") or []))
        rows.append(
            {
                "bill": str(bill["id"]),
                "vendor": bill["vendor"],
                "invoice": bill["invoice"],
                "email received": str(message.get("receivedDateTime") or "") if message else "",
                "sender": mail.get("sender") or "",
                "subject": mail.get("subject") or "",
                "current folder": mail.get("folder") or "",
                "categories": categories,
                "proposed action": proposed(status, mail, unbilled),
                "flag": "; ".join(flags),
                "_status": status,
                "_batch": str(bill["batch"] or ""),
                "_batch_id": str(bill["batch_id"] or ""),
                "_posted": "yes" if bill["posted"] else "no",
                "_date": bill["invoice_date"],
                "_modified": str(message.get("lastModifiedDateTime") or "") if message else "",
                "_message_id": str(message.get("id") or "") if message else "",
                "_comments": bill["comments"],
            }
        )
    return rows


def main() -> None:
    client = login()
    install_401_guard(client)
    refuse_writes(client)
    bills, all_numbers, missing = scan_headers(client)
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    guarded = graph.request

    def readonly(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Refusing Graph {method}. Dry run only.")
        return guarded(method, url, **kwargs)

    graph.request = readonly  # type: ignore[method-assign]
    chosen = choose_messages(graph, bills, all_numbers)
    rows = build_rows(bills, chosen, all_numbers)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "bills": len(rows),
        "missing_ids": missing,
        "outside_range": [int(row["bill"]) for row in rows if int(row["bill"]) not in RANGE],
        "counts": {},
        "statuses": {},
        "move_keep": [],
    }
    for row in rows:
        action = row["proposed action"].split(" ", 1)[0]
        summary["counts"][action] = summary["counts"].get(action, 0) + 1
        summary["statuses"][row["_status"]] = summary["statuses"].get(row["_status"], 0) + 1
        if action in {"MOVE", "KEEP"}:
            summary["move_keep"].append(
                {
                    "bill": int(row["bill"]),
                    "vendor": row["vendor"],
                    "invoice": row["invoice"],
                    "date": row["_date"],
                    "batch": row["_batch"],
                    "batch_id": row["_batch_id"],
                    "posted": row["_posted"],
                    "status": row["_status"],
                    "received": row["email received"],
                    "sender": row["sender"],
                    "subject": row["subject"],
                    "folder": row["current folder"],
                    "categories": row["categories"],
                    "lastModifiedDateTime": row["_modified"],
                    "message_id": row["_message_id"],
                    "proposed": row["proposed action"],
                    "flag": row["flag"],
                    "comments": row["_comments"],
                }
            )
    Path("/tmp/sept-archive-check-summary.json").write_text(json.dumps(summary, indent=2))
    LOGGER.info("Wrote %s bills=%s counts=%s", OUT.name, len(rows), summary["counts"])


if __name__ == "__main__":
    main()
