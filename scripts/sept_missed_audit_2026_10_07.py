"""Read-only scan for September invoices that have no KIMCO bill.

Mailbox accountspayable@ only. Every folder except Inbox/9 - FORT WORTH ARCHIVE.
Messages received 2026-09-01 through 2026-09-30, plus 2026-10-01 through
2026-10-03 when an invoice on the message is dated September. The six
un-entered numbers on the 2026-10-01 Gas & Supply invoice/statement are
always checked. No mail moves, no category changes, no KIMCO writes.
One API Agent sign-in; a failed password is not retried.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, GraphError, load_graph_credentials
from ap_clerk.pdf_invoice import (
    _AMOUNT_LABEL,
    _DATE_LABEL,
    _INV_BILL_HASH,
    _INV_COLON_NUM,
    _INV_DASH_IN,
    _INV_EMJ,
    _INV_FASTENAL,
    _INV_GRM,
    _INV_LABEL,
    _INV_LS,
    _INV_MSC_REAL,
    _INV_PSI,
    _INV_PS_INV,
    _INV_STACKED,
    _INV_STACKED_SHORT,
    _INV_SV,
    _INV_TECHNI,
    _INV_TMC,
    _LABELED_PAYABLE_RE,
    _ONEAL_INV,
    _PO_LABEL,
    _usable_invoice_number,
    classify_pdf_page,
    parse_money,
)
from ap_clerk.pdf_links import download_first_public_pdf
from ap_clerk.rules import extract_subject_invoice_number, lookup_text, money
from scripts.sept25_30_attachment_audit import ACCOUNT_NUMBERS, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_archive_check_2026_10_07 import norm, plain, refuse_writes, sender_of

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-missed")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)
logging.getLogger("pypdf").setLevel(logging.ERROR)

OUT_CSV = ROOT / "runs" / "sept-missed-audit-2026-10-07.csv"
OUT_DIR = ROOT / "runs" / "sept-missed-audit"
SUMMARY = ROOT / "runs" / "sept-missed-audit-2026-10-07-summary.json"
INDEX_CACHE = Path("/tmp/sept-missed-invoice-index.json")
MESSAGE_CACHE = Path("/tmp/sept-missed-messages.json")
DOC_CACHE = Path("/tmp/sept-missed-docs.jsonl")
PDF_DIR = Path("/tmp/sept-missed-pdfs")
CACHE_VERSION = 3
ARCHIVE_NAME = "9 - FORT WORTH ARCHIVE"
SELECT = "id,subject,from,toRecipients,receivedDateTime,parentFolderId,categories,hasAttachments,bodyPreview"
BODY_SELECT = SELECT + ",body"
RECEIVED_FROM = "2026-09-01T00:00:00Z"
RECEIVED_TO = "2026-10-04T00:00:00Z"
OCTOBER_FROM = "2026-10-01T00:00:00Z"
GAS_RECEIVED = "2026-10-01T11:30:28Z"
GAS_FORCE = (
    "0040477446",
    "0000018220",
    "0040344711",
    "0040406308",
    "0040412279",
    "0040482402",
)
CUSTOMER_ACCOUNTS = {"TXFT40601", "14748440", "02627782", "32279"}
COLUMNS = [
    "received time",
    "sender",
    "subject",
    "folder",
    "categories",
    "doc type",
    "invoice #",
    "invoice date",
    "amount",
    "PO",
    "KIMCO bill id or MISSING",
    "notes",
]
LABELED_PATTERNS = (
    _INV_EMJ,
    _INV_PS_INV,
    _INV_FASTENAL,
    _INV_PSI,
    _INV_TECHNI,
    _INV_LS,
    _INV_TMC,
    _INV_SV,
    _INV_DASH_IN,
    _INV_LABEL,
    _INV_BILL_HASH,
    _INV_COLON_NUM,
    _INV_GRM,
    _INV_MSC_REAL,
    _INV_STACKED,
    _INV_STACKED_SHORT,
)
STOP_VENDOR = {
    "INC",
    "LLC",
    "CORP",
    "COMPANY",
    "INVOICE",
    "FROM",
    "THE",
    "AND",
    "LTD",
    "CO",
}


def graph_readonly(graph: GraphClient) -> None:
    guarded = graph.request

    def readonly(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Refusing Graph {method}. Audit only.")
        return guarded(method, url, **kwargs)

    graph.request = readonly  # type: ignore[method-assign]


def graph_get(graph: GraphClient, url: str, **kwargs: Any):
    delay = 2
    for _attempt in range(5):
        response = graph.request("GET", url, **kwargs)
        if response.status_code != 429 and response.status_code < 500:
            return response
        LOGGER.info("Graph HTTP %s; waiting %ss", response.status_code, delay)
        time.sleep(delay)
        delay = min(delay * 2, 30)
    return response


def to_iso(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    cleaned = text.replace(",", "")
    for fmt in (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%m-%d-%Y",
        "%m-%d-%y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%b %d %Y",
        "%B %d %Y",
        "%b-%d-%Y",
        "%B-%d-%Y",
    ):
        try:
            year = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        if year.year < 100:
            return ""
        return year.isoformat()
    return ""


def amount_text(value: Any) -> str:
    parsed = money(value)
    if parsed is None:
        parsed = parse_money(str(value)) if value not in (None, "") else None
    if parsed is None:
        return ""
    return f"{float(parsed):.2f}"


def categories_of(message: dict[str, Any]) -> str:
    return ", ".join(str(item) for item in (message.get("categories") or []) if item)


def is_archive_path(path: str) -> bool:
    parts = [part.strip() for part in (path or "").split("/") if part.strip()]
    return ARCHIVE_NAME in parts


def is_gas_force_message(received: str, sender: str, subject: str) -> bool:
    return (
        str(received or "").startswith(GAS_RECEIVED[:19])
        and "gasandsupply.com" in (sender or "").lower()
        and "invoice" in (subject or "").lower()
        and "statement" in (subject or "").lower()
    )


def vendor_compatible(mail_vendor: str, kimco_vendor: str) -> bool | None:
    left = re.sub(r"[^A-Z0-9]", "", (mail_vendor or "").upper())
    right = re.sub(r"^\d+", "", re.sub(r"[^A-Z0-9]", "", (kimco_vendor or "").upper()))
    if len(left) < 3 or len(right) < 3:
        return None
    if left in right or right in left:
        return True
    left_tokens = {token for token in re.findall(r"[A-Z]{4,}", (mail_vendor or "").upper()) if token not in STOP_VENDOR}
    right_tokens = {token for token in re.findall(r"[A-Z]{4,}", (kimco_vendor or "").upper()) if token not in STOP_VENDOR}
    if not left_tokens or not right_tokens:
        return None
    return bool(left_tokens & right_tokens)


def number_keys(number: str) -> list[str]:
    key = norm(number)
    if not key:
        return []
    keys = [key]
    if key.isdigit():
        stripped = key.lstrip("0") or "0"
        if stripped != key and len(stripped) >= 4:
            keys.append(stripped)
    return keys


def add_number(found: list[str], token: str) -> None:
    usable = _usable_invoice_number(token)
    if not usable:
        return
    if usable.upper() in CUSTOMER_ACCOUNTS or usable in ACCOUNT_NUMBERS:
        return
    if usable not in found:
        found.append(usable)


_DOLLAR = re.compile(
    r"(?:inv(?:oice)?\s*amt|invoice\s*amount|amount\s*due|total\s+amount\s+due|total\s+due|balance\s+due|"
    r"invoice\s*total|please\s+pay(?:\s+this\s+amount)?)\s*(?:on\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4})?\s*[:.]?\s*\$\s*([\d,]+\.\d{2})",
    flags=re.I,
)


def extract_numbers(text: str, *, gas: bool, oneal: bool) -> list[str]:
    found: list[str] = []
    if gas:
        for number in gas_header_numbers(text or ""):
            add_number(found, number)
        return found
    if oneal:
        for match in _ONEAL_INV.finditer(text or ""):
            add_number(found, match.group(1))
    for pattern in LABELED_PATTERNS:
        for match in pattern.finditer(text or ""):
            add_number(found, match.group(1))
    return found


def plausible_amount(raw: str) -> str:
    text = amount_text(raw)
    if not text:
        return ""
    if float(text) >= 250000:
        return ""
    return text


def facts_near(text: str, number: str) -> dict[str, str]:
    blob = text or ""
    match = re.search(rf"(?<![A-Z0-9]){re.escape(number)}(?![A-Z0-9])", blob, flags=re.I)
    window = blob if match is None else blob[max(0, match.start() - 700) : match.end() + 1200]
    date_match = _DATE_LABEL.search(window)
    invoice_date = to_iso(date_match.group(1) if date_match else "")
    if not invoice_date:
        loose = re.search(
            r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})\b",
            window,
            flags=re.I,
        )
        invoice_date = to_iso(loose.group(1) if loose else "")
    if invoice_date[:4] not in {"2024", "2025", "2026"}:
        invoice_date = ""
    amount = ""
    dollar = _DOLLAR.search(window) or _DOLLAR.search(blob)
    if dollar:
        amount = plausible_amount(dollar.group(1))
    if not amount:
        labeled = _AMOUNT_LABEL.search(window) or _LABELED_PAYABLE_RE.search(window)
        if labeled and "$" in labeled.group(0):
            amount = plausible_amount(labeled.group(1))
    po_match = _PO_LABEL.search(window)
    return {
        "invoice date": invoice_date,
        "amount": amount,
        "PO": (po_match.group(1).strip() if po_match else ""),
    }


def is_aging_statement(text: str) -> bool:
    blob = text or ""
    head = blob[:1200]
    if re.search(r"(?:^|\n)\s*STATEMENT\b", head) and re.search(
        r"1 to 30 DAYS|TOTAL BALANCE|STATEMENT NO\.?|aging", blob, flags=re.I
    ):
        return True
    if re.search(r"\bstatement\b", blob, flags=re.I) and re.search(r"STATEMENT NO\.?|1-30 Days Past Due", blob, flags=re.I):
        return True
    if re.search(r"past due invoice", blob, flags=re.I) and not re.search(r"\boriginal\s+invoice\b", blob, flags=re.I):
        return True
    return False


def gas_header_numbers(text: str) -> list[str]:
    """Invoice number printed alone in the Gas header, not a statement line or delivery ticket."""
    found: list[str] = []
    for number in re.findall(r"(?m)^[ \t]*(00\d{8})[ \t]*$", text or ""):
        if number not in found:
            found.append(number)
    return found


_GAS_PRODUCT = re.compile(r"(\d{2}/\d{2}/\d{2})\s+([A-Z]\d{3,5})\s+(00\d{8})")
_GAS_SUBTOTAL = re.compile(r"Subtotal\s+([\d,]+\.\d{2})", flags=re.I)
_GAS_MONEY = r"((?:\d{1,3}(?:,\d{3})+|\d*)\.\d{2}-?)"
_GAS_LINE = re.compile(
    r"(\d{2}/\d{2}/\d{2})\s+(00\d{8})\s+(PAYMENT|FIN CHRG|CYL RENT|INVOICE|CREDIT)\s+"
    rf"(?:(\d+)\s+)?{_GAS_MONEY}\s+{_GAS_MONEY}"
)


def gas_statement_lines(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for match in _GAS_LINE.finditer(text or ""):
        kind = match.group(3).strip().upper()
        rows.append(
            {
                "date": to_iso(match.group(1)),
                "number": match.group(2),
                "line_type": kind,
                "charge": amount_text(match.group(5).rstrip("-")),
                "payment": amount_text(match.group(6).rstrip("-")),
            }
        )
    return rows


def page_kind(text: str, filename: str) -> str:
    blob = text or ""
    if is_aging_statement(blob):
        return "statement"
    if re.search(r"\bpayment\s+inquiry\b", blob, flags=re.I):
        return "payment inquiry"
    if re.search(r"\bcredit\s+(?:memo|note|memorandum)\b", blob, flags=re.I) and not re.search(
        r"\boriginal\s+invoice\b", blob, flags=re.I
    ):
        return "credit memo"
    if re.search(r"\bpacking\s+(?:slip|list)\b", blob, flags=re.I) and not re.search(
        r"\binvoice\s*(?:number|no\.?|#|total)\b", blob, flags=re.I
    ):
        return "packing slip"
    if re.search(r"CYLINDER RENTAL INVOICE", blob, flags=re.I) or gas_header_numbers(blob):
        return "invoice"
    classified = classify_pdf_page(blob)
    if classified == "statement":
        return "statement"
    if classified == "invoice":
        return "invoice"
    if re.search(r"payment status|please advise|pending order|lockbox", blob, flags=re.I):
        return "letter"
    name = filename or ""
    if re.search(r"packing", name, flags=re.I):
        return "packing slip"
    if re.search(r"statement", name, flags=re.I) and not re.search(r"invoice", name, flags=re.I):
        return "statement"
    if re.search(r"invoice", name, flags=re.I):
        return "invoice"
    return "other"


def message_kind(subject: str, body: str) -> str:
    blob = f"{subject}\n{body}"
    if re.search(r"\bpayment\s+inquiry\b", blob, flags=re.I):
        return "payment inquiry"
    if re.search(r"past due", subject or "", flags=re.I):
        return "statement"
    if re.search(r"\bcredit\s+(?:memo|note|memorandum)\b", blob, flags=re.I):
        return "credit memo"
    if re.search(r"payment status|please advise|pending order|lockbox", subject or "", flags=re.I):
        return "letter"
    if re.search(r"^\s*statement\b", subject or "", flags=re.I) or (
        re.search(r"\bstatement\b", subject or "", flags=re.I) and not re.search(r"\binvoice\b", subject or "", flags=re.I)
    ):
        return "statement"
    if re.search(r"packing\s+(?:slip|list)", blob, flags=re.I):
        return "packing slip"
    if re.search(r"transaction receipt", subject or "", flags=re.I):
        return "other"
    if re.search(r"\binvoice\b", blob, flags=re.I):
        return "invoice"
    return "other"


def choose_doc_type(kinds: list[str], fallback: str) -> str:
    order = ("invoice", "credit memo", "payment inquiry", "letter", "statement", "packing slip", "other")
    present = set(kinds)
    for kind in order:
        if kind in present:
            return kind
    return fallback or "other"


def list_folders(graph: GraphClient) -> list[dict[str, str]]:
    folders: list[dict[str, str]] = []
    queue = [("mailFolders", "")]
    seen: set[str] = set()
    while queue:
        suffix, parent = queue.pop(0)
        url: str | None = graph._user_url(ALLOWED_MAILBOX, suffix)
        params: dict[str, Any] | None = {"$top": 100, "$select": "id,displayName,parentFolderId,childFolderCount"}
        while url:
            response = graph_get(graph, url, params=params)
            params = None
            if response.status_code != 200:
                raise GraphError(f"Graph list folders HTTP {response.status_code} at {suffix}")
            payload = response.json() or {}
            for item in payload.get("value") or []:
                folder_id = str(item.get("id") or "")
                if not folder_id or folder_id in seen:
                    continue
                seen.add(folder_id)
                name = str(item.get("displayName") or "")
                path = f"{parent}/{name}" if parent else name
                if is_archive_path(path):
                    LOGGER.info("Skipping archive folder %s", path)
                    continue
                folders.append({"id": folder_id, "path": path})
                if int(item.get("childFolderCount") or 0) > 0 or suffix == "mailFolders":
                    queue.append((f"mailFolders/{folder_id}/childFolders", path))
            url = payload.get("@odata.nextLink")
    return folders


def list_folder_messages(graph: GraphClient, folder_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    url: str | None = graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{folder_id}/messages")
    params: dict[str, Any] | None = {
        "$select": SELECT,
        "$filter": f"receivedDateTime ge {RECEIVED_FROM} and receivedDateTime lt {RECEIVED_TO}",
        "$orderby": "receivedDateTime desc",
        "$top": 50,
    }
    while url:
        response = graph_get(graph, url, params=params)
        params = None
        if response.status_code == 400:
            LOGGER.info("Folder filter was rejected; listing without order")
            return list_folder_messages_unordered(graph, folder_id)
        if response.status_code != 200:
            raise GraphError(f"Graph list messages HTTP {response.status_code}")
        payload = response.json() or {}
        messages.extend(item for item in (payload.get("value") or []) if isinstance(item, dict))
        url = payload.get("@odata.nextLink")
    return messages


def list_folder_messages_unordered(graph: GraphClient, folder_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    url: str | None = graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{folder_id}/messages")
    params: dict[str, Any] | None = {
        "$select": SELECT,
        "$filter": f"receivedDateTime ge {RECEIVED_FROM} and receivedDateTime lt {RECEIVED_TO}",
        "$top": 50,
    }
    while url:
        response = graph_get(graph, url, params=params)
        params = None
        if response.status_code != 200:
            raise GraphError(f"Graph list messages HTTP {response.status_code}")
        payload = response.json() or {}
        messages.extend(item for item in (payload.get("value") or []) if isinstance(item, dict))
        url = payload.get("@odata.nextLink")
    return messages


def load_messages(graph: GraphClient) -> list[dict[str, Any]]:
    if MESSAGE_CACHE.is_file():
        payload = json.loads(MESSAGE_CACHE.read_text())
        if payload.get("version") == CACHE_VERSION:
            LOGGER.info("Message cache %s", len(payload["messages"]))
            return payload["messages"]
    folders = list_folders(graph)
    LOGGER.info("Folders in scope %s", len(folders))
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, folder in enumerate(folders, start=1):
        chunk = list_folder_messages(graph, folder["id"])
        LOGGER.info("Folder %s/%s %s messages=%s", index, len(folders), folder["path"], len(chunk))
        for message in chunk:
            message_id = str(message.get("id") or "")
            received = str(message.get("receivedDateTime") or "")
            if not message_id or message_id in seen:
                continue
            if received < RECEIVED_FROM or received >= RECEIVED_TO:
                continue
            seen.add(message_id)
            messages.append(
                {
                    "id": message_id,
                    "received": received,
                    "sender": sender_of(message),
                    "subject": plain(str(message.get("subject") or "")),
                    "folder": folder["path"],
                    "folder_id": folder["id"],
                    "categories": categories_of(message),
                    "has_attachments": bool(message.get("hasAttachments")),
                    "preview": plain(str(message.get("bodyPreview") or ""))[:500],
                }
            )
    MESSAGE_CACHE.write_text(json.dumps({"version": CACHE_VERSION, "messages": messages}))
    LOGGER.info("Cached %s messages", len(messages))
    return messages


def load_index(client) -> dict[str, list[dict[str, Any]]]:
    if INDEX_CACHE.is_file():
        payload = json.loads(INDEX_CACHE.read_text())
        if payload.get("version") == CACHE_VERSION:
            LOGGER.info("Invoice index %s numbers", len(payload["by_key"]))
            return payload["by_key"]
    listed = client.list_items("ap_invoices")
    by_key: dict[str, list[dict[str, Any]]] = {}
    for item in listed:
        values = item.get("values") or {}
        number = str(values.get("Invoice_Number") or "").strip()
        if not number:
            continue
        batch = values.get("AP_Invoice_Batch") if isinstance(values.get("AP_Invoice_Batch"), dict) else {}
        row = {
            "id": int(item["id"]),
            "invoice": number,
            "vendor": lookup_text(values.get("Vendor")),
            "invoice_date": str(values.get("Invoice_Date") or "")[:10],
            "amount": amount_text(values.get("Invoice_Amount") if values.get("Invoice_Amount") not in (None, "") else values.get("Invoice_Verification_Amount")),
            "posted": bool(values.get("Posted")),
            "void": bool(values.get("Void") or values.get("Voided")),
            "batch": str(batch.get("text") or batch.get("name") or ""),
        }
        for key in number_keys(number):
            bucket = by_key.setdefault(key, [])
            if not any(existing["id"] == row["id"] for existing in bucket):
                bucket.append(row)
    INDEX_CACHE.write_text(json.dumps({"version": CACHE_VERSION, "by_key": by_key}))
    LOGGER.info("Indexed %s invoice numbers from %s bills", len(by_key), len(listed))
    return by_key


def lookup_bills(index: dict[str, list[dict[str, Any]]], number: str) -> tuple[list[dict[str, Any]], str]:
    exact = index.get(norm(number), [])
    if exact:
        return exact, "exact"
    keys = number_keys(number)
    if len(keys) > 1:
        stripped = index.get(keys[1], [])
        if stripped:
            return stripped, "leading-zero"
    return [], ""


def match_bill(index: dict[str, list[dict[str, Any]]], number: str, mail_vendor: str) -> tuple[str, str]:
    hits, how = lookup_bills(index, number)
    if not hits:
        return "MISSING", ""
    compatible = []
    rejected = []
    unknown = []
    for hit in hits:
        verdict = vendor_compatible(mail_vendor, hit["vendor"])
        if verdict is True:
            compatible.append(hit)
        elif verdict is False:
            rejected.append(hit)
        else:
            unknown.append(hit)
    chosen = compatible or (unknown if not rejected else [])
    if not chosen and unknown and not compatible:
        chosen = unknown
    if not chosen:
        others = "; ".join(f"{hit['id']} {hit['vendor']}" for hit in rejected)
        return "MISSING", f"invoice number is on a different vendor ({others})"
    active = [hit for hit in chosen if not hit.get("void")] or chosen
    ids = "; ".join(str(hit["id"]) for hit in active)
    details = []
    for hit in active:
        state = "posted" if hit.get("posted") else "unposted"
        if hit.get("void"):
            state = "void"
        details.append(
            f"bill {hit['id']} {hit['vendor']} date {hit['invoice_date']} amount {hit['amount']} {state} batch {hit['batch']}"
        )
    note = "; ".join(details)
    if how == "leading-zero":
        note = f"matched on leading-zero variant; {note}"
    if len(active) > 1:
        note = f"more than one bill; {note}"
    return ids, note


def load_any_docs() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not DOC_CACHE.is_file():
        return found
    for line in DOC_CACHE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        found[str(row["id"])] = row
    return found


def cached_docs() -> dict[str, dict[str, Any]]:
    return {key: row for key, row in load_any_docs().items() if row.get("version") == CACHE_VERSION}


def reuse_pdfs(prior_doc: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not prior_doc or not prior_doc.get("pdfs"):
        return None
    pdfs: list[dict[str, Any]] = []
    for item in prior_doc["pdfs"]:
        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return None
        pdfs.append({"name": item.get("name") or "attachment.pdf", "path": str(path), "pages": audit_pages(path.read_bytes())})
    return pdfs


def extract_from_pdfs(message: dict[str, Any], pdfs: list[dict[str, Any]], note: str) -> dict[str, Any]:
    doc_type, invoices, extra = invoice_rows_for_message(message, message.get("preview") or "", pdfs)
    stored_pdfs = [{"name": pdf["name"], "path": pdf["path"], "page_count": len(pdf["pages"])} for pdf in pdfs]
    return {
        "version": CACHE_VERSION,
        "id": message["id"],
        "received": message["received"],
        "sender": message["sender"],
        "subject": message["subject"],
        "folder": message["folder"],
        "categories": message["categories"],
        "doc_type": doc_type,
        "invoices": invoices,
        "pdfs": stored_pdfs,
        "note": "; ".join(part for part in (note, extra) if part),
        "october": message["received"] >= OCTOBER_FROM,
    }


def append_doc(row: dict[str, Any]) -> None:
    with DOC_CACHE.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


def save_pdf(message_id: str, index: int, content: bytes) -> str:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(message_id.encode()).hexdigest()[:16]
    path = PDF_DIR / f"{digest}-{index}.pdf"
    path.write_bytes(content)
    return str(path)


def read_body(graph: GraphClient, message_id: str) -> str:
    message = graph.get_message(ALLOWED_MAILBOX, message_id, select=BODY_SELECT)
    body = ((message.get("body") or {}).get("content") or "")
    return plain(body)


def audit_pages(content: bytes) -> list[str]:
    import fitz

    document = fitz.open(stream=content, filetype="pdf")
    rendered = [page.get_text() or "" for page in document]
    extracted = page_texts(content)
    if sum(len(page) for page in rendered) >= sum(len(page) for page in extracted):
        return rendered
    return extracted


def collect_pdfs(graph: GraphClient, message: dict[str, Any], body: str) -> list[dict[str, Any]]:
    pdfs: list[dict[str, Any]] = []
    if message.get("has_attachments"):
        for index, (name, content) in enumerate(graph.download_pdf_attachments(ALLOWED_MAILBOX, message["id"]), start=1):
            if not content.startswith(b"%PDF-"):
                continue
            pages = audit_pages(content)
            pdfs.append(
                {
                    "name": name,
                    "path": save_pdf(message["id"], index, content),
                    "pages": pages,
                }
            )
    if pdfs:
        return pdfs
    if "invoice" not in (message.get("subject") or "").lower() and "quickbooks" not in (message.get("sender") or "").lower():
        return pdfs
    public = download_first_public_pdf(body)
    content = public.get("content") if isinstance(public, dict) else None
    if isinstance(public, dict) and public.get("ok") and content and bytes(content).startswith(b"%PDF-"):
        raw = bytes(content)
        pdfs.append({"name": "linked.pdf", "path": save_pdf(message["id"], 1, raw), "pages": audit_pages(raw)})
    return pdfs


def invoice_rows_for_message(message: dict[str, Any], body: str, pdfs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str]:
    sender = message["sender"]
    subject = message["subject"]
    gas = "gasandsupply" in sender.lower() or "gas & supply" in subject.lower() or "gas&supply" in subject.lower()
    oneal = bool(re.search(r"o'?neal|onealsteel", f"{sender} {subject}", flags=re.I))
    fallback = message_kind(subject, body)
    page_kinds: list[str] = []
    invoices: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(number: str, kind: str, date: str, amount: str, po: str, pdf_index: int | None, pages: list[int], source: str) -> None:
        key = norm(number)
        if not key or key in seen:
            return
        seen.add(key)
        invoices.append(
            {
                "number": number,
                "doc_type": kind,
                "invoice date": date,
                "amount": amount,
                "PO": po,
                "pdf_index": pdf_index,
                "pages": pages,
                "source": source,
            }
        )

    statement_lines: list[dict[str, str]] = []
    for pdf_index, pdf in enumerate(pdfs):
        for page_index, page in enumerate(pdf["pages"], start=1):
            kind = page_kind(page, pdf["name"])
            page_kinds.append(kind)
            if kind == "statement":
                statement_lines.extend(gas_statement_lines(page))
                continue
            if kind not in {"invoice", "credit memo"}:
                continue
            for number in extract_numbers(page, gas=gas, oneal=oneal):
                facts = facts_near(page, number)
                push(number, kind, facts["invoice date"], facts["amount"], facts["PO"], pdf_index, [page_index], "pdf")
        if not any(page.strip() for page in pdf["pages"]):
            filename_number = extract_subject_invoice_number(pdf["name"]) or ""
            kind = page_kind("", pdf["name"])
            page_kinds.append(kind)
            if filename_number and kind == "invoice":
                push(filename_number, "invoice", "", "", "", pdf_index, [], "filename")

    doc_type = choose_doc_type(page_kinds, fallback)
    if doc_type == "invoice" and not invoices:
        for number in extract_numbers(f"{subject}\n{body}", gas=gas, oneal=oneal):
            facts = facts_near(body or subject, number)
            push(number, "invoice", facts["invoice date"], facts["amount"], facts["PO"], None, [], "body")
        subject_number = extract_subject_invoice_number(subject)
        if subject_number:
            facts = facts_near(body or subject, subject_number)
            push(subject_number, "invoice", facts["invoice date"], facts["amount"], facts["PO"], None, [], "subject")
    if is_gas_force_message(message["received"], sender, subject):
        by_number = {norm(line["number"]): line for line in statement_lines}
        for number in GAS_FORCE:
            line = by_number.get(norm(number), {})
            line_type = line.get("line_type") or ""
            kind = "other" if line_type == "PAYMENT" else "invoice"
            note_date = line.get("date") or ""
            if norm(number) in seen:
                for invoice in invoices:
                    if norm(invoice["number"]) == norm(number):
                        if not invoice.get("invoice date") and note_date:
                            invoice["invoice date"] = note_date
                        invoice["line_type"] = line_type
                continue
            push(number, kind, note_date, "", "", None, [], "kyle-list")
            invoices[-1]["line_type"] = line_type
    enrich_from_pdfs(invoices, subject, pdfs)
    note = ""
    if not pdfs and message.get("has_attachments"):
        note = "attachments were not PDFs"
    elif not pdfs and doc_type == "invoice":
        note = "no PDF attachment"
    return doc_type, invoices, note


def enrich_from_pdfs(invoices: list[dict[str, Any]], subject: str, pdfs: list[dict[str, Any]]) -> None:
    """Fill date, amount, and PO from the page that actually prints the invoice number."""
    subject_number = extract_subject_invoice_number(subject) or ""
    if subject_number and subject_number not in {row["number"] for row in invoices}:
        if re.search(r"\binvoice\b", subject or "", flags=re.I):
            invoices.append(
                {
                    "number": subject_number,
                    "doc_type": "invoice",
                    "invoice date": "",
                    "amount": "",
                    "PO": "",
                    "pdf_index": None,
                    "pages": [],
                    "source": "subject",
                    "line_type": "",
                }
            )
    if re.search(r"invoice\s*#", subject or "", flags=re.I) and subject_number:
        extras = [row for row in invoices if norm(row["number"]) != norm(subject_number)]
        if extras and any(norm(row["number"]) == norm(subject_number) for row in invoices) and len(invoices) <= 3:
            kept = [row for row in invoices if norm(row["number"]) == norm(subject_number)]
            form_ids = ", ".join(row["number"] for row in extras)
            for row in kept:
                row["form_note"] = f"PDF also prints {form_ids} above the invoice number"
            invoices[:] = kept
    for row in invoices:
        printed = row["number"]
        for pdf_index, pdf in enumerate(pdfs):
            for page_index, page in enumerate(pdf["pages"], start=1):
                prefixed = re.search(rf"(?<![A-Z0-9])(INV{re.escape(printed)})(?![A-Z0-9])", page or "", flags=re.I)
                plain_hit = re.search(rf"(?<![A-Z0-9]){re.escape(printed)}(?![A-Z0-9])", page or "", flags=re.I)
                if not prefixed and not plain_hit:
                    continue
                if prefixed:
                    row["number"] = prefixed.group(1).upper()
                facts = facts_near(page, row["number"])
                labeled_date = re.search(
                    r"(?:invoice\s*date|inv\s*date)\s*[:.]?\s*(\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{4})",
                    page,
                    flags=re.I,
                )
                if labeled_date:
                    facts["invoice date"] = to_iso(labeled_date.group(1))
                due_amount = re.search(
                    r"(?:amount\s+due|inv(?:oice)?\s*amt|balance\s+due)\s*[:.]?(?:[\s\S]{0,80}?)\$\s*([\d,]+\.\d{2})",
                    page,
                    flags=re.I,
                )
                if due_amount:
                    facts["amount"] = plausible_amount(due_amount.group(1)) or facts["amount"]
                total_line = re.search(r"(?:^|\n)\s*Total\s+[\$]?([\d,]+\.\d{2})", page, flags=re.I)
                if not facts["amount"] and total_line:
                    facts["amount"] = plausible_amount(total_line.group(1))
                if facts["invoice date"]:
                    row["invoice date"] = facts["invoice date"]
                if facts["amount"]:
                    row["amount"] = facts["amount"]
                if facts["PO"] and not row.get("PO"):
                    row["PO"] = facts["PO"]
                row["pdf_index"] = pdf_index
                pages = row.get("pages") or []
                if page_index not in pages:
                    pages.append(page_index)
                row["pages"] = pages
                if row.get("source") == "subject":
                    row["source"] = "pdf"
                break
            else:
                continue
            break


def include_invoice(message: dict[str, Any], invoice: dict[str, Any]) -> bool:
    if message["received"] < OCTOBER_FROM:
        return True
    if invoice.get("source") == "kyle-list":
        return True
    if is_gas_force_message(message["received"], message["sender"], message["subject"]) and norm(invoice["number"]) in {
        norm(number) for number in GAS_FORCE
    }:
        return True
    return str(invoice.get("invoice date") or "").startswith("2026-09")


def extract_message(graph: GraphClient, message: dict[str, Any]) -> dict[str, Any]:
    body = ""
    if not message.get("has_attachments") or "invoice" in message["subject"].lower() or "statement" in message["subject"].lower():
        try:
            body = read_body(graph, message["id"])
        except GraphError:
            body = message.get("preview") or ""
    else:
        body = message.get("preview") or ""
    pdfs = collect_pdfs(graph, message, body)
    doc_type, invoices, note = invoice_rows_for_message(message, body, pdfs)
    stored_pdfs = [{"name": pdf["name"], "path": pdf["path"], "page_count": len(pdf["pages"])} for pdf in pdfs]
    return {
        "version": CACHE_VERSION,
        "id": message["id"],
        "received": message["received"],
        "sender": message["sender"],
        "subject": message["subject"],
        "folder": message["folder"],
        "categories": message["categories"],
        "doc_type": doc_type,
        "invoices": invoices,
        "pdfs": stored_pdfs,
        "note": note,
        "october": message["received"] >= OCTOBER_FROM,
    }


def mail_vendor(message: dict[str, Any]) -> str:
    return f"{message.get('sender') or ''} {message.get('subject') or ''}"


def supplement_gas(docs: list[dict[str, Any]]) -> None:
    """Gas product invoices print the number beside the account, not alone on a line.

    Statement pages stay statements. A September-received copy, and an Oct 1-3
    packet whose invoice date is September, still has to be checked.
    """
    for doc in docs:
        if "gasandsupply" not in (doc.get("sender") or "").lower() or not doc.get("pdfs"):
            continue
        pages_by_pdf: list[list[str]] = []
        statement_lines: dict[str, dict[str, str]] = {}
        for pdf in doc["pdfs"]:
            path = Path(pdf.get("path") or "")
            pages = audit_pages(path.read_bytes()) if path.is_file() else []
            pages_by_pdf.append(pages)
            for page in pages:
                if not is_aging_statement(page):
                    continue
                for line in gas_statement_lines(page):
                    statement_lines[norm(line["number"])] = line
        seen = {norm(invoice["number"]) for invoice in doc["invoices"]}
        for pdf_index, pages in enumerate(pages_by_pdf):
            for page_index, page in enumerate(pages, start=1):
                if is_aging_statement(page):
                    continue
                for number in gas_header_numbers(page or ""):
                    key = norm(number)
                    if key not in seen:
                        continue
                    for invoice in doc["invoices"]:
                        if norm(invoice["number"]) != key:
                            continue
                        pages_found = invoice.setdefault("pages", [])
                        if page_index not in pages_found:
                            pages_found.append(page_index)
                for date, _account, number in _GAS_PRODUCT.findall(page or ""):
                    key = norm(number)
                    line = statement_lines.get(key, {})
                    subtotal = _GAS_SUBTOTAL.search(page or "")
                    amount = line.get("charge") or (amount_text(subtotal.group(1)) if subtotal else "")
                    invoice_date = line.get("date") or to_iso(date)
                    if key in seen:
                        for invoice in doc["invoices"]:
                            if norm(invoice["number"]) != key:
                                continue
                            pages_found = invoice.setdefault("pages", [])
                            if page_index not in pages_found:
                                pages_found.append(page_index)
                            if invoice.get("pdf_index") is None:
                                invoice["pdf_index"] = pdf_index
                            if not invoice.get("amount") and amount:
                                invoice["amount"] = amount
                            if not invoice.get("invoice date") and invoice_date:
                                invoice["invoice date"] = invoice_date
                        continue
                    doc["invoices"].append(
                        {
                            "number": number,
                            "doc_type": "invoice",
                            "invoice date": invoice_date,
                            "amount": amount,
                            "PO": "",
                            "pdf_index": pdf_index,
                            "pages": [page_index],
                            "source": "pdf",
                            "line_type": line.get("line_type") or "",
                            "extra_note": "Gas product invoice",
                        }
                    )
                    seen.add(key)
        for invoice in doc["invoices"]:
            line = statement_lines.get(norm(invoice["number"]))
            if not line:
                continue
            if line.get("date"):
                invoice["invoice date"] = line["date"]
            invoice["line_type"] = line.get("line_type") or invoice.get("line_type") or ""
            if line.get("line_type") == "PAYMENT":
                invoice["doc_type"] = "other"
                invoice["amount"] = line.get("payment") or ""
                invoice["extra_note"] = "statement payment, not an invoice"
            elif line.get("charge"):
                invoice["amount"] = line["charge"]


# Amounts, dates, and POs read from the page images. Empty fields are filled.
# force_date replaces a date the parser took from a later "payments received" line.
VERIFIED_FACTS: dict[str, dict[str, str]] = {
    "951277": {"amount": "93.13", "extra_note": "invoice date 2026-09-01; due 2026-10-01; total $93.13"},
    "20973449": {
        "invoice date": "2024-03-20",
        "amount": "98.91",
        "force_date": "1",
        "extra_note": "due 2024-03-20; PDF says Total Paid By ACH on 03/20/2024 $98.91; not a September invoice",
    },
    "IN0000183743": {"amount": "1399.24", "PO": "58150", "invoice date": "2026-09-30"},
    "28377": {"amount": "1245.81", "PO": "59225", "invoice date": "2026-09-30", "extra_note": "printed subtotal $1,245.81"},
    "1390049": {"amount": "660.50", "PO": "59332", "invoice date": "2026-09-30"},
    "154258": {"amount": "45.00", "invoice date": "2026-09-25", "extra_note": "this invoice $45.00; customer total balance $90.00"},
    "S1395419.001": {"amount": "19.34", "PO": "59299", "invoice date": "2026-09-29"},
    "S1395419.002": {"amount": "43.61", "PO": "59299", "invoice date": "2026-09-28"},
    "0099474-IN": {"amount": "165.00", "PO": "59295", "invoice date": "2026-09-28"},
    "1474316": {"amount": "194.40", "PO": "59273", "invoice date": "2026-09-29"},
    "1474409": {"amount": "259.20", "PO": "59298", "invoice date": "2026-09-30"},
    "1557568": {
        "amount": "122.94",
        "PO": "59127",
        "invoice date": "2026-09-12",
        "extra_note": "PDF prints 1557568 under INVOICE; subject and ORDER NUMBER say 93669897",
    },
    "93669897": {
        "amount": "122.94",
        "PO": "59127",
        "invoice date": "2026-09-12",
        "extra_note": "subject says Invoice 93669897; PDF prints 1557568 under INVOICE and 93669897 beside ORDER NUMBER",
    },
    "INV11518041": {"extra_note": "inv amt $2,667.01 already paid; total due $0.00"},
    "WB4337861620": {"PO": "59126"},
    "389070": {"PO": "59284"},
    "470223": {"invoice date": "2026-09-16", "PO": "59060", "extra_note": "scanned invoice; no total printed on the saved pages"},
    "470471": {"invoice date": "2026-09-28", "PO": "59216", "extra_note": "scanned invoice; no total printed on the saved pages"},
    "3395995": {"PO": "", "clear_po": "1", "extra_note": "55483 is not the PO; contract 50612580-333237"},
    "PS-INV103344": {"PO": "59088", "extra_note": "external document 59088"},
    "78129701": {"PO": "VENDING/1571"},
    "82970031": {"PO": "VENDING/1575"},
}


def apply_verified_facts(docs: list[dict[str, Any]]) -> None:
    for doc in docs:
        for invoice in doc["invoices"]:
            fact = VERIFIED_FACTS.get(invoice.get("number") or "")
            if not fact:
                continue
            if fact.get("force_date") and fact.get("invoice date"):
                invoice["invoice date"] = fact["invoice date"]
            elif fact.get("invoice date") and not invoice.get("invoice date"):
                invoice["invoice date"] = fact["invoice date"]
            if fact.get("amount") and not invoice.get("amount"):
                invoice["amount"] = fact["amount"]
            if fact.get("clear_po"):
                invoice["PO"] = fact.get("PO") or ""
            elif fact.get("PO") and not invoice.get("PO"):
                invoice["PO"] = fact["PO"]
            if fact.get("extra_note"):
                invoice["extra_note"] = fact["extra_note"]


def restore_line_dates(docs: list[dict[str, Any]]) -> None:
    """Statement-line dates and MSC invoice dates were getting replaced by a nearby due date."""
    for doc in docs:
        if is_gas_force_message(doc["received"], doc["sender"], doc["subject"]) and doc.get("pdfs"):
            blob = "\n".join(
                "\n".join(audit_pages(Path(pdf["path"]).read_bytes()))
                for pdf in doc["pdfs"]
                if Path(pdf["path"]).is_file()
            )
            lines = {norm(line["number"]): line for line in gas_statement_lines(blob)}
            for invoice in doc["invoices"]:
                line = lines.get(norm(invoice["number"]))
                if not line:
                    continue
                if line.get("date"):
                    invoice["invoice date"] = line["date"]
                if line.get("line_type"):
                    invoice["line_type"] = line["line_type"]
                    if line["line_type"] == "PAYMENT":
                        invoice["doc_type"] = "other"
        if "mscdirect.com" not in (doc.get("sender") or "").lower() or not doc.get("pdfs"):
            continue
        blob = "\n".join(
            "\n".join(audit_pages(Path(pdf["path"]).read_bytes()))
            for pdf in doc["pdfs"]
            if Path(pdf["path"]).is_file()
        )
        due = re.search(r"Due Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", blob, flags=re.I)
        due_iso = to_iso(due.group(1) if due else "")
        found = []
        for token in re.findall(r"\b\d{2}/\d{2}/\d{2,4}\b", blob):
            iso = to_iso(token)
            if iso and iso != due_iso and iso.startswith("2026-"):
                found.append(iso)
        invoice_date = found[0] if found else ""
        if not invoice_date:
            continue
        for invoice in doc["invoices"]:
            if invoice.get("invoice date") == due_iso or not invoice.get("invoice date"):
                invoice["invoice date"] = invoice_date
                invoice["date_note"] = f"invoice date {invoice_date}; due {due_iso}" if due_iso else ""


def build_rows(docs: list[dict[str, Any]], index: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, str]], dict[str, int]]:
    rows: list[dict[str, str]] = []
    counts = {
        "messages": len(docs),
        "september_messages": 0,
        "october_messages": 0,
        "october_included": 0,
        "statements": 0,
        "invoice_rows": 0,
        "matched": 0,
        "missing": 0,
    }
    type_counts: dict[str, int] = {}
    for doc in docs:
        if doc["received"] >= OCTOBER_FROM:
            counts["october_messages"] += 1
        else:
            counts["september_messages"] += 1
        type_counts[doc["doc_type"]] = type_counts.get(doc["doc_type"], 0) + 1
        if re.search(r"past due", doc.get("subject") or "", flags=re.I):
            counts["statements"] += 1
            rows.append(blank_row(doc, "statement", "past-due notice; not checked (Treyce)"))
            continue
        if doc["doc_type"] == "statement" and not doc["invoices"]:
            counts["statements"] += 1
            rows.append(blank_row(doc, "statement", "statement; not checked (Treyce)"))
            continue
        if doc["doc_type"] == "statement" and doc["invoices"]:
            counts["statements"] += 1
        kept = [invoice for invoice in doc["invoices"] if include_invoice(doc, invoice)]
        if doc["october"] and kept:
            counts["october_included"] += 1
        if doc["october"] and not kept:
            continue
        if not kept:
            if doc["doc_type"] == "statement":
                continue
            note = doc.get("note") or ""
            if doc["doc_type"] != "invoice":
                note = "; ".join(part for part in (note, "no invoice number") if part)
            rows.append(blank_row(doc, doc["doc_type"], note))
            continue
        vendor = mail_vendor(doc)
        for invoice in kept:
            bill, match_note = match_bill(index, invoice["number"], vendor)
            notes = [
                part
                for part in (
                    doc.get("note") or "",
                    invoice.get("form_note") or "",
                    invoice.get("date_note") or "",
                    invoice.get("extra_note") or "",
                    match_note,
                    f"source {invoice.get('source') or ''}",
                )
                if part
            ]
            if invoice.get("source") == "kyle-list" or norm(invoice["number"]) in {norm(number) for number in GAS_FORCE} and is_gas_force_message(doc["received"], doc["sender"], doc["subject"]):
                line_type = invoice.get("line_type") or ""
                notes.append("10/1 Gas number Kyle asked to check" + (f"; statement line {line_type}" if line_type else ""))
            if doc["doc_type"] == "statement" and invoice.get("source") == "kyle-list":
                notes.append("printed on a statement")
            row = {
                "received time": doc["received"],
                "sender": doc["sender"],
                "subject": doc["subject"],
                "folder": doc["folder"],
                "categories": doc["categories"],
                "doc type": invoice.get("doc_type") or doc["doc_type"],
                "invoice #": invoice["number"],
                "invoice date": invoice.get("invoice date") or "",
                "amount": invoice.get("amount") or "",
                "PO": invoice.get("PO") or "",
                "KIMCO bill id or MISSING": bill,
                "notes": "; ".join(notes),
            }
            rows.append(row)
            if invoice.get("doc_type") == "invoice" or doc["doc_type"] == "invoice":
                counts["invoice_rows"] += 1
            if bill == "MISSING":
                counts["missing"] += 1
            else:
                counts["matched"] += 1
        if doc["doc_type"] == "statement" and not any(invoice.get("source") == "kyle-list" for invoice in kept):
            counts["statements"] += 0
    counts["doc_types"] = type_counts  # type: ignore[assignment]
    rows.sort(key=lambda row: (row["received time"], row["invoice #"], row["subject"]))
    return rows, counts


def blank_row(doc: dict[str, Any], doc_type: str, note: str) -> dict[str, str]:
    return {
        "received time": doc["received"],
        "sender": doc["sender"],
        "subject": doc["subject"],
        "folder": doc["folder"],
        "categories": doc["categories"],
        "doc type": doc_type,
        "invoice #": "",
        "invoice date": "",
        "amount": "",
        "PO": "",
        "KIMCO bill id or MISSING": "",
        "notes": note,
    }


def pages_containing(path: str, number: str) -> list[int]:
    if not path or not Path(path).is_file():
        return []
    pages = audit_pages(Path(path).read_bytes())
    found = []
    for index, page in enumerate(pages, start=1):
        if re.search(rf"(?<![A-Z0-9]){re.escape(number)}(?![A-Z0-9])", page or "", flags=re.I):
            found.append(index)
    return found


def render_missing(docs: list[dict[str, Any]], rows: list[dict[str, str]]) -> list[str]:
    import fitz

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.png"):
        old.unlink()
    by_id = {doc["id"]: doc for doc in docs}
    saved: list[str] = []
    missing_rows = [row for row in rows if row["KIMCO bill id or MISSING"] == "MISSING" and row["invoice #"]]
    seen_pages: set[str] = set()
    for row in missing_rows:
        doc = next(
            (
                item
                for item in docs
                if item["received"] == row["received time"]
                and item["sender"] == row["sender"]
                and item["subject"] == row["subject"]
                and any(norm(invoice["number"]) == norm(row["invoice #"]) for invoice in item["invoices"])
            ),
            None,
        )
        if doc is None:
            continue
        slug = re.sub(r"[^A-Za-z0-9]+", "-", row["invoice #"]).strip("-")[:40]
        wrote = False
        targets: list[tuple[int, list[int]]] = []
        for invoice in doc["invoices"]:
            if norm(invoice["number"]) != norm(row["invoice #"]):
                continue
            pdf_index = invoice.get("pdf_index")
            if pdf_index is None:
                for index, pdf in enumerate(doc["pdfs"]):
                    pages = pages_containing(pdf["path"], row["invoice #"])
                    if pages:
                        targets.append((index, pages))
            elif pdf_index < len(doc["pdfs"]):
                targets.append((pdf_index, invoice.get("pages") or []))
        if not targets and doc.get("pdfs"):
            targets = [(index, []) for index, _pdf in enumerate(doc["pdfs"])]
        for pdf_index, wanted_pages in targets:
            path = Path(doc["pdfs"][pdf_index]["path"])
            if not path.is_file():
                continue
            document = fitz.open(path)
            wanted = wanted_pages or list(range(1, document.page_count + 1))
            for page_number in wanted:
                key = f"{slug}-{hashlib.sha256(doc['id'].encode()).hexdigest()[:12]}-{page_number}"
                if key in seen_pages or page_number < 1 or page_number > document.page_count:
                    continue
                seen_pages.add(key)
                pixmap = document[page_number - 1].get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
                target = OUT_DIR / f"{slug}-p{page_number}.png"
                if target.exists():
                    target = OUT_DIR / f"{slug}-{hashlib.sha256(doc['id'].encode()).hexdigest()[:6]}-p{page_number}.png"
                pixmap.save(str(target))
                saved.append(str(target.relative_to(ROOT)))
                wrote = True
        if not wrote:
            LOGGER.info("No page image for missing invoice %s", row["invoice #"])
        by_id.setdefault(doc["id"], doc)
    return saved


def redact_text(text: str) -> str:
    """Login usernames are configured secrets. Keep them out of the committed CSV."""
    replacements: list[tuple[str, str]] = []
    for name, label in (
        ("OUTLOOK_AP_USERNAME", "outlook-ap-user"),
        ("KIMCO_LIVE_USERNAME", "kimco-user"),
        ("KIMCO_PROTOTYPE_USERNAME", "kimco-user"),
    ):
        value = os.environ.get(name) or ""
        if value:
            replacements.append((value, label))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for value, label in replacements:
        text = text.replace(value, label)
    return text


def write_csv(rows: list[dict[str, str]]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    OUT_CSV.write_text(redact_text(buffer.getvalue()))


def self_check() -> None:
    assert to_iso("09/15/26") == "2026-09-15"
    assert to_iso("2026-09-02T00:00:00") == "2026-09-02"
    assert vendor_compatible("Ryerson", "1150-RYERSON, JOSEPH T. & SON") is True
    assert vendor_compatible("Capital Machine", "LEGACY WIRE PRODUCTS") is False
    assert is_archive_path("Inbox/9 - FORT WORTH ARCHIVE")
    assert not is_archive_path("Inbox")
    assert is_gas_force_message(GAS_RECEIVED, "billing@gasandsupply.com", "Gas&Supply Invoice/Statement")
    september = {"received": "2026-09-11T19:36:12Z", "sender": "a@b.com", "subject": "Invoice"}
    october = {"received": "2026-10-02T12:00:00Z", "sender": "a@b.com", "subject": "Invoice"}
    assert include_invoice(september, {"number": "1", "invoice date": "2026-08-01", "source": "pdf"})
    assert include_invoice(october, {"number": "1", "invoice date": "2026-09-30", "source": "pdf"})
    assert not include_invoice(october, {"number": "1", "invoice date": "2026-10-01", "source": "pdf"})
    gas = {
        "received": GAS_RECEIVED,
        "sender": "billing@gasandsupply.com",
        "subject": "Gas&Supply Invoice/Statement",
    }
    assert include_invoice(gas, {"number": GAS_FORCE[0], "invoice date": "2024-01-01", "source": "kyle-list"})
    numbers = extract_numbers("INVOICE NUMBER T609053432\nInvoice # 2216", gas=False, oneal=False)
    assert "T609053432" in numbers
    assert "2216" in numbers
    assert is_aging_statement("STATEMENT\n1 to 30 DAYS\nTOTAL BALANCE\n08/31/26 0040188343 INVOICE")
    assert page_kind("STATEMENT\n1 to 30 DAYS\nTOTAL BALANCE", "billing.pdf") == "statement"
    rental = "CYLINDER RENTAL INVOICE\n                                                                              0040401083\n08/28/26 0040392829\n"
    assert gas_header_numbers(rental) == ["0040401083"]
    assert extract_numbers(rental, gas=True, oneal=False) == ["0040401083"]
    product = "09/30/26   A3050      0040462826\nSubtotal                    704.00"
    assert _GAS_PRODUCT.findall(product) == [("09/30/26", "A3050", "0040462826")]
    lines = gas_statement_lines(
        "07/13/26      0000018220 PAYMENT                    .00      643.22-        .00      643.22-\n"
        "09/30/26      0040472242 CYL RENT              1,563.16         .00         .00    1,563.16\n"
        "09/30/26      0040460840 INVOICE    11117957     296.76         .00         .00      296.76"
    )
    by_number = {row["number"]: row for row in lines}
    assert by_number["0000018220"]["line_type"] == "PAYMENT"
    assert by_number["0000018220"]["payment"] == "643.22"
    assert by_number["0040472242"]["charge"] == "1563.16"
    assert by_number["0040460840"]["charge"] == "296.76"
    LOGGER.info("Self-check passed")


def main() -> None:
    self_check()
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    client = login()
    install_401_guard(client)
    refuse_writes(client)
    index = load_index(client)
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    graph_readonly(graph)
    messages = load_messages(graph)
    prior = load_any_docs()
    done = {
        key: row
        for key, row in prior.items()
        if row.get("version") == CACHE_VERSION and "could not open" not in (row.get("note") or "")
    }
    LOGGER.info("Docs already extracted %s of %s", len(done), len(messages))
    if len(done) < len(messages):
        backup = Path("/tmp/sept-missed-docs-v1.jsonl")
        if DOC_CACHE.is_file() and not backup.exists():
            backup.write_text(DOC_CACHE.read_text())
        DOC_CACHE.write_text("")
        for row in done.values():
            append_doc(row)
    for index_number, message in enumerate(messages, start=1):
        if message["id"] in done:
            continue
        reused = reuse_pdfs(prior.get(message["id"]))
        try:
            if reused is not None:
                doc = extract_from_pdfs(message, reused, prior.get(message["id"], {}).get("note") or "")
            else:
                doc = extract_message(graph, message)
        except Exception as exc:
            LOGGER.info("Message %s failed: %s", index_number, exc.__class__.__name__)
            doc = {
                "version": CACHE_VERSION,
                "id": message["id"],
                "received": message["received"],
                "sender": message["sender"],
                "subject": message["subject"],
                "folder": message["folder"],
                "categories": message["categories"],
                "doc_type": "other",
                "invoices": [],
                "pdfs": [],
                "note": f"could not open ({exc.__class__.__name__})",
                "october": message["received"] >= OCTOBER_FROM,
            }
        append_doc(doc)
        done[message["id"]] = doc
        if index_number % 25 == 0:
            LOGGER.info("Opened %s of %s", index_number, len(messages))
    docs = [done[message["id"]] for message in messages if message["id"] in done]
    restore_line_dates(docs)
    supplement_gas(docs)
    apply_verified_facts(docs)
    rows, counts = build_rows(docs, index)
    images = render_missing(docs, rows)
    write_csv(rows)
    summary = {
        "counts": counts,
        "missing": [row for row in rows if row["KIMCO bill id or MISSING"] == "MISSING"],
        "images": images,
        "statements": counts["statements"],
    }
    SUMMARY.write_text(redact_text(json.dumps(summary, indent=2)))
    LOGGER.info(
        "Messages %s september %s october %s statements %s invoice rows %s matched %s missing %s images %s",
        counts["messages"],
        counts["september_messages"],
        counts["october_messages"],
        counts["statements"],
        counts["invoice_rows"],
        counts["matched"],
        counts["missing"],
        len(images),
    )


if __name__ == "__main__":
    main()
