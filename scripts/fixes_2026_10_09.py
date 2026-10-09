"""Kyle's 2026-10-09 8:20 AM CT fixes. Live KIMCO and Graph.

One API Agent sign-in. A failed password is not retried. A later 401 signs
in once more and never a third time. Does not post, auto-pay, close a batch,
delete a bill, delete a note, or send mail.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    GraphClient,
    load_graph_credentials,
)
from ap_clerk.kimco import (
    KimcoClient,
    KimcoError,
    _ppv_charge_removals,
    added_comment_payload,
)
from ap_clerk.rules import (
    SHAWN_MENTION_HTML,
    ap_clerk_edit_note,
    comments_for,
    due_date_from_terms,
    kimco_datetime,
    lookup_id,
    lookup_text,
    money,
    ppv_limit,
)
from ap_clerk.transfer_ap import find_transfer_ap_batch
from scripts.ap_run_2026_10_08_enter import load_open_receipts, sample_for, uom_text
from scripts.sept25_30_attachment_audit import attachment_bytes, receipt_fact
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of, stamp
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_archive_check_2026_10_07 import plain
from scripts.sept_missed_entry_2026_10_07 import put, totals

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("fixes-1009")

OUT = ROOT / "runs" / "fixes-2026-10-09"
ONEAL_PDF = Path("/tmp/fixes-1009/mail-1.pdf")
INDEX = ROOT / "runs" / "ap-run-2026-10-09" / "kimco-index.json"
BATCH_NAME = "API Agent - 10/9/26"
BATCH_ID = 751
TRANSFER_ID = 375
ONEAL_VENDOR = 137
NEW_INVOICE = "15492850"
NEW_PO = "59137"
NEW_TOTAL = 10503.04
NEW_DATE = date(2026, 10, 6)
MIN_EXISTING_ID = 10589
MAIL_RECEIVED = "2026-10-07T04:27:55Z"
MAIL_SENDER = "vsanders@onealsteel.com"
MAIL_SUBJECT = " O'Neal Steel Invoice For Account # 14748440 "
MAIL_MODIFIED = "2026-10-07T04:28:13Z"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime"
ROWS: list[dict[str, str]] = []


def posted(value: Any) -> bool:
    return value not in (None, "", False)


def line_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        receipt = vals.get("Receipt")
        rows.append(
            {
                "id": line.get("id"),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": money(vals.get("Extended_Amount")),
                "description": vals.get("Misc_Description") or lookup_text(vals.get("Part_ID") or vals.get("MFG_Miscellaneous_Item")),
            }
        )
    return rows


def note_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        vals = comment.get("values") or {}
        html = str(vals.get("HtmlValue") or "")
        creator = vals.get("CreatorId") if isinstance(vals.get("CreatorId"), dict) else {}
        mentions = [int(match) for match in re.findall(r'data-mention-id="(\d+)"', html)]
        found.append(
            {
                "id": comment.get("id"),
                "text": plain(html),
                "html": html,
                "creator_id": creator.get("id"),
                "creator_name": creator.get("text") or creator.get("name"),
                "mentions": mentions,
            }
        )
    return found


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    live = totals(record)
    po = values.get("Purchase_Order")
    return {
        "id": record.get("id"),
        "invoice": str(values.get("Invoice_Number") or ""),
        "vendor_id": lookup_id(values.get("Vendor")),
        "vendor": lookup_text(values.get("Vendor")),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "batch_id": live.get("batch_id"),
        "batch": live.get("batch"),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "invoice_amount": money(values.get("Invoice_Amount")),
        "po_id": lookup_id(po),
        "po": lookup_text(po),
        "lines": line_rows(record),
        "charges": live.get("charges") or [],
        "notes": note_rows(record),
        "attachments": None,
        "covered": live.get("covered"),
    }


def brief(row: dict[str, Any]) -> str:
    receipts = ",".join(str(line["receipt_id"]) for line in row["lines"] if line.get("receipt_id")) or "none"
    qtys = ",".join(f"{line.get('qty')}" for line in row["lines"]) or "none"
    return (
        f"invoice={row['invoice']} vendor={row['vendor_id']} posted={row['posted']} "
        f"void={row['void']} batch={row['batch_id']}:{row['batch']} "
        f"verification={row['verification']} po={row['po'] or row['po_id'] or ''} "
        f"line_qty={qtys} receipts={receipts} covered={row['covered']} notes={len(row['notes'])}"
    )


def record_row(bill: Any, change: str, before: str, after: str, note_id: Any, note_text: str, ok: bool) -> None:
    ROWS.append(
        {
            "bill": "" if bill in (None, "") else str(bill),
            "change": change,
            "before": before,
            "after": after,
            "note id + text": f"{note_id}: {note_text}" if note_id not in (None, "") else note_text,
            "readback ok y/n": "y" if ok else "n",
        }
    )


def read_bill(client: KimcoClient, bill_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    record = client.get_item("ap_invoices", bill_id)
    return record, snapshot(record)


def require_author(note: dict[str, Any]) -> bool:
    return int(note.get("creator_id") or 0) == 175 and str(note.get("creator_name") or "") == "API Agent"


def add_note(client: KimcoClient, bill_id: int, html: str, marker: str) -> dict[str, Any] | None:
    before = note_rows(client.get_item("ap_invoices", bill_id))
    existing = [note for note in before if marker in note["text"]]
    if existing:
        return existing[-1]
    status = put(client, bill_id, added_comment_payload(bill_id, html))
    if status >= 400:
        LOGGER.info("Note on %s HTTP %s", bill_id, status)
        return None
    after = note_rows(client.get_item("ap_invoices", bill_id))
    hits = [note for note in after if marker in note["text"]]
    return hits[-1] if hits else None


def anthony_html(text: str) -> str:
    body = text.strip()
    if not body.startswith("AP Clerk:"):
        body = "AP Clerk: " + body
    if "@Anthony" not in body:
        body = body.replace("AP Clerk:", "AP Clerk: @Anthony", 1)
    if "@Shawn McKibben" not in body:
        body = body.replace("@Anthony", "@Anthony @Shawn McKibben", 1)
    html = body.replace("@Shawn McKibben", SHAWN_MENTION_HTML, 1)
    if "@Anthony" not in html or 'data-mention-id="104"' not in html:
        raise SystemExit("Anthony note is missing the plain name or Shawn mention")
    return html if html.startswith("<") else f"<p>{html}</p>"


def transfer_id(client: KimcoClient) -> int:
    record = client.get_item("ap_batches", TRANSFER_ID)
    name = str((record.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if name.casefold() != "transfer ap":
        raise SystemExit(f"Batch {TRANSFER_ID} is {name!r}, not Transfer AP. No batch move.")
    found = find_transfer_ap_batch(client.list_items("ap_batches"))
    if not found.get("found") or int(found["id"]) != TRANSFER_ID:
        raise SystemExit(f"Transfer AP lookup was {found}. No batch move.")
    return TRANSFER_ID


def move_to_transfer(client: KimcoClient, bill_id: int) -> bool:
    dest = transfer_id(client)
    live = client.get_item("ap_invoices", bill_id)
    if posted((live.get("values") or {}).get("Posted")):
        raise SystemExit(f"Refusing to move posted bill {bill_id}")
    if int(snapshot(live).get("batch_id") or 0) == dest:
        return True
    status = put(
        client,
        bill_id,
        {"id": bill_id, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": dest}}},
    )
    if status >= 400:
        return False
    after = snapshot(client.get_item("ap_invoices", bill_id))
    return int(after.get("batch_id") or 0) == dest and not posted(after.get("posted"))


def remove_child_lines(client: KimcoClient, bill_id: int, line_ids: list[int], *, drop_ppv: bool) -> str:
    record = client.get_item("ap_invoices", bill_id)
    if posted((record.get("values") or {}).get("Posted")):
        return "posted"
    if not line_ids:
        return "none"
    original = money((record.get("values") or {}).get("Invoice_Verification_Amount"))
    payload: dict[str, Any] = {
        "id": bill_id,
        "state": "Modified",
        "lists": {"APInvoiceLine": [{"id": int(line_id), "state": "Removed"} for line_id in line_ids]},
    }
    status = put(client, bill_id, payload)
    if status >= 400:
        return f"http-{status}"
    after = client.get_item("ap_invoices", bill_id)
    still = [line["id"] for line in line_rows(after) if int(line["id"] or 0) in set(line_ids)]
    if not still:
        return "removed"
    persist: dict[str, Any] = {
        "id": bill_id,
        "state": "Modified",
        "values": {"Invoice_Verification_Amount": 0},
        "lists": {"APInvoiceLine": [{"id": int(line_id), "state": "Removed"} for line_id in still]},
    }
    if drop_ppv:
        name, charges = _ppv_charge_removals(after)
        if name and charges:
            persist["lists"][name] = charges
    status = put(client, bill_id, persist)
    if status >= 400:
        return f"persist-http-{status}"
    confirm = client.get_item("ap_invoices", bill_id)
    if any(int(line["id"] or 0) in set(line_ids) for line in line_rows(confirm)):
        return "still-present"
    if original not in (None, 0, 0.0):
        put(
            client,
            bill_id,
            {"id": bill_id, "state": "Modified", "values": {"Invoice_Verification_Amount": original}},
        )
    return "removed"


def vendor_invoice_ids(client: KimcoClient, number: str, vendor_id: int) -> list[int]:
    hits: set[int] = set()
    if INDEX.is_file():
        index = json.loads(INDEX.read_text())
        for row in index.get("invoices") or []:
            if str(row.get("number") or "") == number and row.get("id"):
                hits.add(int(row["id"]))
    misses = 0
    for invoice_id in range(MIN_EXISTING_ID + 1, MIN_EXISTING_ID + 40):
        try:
            record = client.get_item("ap_invoices", invoice_id)
        except KimcoError:
            misses += 1
            if misses >= 4:
                break
            continue
        misses = 0
        values = record.get("values") or {}
        if str(values.get("Invoice_Number") or "") == number:
            hits.add(invoice_id)
    matched = []
    for invoice_id in sorted(hits):
        record = client.get_item("ap_invoices", invoice_id)
        values = record.get("values") or {}
        if lookup_id(values.get("Vendor")) == vendor_id and str(values.get("Invoice_Number") or "") == number:
            if values.get("Void") in (None, "", False):
                matched.append(invoice_id)
    return matched


def attachment_count(client: KimcoClient, bill_id: int) -> int:
    return len(client.list_attachments(bill_id))


def render_attachment(client: KimcoClient, bill_id: int, number: str) -> tuple[int, str]:
    OUT.mkdir(parents=True, exist_ok=True)
    items = client.list_attachments(bill_id)
    if len(items) != 1:
        return 0, f"attachment-count-{len(items)}"
    content = attachment_bytes(client, bill_id, items[0])
    if not content:
        return 0, "attachment-download-failed"
    document = pymupdf.open(stream=content, filetype="pdf")
    others = []
    for page in document:
        text = page.get_text("text")
        if number not in text:
            return document.page_count, "page-missing-invoice-number"
        for found in re.findall(r"\b15\d{6}\b", text):
            if found != number and found not in others:
                others.append(found)
    if others:
        return document.page_count, "also-shows-" + ",".join(others)
    for index, page in enumerate(document, start=1):
        image = OUT / f"{bill_id}-p{index}.png"
        page.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7), alpha=False).save(str(image))
    return document.page_count, "ok"


def uom_kind(value: str) -> str:
    token = re.sub(r"[^A-Z]", "", (value or "").upper())
    if not token:
        return ""
    # "IN-Inches" strips to ININCHES. Any inch word is inches. EACH has no INCH.
    if "INCH" in token or token in {"IN", "INCH", "INCHES"}:
        return "in"
    if token.startswith("FT") or token in {"FEET", "FOOT"}:
        return "ft"
    if token.startswith("EA") or token.startswith("PC") or "EACH" in token or "PIECE" in token:
        return "ea"
    return "other"


def assign_piece_receipts(lines: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    open_rows = [
        row
        for row in receipts
        if row.get("id") and row.get("open") and not row.get("placeholder") and uom_kind(str(row.get("uom") or "")) == "ea"
    ]
    used: set[int] = set()
    chosen: list[dict[str, Any]] = []
    for line in lines:
        hits = [
            row
            for row in open_rows
            if int(row["id"]) not in used and abs(float(row.get("qty") or 0) - float(line["qty"])) < 0.02
        ]
        tokens = [str(token) for token in line.get("tokens") or [] if token]
        if tokens:
            blob = lambda row: f"{row.get('part') or ''} {row.get('description') or ''}".lower()
            narrowed = [row for row in hits if any(token.lower() in blob(row) for token in tokens)]
            if narrowed:
                hits = narrowed
        detail = ", ".join(
            f"{row['id']} qty {row.get('qty')} {row.get('uom')} ${row.get('extended')} {row.get('part')}"
            for row in open_rows[:8]
        )
        if not hits:
            return [], (
                f"No open piece receipt matches quantity {line['qty']} {line.get('uom') or 'EA'}. "
                f"Open piece receipts: {detail or 'none'}."
            )
        if len(hits) > 1:
            hits.sort(key=lambda row: abs(float(row.get("extended") or 0) - float(line["amount"])))
            best = abs(float(hits[0].get("extended") or 0) - float(line["amount"]))
            second = abs(float(hits[1].get("extended") or 0) - float(line["amount"]))
            if not best + 0.02 < second:
                return [], f"More than one open receipt matches quantity {line['qty']}. Nothing was selected."
        chosen.append(hits[0])
        used.add(int(hits[0]["id"]))
    return chosen, ""


def fix_10582(client: KimcoClient) -> None:
    record, before = read_bill(client, 10582)
    before_text = brief(before)
    content = b""
    items = client.list_attachments(10582)
    if len(items) == 1:
        content = attachment_bytes(client, 10582, items[0]) or b""
    shows = False
    if content:
        text = "\n".join(page.get_text("text") for page in pymupdf.open(stream=content, filetype="pdf"))
        shows = "15491464" in text and "59341" in text and "9,490.50" in text and "885055" in text
    if posted(before["posted"]) or before.get("void") not in (None, "", False):
        note = ap_clerk_edit_note(
            "AP Clerk: @Treyce Hodges bill 10582 is already posted or void, so the vendor invoice number was not changed. "
            "It was entered as 885055, which is O'Neal's ship-to customer code. "
            "The printed INVOICE NO. is 15491464 for $9,490.50 on PO 59341. Please correct the posted bill.",
            action="treyce",
        )
        saved = add_note(client, 10582, note, "15491464")
        after = snapshot(client.get_item("ap_invoices", 10582))
        ok = bool(saved) and require_author(saved) and 33 in saved["mentions"] and after["invoice"] == before["invoice"]
        record_row(10582, "posted-note-only invoice number", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)
        return
    if not shows:
        record_row(10582, "invoice number not changed; attachment did not show 15491464", before_text, before_text, "", "", False)
        return
    if before["invoice"] == "15491464":
        changed = False
    elif before["invoice"] == "885055":
        duplicates = [item for item in vendor_invoice_ids(client, "15491464", ONEAL_VENDOR) if item != 10582]
        if duplicates:
            record_row(10582, f"not changed; 15491464 already on {duplicates}", before_text, before_text, "", "", False)
            return
        fresh, live = read_bill(client, 10582)
        if posted(live["posted"]) or live["invoice"] != "885055":
            record_row(10582, "not changed; bill changed before the write", before_text, brief(live), "", "", False)
            return
        status = put(client, 10582, {"id": 10582, "state": "Modified", "values": {"Invoice_Number": "15491464"}})
        if status >= 400:
            record_row(10582, f"invoice number put HTTP {status}", before_text, brief(snapshot(client.get_item("ap_invoices", 10582))), "", "", False)
            return
        changed = True
    else:
        record_row(10582, f"not changed; invoice is {before['invoice']}", before_text, before_text, "", "", False)
        return
    note = ap_clerk_edit_note(
        "AP Clerk: @Treyce Hodges the vendor invoice number was entered as 885055, which is O'Neal's ship-to customer code. "
        "I corrected it to 15491464, the printed INVOICE NO. The amount is $9,490.50 on PO 59341. The bill is not posted.",
        action="treyce",
    )
    saved = add_note(client, 10582, note, "corrected it to 15491464")
    after = snapshot(client.get_item("ap_invoices", 10582))
    ok = (
        after["invoice"] == "15491464"
        and not posted(after["posted"])
        and after["verification"] == before["verification"]
        and [line.get("receipt_id") for line in after["lines"]] == [line.get("receipt_id") for line in before["lines"]]
        and bool(saved)
        and require_author(saved)
        and 33 in (saved or {}).get("mentions", [])
        and "15491464" in (saved or {}).get("text", "")
    )
    record_row(
        10582,
        "invoice number 885055 to 15491464" if changed else "invoice number already 15491464; note added",
        before_text,
        brief(after),
        (saved or {}).get("id"),
        (saved or {}).get("text") or "",
        ok,
    )


def batch_751(client: KimcoClient) -> int:
    batches = client.list_items("ap_batches")
    hits = []
    for item in batches:
        name = str((item.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
        if name == BATCH_NAME and item.get("id"):
            hits.append(int(item["id"]))
    if hits != [BATCH_ID]:
        raise SystemExit(f"Batch {BATCH_NAME!r} ids were {hits}, not {[BATCH_ID]}. No new bill.")
    return BATCH_ID


def enter_oneal(client: KimcoClient) -> dict[str, Any]:
    outcome: dict[str, Any] = {"ready": False, "bill_id": None, "invoice": NEW_INVOICE}
    duplicates = vendor_invoice_ids(client, NEW_INVOICE, ONEAL_VENDOR)
    if duplicates:
        record_row("", f"O'Neal {NEW_INVOICE} already on bill {duplicates}; no header created", "", "", "", "", False)
        outcome["bill_id"] = duplicates[0]
        outcome["already"] = True
        return outcome
    batch_id = batch_751(client)
    sample = sample_for(client, ONEAL_VENDOR, {})
    receipts = load_open_receipts(client, {NEW_PO})
    facts = receipts.get(NEW_PO) or []
    po_ids = sorted({int(row["po_id"]) for row in facts if row.get("po_id")})
    lines = [
        {"qty": 8, "amount": 6302.65, "uom": "EA", "tokens": ["3/8"]},
        {"qty": 8, "amount": 4200.39, "uom": "EA", "tokens": ["1/4", "TMPR", "TEMPER"]},
    ]
    chosen, hold = assign_piece_receipts(lines, facts)
    limit = ppv_limit()
    gap = 0.0
    if chosen and not hold:
        extended = round(sum(float(row.get("extended") or 0) for row in chosen), 2)
        gap = round(NEW_TOTAL - extended, 2)
        if abs(gap) >= limit:
            hold = f"The price gap is ${abs(gap):,.2f}, which is ${limit:,.0f} or more. Receipts were not selected."
            chosen = []
        elif abs(gap) in {0.0, 0.02}:
            gap = 0.0
    real = [row for row in facts if row.get("id") and not row.get("placeholder")]
    if not po_ids:
        hold = f"Purchase order {NEW_PO} was not found. Nothing was selected."
        chosen = []
    elif not real and not hold:
        hold = f"No receipt recorded yet on PO {NEW_PO} for 8 pieces of 3/8 plate and 8 pieces of 1/4 tempered plate. Nothing was selected."
    LOGGER.info("O'Neal match hold=%s receipts=%s gap=%s po=%s", hold, [row.get("id") for row in chosen], gap, po_ids)
    day = NEW_DATE
    terms = str(sample.get("terms") or "")
    due = due_date_from_terms(day, terms)
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": batch_id},
        "Vendor": {"id": ONEAL_VENDOR},
        "Invoice_Number": NEW_INVOICE,
        "Invoice_Type": 3 if po_ids else 4,
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": NEW_TOTAL,
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": int(sample["terms_id"])},
        "Currency": {"id": 3},
        "Remit_To_Address": {"id": int(sample["remit_id"])},
        "Transaction_Date": kimco_datetime(day),
        "Comments": comments_for("live"),
    }
    if len(po_ids) == 1:
        payload["Purchase_Order"] = {"id": po_ids[0]}
    created, _body, status, error = client.create("ap_invoices", payload)
    if created is None or int(created) <= MIN_EXISTING_ID:
        record_row("", f"header create failed HTTP {status} id {created}", "", error[:240], "", "", False)
        return outcome
    created = int(created)
    made = snapshot(client.get_item("ap_invoices", created))
    if made["invoice"] != NEW_INVOICE or posted(made["posted"]) or made["vendor_id"] != ONEAL_VENDOR:
        record_row(created, "created bill failed readback; not finished", "", brief(made), "", "", False)
        return outcome
    select_status = ""
    if chosen and not hold:
        try:
            select_status = client.try_select_receipts(created, [int(row["id"]) for row in chosen])
        except KimcoError as exc:
            select_status = f"blocked-{exc}"
        if select_status != "selected":
            hold = f"Select Receipts returned {select_status}. Nothing stayed selected."
        elif abs(gap) >= 0.005:
            ppv_status = client.try_post_ppv(created, gap)
            if ppv_status != "posted":
                hold = f"Purchase price variance of ${gap:,.2f} did not post ({ppv_status})."
    if not ONEAL_PDF.is_file():
        hold = (hold + " " if hold else "") + "Source PDF missing. No attachment."
        attach_status = "missing"
    else:
        blob = ONEAL_PDF.read_bytes()
        attach_status = client.try_official_attach(
            created,
            name="oneal-15492850.pdf",
            content_type="application/pdf",
            size=len(blob),
            content=blob,
        )
    pages, attach_check = render_attachment(client, created, NEW_INVOICE) if attach_status == "attached" else (0, attach_status)
    live = snapshot(client.get_item("ap_invoices", created))
    covered_ok = money(live["verification"]) == NEW_TOTAL and money(live["covered"]) == NEW_TOTAL
    passed = not hold and covered_ok and not posted(live["posted"]) and attach_check == "ok" and select_status == "selected"
    if not passed and not hold:
        hold = f"Live lines and charges are ${live['covered']} against ${NEW_TOTAL:,.2f}. PDF {attach_check}."
    if not passed:
        moved = move_to_transfer(client, created)
        if not moved:
            hold = (hold + " " if hold else "") + "The bill did not move to Transfer AP."
    final = snapshot(client.get_item("ap_invoices", created))
    if passed:
        text = (
            f"AP Clerk: O'Neal Steel invoice {NEW_INVOICE} is entered and is not posted. "
            f"The PDF total is ${NEW_TOTAL:,.2f}. Purchase order {NEW_PO} receipts were selected."
        )
        if abs(gap) >= 0.005:
            text += f" A purchase price variance of ${gap:,.2f} covers the difference."
    else:
        text = (
            f"AP Clerk: O'Neal Steel invoice {NEW_INVOICE} is on hold and is not posted. "
            f"The PDF total is ${NEW_TOTAL:,.2f}. {hold} The bill is in Transfer AP."
        )
    saved = add_note(client, created, anthony_html(text), NEW_INVOICE)
    final = snapshot(client.get_item("ap_invoices", created))
    ok = (
        final["invoice"] == NEW_INVOICE
        and not posted(final["posted"])
        and money(final["verification"]) == NEW_TOTAL
        and bool(saved)
        and require_author(saved)
        and 104 in (saved or {}).get("mentions", [])
        and "@Anthony" in (saved or {}).get("text", "")
        and attach_check == "ok"
        and ((passed and int(final.get("batch_id") or 0) == BATCH_ID) or (not passed and int(final.get("batch_id") or 0) == TRANSFER_ID))
    )
    record_row(
        created,
        "entered O'Neal 15492850 on batch 751" if passed else "entered O'Neal 15492850 on hold in Transfer AP",
        "not in KIMCO",
        brief(final) + f" attach={attach_check} pages={pages}",
        (saved or {}).get("id"),
        (saved or {}).get("text") or "",
        ok,
    )
    outcome["ready"] = ok and attachment_count(client, created) >= 1 and bool(saved)
    outcome["bill_id"] = created
    outcome["passed"] = passed
    return outcome


def email_has_complete_bills(client: KimcoClient, numbers: list[str]) -> bool:
    for number in numbers:
        hits = vendor_invoice_ids(client, number, ONEAL_VENDOR)
        if len(hits) != 1:
            LOGGER.info("Email invoice %s bills %s", number, hits)
            return False
        record = client.get_item("ap_invoices", hits[0])
        notes = [note for note in note_rows(record) if note["text"].startswith("AP Clerk:")]
        if not notes or not client.list_attachments(hits[0]):
            return False
    return bool(numbers)


def file_email(client: KimcoClient, numbers: list[str]) -> None:
    if not email_has_complete_bills(client, numbers):
        record_row("email", "left in Inbox; not every invoice has a header, attachment, and note", MAIL_RECEIVED, "", "", "", False)
        return
    creds = load_graph_credentials()
    if not creds.ready:
        record_row("email", "graph credentials missing; mail not changed", MAIL_RECEIVED, "", "", "", False)
        return
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    dest = archive_folder(graph)
    day = date(2026, 10, 7)
    messages = graph.list_messages(ALLOWED_MAILBOX, received_from=day, received_to=day)
    found = []
    for message in messages:
        if str(message.get("receivedDateTime") or "") != MAIL_RECEIVED:
            continue
        if sender_of(message).lower() != MAIL_SENDER:
            continue
        if plain(str(message.get("subject") or "")) != plain(MAIL_SUBJECT):
            continue
        full = graph.get_message(ALLOWED_MAILBOX, str(message.get("id") or ""), select=SELECT)
        if (
            str(full.get("receivedDateTime") or "") == MAIL_RECEIVED
            and sender_of(full).lower() == MAIL_SENDER
            and plain(str(full.get("subject") or "")) == plain(MAIL_SUBJECT)
        ):
            found.append(full)
    unique = {str(item.get("id") or ""): item for item in found if item.get("id")}
    if len(unique) != 1:
        record_row("email", f"re-find returned {len(unique)}; mail not changed", MAIL_RECEIVED, "", "", "", False)
        return
    message = next(iter(unique.values()))
    cache: dict[str, dict] = {}
    path = folder_path(graph, str(message.get("parentFolderId") or ""), cache)
    if path != "Inbox":
        record_row("email", f"not in Inbox ({path}); mail not changed", MAIL_RECEIVED, path, "", "", False)
        return
    if stamp(message.get("lastModifiedDateTime")) != stamp(MAIL_MODIFIED):
        record_row(
            "email",
            "lastModified changed; mail not changed",
            MAIL_MODIFIED,
            str(message.get("lastModifiedDateTime") or ""),
            "",
            "",
            False,
        )
        return
    old_id = str(message.get("id") or "")
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, old_id),
        json={"categories": [ENTERED_IN_AI_CATEGORY]},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        record_row("email", f"category HTTP {response.status_code}", "Inbox", "", "", "", False)
        return
    moved = graph.request(
        "POST",
        graph._messages_url(ALLOWED_MAILBOX, old_id, "move"),
        json={"destinationId": dest},
        headers={"Content-Type": "application/json"},
    )
    if moved.status_code >= 400:
        record_row("email", f"move HTTP {moved.status_code}", "Inbox / Entered in AI", "", "", "", False)
        return
    new_id = str((moved.json() or {}).get("id") or "")
    check = graph.get_message(ALLOWED_MAILBOX, new_id, select=SELECT)
    after = folder_path(graph, str(check.get("parentFolderId") or ""), cache)
    cats = [str(item) for item in (check.get("categories") or [])]
    ok = after == "Inbox/9 - FORT WORTH ARCHIVE" and cats == [ENTERED_IN_AI_CATEGORY]
    record_row("email", "tagged Entered in AI and moved to Fort Worth archive", f"Inbox lastModified={MAIL_MODIFIED}", f"{after} categories={cats}", "", "", ok)


def fix_10569(client: KimcoClient) -> None:
    record, before = read_bill(client, 10569)
    before_text = brief(before)
    receipt = client.get_item("receipts", 25214)
    fact = receipt_fact(receipt)
    fact["uom"] = uom_text(receipt)
    selected = [line for line in before["lines"] if int(line.get("receipt_id") or 0) == 25214]
    if posted(before["posted"]) or before.get("void") not in (None, "", False):
        note = ap_clerk_edit_note(
            "AP Clerk: @Treyce Hodges bill 10569 is already posted or void, so the receipt was not unselected and the batch was not changed. "
            f"Morgan invoice 130154 bills 40 pieces. Receipt 25214 is quantity {fact.get('qty')} {fact.get('uom')}. "
            "Those units do not match. Please confirm the receipt. It looks similar to held invoice 130153 on PO 59013, but this is a separate PO 59199.",
            action="treyce",
        )
        saved = add_note(client, 10569, note, "units don't match")
        after = snapshot(client.get_item("ap_invoices", 10569))
        ok = (
            bool(saved)
            and require_author(saved)
            and 33 in saved["mentions"]
            and after["lines"] == before["lines"]
            and after["batch_id"] == before["batch_id"]
            and after["invoice"] == before["invoice"]
        )
        record_row(10569, "posted-note-only unit mismatch", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)
        return
    if uom_kind(str(fact.get("uom") or "")) != "in" or abs(float(fact.get("qty") or 0) - 40) > 0.02:
        record_row(10569, f"not changed; receipt 25214 is qty {fact.get('qty')} {fact.get('uom')}", before_text, before_text, "", "", False)
        return
    if selected:
        status = remove_child_lines(client, 10569, [int(line["id"]) for line in selected], drop_ppv=True)
        if status != "removed":
            after = snapshot(client.get_item("ap_invoices", 10569))
            record_row(10569, f"deselect receipt 25214 {status}", before_text, brief(after), "", "", False)
            return
    moved = move_to_transfer(client, 10569)
    note = ap_clerk_edit_note(
        "AP Clerk: @Shawn McKibben units don't match on Morgan Steel invoice 130154. "
        "It bills 40 pieces and receipt 25214 is recorded as 40 inches, so I unselected that receipt and moved the bill to Transfer AP. "
        "Please confirm the receipt. It looks similar to held invoice 130153 on PO 59013, but this is a separate PO 59199 and a separate sales order.",
        action="shawn",
    )
    saved = add_note(client, 10569, note, "units don't match")
    after = snapshot(client.get_item("ap_invoices", 10569))
    ok = (
        moved
        and int(after.get("batch_id") or 0) == TRANSFER_ID
        and not posted(after["posted"])
        and 25214 not in {int(line.get("receipt_id") or 0) for line in after["lines"]}
        and bool(saved)
        and require_author(saved)
        and 104 in saved["mentions"]
    )
    record_row(10569, "unselected receipt 25214 and moved to Transfer AP", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)


def compare_10471(client: KimcoClient, this_invoice: str, this_amount: Any) -> str:
    _record, other = read_bill(client, 10471)
    if other["invoice"] == this_invoice and money(other["verification"]) == money(this_amount):
        finding = "The invoice numbers and amounts match, so it may be a duplicate."
    elif other["invoice"] == this_invoice:
        finding = (
            f"The invoice numbers match, but bill 10471 is ${other['verification']} and this bill is ${this_amount}."
        )
    else:
        finding = (
            f"Bill 10471 is invoice {other['invoice']} for ${other['verification']}. "
            f"This bill is invoice {this_invoice} for ${this_amount}. The invoice numbers are different."
        )
    return (
        f"Bill 10471 is {other['vendor']} invoice {other['invoice']} for ${other['verification']}, "
        f"batch {other['batch_id']}, posted={other['posted']}. {finding}"
    )


def fix_10515(client: KimcoClient) -> None:
    _record, before = read_bill(client, 10515)
    before_text = brief(before)
    duplicate_sentence = compare_10471(client, before["invoice"], before["verification"])
    if posted(before["posted"]) or before.get("void") not in (None, "", False):
        note = ap_clerk_edit_note(
            "AP Clerk: @Treyce Hodges bill 10515 is already posted or void, so I did not remove the miscellaneous line or move the batch. "
            f"Capital Machine invoice {before['invoice']} for ${before['verification']} prints PO 59088, which needs a receipt. {duplicate_sentence}",
            action="treyce",
        )
        saved = add_note(client, 10515, note, "prints PO 59088")
        after = snapshot(client.get_item("ap_invoices", 10515))
        ok = bool(saved) and require_author(saved) and 33 in saved["mentions"] and after["invoice"] == before["invoice"]
        record_row(10515, "posted-note-only PO 59088", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)
        return
    misc = [line for line in before["lines"] if not line.get("receipt_id")]
    if misc:
        fresh, live = read_bill(client, 10515)
        if posted(live["posted"]):
            record_row(10515, "became posted before the edit; not changed", before_text, brief(live), "", "", False)
            return
        status = remove_child_lines(client, 10515, [int(line["id"]) for line in misc if line.get("id")], drop_ppv=False)
        if status != "removed":
            record_row(10515, f"misc line removal {status}", before_text, brief(snapshot(client.get_item("ap_invoices", 10515))), "", "", False)
            return
    moved = move_to_transfer(client, 10515)
    note = ap_clerk_edit_note(
        "AP Clerk: @Shawn McKibben Capital Machine invoice "
        f"{before['invoice']} for ${before['verification']} was coded miscellaneous, but the invoice prints PO 59088. "
        "I removed the miscellaneous line and moved the bill to Transfer AP. PO 59088 exists and needs a receipt. "
        f"{duplicate_sentence}",
        action="shawn",
    )
    saved = add_note(client, 10515, note, "prints PO 59088")
    after = snapshot(client.get_item("ap_invoices", 10515))
    ok = (
        moved
        and int(after.get("batch_id") or 0) == TRANSFER_ID
        and not posted(after["posted"])
        and not [line for line in after["lines"] if not line.get("receipt_id")]
        and bool(saved)
        and require_author(saved)
        and 104 in saved["mentions"]
    )
    record_row(10515, "removed misc line and moved to Transfer AP", before_text, brief(after) + " " + comparison, (saved or {}).get("id"), (saved or {}).get("text") or "", ok)


def report_10519(client: KimcoClient) -> None:
    record, row = read_bill(client, 10519)
    receipt_bits = []
    for line in row["lines"]:
        if not line.get("receipt_id"):
            continue
        receipt = client.get_item("receipts", int(line["receipt_id"]))
        fact = receipt_fact(receipt)
        fact["uom"] = uom_text(receipt)
        values = receipt.get("values") or {}
        po = values.get("PO_Number") or values.get("Purchase_Order")
        vendor = values.get("Vendor")
        receipt_bits.append(
            f"receipt {line['receipt_id']} qty {fact.get('qty')} {fact.get('uom')} ${fact.get('extended')} "
            f"part {fact.get('part')} po {lookup_text(po) or lookup_id(po)} vendor {lookup_text(vendor) or lookup_id(vendor)}"
        )
    text = (
        f"{brief(row)}. QA said this used Capital Machine's PO 59302 receipt. "
        f"Live vendor is {row['vendor']}. Header PO is {row['po'] or row['po_id'] or 'blank'}. "
        + ("; ".join(receipt_bits) if receipt_bits else "No receipt is selected.")
        + " Not changed."
    )
    record_row(10519, "report only", text, text, "", "", True)


def note_10472(client: KimcoClient) -> None:
    record, before = read_bill(client, 10472)
    before_text = brief(before)
    receipts = ",".join(str(line["receipt_id"]) for line in before["lines"] if line.get("receipt_id")) or "none"
    on_transfer = int(before.get("batch_id") or 0) == TRANSFER_ID
    if posted(before["posted"]) or on_transfer:
        owner = "treyce"
        who = "@Treyce Hodges"
    else:
        owner = "shawn"
        who = "@Shawn McKibben"
    held_because = []
    if not before["lines"]:
        held_because.append("it has no invoice lines")
    if receipts == "none":
        held_because.append("no receipt is selected")
    if before["verification"] not in (None, "") and money(before["covered"]) != money(before["verification"]):
        held_because.append(f"lines and charges are ${before['covered']} against the header ${before['verification']}")
    if posted(before["posted"]):
        held_because.append("it is posted")
    why = "; ".join(held_because) if held_because else "the catch-up died before the bill was finished or noted"
    text = (
        f"AP Clerk: {who} Xcaliber invoice {before['invoice']} is an orphan from a crashed run. "
        "The 10/6 catch-up stopped on this invoice when the response ended prematurely, and no note was saved. "
        f"Live state: batch {before['batch'] or before['batch_id']}, header ${before['verification']}, "
        f"posted {before['posted']}, receipts {receipts}, {len(before['notes'])} existing notes. "
        f"It is held because {why}. I did not change anything else."
    )
    html = ap_clerk_edit_note(text, action=owner)
    saved = add_note(client, 10472, html, "orphan from a crashed run")
    after = snapshot(client.get_item("ap_invoices", 10472))
    same_coding = (
        after["invoice"] == before["invoice"]
        and after["batch_id"] == before["batch_id"]
        and after["lines"] == before["lines"]
        and after["posted"] == before["posted"]
        and after["verification"] == before["verification"]
    )
    mention = 33 if owner == "treyce" else 104
    ok = bool(saved) and require_author(saved) and mention in saved["mentions"] and same_coding
    record_row(10472, "note only; orphan from crashed run", before_text, brief(after), (saved or {}).get("id"), (saved or {}).get("text") or "", ok)


def notes_tpi(client: KimcoClient) -> None:
    _left, bill = read_bill(client, 10491)
    _right, other = read_bill(client, 10555)
    receipt = client.get_item("receipts", 24982)
    fact = receipt_fact(receipt)
    fact["uom"] = uom_text(receipt)
    values = receipt.get("values") or {}
    po = lookup_text(values.get("PO_Number") or values.get("Purchase_Order"))
    selected = [line for line in bill["lines"] if int(line.get("receipt_id") or 0) == 24982]
    qty_on_bill = ",".join(str(line.get("qty")) for line in selected) or "none"
    facts = (
        f"10491 invoice {bill['invoice']} posted={bill['posted']} receipt line qty {qty_on_bill}. "
        f"Receipt 24982 qty {fact.get('qty')} {fact.get('uom')} part {fact.get('part')} po {po}. "
        f"10555 invoice {other['invoice']} posted={other['posted']} batch {other['batch_id']}."
    )
    part = str(fact.get("part") or "")
    matches = (
        bill["invoice"] == "18564"
        and other["invoice"] == "18664"
        and selected
        and "A-04421-000" in part
        and "58931" in str(po)
        and any(abs(float(line.get("qty") or 0) - 4) < 0.02 for line in selected)
    )
    if matches:
        left_text = (
            "AP Clerk: @Treyce Hodges this posted bill selected receipt 24982 (66 ea A-04421-000, PO 58931) "
            "that belongs to invoice 18664, and its quantity was changed to 4. "
            "It needs a posted-bill adjustment so 18664 (bill 10555) can be matched."
        )
        right_text = (
            "AP Clerk: @Treyce Hodges this note points at bill 10491. Invoice 18664 is waiting on that posted bill, "
            "invoice 18564, which selected receipt 24982 that belongs here and changed its quantity to 4."
        )
    else:
        left_text = (
            "AP Clerk: @Treyce Hodges I did not edit this bill. "
            f"Live read: {facts} Please review the receipt selection. Kyle's note expected receipt 24982, part A-04421-000, PO 58931, quantity changed to 4, for invoice 18664."
        )
        right_text = (
            "AP Clerk: @Treyce Hodges this note points at bill 10491. "
            f"Live read: {facts}"
        )
    if not posted(bill["posted"]):
        left_text = left_text.replace("this posted bill", "this bill is not posted and", 1)
    saved_left = add_note(client, 10491, ap_clerk_edit_note(left_text, action="treyce"), "posted-bill adjustment")
    saved_right = add_note(client, 10555, ap_clerk_edit_note(right_text, action="treyce"), "points at bill 10491")
    after_left = snapshot(client.get_item("ap_invoices", 10491))
    after_right = snapshot(client.get_item("ap_invoices", 10555))
    coding_same = after_left["lines"] == bill["lines"] and after_left["batch_id"] == bill["batch_id"] and after_left["invoice"] == bill["invoice"]
    coding_same = coding_same and after_right["lines"] == other["lines"] and after_right["invoice"] == other["invoice"]
    ok_left = bool(saved_left) and require_author(saved_left) and 33 in saved_left["mentions"] and coding_same
    ok_right = bool(saved_right) and require_author(saved_right) and 33 in saved_right["mentions"] and coding_same
    record_row(10491, "note only; posted receipt 24982", brief(bill), brief(after_left) + " " + facts, (saved_left or {}).get("id"), (saved_left or {}).get("text") or "", ok_left)
    record_row(10555, "note only; points at bill 10491", brief(other), brief(after_right), (saved_right or {}).get("id"), (saved_right or {}).get("text") or "", ok_right)


def invoice_numbers_in_email() -> list[str]:
    document = pymupdf.open(ONEAL_PDF)
    numbers = []
    for page in document:
        for word in page.get_text("words"):
            if word[1] < 80 and re.fullmatch(r"15\d{6}", word[4]) and word[4] not in numbers:
                numbers.append(word[4])
    if numbers != [NEW_INVOICE]:
        raise SystemExit(f"Email invoice numbers were {numbers}, not {[NEW_INVOICE]}. No header created.")
    return numbers


def run_step(name: str, func) -> Any:
    try:
        return func()
    except SystemExit as exc:
        LOGGER.info("Stopped %s: %s", name, exc)
        record_row(name, f"stopped: {exc}", "", "", "", "", False)
        return None
    except KimcoError as exc:
        LOGGER.info("KIMCO error on %s: %s", name, exc)
        record_row(name, f"kimco error: {exc}", "", "", "", "", False)
        return None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    numbers = invoice_numbers_in_email()
    source = pymupdf.open(ONEAL_PDF)
    source[0].get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7), alpha=False).save(str(OUT / "15492850-source-p1.png"))
    client = login()
    install_401_guard(client)
    run_step("10582", lambda: fix_10582(client))
    outcome = run_step("15492850", lambda: enter_oneal(client)) or {}
    if outcome.get("ready"):
        run_step("email", lambda: file_email(client, numbers))
    else:
        record_row("email", "left in Inbox because the new invoice is not header+attachment+note", MAIL_RECEIVED, json.dumps(outcome), "", "", False)
    run_step("10569", lambda: fix_10569(client))
    run_step("10515", lambda: fix_10515(client))
    run_step("10519", lambda: report_10519(client))
    run_step("10472", lambda: note_10472(client))
    run_step("10491", lambda: notes_tpi(client))
    with (OUT / "fixes.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["bill", "change", "before", "after", "note id + text", "readback ok y/n"],
        )
        writer.writeheader()
        writer.writerows(ROWS)
    (OUT / "fixes.json").write_text(json.dumps(ROWS, indent=2))
    LOGGER.info("Wrote %s", OUT / "fixes.csv")


if __name__ == "__main__":
    main()
