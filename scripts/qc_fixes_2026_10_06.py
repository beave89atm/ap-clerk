"""Apply Kyle's five approved QC fixes. Does not post, close a batch, or email.

One API Agent sign-in. A failed password is not retried. A later 401 signs
in once more and never a third time. A bill that is posted or whose invoice
number does not match is skipped. One new Comments_1 note per noted bill,
written only after that bill's final coding and attachment state is known,
and re-read before any batch move.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pypdf import PdfReader, PdfWriter

from ap_clerk.kimco import PPV_CHARGE_LOOKUP_ID, added_comment_payload
from ap_clerk.receiving_owners import PEOPLE
from ap_clerk.rules import SHAWN_MENTION_HTML, TREYCE_MENTION_HTML, money
from scripts.sept25_30_attachment_audit import (
    attachment_bytes,
    attachment_name,
    numbers_on_page,
    page_texts,
    same_invoice,
)
from scripts import sept25_30_finish_2026_10_06 as finish

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("qc-fixes")

TRANSFER_ID = 375
TRANSFER_NAME = "TRANSFER AP"
SEPT_BATCH = 747
OUT = ROOT / "runs" / "qc-fixes-2026-10-06.json"
PDF_DIR = ROOT / "runs" / "qc-fixes-attachments"
GAS_PDF = ROOT / "runs" / "qc2530-missing3" / "Gas_2026-09-29T04:48:59Z.pdf"
EMJ_PDF = ROOT / "runs" / "qc2530-missing3" / "EMJ_2026-09-30T03:11:51Z.pdf"
ONEAL_PDF = ROOT / "runs" / "qc2530-missing3" / "ONeal_2026-10-01T04:29:10Z.pdf"

GAS_KNOWN = {"0040455879", "0040455916", "0040455615", "0040455291"}
EMJ_KNOWN = {"T609053432", "T609084432"}
ONEAL_KNOWN = {
    "15486572",
    "15486598",
    "15486600",
    "15486770",
    "15486823",
    "15486842",
    "15487245",
}

NOTE_10477 = (
    "AP Clerk: @Treyce Hodges this was my mistake. This O'Neal document is a "
    "pending-orders letter, not an invoice, and I entered it as a bill in error. "
    "Please delete this bill. @Shawn McKibben please disregard my earlier note on "
    "this one; no receiving action needed."
)
NOTE_10494 = (
    "AP Clerk: @Shawn McKibben please disregard my earlier note on T609053432. "
    "23.12 ft is the 276 in on receipt 24880; selected with a $2.51 PPV. "
    "No action needed. @Treyce Hodges ready to process, not posted."
)
NOTE_10493 = (
    "AP Clerk: @Shawn McKibben please disregard my earlier note on 216055. "
    "The invoice 1 piece / 272.66 lb matches receipt 25161 in inches; dollars match "
    "with a $0 PPV. No action needed. @Treyce Hodges ready to process, not posted."
)
NOTE_10503 = (
    "AP Clerk: @Shawn McKibben please disregard my earlier note on S816901432. "
    "The invoice 138 lb matches receipt 25160 in inches; dollars match with a "
    "$2.41 PPV. No action needed. @Treyce Hodges ready to process, not posted."
)
NOTE_10480 = (
    "AP Clerk: @Shawn McKibben correction to my earlier note: this isn't a price "
    "mismatch. The $213.93 MLW49-22-4170 hole saw kit is not on PO 59081 at all "
    "(all 23 lines are other items, fully received). Please add or receive it on a "
    "PO, or tell AP how to code it. On hold in Transfer AP, not posted."
)
NOTE_10468 = (
    "AP Clerk: @Shawn McKibben Tricor invoice 000444279/1/202659035 ($3,552.19) "
    "has no matching PO; only the $212.19 freight is entered. Please create or "
    "point AP at the PO. On hold in Transfer AP, not posted."
)
NOTE_10509 = (
    "AP Clerk: @Shawn McKibben Anthony's KIMCO mention id was not found, so this "
    "is to you. No open receipt on PO 59293 matches the lines on O'Neal invoice "
    "15486842 ($5,328.46) within $75. Please receive or correct it. On hold in "
    "Transfer AP, not posted."
)

MENTION_RE = re.compile(
    r'data-mention-id="(\d+)"[^>]*data-mention-name="([^"]*)"',
    flags=re.I,
)
SLICES = {
    10478: {"src": GAS_PDF, "pages": [0], "invoice": "0040455879", "known": GAS_KNOWN},
    10479: {"src": GAS_PDF, "pages": [1], "invoice": "0040455916", "known": GAS_KNOWN},
    10480: {"src": GAS_PDF, "pages": [2], "invoice": "0040455615", "known": GAS_KNOWN},
    10494: {"src": EMJ_PDF, "pages": [0], "invoice": "T609053432", "known": EMJ_KNOWN},
    10504: {"src": ONEAL_PDF, "pages": [0], "invoice": "15486572", "known": ONEAL_KNOWN},
    10505: {"src": ONEAL_PDF, "pages": [1], "invoice": "15486598", "known": ONEAL_KNOWN},
    10506: {"src": ONEAL_PDF, "pages": [2], "invoice": "15486600", "known": ONEAL_KNOWN},
    10507: {"src": ONEAL_PDF, "pages": [3], "invoice": "15486770", "known": ONEAL_KNOWN},
    10508: {"src": ONEAL_PDF, "pages": [4], "invoice": "15486823", "known": ONEAL_KNOWN},
    10509: {"src": ONEAL_PDF, "pages": [5, 6], "invoice": "15486842", "known": ONEAL_KNOWN},
}


class BillAbort(RuntimeError):
    def __init__(self, message: str, before: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.before = before


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def plain(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def unposted(value: Any) -> bool:
    return value in (None, "", False)


def as_float(value: Any) -> float | None:
    if value in (None, "", False) or isinstance(value, (dict, list)):
        return None
    return float(value)


def invoice_hits(text: str, known: set[str], own: str) -> list[str]:
    found = list(numbers_on_page(text or "", known, own))
    for number in sorted(known, key=len, reverse=True):
        if number in found:
            continue
        if re.search(rf"(?<![A-Z0-9]){re.escape(number)}(?![A-Z0-9])", text or "", flags=re.I):
            found.append(number)
    return found


def only_own(numbers: list[str], own: str) -> bool:
    return bool(numbers) and all(same_invoice(number, own) for number in numbers)


def slice_bytes(src: Path, pages: list[int]) -> bytes:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for index in pages:
        writer.add_page(reader.pages[index])
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def describe_pdf(content: bytes, invoice: str, known: set[str]) -> dict[str, Any]:
    pages = page_texts(content)
    per_page = []
    combined: list[str] = []
    for index, text in enumerate(pages, start=1):
        hits = invoice_hits(text, known, invoice)
        per_page.append({"page": index, "invoices": hits})
        for hit in hits:
            if hit not in combined:
                combined.append(hit)
    return {"page_count": len(pages), "pages": per_page, "invoices": combined, "text_ok": only_own(combined, invoice)}


def build_pdfs() -> dict[int, dict[str, Any]]:
    built: dict[int, dict[str, Any]] = {}
    for bill_id, spec in SLICES.items():
        src = spec["src"]
        if not src.is_file():
            raise SystemExit(f"Source PDF missing: {src.name}")
        source_pages = page_texts(src.read_bytes())
        if max(spec["pages"]) >= len(source_pages):
            raise SystemExit(f"{src.name} has {len(source_pages)} pages, short of the slice for {bill_id}.")
        for index in spec["pages"]:
            hits = invoice_hits(source_pages[index], spec["known"], spec["invoice"])
            if not only_own(hits, spec["invoice"]):
                raise SystemExit(
                    f"{src.name} page {index + 1} invoices {hits}, not only {spec['invoice']}. Not uploading."
                )
        content = slice_bytes(src, spec["pages"])
        check = describe_pdf(content, spec["invoice"], spec["known"])
        if not check["text_ok"]:
            raise SystemExit(f"Sliced PDF for {bill_id} contains {check['invoices']}. Not uploading.")
        if bill_id == 10509 and "5,328.46" not in "\n".join(page_texts(content)) and "5328.46" not in "\n".join(page_texts(content)):
            raise SystemExit("O'Neal 15486842 slice does not show the $5,328.46 total. Not uploading.")
        PDF_DIR.mkdir(parents=True, exist_ok=True)
        path = PDF_DIR / f"{bill_id}-{spec['invoice']}.pdf"
        path.write_bytes(content)
        built[bill_id] = {"path": str(path.relative_to(ROOT)), "bytes": content, "check": check, "name": path.name}
        LOGGER.info("Sliced %s pages %s -> %s %s", bill_id, [p + 1 for p in spec["pages"]], path.name, check["invoices"])
    return built


def note_html(text: str, *who: str) -> str:
    body = text
    expected = set()
    if "shawn" in who:
        body = body.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
        expected.add("104")
    if "treyce" in who:
        body = body.replace("@Treyce Hodges", TREYCE_MENTION_HTML, 1)
        expected.add("33")
    if "data-mention-id" in text:
        raise BillAbort("Note text already had a mention span. Not writing.")
    counts = {token: body.count(f'data-mention-id="{token}"') for token in ("33", "104")}
    if any(body.count(f'data-mention-id="{token}"') != (1 if token in expected else 0) for token in ("33", "104")):
        raise BillAbort(f"Note mention counts are {counts}. Not writing.")
    if "@Anthony" in body and "data-mention-id" in body.split("@Anthony", 1)[0][-40:]:
        raise BillAbort("Refusing an invented Anthony mention.")
    return f"<p>{body}</p>"


def comment_fact(comment: dict[str, Any]) -> dict[str, Any]:
    values = comment.get("values") if isinstance(comment.get("values"), dict) else {}
    html = str(values.get("HtmlValue") or "")
    creator = values.get("CreatorId")
    modifier = values.get("ModifierId")
    return {
        "id": comment.get("id"),
        "text": plain(html),
        "mentions": [
            {"id": int(match.group(1)), "name": match.group(2)}
            for match in MENTION_RE.finditer(html)
        ],
        "created_on": values.get("CreatedOn"),
        "creator_id": creator.get("id") if isinstance(creator, dict) else creator,
        "creator_name": (creator.get("text") or creator.get("name")) if isinstance(creator, dict) else None,
        "modified_on": values.get("ModifiedOn"),
        "modifier_id": modifier.get("id") if isinstance(modifier, dict) else modifier,
    }


def snapshot(client, record: dict[str, Any], invoice: str, known: set[str]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    batch = values.get("AP_Invoice_Batch") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        receipt = vals.get("Receipt")
        lines.append(
            {
                "id": line.get("id"),
                "qty": as_float(vals.get("Quantity")),
                "unit_price": as_float(vals.get("Unit_Price")),
                "extended": money(vals.get("Extended_Amount")),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
                "description": str(vals.get("Description") or vals.get("Part_Description") or ""),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        kind = vals.get("Additional_Charges")
        charges.append(
            {
                "id": charge.get("id"),
                "kind_id": kind.get("id") if isinstance(kind, dict) else None,
                "kind": (kind.get("text") or kind.get("name")) if isinstance(kind, dict) else vals.get("Name"),
                "amount": money(vals.get("Amount")),
            }
        )
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append({"id": tax.get("id"), "amount": money(vals.get("Tax_Amount"))})
    line_sum = round(sum(float(row["extended"] or 0) for row in lines), 2)
    charge_sum = round(sum(float(row["amount"] or 0) for row in charges), 2)
    tax_sum = round(sum(float(row["amount"] or 0) for row in taxes), 2)
    attachments = []
    for item in client.list_attachments(record["id"]):
        content = attachment_bytes(client, int(record["id"]), item)
        described = describe_pdf(content, invoice, known) if content else None
        attachments.append(
            {
                "id": item.get("id"),
                "name": attachment_name(item),
                "pages": None if described is None else described["pages"],
                "invoices": None if described is None else described["invoices"],
                "own_only": bool(described and described["text_ok"]),
            }
        )
    return {
        "id": int(record["id"]),
        "invoice": str(values.get("Invoice_Number") or ""),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": (batch.get("text") or batch.get("name")) if isinstance(batch, dict) else None,
        "invoice_amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "net": money(values.get("Invoice_Net_Amount")),
        "lines": lines,
        "charges": charges,
        "taxes": taxes,
        "receipts": [row["receipt_id"] for row in lines if row.get("receipt_id")],
        "lines_plus_charges": round(line_sum + charge_sum, 2),
        "lines_charges_tax": round(line_sum + charge_sum + tax_sum, 2),
        "pdf_total": None,
        "comments": [comment_fact(row) for row in lists.get("Comments_1") or [] if isinstance(row, dict)],
        "attachments": attachments,
        "read_at": now(),
    }


def fresh(client, bill_id: int, invoice: str, known: set[str]) -> dict[str, Any]:
    return snapshot(client, client.get_item("ap_invoices", bill_id), invoice, known)


def confirm_bill(row: dict[str, Any], bill_id: int, invoice: str) -> None:
    if int(row["id"]) != bill_id or str(row["invoice"]) != invoice:
        raise BillAbort(f"Bill {row.get('id')} invoice {row.get('invoice')!r} is not {invoice}.", row)
    if not unposted(row.get("posted")) or row.get("void") not in (None, "", False):
        raise BillAbort("Bill is posted or void.", row)


def put_record(client, bill_id: int, payload: dict[str, Any]) -> int:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if payload.get("Posted") not in (None, "", False) or values.get("Posted") not in (None, "", False):
        raise SystemExit("Refusing a payload that posts the bill.")
    response = client.request("PUT", client._record_url("ap_invoices", bill_id), json=payload)
    LOGGER.info("PUT bill %s HTTP %s", bill_id, response.status_code)
    if response.status_code >= 400:
        raise BillAbort(f"PUT bill {bill_id} HTTP {response.status_code}.")
    return int(response.status_code)


def matching_note(row: dict[str, Any], text: str) -> dict[str, Any] | None:
    for comment in row["comments"]:
        if comment["text"] == text:
            return comment
    return None


def confirm_note(comment: dict[str, Any], mention_ids: set[int]) -> None:
    found = {int(item["id"]) for item in comment["mentions"]}
    if found != mention_ids:
        raise BillAbort(f"Note mentions are {sorted(found)}, expected {sorted(mention_ids)}.")
    if int(comment.get("creator_id") or 0) != 175:
        raise BillAbort("Note author is not API Agent 175.")


def add_note(client, bill_id: int, invoice: str, known: set[str], text: str, mention_ids: set[int], who: tuple[str, ...], before_count: int) -> dict[str, Any]:
    current = fresh(client, bill_id, invoice, known)
    existing = matching_note(current, text)
    if existing is not None:
        confirm_note(existing, mention_ids)
        return current
    put_record(client, bill_id, added_comment_payload(bill_id, note_html(text, *who)))
    saved_row = fresh(client, bill_id, invoice, known)
    saved = matching_note(saved_row, text)
    if saved is None:
        raise BillAbort("The new note was not found on readback.")
    confirm_note(saved, mention_ids)
    if len(saved_row["comments"]) != before_count + 1 and len(saved_row["comments"]) != len(current["comments"]) + 1:
        raise BillAbort("Comment count did not increase by one.")
    return saved_row


def move_batch(client, bill_id: int, invoice: str, known: set[str]) -> str:
    current = fresh(client, bill_id, invoice, known)
    if int(current.get("batch_id") or 0) == TRANSFER_ID:
        if current.get("batch") != TRANSFER_NAME:
            raise BillAbort(f"Bill is on batch 375 named {current.get('batch')!r}.")
        return "already"
    put_record(
        client,
        bill_id,
        {"id": bill_id, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": TRANSFER_ID}}},
    )
    moved = fresh(client, bill_id, invoice, known)
    if int(moved.get("batch_id") or 0) != TRANSFER_ID or moved.get("batch") != TRANSFER_NAME:
        raise BillAbort(f"Bill is on batch {moved.get('batch_id')} {moved.get('batch')}.")
    if not unposted(moved.get("posted")):
        raise BillAbort("Bill posted during the batch move.")
    return "moved"


def delete_attachment(client, bill_id: int, attachment_id: Any) -> None:
    url = client._record_url("ap_invoices", bill_id, f"attachments/{attachment_id}")
    response = client.request("DELETE", url)
    LOGGER.info("DELETE attachment %s on %s HTTP %s", attachment_id, bill_id, response.status_code)
    if response.status_code not in (200, 202, 204):
        raise BillAbort(f"DELETE attachment {attachment_id} HTTP {response.status_code}.")


def swap_attachment(client, bill_id: int, invoice: str, known: set[str], built: dict[str, Any]) -> str:
    def listed() -> dict[str, Any]:
        return fresh(client, bill_id, invoice, known)

    current = listed()
    correct = [item for item in current["attachments"] if item.get("own_only")]
    unread = [item for item in current["attachments"] if item.get("invoices") is None]
    if unread:
        raise BillAbort(f"Could not read attachment(s) {[item.get('id') for item in unread]}. Not deleting.")
    if len(correct) == 1 and len(current["attachments"]) == 1:
        return "already-correct"
    if not correct:
        status = client.try_official_attach(
            bill_id,
            name=built["name"],
            content_type="application/pdf",
            size=len(built["bytes"]),
            content=built["bytes"],
        )
        LOGGER.info("Attach %s on %s %s", built["name"], bill_id, status)
        if status != "attached":
            raise BillAbort(f"Attach returned {status}. Old attachment left in place.")
        uploaded = listed()
        names = [item for item in uploaded["attachments"] if item.get("name") == built["name"] and item.get("own_only")]
        if not names:
            raise BillAbort("Uploaded file is not an own-invoice attachment on readback. Not deleting the old file.")
        correct = names
    keeper = max(correct, key=lambda item: int(item.get("id") or 0))
    for item in listed()["attachments"]:
        if int(item.get("id") or 0) == int(keeper.get("id") or 0):
            continue
        delete_attachment(client, bill_id, item.get("id"))
    final = listed()
    if len(final["attachments"]) != 1 or not final["attachments"][0].get("own_only"):
        raise BillAbort(
            f"Attachment readback is {[item.get('id') for item in final['attachments']]} "
            f"invoices {[item.get('invoices') for item in final['attachments']]}."
        )
    if not same_invoice_list(final["attachments"][0].get("invoices") or [], invoice):
        raise BillAbort("Remaining attachment does not show only this invoice.")
    return "replaced"


def same_invoice_list(numbers: list[str], invoice: str) -> bool:
    return only_own(numbers, invoice)


def require_batch(row: dict[str, Any], batch_id: int) -> None:
    if int(row.get("batch_id") or 0) != batch_id:
        raise BillAbort(f"Bill is on batch {row.get('batch_id')} {row.get('batch')}, expected {batch_id}.")


def ppv_sum(row: dict[str, Any]) -> float:
    return round(
        sum(float(charge["amount"] or 0) for charge in row["charges"] if int(charge.get("kind_id") or 0) == PPV_CHARGE_LOOKUP_ID),
        2,
    )


def receipt_check(client, receipt_id: int, qty: float, unit: float, extended: float) -> None:
    record = client.get_item("receipts", receipt_id)
    values = record.get("values") or {}
    got_qty = as_float(values.get("Quantity_Received"))
    got_unit = as_float(values.get("PO_Item_Number_$_Unit_Price") or values.get("Purchase_Cost") or values.get("Unit_Cost"))
    got_ext = money(values.get("Extended_Purchase_Cost"))
    if got_ext is None and got_qty is not None and got_unit is not None:
        got_ext = round(got_qty * got_unit, 2)
    if got_qty is None or abs(got_qty - qty) > 0.001 or got_unit is None or abs(got_unit - unit) > 0.0001 or got_ext != extended:
        raise BillAbort(
            f"Receipt {receipt_id} is qty {got_qty} at {got_unit}, extended {got_ext}. "
            f"Expected {qty} at {unit}, ${extended:.2f}."
        )


def line_for(row: dict[str, Any], receipt_id: int) -> dict[str, Any] | None:
    hits = [line for line in row["lines"] if int(line.get("receipt_id") or 0) == receipt_id]
    if len(hits) != 1:
        return None
    return hits[0]


def fix_10477(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    del built
    invoice = "15478871"
    before = fresh(client, 10477, invoice, {invoice})
    confirm_bill(before, 10477, invoice)
    noted = add_note(client, 10477, invoice, {invoice}, NOTE_10477, {33, 104}, ("treyce", "shawn"), len(before["comments"]))
    if matching_note(noted, NOTE_10477) is None:
        raise BillAbort("Note missing before the batch move.")
    move = move_batch(client, 10477, invoice, {invoice})
    after = fresh(client, 10477, invoice, {invoice})
    confirm_bill(after, 10477, invoice)
    require_batch(after, TRANSFER_ID)
    confirm_note(matching_note(after, NOTE_10477) or {}, {33, 104})
    return {"status": "ok", "move": move, "before": before, "after": after}


def fix_10494(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    invoice = "T609053432"
    known = EMJ_KNOWN
    before = fresh(client, 10494, invoice, known)
    confirm_bill(before, 10494, invoice)
    require_batch(before, SEPT_BATCH)
    receipt_check(client, 24880, 276, 1.7559, 484.63)
    other = [line for line in before["lines"] if int(line.get("receipt_id") or 0) not in (0, 24880)]
    if other:
        raise BillAbort(f"Bill already has other receipts {[line.get('receipt_id') for line in other]}.")
    if line_for(before, 24880) is None:
        values = (client.get_item("receipts", 24880).get("values") or {})
        invoiced = values.get("Quantity_Invoiced") or values.get("Quantity_Billed")
        if invoiced not in (None, "", 0, 0.0, False) and float(invoiced) >= 276:
            raise BillAbort(f"Receipt 24880 is already invoiced for {invoiced}.")
        status = client.try_select_receipts(10494, [24880])
        LOGGER.info("Select 24880 %s", status)
        if status != "selected":
            raise BillAbort(f"Select Receipts returned {status}.")
    selected = fresh(client, 10494, invoice, known)
    confirm_bill(selected, 10494, invoice)
    line = line_for(selected, 24880)
    if line is None or line.get("extended") != 484.63 or abs(float(line.get("qty") or 0) - 276) > 0.001:
        raise BillAbort(f"Selected line is {line}. Expected receipt 24880 qty 276 extended 484.63.")
    if ppv_sum(selected) != 2.51:
        extra = [charge for charge in selected["charges"] if not (int(charge.get("kind_id") or 0) == PPV_CHARGE_LOOKUP_ID and charge.get("amount") == 2.51)]
        if selected["charges"] and extra:
            raise BillAbort(f"Bill charges are {selected['charges']}. Not adding another PPV.")
        status = client.try_post_ppv(10494, 2.51)
        LOGGER.info("PPV 2.51 %s", status)
        if status != "posted":
            raise BillAbort(f"PPV post returned {status}.")
    priced = fresh(client, 10494, invoice, known)
    confirm_bill(priced, 10494, invoice)
    require_batch(priced, SEPT_BATCH)
    if priced["lines_plus_charges"] != 487.14 or ppv_sum(priced) != 2.51:
        raise BillAbort(f"Lines plus charges are {priced['lines_plus_charges']} PPV {ppv_sum(priced)}.")
    attach = swap_attachment(client, 10494, invoice, known, built[10494])
    noted = add_note(client, 10494, invoice, known, NOTE_10494, {33, 104}, ("shawn", "treyce"), len(before["comments"]))
    require_batch(noted, SEPT_BATCH)
    after = fresh(client, 10494, invoice, known)
    confirm_bill(after, 10494, invoice)
    require_batch(after, SEPT_BATCH)
    after["pdf_total"] = 487.14
    if after["lines_plus_charges"] != 487.14 or len(after["attachments"]) != 1:
        raise BillAbort("Final 10494 total or attachment is wrong.")
    return {"status": "ok", "attach": attach, "before": before, "after": after, "pdf_total": 487.14}


def fix_10493(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    del built
    invoice = "216055"
    known = {invoice}
    before = fresh(client, 10493, invoice, known)
    confirm_bill(before, 10493, invoice)
    require_batch(before, SEPT_BATCH)
    line = line_for(before, 25161)
    if line is None or line.get("extended") != 381.72 or abs(float(line.get("qty") or 0) - 288) > 0.001:
        raise BillAbort(f"Receipt line is {line}. Expected 25161 qty 288 extended 381.72.")
    if before["lines_plus_charges"] != 381.72:
        raise BillAbort(f"Lines plus charges are {before['lines_plus_charges']}, not 381.72.")
    negatives = [charge for charge in before["charges"] if int(charge.get("kind_id") or 0) == PPV_CHARGE_LOOKUP_ID and charge.get("amount") == -1.32]
    positives = [charge for charge in before["charges"] if int(charge.get("kind_id") or 0) == PPV_CHARGE_LOOKUP_ID and charge.get("amount") == 1.32]
    others = [charge for charge in before["charges"] if charge not in negatives and charge not in positives]
    if others:
        raise BillAbort(f"Unexpected charges {others}.")
    if (negatives or positives) and not (len(negatives) == 1 and len(positives) == 1):
        raise BillAbort(f"PPV pair is not one -1.32 and one +1.32: {before['charges']}.")
    if negatives and positives:
        put_record(
            client,
            10493,
            {
                "id": 10493,
                "state": "Modified",
                "lists": {
                    "InvoiceAdditionalCharges": [
                        {"id": int(negatives[0]["id"]), "state": "Removed"},
                        {"id": int(positives[0]["id"]), "state": "Removed"},
                    ]
                },
            },
        )
    cleared = fresh(client, 10493, invoice, known)
    confirm_bill(cleared, 10493, invoice)
    require_batch(cleared, SEPT_BATCH)
    if cleared["charges"] or cleared["lines_plus_charges"] != 381.72 or ppv_sum(cleared) != 0:
        raise BillAbort(f"After charge removal total is {cleared['lines_plus_charges']} charges {cleared['charges']}.")
    noted = add_note(client, 10493, invoice, known, NOTE_10493, {33, 104}, ("shawn", "treyce"), len(before["comments"]))
    require_batch(noted, SEPT_BATCH)
    after = fresh(client, 10493, invoice, known)
    confirm_bill(after, 10493, invoice)
    require_batch(after, SEPT_BATCH)
    after["pdf_total"] = 381.72
    return {"status": "ok", "before": before, "after": after, "pdf_total": 381.72}


def fix_10503(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    del built
    invoice = "S816901432"
    known = EMJ_KNOWN | {invoice}
    before = fresh(client, 10503, invoice, known)
    confirm_bill(before, 10503, invoice)
    require_batch(before, SEPT_BATCH)
    line = line_for(before, 25160)
    if line is None or line.get("extended") != 164.57 or abs(float(line.get("qty") or 0) - 240) > 0.001:
        raise BillAbort(f"Receipt line is {line}. Expected 25160 qty 240 extended 164.57.")
    if ppv_sum(before) != 2.41 or before["lines_plus_charges"] != 166.98:
        raise BillAbort(f"Total {before['lines_plus_charges']} PPV {ppv_sum(before)} is not 166.98 with $2.41 PPV.")
    if len(before["charges"]) != 1:
        raise BillAbort(f"Expected one PPV charge, found {before['charges']}.")
    noted = add_note(client, 10503, invoice, known, NOTE_10503, {33, 104}, ("shawn", "treyce"), len(before["comments"]))
    require_batch(noted, SEPT_BATCH)
    after = fresh(client, 10503, invoice, known)
    confirm_bill(after, 10503, invoice)
    require_batch(after, SEPT_BATCH)
    after["pdf_total"] = 166.98
    if after["lines_plus_charges"] != 166.98:
        raise BillAbort("Final 10503 total changed.")
    return {"status": "ok", "before": before, "after": after, "pdf_total": 166.98}


def fix_10480(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    invoice = "0040455615"
    known = GAS_KNOWN
    before = fresh(client, 10480, invoice, known)
    confirm_bill(before, 10480, invoice)
    attach = swap_attachment(client, 10480, invoice, known, built[10480])
    noted = add_note(client, 10480, invoice, known, NOTE_10480, {104}, ("shawn",), len(before["comments"]))
    if matching_note(noted, NOTE_10480) is None:
        raise BillAbort("Note missing before the batch move.")
    move = move_batch(client, 10480, invoice, known)
    after = fresh(client, 10480, invoice, known)
    confirm_bill(after, 10480, invoice)
    require_batch(after, TRANSFER_ID)
    confirm_note(matching_note(after, NOTE_10480) or {}, {104})
    after["pdf_total"] = 213.93
    return {"status": "ok", "attach": attach, "move": move, "before": before, "after": after, "pdf_total": 213.93}


def fix_10468(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    del built
    invoice = "000444279/1/202659035"
    known = {invoice}
    before = fresh(client, 10468, invoice, known)
    confirm_bill(before, 10468, invoice)
    if before["lines"]:
        raise BillAbort(f"Merchandise lines exist: {before['lines']}.", before)
    # The $212.19 was entered as F-Fees & Surcharges (kind 11), not Freight External.
    # Kyle's note still calls that entered amount the freight. Do not recode it.
    entered = [charge for charge in before["charges"] if charge.get("amount") == 212.19]
    if len(before["charges"]) != 1 or len(entered) != 1:
        raise BillAbort(f"The entered amount is not one $212.19 charge: {before['charges']}.", before)
    if before["verification"] != 3552.19:
        raise BillAbort(f"Verification is {before['verification']}, not 3552.19.", before)
    if before["lines_plus_charges"] != 212.19:
        raise BillAbort(f"Entered total is {before['lines_plus_charges']}, not 212.19.", before)
    noted = add_note(client, 10468, invoice, known, NOTE_10468, {104}, ("shawn",), len(before["comments"]))
    if matching_note(noted, NOTE_10468) is None:
        raise BillAbort("Note missing before the batch move.")
    move = move_batch(client, 10468, invoice, known)
    after = fresh(client, 10468, invoice, known)
    confirm_bill(after, 10468, invoice)
    require_batch(after, TRANSFER_ID)
    confirm_note(matching_note(after, NOTE_10468) or {}, {104})
    if after["lines"] or after["lines_plus_charges"] != 212.19 or after["verification"] != 3552.19:
        raise BillAbort("10468 freight or verification changed during the move.")
    after["pdf_total"] = 3552.19
    return {
        "status": "ok",
        "move": move,
        "before": before,
        "after": after,
        "pdf_total": 3552.19,
        "entered_amount": 212.19,
        "entered_charge": before["charges"][0],
        "confirmation": (
            "No merchandise lines. Verification is $3,552.19. "
            "The only entered amount is one $212.19 charge."
        ),
    }


def fix_10509(client, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    invoice = "15486842"
    known = ONEAL_KNOWN
    anthony_id = PEOPLE["anthony"]["mention_id"]
    if anthony_id is not None:
        raise BillAbort(f"Anthony mention id {anthony_id} appeared. Stop and use that id instead of Shawn.")
    before = fresh(client, 10509, invoice, known)
    confirm_bill(before, 10509, invoice)
    if before["verification"] != 5328.46 and before["invoice_amount"] != 5328.46:
        raise BillAbort(f"Bill total fields are amount {before['invoice_amount']} verification {before['verification']}.")
    if any(int(receipt or 0) == 25182 for receipt in before["receipts"]):
        raise BillAbort("Receipt 25182 is on this bill. Not touching it.")
    attach = swap_attachment(client, 10509, invoice, known, built[10509])
    noted = add_note(client, 10509, invoice, known, NOTE_10509, {104}, ("shawn",), len(before["comments"]))
    if matching_note(noted, NOTE_10509) is None:
        raise BillAbort("Note missing before the batch move.")
    move = move_batch(client, 10509, invoice, known)
    after = fresh(client, 10509, invoice, known)
    confirm_bill(after, 10509, invoice)
    require_batch(after, TRANSFER_ID)
    confirm_note(matching_note(after, NOTE_10509) or {}, {104})
    after["pdf_total"] = 5328.46
    return {
        "status": "ok",
        "attach": attach,
        "move": move,
        "anthony_mention_id": None,
        "tagged": "Shawn McKibben 104 because Anthony has no proven mention id",
        "before": before,
        "after": after,
        "pdf_total": 5328.46,
    }


def fix_attachment_only(client, bill_id: int, built: dict[int, dict[str, Any]]) -> dict[str, Any]:
    spec = SLICES[bill_id]
    invoice = spec["invoice"]
    known = spec["known"]
    before = fresh(client, bill_id, invoice, known)
    confirm_bill(before, bill_id, invoice)
    notes_before = [comment["id"] for comment in before["comments"]]
    attach = swap_attachment(client, bill_id, invoice, known, built[bill_id])
    after = fresh(client, bill_id, invoice, known)
    confirm_bill(after, bill_id, invoice)
    if [comment["id"] for comment in after["comments"]] != notes_before:
        raise BillAbort("Attachment swap changed the notes.")
    if int(after.get("batch_id") or 0) != int(before.get("batch_id") or 0):
        raise BillAbort("Attachment swap changed the batch.")
    return {"status": "ok", "attach": attach, "before": before, "after": after}


def run_one(client, label: str, func, built: dict[int, dict[str, Any]], results: dict[str, Any]) -> None:
    try:
        results[label] = func(client, built)
        LOGGER.info("Bill %s %s", label, results[label].get("status"))
    except BillAbort as exc:
        results[label] = {"status": "aborted", "reason": str(exc), "before": exc.before}
        LOGGER.info("Bill %s aborted: %s", label, exc)
    except SystemExit:
        raise
    except Exception as exc:
        results[label] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:400]}
        LOGGER.info("Bill %s error: %s", label, results[label]["reason"])


def main() -> None:
    built = build_pdfs()
    client = finish.login()
    finish.install_401_guard(client)
    transfer = client.get_item("ap_batches", TRANSFER_ID)
    transfer_name = str((transfer.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if transfer_name != TRANSFER_NAME:
        raise SystemExit(f"Batch {TRANSFER_ID} name is {transfer_name!r}, not {TRANSFER_NAME}.")

    results: dict[str, Any] = {}
    steps = [
        ("10494", fix_10494),
        ("10493", fix_10493),
        ("10503", fix_10503),
        ("10478", lambda c, b: fix_attachment_only(c, 10478, b)),
        ("10479", lambda c, b: fix_attachment_only(c, 10479, b)),
        ("10504", lambda c, b: fix_attachment_only(c, 10504, b)),
        ("10505", lambda c, b: fix_attachment_only(c, 10505, b)),
        ("10506", lambda c, b: fix_attachment_only(c, 10506, b)),
        ("10507", lambda c, b: fix_attachment_only(c, 10507, b)),
        ("10508", lambda c, b: fix_attachment_only(c, 10508, b)),
        ("10480", fix_10480),
        ("10509", fix_10509),
        ("10468", fix_10468),
        ("10477", fix_10477),
    ]
    try:
        for label, func in steps:
            run_one(client, label, func, built, results)
            write_out(results, built, final=False)
    finally:
        write_out(results, built, final=True)
    failed = [label for label, row in results.items() if row.get("status") != "ok"]
    if failed:
        raise SystemExit(f"Bills not completed: {', '.join(failed)}")


def write_out(results: dict[str, Any], built: dict[int, dict[str, Any]], final: bool) -> None:
    payload = {
        "final": final,
        "written_at": now(),
        "sign_ins": finish.SIGN_INS,
        "constraints": {
            "author": "API Agent 175",
            "posted": False,
            "batch_closed": False,
            "email_sent": False,
        },
        "sliced_pdfs": {
            str(bill_id): {"path": item["path"], "check": item["check"]}
            for bill_id, item in built.items()
        },
        "bills": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def main_10468() -> None:
    """Finish the Tricor bill without repeating the bills already saved."""
    payload = json.loads(OUT.read_text())
    client = finish.login()
    finish.install_401_guard(client)
    transfer = client.get_item("ap_batches", TRANSFER_ID)
    transfer_name = str((transfer.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if transfer_name != TRANSFER_NAME:
        raise SystemExit(f"Batch {TRANSFER_ID} name is {transfer_name!r}, not {TRANSFER_NAME}.")
    outcome = fix_10468(client, {})
    payload["bills"]["10468"] = outcome
    payload["final"] = True
    payload["written_at"] = now()
    payload["sign_ins"] = finish.SIGN_INS
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    LOGGER.info("Bill 10468 %s", outcome.get("move"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "10468":
        main_10468()
    else:
        main()
