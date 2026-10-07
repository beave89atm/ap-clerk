"""Read-only check for statement 15409416 and non-invoices in 10465-10512.

No KIMCO writes. No mail changes.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.pdf_invoice import classify_pdf_page
from ap_clerk.rules import lookup_text, money
from scripts.sept25_30_attachment_audit import (
    attachment_bytes,
    attachment_name,
    numbers_on_page,
    page_texts,
)
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login, slim_bill

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("statement-check")

OUT = ROOT / "runs" / "statement-15409416-check.json"
BILL_IDS = list(range(10465, 10513))
LETTER_RE = re.compile(
    r"payment[\s-]*status|pending\s+orders|payment\s+inquiry|"
    r"this\s+is\s+not\s+an\s+invoice|not\s+an\s+invoice|"
    r"statement\s+of\s+account|account\s+statement",
    flags=re.I,
)
MENTION_RE = re.compile(
    r'data-mention-id="(\d+)"[^>]*data-mention-name="([^"]*)"',
    flags=re.I,
)


def slim_lookup(value: Any) -> Any:
    if isinstance(value, dict):
        return {"id": value.get("id"), "text": value.get("text") or value.get("name")}
    return value


def comment_fact(comment: dict[str, Any]) -> dict[str, Any]:
    values = comment.get("values") if isinstance(comment.get("values"), dict) else {}
    html = str(values.get("HtmlValue") or "")
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    mentions = [
        {"id": int(match.group(1)), "name": match.group(2)}
        for match in MENTION_RE.finditer(html)
    ]
    author = None
    author_fields: dict[str, Any] = {}
    for key in ("CreatorId", "ModifierId", "CreatedOn", "ModifiedOn"):
        if key not in values:
            continue
        value = values[key]
        author_fields[key] = slim_lookup(value)
        if key == "CreatorId" and isinstance(value, dict):
            author = {
                "id": value.get("id"),
                "name": value.get("text") or value.get("name"),
                "field": "CreatorId",
            }
    return {
        "id": comment.get("id"),
        "author": author,
        "author_fields": author_fields,
        "text": plain,
        "mentions": mentions,
    }


def document_class(pages: list[dict[str, Any]]) -> str:
    classes = [page["class"] for page in pages]
    blob = "\n".join(page.get("text") or "" for page in pages)
    # Judge the heading before classify_pdf_page. O'Neal payment-status and
    # pending-order letters contain an "Invoice No." column, and several real
    # invoices (AQPC, Ryerson, Capital, Trace Metal) come back as "other".
    if re.search(r"Please provide payment status on the below listed invoices", blob, flags=re.I):
        return "statement"
    if re.search(r"PENDING ORDERS", blob) and re.search(r"payment status", blob, flags=re.I):
        return "letter"
    if re.search(r"(?m)^INVOICE\b", blob) or re.search(
        r"Invoice\s+#\s*\S|Invoice\s+no\.?:|Invoice\s+PS-INV|Invoice\s+9\d{6,}",
        blob,
    ):
        return "invoice"
    if re.search(r"Reg Bill:", blob) and re.search(r"WeekWorked", blob):
        return "invoice"
    if any(item == "statement" for item in classes) or LETTER_RE.search(blob):
        if any(item == "invoice" for item in classes) and not LETTER_RE.search(blob):
            return "mixed"
        if LETTER_RE.search(blob) and not any(item == "invoice" for item in classes):
            return "statement" if re.search(r"statement", blob, flags=re.I) else "letter"
        if any(item == "statement" for item in classes) and not any(item == "invoice" for item in classes):
            return "statement"
    if classes and all(item == "invoice" for item in classes):
        return "invoice"
    if any(item == "invoice" for item in classes):
        return "invoice"
    if not pages:
        return "no-attachment"
    if any(not (page.get("text") or "").strip() for page in pages):
        return "unreadable"
    return "other"


def main() -> None:
    client = login()
    install_401_guard(client)
    bills = []
    for bill_id in BILL_IDS:
        try:
            record = client.get_item("ap_invoices", bill_id)
        except Exception as exc:
            if "HTTP 404" not in str(exc):
                raise
            bills.append({"id": bill_id, "missing": True, "document_class": "missing"})
            LOGGER.info("Bill %s missing", bill_id)
            continue
        slim = slim_bill(record)
        comments = [
            comment_fact(comment)
            for comment in (record.get("lists") or {}).get("Comments_1") or []
            if isinstance(comment, dict)
        ]
        attachments = []
        known = {str(slim.get("invoice") or "")}
        for item in client.list_attachments(bill_id):
            content = attachment_bytes(client, bill_id, item)
            name = attachment_name(item)
            pages = []
            if content and content[:5] == b"%PDF-":
                for index, text in enumerate(page_texts(content), start=1):
                    pages.append(
                        {
                            "page": index,
                            "class": classify_pdf_page(text),
                            "invoice_numbers": numbers_on_page(text, known, str(slim.get("invoice") or "")),
                            "text": text,
                        }
                    )
            attachments.append(
                {
                    "id": item.get("id"),
                    "filename": name,
                    "page_count": len(pages),
                    "pages": [
                        {
                            "page": page["page"],
                            "class": page["class"],
                            "invoice_numbers": page["invoice_numbers"],
                        }
                        for page in pages
                    ],
                    "text_excerpt": "\n".join(page["text"] for page in pages)[:1200],
                }
            )
        excerpt = "\n".join(attachment["text_excerpt"] for attachment in attachments)
        kind = document_class(
            [
                {"class": page["class"], "text": excerpt}
                for attachment in attachments
                for page in attachment["pages"]
            ]
        )
        if LETTER_RE.search(excerpt) and kind in {"other", "unreadable"}:
            kind = "statement" if re.search(r"\bstatement\b", excerpt, flags=re.I) else "letter"
        if re.search(r"\bstatement\b", excerpt, flags=re.I) and kind in {"other", "letter"}:
            kind = "statement"
        bills.append(
            {
                "id": bill_id,
                "invoice": slim.get("invoice"),
                "vendor": lookup_text(slim.get("vendor")) if isinstance(slim.get("vendor"), dict) else slim.get("vendor"),
                "batch": slim.get("batch"),
                "type": slim.get("type"),
                "posted": slim.get("posted"),
                "po": slim.get("po"),
                "invoice_amount": money(slim.get("invoice_amount")),
                "verification": money(slim.get("verification")),
                "net": money(slim.get("net")),
                "lines": slim.get("lines"),
                "charges": slim.get("charges"),
                "taxes": slim.get("taxes"),
                "comments": comments,
                "attachments": [
                    {key: value for key, value in attachment.items() if key != "text_excerpt"}
                    for attachment in attachments
                ],
                "document_class": kind,
                "text_excerpt": excerpt[:1500],
            }
        )
        LOGGER.info("Bill %s %s class %s comments %s", bill_id, slim.get("invoice"), kind, len(comments))

    oneal = [bill for bill in bills if bill["id"] in {10475, 10477}]
    target = next((bill for bill in oneal if str(bill["invoice"]) == "15409416"), None)
    other = next((bill for bill in oneal if bill is not target), None)
    non_invoices = [
        {
            "id": bill["id"],
            "invoice": bill["invoice"],
            "vendor": bill["vendor"],
            "document_class": bill["document_class"],
            "batch": (bill.get("batch") or {}).get("text"),
        }
        for bill in bills
        if not bill.get("missing") and bill.get("document_class") not in {"invoice"}
    ]
    payload = {
        "invoice_15409416": target,
        "other_oneal_letter_bill": other,
        "non_invoices_10465_10512": non_invoices,
        "bills_checked": len(bills),
        "bills": [
            {
                "id": bill["id"],
                "invoice": bill.get("invoice"),
                "vendor": bill.get("vendor"),
                "document_class": bill.get("document_class"),
                "batch_id": (bill.get("batch") or {}).get("id"),
                "posted": bill.get("posted"),
                "missing": bool(bill.get("missing")),
            }
            for bill in bills
        ],
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str))
    LOGGER.info("Non-invoices %s", [row["id"] for row in non_invoices])


if __name__ == "__main__":
    main()
