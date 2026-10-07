"""Read-only review of the 11 non-statement MOVE emails from the 2026-10-07 v2 dry run.

No mail moves, no category changes, no KIMCO writes. One API Agent sign-in.
Renders each email PDF and each KIMCO attachment to page images and writes
runs/review11-2026-10-07/facts.json for the review sheet.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials
from ap_clerk.pdf_invoice import _INV_LABEL, gas_invoice_numbers
from ap_clerk.pdf_links import download_first_public_pdf
from scripts.sept25_30_attachment_audit import attachment_bytes, attachment_name, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_archive_check_2026_10_07 import (
    folder_path,
    norm,
    plain,
    sender_of,
)
from scripts.sept_archive_check_v2_2026_10_07 import contains_invoice, refuse_writes

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("review11")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)
logging.getLogger("pypdf").setLevel(logging.ERROR)

OUT = ROOT / "runs" / "review11-2026-10-07"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,hasAttachments,bodyPreview,body"

TARGETS = [
    {"bill": 10476, "received": "2026-07-31T05:11:01Z", "subject": "A. M. Castle & Co. Payment Inquiry: KANNON MFG INC-FT WORTH #32279"},
    {"bill": 10468, "received": "2026-09-02T18:53:07Z", "subject": "Invoice 00044427"},
    {"bill": 10470, "received": "2026-09-05T06:29:32Z", "subject": "PO 59064 Inv# 9307057099"},
    {"bill": 10358, "received": "2026-09-08T16:49:17Z", "subject": "Invoice from Greentree Packaging & Lumber"},
    {"bill": 10013, "received": "2026-09-11T18:39:08Z", "subject": "New payment request from AMERICAN QUALITY POWDER COATING - invoice 10991"},
    {"bill": 10471, "received": "2026-09-11T19:11:40Z", "subject": "Capital Machine - Sales Invoice PS-INV103317 - KANNON MANUFACTURING - AMTECH"},
    {"bill": 10382, "received": "2026-09-11T19:36:12Z", "subject": "New payment request from Green Valley Compressor LLC - invoice 2216"},
    {"bill": 10009, "received": "2026-09-15T18:10:45Z", "subject": "New payment request from AMERICAN QUALITY POWDER COATING - invoice 11003"},
    {"bill": 10107, "received": "2026-09-16T20:15:30Z", "subject": "JP Steel Invoice# (125315) Transmission for KANNON MFG"},
    {"bill": 10280, "received": "2026-09-21T18:59:20Z", "subject": "Legacy Wire Products - Sales Invoice PS-INV104046"},
    {"bill": 10275, "received": "2026-09-22T13:58:43Z", "subject": "Legacy Wire Products - Sales Invoice PS-INV104051"},
]

EXTRA_NUMBER = re.compile(
    r"\b(?:PS-INV\d{5,}|PSI-\d{6,}|TXFT\d{5,}|WB\d{6,}|00\d{8}|\d{6,}(?:/\d+){1,3}|[A-Z]{1,4}\d{6,})\b",
    flags=re.I,
)


def full_notes(record: dict[str, Any]) -> list[str]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        if not isinstance(comment, dict):
            continue
        values = comment.get("values") or {}
        rows.append((str(values.get("CreatedOn") or ""), int(comment.get("id") or 0), plain(str(values.get("HtmlValue") or ""))))
    rows.sort()
    return [text for _, _, text in rows if text]


def bill_facts(record: dict[str, Any]) -> dict[str, Any]:
    from scripts.sept_archive_check_2026_10_07 import compact, disposition
    from scripts.sept_archive_check_v2_2026_10_07 import hold_note_yn, money_gap, needs_issue_note

    row = compact(record)
    row["notes"] = full_notes(record)
    return {
        "id": row["id"],
        "invoice": row["invoice"],
        "vendor": row["vendor"],
        "invoice_date": row["invoice_date"],
        "batch": row["batch"],
        "posted": bool(row["posted"]),
        "verification": row["verification"],
        "covered": row["covered"],
        "gap": money_gap(row),
        "status": disposition(row),
        "hold_note": hold_note_yn(row),
        "needs_issue_note": needs_issue_note(row),
        "comments": row["comments"],
        "notes": row["notes"],
        "line_count": len(((record.get("lists") or {}).get("APInvoiceLine") or [])),
    }


def labeled_numbers(text: str) -> list[str]:
    found: list[str] = []

    def add(token: str) -> None:
        token = str(token or "").strip().strip(" .,#")
        if token and token not in found and token != "14748440":
            found.append(token)

    for number in gas_invoice_numbers(text or ""):
        add(number)
    for match in _INV_LABEL.finditer(text or ""):
        add(match.group(1))
    for match in EXTRA_NUMBER.finditer(text or ""):
        add(match.group(0))
    return found


def render_pdf(content: bytes, stem: str) -> list[str]:
    import fitz

    document = fitz.open(stream=content, filetype="pdf")
    saved: list[str] = []
    for index, page in enumerate(document, start=1):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
        path = OUT / f"{stem}-p{index}.png"
        pixmap.save(str(path))
        saved.append(str(path.relative_to(ROOT)))
    return saved


def graph_readonly(graph: GraphClient) -> None:
    guarded = graph.request

    def readonly(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Refusing Graph {method}. Review only.")
        return guarded(method, url, **kwargs)

    graph.request = readonly  # type: ignore[method-assign]


def find_message(graph: GraphClient, target: dict[str, Any], days: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    day = target["received"][:10]
    if day not in days:
        days[day] = graph.list_messages(
            ALLOWED_MAILBOX,
            received_from=date.fromisoformat(day),
            received_to=date.fromisoformat(day),
        )
        LOGGER.info("Day %s has %s messages", day, len(days[day]))
    wanted_subject = plain(target["subject"])
    matches = []
    for message in days[day]:
        if str(message.get("receivedDateTime") or "") != target["received"]:
            continue
        if plain(str(message.get("subject") or "")) != wanted_subject:
            continue
        matches.append(message)
    if len(matches) != 1:
        raise SystemExit(f"Bill {target['bill']} matched {len(matches)} messages at {target['received']}")
    return graph.get_message(ALLOWED_MAILBOX, str(matches[0]["id"]), select=SELECT)


def email_pdfs(graph: GraphClient, message: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    found: list[tuple[str, bytes, str]] = []
    for name, content in graph.download_pdf_attachments(ALLOWED_MAILBOX, str(message.get("id") or "")):
        found.append((name, content, "attachment"))
    if found:
        return found
    body = ((message.get("body") or {}).get("content") or "") + "\n" + str(message.get("bodyPreview") or "")
    public = download_first_public_pdf(body)
    if public.get("ok") and public.get("content"):
        found.append(("linked.pdf", public["content"], "public-link"))
    return found


def document_of(content: bytes, name: str, source: str) -> dict[str, Any]:
    text = "\n".join(page_texts(content))
    return {
        "name": name,
        "source": source,
        "bytes": len(content),
        "text": text,
        "numbers": labeled_numbers(text),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = login()
    install_401_guard(client)
    refuse_writes(client)
    listed = client.list_items("ap_invoices")
    by_invoice: dict[str, list[int]] = {}
    for item in listed:
        number = str((item.get("values") or {}).get("Invoice_Number") or "")
        if not number:
            continue
        by_invoice.setdefault(norm(number), []).append(int(item["id"]))
    LOGGER.info("Invoice index %s", len(by_invoice))

    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    graph_readonly(graph)
    days: dict[str, list[dict[str, Any]]] = {}
    folders: dict[str, str] = {}
    reviews = []
    for target in TARGETS:
        bill_id = int(target["bill"])
        record = client.get_item("ap_invoices", bill_id)
        facts = bill_facts(record)
        message = find_message(graph, target, days)
        folder = folder_path(graph, str(message.get("parentFolderId") or ""), folders)
        mail_docs = []
        for index, (name, content, source) in enumerate(email_pdfs(graph, message), start=1):
            stem = f"{bill_id}-email" if index == 1 else f"{bill_id}-email-{index}"
            images = render_pdf(content, stem)
            doc = document_of(content, name, source)
            doc["pages"] = images
            mail_docs.append(doc)
            LOGGER.info("Bill %s email pdf %s pages=%s", bill_id, name, len(images))
        kimco_docs = []
        for index, item in enumerate(client.list_attachments(bill_id), start=1):
            name = attachment_name(item) or f"attachment-{index}"
            content = attachment_bytes(client, bill_id, item)
            if not content or content[:5] != b"%PDF-":
                kimco_docs.append({"name": name, "source": "kimco", "pages": [], "text": "", "numbers": [], "bytes": len(content or b"")})
                continue
            images = render_pdf(content, f"{bill_id}-kimco" if index == 1 else f"{bill_id}-kimco-{index}")
            doc = document_of(content, name, "kimco")
            doc["pages"] = images
            kimco_docs.append(doc)
            LOGGER.info("Bill %s kimco pdf %s pages=%s", bill_id, name, len(images))
        own = facts["invoice"]
        email_numbers = []
        for doc in mail_docs:
            for number in doc["numbers"]:
                if number not in email_numbers:
                    email_numbers.append(number)
        if own not in email_numbers and any(contains_invoice(doc["text"], own) for doc in mail_docs):
            email_numbers.insert(0, own)
        number_bills = []
        for number in email_numbers:
            hits = by_invoice.get(norm(number), [])
            number_bills.append({"number": number, "bills": hits})
        kimco_numbers = []
        for doc in kimco_docs:
            for number in doc["numbers"]:
                if number not in kimco_numbers:
                    kimco_numbers.append(number)
        reviews.append(
            {
                "bill": bill_id,
                "received": str(message.get("receivedDateTime") or ""),
                "sender": sender_of(message),
                "subject": plain(str(message.get("subject") or "")),
                "folder": folder,
                "categories": message.get("categories") or [],
                "body_preview": plain(str(message.get("bodyPreview") or ""))[:500],
                "email_pdf_count": len(mail_docs),
                "kimco": facts,
                "email_numbers": number_bills,
                "kimco_attachment_numbers": kimco_numbers,
                "email_text": "\n\n".join(doc["text"][:8000] for doc in mail_docs),
                "kimco_text": "\n\n".join(doc["text"][:8000] for doc in kimco_docs),
                "email_pages": [page for doc in mail_docs for page in doc["pages"]],
                "kimco_pages": [page for doc in kimco_docs for page in doc["pages"]],
                "email_files": [doc["name"] for doc in mail_docs],
                "kimco_files": [doc["name"] for doc in kimco_docs],
            }
        )
    # Drop the long text from the committed facts copy after writing a review file.
    Path("/tmp/review11-facts.json").write_text(json.dumps(reviews, indent=2))
    LOGGER.info("Wrote facts for %s emails", len(reviews))


if __name__ == "__main__":
    main()
