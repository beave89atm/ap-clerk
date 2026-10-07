"""Enter Source Metals invoices 470223 and 470471 on batch 749.

The mailbox sender is Fabcorp. The invoices are Source Metals, vendor 338.
Does not post, close the batch, or send mail. One login, then at most one
re-sign-in after a 401. Page 1 of each scan is the invoice. Pages 2 and 3
are the packing list and the bill of lading and are not attached.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import fitz
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, ENTERED_IN_AI_CATEGORY, GraphClient, load_graph_credentials
from ap_clerk.rules import due_date_from_terms, invoice_number_key, lookup_id, lookup_text
from scripts.sept25_30_attachment_audit import attachment_bytes, receipt_fact
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of
from scripts.sept25_30_po59081_receipts import child_receipt_ids
from scripts.sept_missed_entry_2026_10_07 import (
    ARCHIVE,
    BATCH_NAME,
    BATCH_TRANSFER,
    OUT,
    attach,
    create_header,
    EMAILS as ALL_EMAILS,
    enter_job,
    existing_map,
    find_message,
    install_401_guard,
    login,
    move_batch,
    note_html,
    require_transfer,
    totals,
    write_note,
    write_xlsx,
)
from scripts.sept_missed_qc_readback_2026_10_07 import line_charge_sum, note_rows, render_pdf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("source-metals")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)

LAST_ENTERED = 10537
QC = OUT / "qc"
SOURCES = {
    "470223": Path("/tmp/sept-missed-pdfs/3a80ae7c698f585a-1.pdf"),
    "470471": Path("/tmp/sept-missed-pdfs/9d6fc8bd45f6ffa7-1.pdf"),
}
EMAILS = [row for row in ALL_EMAILS if row["invoices"] in (["470223"], ["470471"])]
if [row["invoices"] for row in EMAILS] != [["470223"], ["470471"]]:
    raise SystemExit("The two Source Metals messages are not the expected pair")
# Receipt ids, quantity, unit price, extended, part token. All purchase UOM EA.
EXPECTED = {
    "470223": {
        24175: (2.0, 95.0, 190.0, "CC372310"),
        24176: (2.0, 95.0, 190.0, "CC372310"),
        24177: (2.0, 95.0, 190.0, "CC372310"),
        24178: (2.0, 95.0, 190.0, "CC372310"),
        24181: (2.0, 95.0, 190.0, "CC372310"),
        24182: (2.0, 95.0, 190.0, "CC372310"),
        24184: (2.0, 20.0, 40.0, "CB372311"),
        24185: (2.0, 20.0, 40.0, "CB372311"),
        24186: (2.0, 20.0, 40.0, "CB372311"),
        24179: (2.0, 15.0, 30.0, "CB372312"),
        24180: (2.0, 15.0, 30.0, "CB372312"),
        24183: (2.0, 15.0, 30.0, "CB372312"),
    },
    "470471": {
        25258: (4.0, 262.5, 1050.0, "316-RECT"),
    },
}
LINES = {
    "470223": [17482, 17483, 17484, 17485, 17486, 17487, 17488, 17489, 17580, 17581, 17582, 17583],
    "470471": [17827],
}


def collapsed(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def page_one(number: str) -> Path:
    source = SOURCES[number]
    reader = PdfReader(str(source))
    if len(reader.pages) != 3:
        raise SystemExit(f"{number} scan has {len(reader.pages)} pages, not the invoice plus packing list plus bill of lading")
    dest = OUT / f"{number}-page1.pdf"
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)
    if len(PdfReader(str(dest)).pages) != 1:
        raise SystemExit(f"{number} attachment is not a single page")
    return dest


def ocr_page(path: Path) -> str:
    document = fitz.open(path)
    if document.page_count != 1:
        raise SystemExit(f"{path.name} has {document.page_count} pages")
    image = path.with_suffix(".png")
    document[0].get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False).save(str(image))
    return subprocess.check_output(["tesseract", str(image), "stdout"], text=True, stderr=subprocess.DEVNULL)


def require_invoice_page(number: str, path: Path, total: str, freight: str, po: str) -> None:
    text = ocr_page(path)
    upper = text.upper()
    for banned in ("PACKING LIST", "BILL OF LADING", "SAIA"):
        if banned in upper:
            raise SystemExit(f"{number} page 1 contains {banned}. Not attaching it.")
    if number not in text or po not in text or total not in text or freight not in text:
        raise SystemExit(f"{number} page 1 is missing the invoice number, PO, freight, or total")
    if "NET 30" not in upper:
        raise SystemExit(f"{number} page 1 does not print NET 30")


def uom_text(fact: dict[str, Any]) -> str:
    purchase = (fact.get("unit_fields") or {}).get("Purchase_UOM") or {}
    if isinstance(purchase, dict):
        return str(purchase.get("text") or "")
    return str(purchase or "")


def live_receipts(client, number: str) -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for line_id in LINES[number]:
        record = client.get_item("purchase_lines", line_id)
        for receipt_id in child_receipt_ids(record):
            receipt = client.get_item("receipts", receipt_id)
            fact = receipt_fact(receipt)
            values = receipt.get("values") or {}
            fact["unit_fields"] = {
                "Purchase_UOM": values.get("Purchase_UOM"),
                "Inventory_UOM": values.get("Inventory_UOM"),
            }
            fact.pop("fields", None)
            found[int(receipt_id)] = fact
    return found


def receipts_ready(number: str, live: dict[int, dict[str, Any]]) -> str:
    """Return '' when the open each-unit receipts match the invoice.

    A non-empty string is a hold reason and means nothing may be selected.
    """
    expected = EXPECTED[number]
    if set(live) != set(expected):
        return (
            f"PO receipt ids are {sorted(live)}, not {sorted(expected)}. "
            "Nothing was selected."
        )
    for receipt_id, (qty, unit, extended, token) in expected.items():
        fact = live[receipt_id]
        uom = uom_text(fact)
        if not uom.upper().startswith("EA"):
            return (
                f"Receipt {receipt_id} unit is {uom or 'blank'}, not each. "
                "Nothing was selected."
            )
        if not fact.get("open") or fact.get("quantity_invoiced") not in (None, 0, 0.0):
            return f"Receipt {receipt_id} is already invoiced. Nothing was selected."
        if fact.get("invoiced") in (True, "true", "True"):
            return f"Receipt {receipt_id} is already invoiced. Nothing was selected."
        if token not in str(fact.get("part") or ""):
            return f"Receipt {receipt_id} part is {fact.get('part')}. Nothing was selected."
        if float(fact.get("qty") or 0) != qty or float(fact.get("unit") or 0) != unit or float(fact.get("extended") or 0) != extended:
            got = (fact.get("qty"), fact.get("unit"), fact.get("extended"))
            invoice_ext = round(qty * unit, 2)
            gap = round(abs(float(fact.get("extended") or 0) - invoice_ext), 2)
            if gap >= 75:
                return (
                    f"Receipt {receipt_id} is {got} against {qty} at {unit}. "
                    f"The ${gap:,.2f} gap is a price variance of $75 or more. Nothing was selected."
                )
            return (
                f"Receipt {receipt_id} is {got} against {qty} each at {unit}. "
                "The unit is each, so this is not a unit conversion. Nothing was selected."
            )
    return ""


def latest_terms(client) -> dict[str, Any]:
    ids: list[int] = []
    for item in client.list_items("ap_invoices", page_size=2000):
        values = item.get("values") or {}
        name = lookup_text(values.get("Vendor_$_Display_Name"))
        if "SOURCE METALS" in name.upper() and item.get("id") not in (None, ""):
            ids.append(int(item["id"]))
    posted: list[dict[str, Any]] = []
    for invoice_id in ids:
        record = client.get_item("ap_invoices", invoice_id)
        values = record.get("values") or {}
        vendor = values.get("Vendor")
        if lookup_id(vendor) != 338:
            continue
        if values.get("Posted") in (None, "", False):
            continue
        if values.get("Void") in (True, "true", "True"):
            continue
        posted.append(
            {
                "id": invoice_id,
                "invoice": values.get("Invoice_Number"),
                "date": str(values.get("Invoice_Date") or ""),
                "terms_id": lookup_id(values.get("Terms_Code")),
                "terms_text": lookup_text(values.get("Terms_Code")),
                "remit_id": lookup_id(values.get("Remit_To_Address")),
                "remit_text": lookup_text(values.get("Remit_To_Address")),
            }
        )
    if not posted:
        raise SystemExit("No posted Source Metals bill to copy terms and remit from. Stopping.")
    posted.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
    sample = posted[0]
    if sample["terms_id"] is None or sample["remit_id"] is None:
        raise SystemExit(f"Posted bill {sample['id']} is missing terms or remit. Stopping.")
    if "30" not in str(sample["terms_text"]):
        raise SystemExit(f"Latest posted terms are {sample['terms_text']}, not Net 30. Stopping.")
    LOGGER.info(
        "Terms from posted bill %s %s %s remit %s",
        sample["id"],
        sample["invoice"],
        sample["terms_text"],
        sample["remit_id"],
    )
    return sample


def build_jobs(pdfs: dict[str, Path], terms: dict[str, Any]) -> list[dict[str, Any]]:
    due_223 = due_date_from_terms(date(2026, 9, 15), terms["terms_text"])
    due_471 = due_date_from_terms(date(2026, 9, 28), terms["terms_text"])
    if due_223 != date(2026, 10, 15) or due_471 != date(2026, 10, 28):
        raise SystemExit(f"Net 30 due dates came out {due_223} and {due_471}. Stopping.")
    note_223 = (
        "PO 59060 receipts 24175, 24176, 24177, 24178, 24181, and 24182 are 12 pieces of the 1.25 plate at $95.00, $1,140.00. "
        "Receipts 24184, 24185, and 24186 are 6 pieces at $20.00, $120.00. "
        "Receipts 24179, 24180, and 24183 are 6 pieces at $15.00, $90.00. "
        "The unit is each on the invoice and the receipts. Freight $275.00 is Freight External. There is no price variance."
    )
    note_471 = (
        "PO 59216 receipt 25258 is 4 pieces at $262.50, $1,050.00. "
        "The unit is each on the invoice and the receipt. Freight $285.00 is Freight External. There is no price variance."
    )
    common = {
        "vendor": "Source Metals",
        "vendor_id": 338,
        "terms_id": int(terms["terms_id"]),
        "terms_text": terms["terms_text"],
        "remit_id": int(terms["remit_id"]),
        "tax": 0.0,
        "ppv": 0.0,
        "fee": 0.0,
        "lines": [],
        "kind": "po",
        "owner": "treyce",
        "wave": "A",
    }
    first = dict(common)
    first.update(
        {
            "pdf": pdfs["470223"],
            "number": "470223",
            "amount": 1625.00,
            "day": date(2026, 9, 15),
            "due": due_223,
            "po_id": 7062,
            "po_text": "PO 59060",
            "receipts": list(EXPECTED["470223"]),
            "freight": 275.00,
            "merch": 1350.00,
            "note": note_223,
        }
    )
    second = dict(common)
    second.update(
        {
            "pdf": pdfs["470471"],
            "number": "470471",
            "amount": 1335.00,
            "day": date(2026, 9, 28),
            "due": due_471,
            "po_id": 7218,
            "po_text": "PO 59216",
            "receipts": list(EXPECTED["470471"]),
            "freight": 285.00,
            "merch": 1050.00,
            "note": note_471,
        }
    )
    return [first, second]


def hold_without_receipts(client, job: dict[str, Any], batch_id: int, existing: dict[str, list[dict[str, Any]]], reason: str) -> dict[str, Any]:
    """Header, page 1, Transfer AP, and one Shawn note. No receipt selection."""
    hits = existing.get(invoice_number_key(job["number"])) or []
    if hits:
        raise SystemExit(f"{job['number']} already exists as {hits}. Not creating a hold over it.")
    job["kind"] = "missing"
    job["owner"] = "shawn"
    job["note"] = reason
    created = create_header(client, job, batch_id)
    if created <= LAST_ENTERED:
        raise SystemExit(f"Created id {created} is not a new bill. Stopping.")
    attach_status = attach(client, created, job["pdf"])
    move_status = move_batch(client, created, BATCH_TRANSFER)
    text = f"AP Clerk: Source Metals invoice {job['number']}. {reason} The bill is not posted. It is on hold in Transfer AP."
    note_id = write_note(client, created, note_html(job, text))
    final = totals(client.get_item("ap_invoices", created))
    return {
        "vendor": job["vendor"],
        "invoice": job["number"],
        "amount": job["amount"],
        "bill_id": created,
        "result": "HOLD",
        "batch": "A / TRANSFER AP",
        "reason": reason,
        "note_id": note_id or "",
        "email_moved": "n",
        "email_ready": bool(note_id) and attach_status == "attached" and final["posted"] in (None, "", False),
        "wave": "A",
        "kind": "missing",
        "covered": final["covered"],
        "attachments": [item.get("name") or item.get("fileName") for item in client.list_attachments(created)],
        "batch_id": final.get("batch_id"),
        "posted": final.get("posted"),
        "attach_status": attach_status,
        "move_status": move_status,
    }


def norm_subject(value: str) -> str:
    return collapsed(value).lower()


def locate(graph: GraphClient, expected: dict[str, Any], cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    exact = find_message(graph, expected, cache)
    if exact:
        return exact
    day = date.fromisoformat(expected["received"][:10])
    key = day.isoformat()
    if key not in cache:
        cache[key] = graph.list_messages(ALLOWED_MAILBOX, received_from=day, received_to=day)
    found = []
    for row in cache[key]:
        if str(row.get("receivedDateTime") or "") != expected["received"]:
            continue
        if sender_of(row).lower() != expected["sender"].lower():
            continue
        if norm_subject(str(row.get("subject") or "")) != norm_subject(expected["subject"]):
            continue
        full = graph.get_message(
            ALLOWED_MAILBOX,
            str(row.get("id") or ""),
            select="id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime",
        )
        if str(full.get("receivedDateTime") or "") == expected["received"] and sender_of(full).lower() == expected["sender"].lower():
            found.append(full)
    unique = {str(item.get("id") or ""): item for item in found if item.get("id")}
    return list(unique.values())


def move_one(graph: GraphClient, archive_id: str, folders: dict[str, dict[str, Any]], expected: dict[str, Any], cache: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    matches = locate(graph, expected, cache)
    if len(matches) != 1:
        return {"email_moved": "n", "email_error": "not-found" if not matches else "several"}
    message = matches[0]
    before = str(message.get("lastModifiedDateTime") or "")
    again = locate(graph, expected, {})
    if len(again) != 1 or str(again[0].get("id") or "") != str(message.get("id") or ""):
        return {"email_moved": "n", "email_error": "changed-before-move"}
    if str(again[0].get("lastModifiedDateTime") or "") != before:
        return {"email_moved": "n", "email_error": "lastModified-changed"}
    path = folder_path(graph, str(message.get("parentFolderId") or ""), folders)
    if path == ARCHIVE:
        cats = [str(item) for item in (message.get("categories") or [])]
        ok = cats == [ENTERED_IN_AI_CATEGORY]
        return {
            "email_moved": "y" if ok else "n",
            "email_verified_folder": path,
            "email_verified_categories": cats,
            "email_error": "" if ok else "already-archived",
        }
    message_id = str(message.get("id") or "")
    patched = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        json={"categories": [ENTERED_IN_AI_CATEGORY]},
        headers={"Content-Type": "application/json"},
    )
    if patched.status_code >= 400:
        return {"email_moved": "n", "email_error": f"category-{patched.status_code}"}
    outcome = graph.move_message(ALLOWED_MAILBOX, message_id, archive_id)
    new_id = str(outcome.get("new_id") or "")
    if outcome.get("status") != "moved-fort-worth" or not new_id:
        return {"email_moved": "n", "email_error": "move-failed"}
    verified = graph.get_message(
        ALLOWED_MAILBOX,
        new_id,
        select="id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime",
    )
    new_path = folder_path(graph, str(verified.get("parentFolderId") or ""), folders)
    cats = [str(item) for item in (verified.get("categories") or [])]
    ok = (
        new_path == ARCHIVE
        and cats == [ENTERED_IN_AI_CATEGORY]
        and str(verified.get("receivedDateTime") or "") == expected["received"]
        and sender_of(verified).lower() == expected["sender"].lower()
    )
    return {
        "email_moved": "y" if ok else "n",
        "email_verified_folder": new_path,
        "email_verified_categories": cats,
        "email_error": "" if ok else "verify-failed",
    }


def store_page(client, row: dict[str, Any]) -> None:
    bill_id = int(row["bill_id"])
    items = client.list_attachments(bill_id)
    content = None
    for item in items:
        content = attachment_bytes(client, bill_id, item)
        if content:
            break
    if not content or content[:5] != b"%PDF-":
        raise SystemExit(f"Bill {bill_id} attachment could not be downloaded")
    document = fitz.open(stream=content, filetype="pdf")
    if document.page_count != 1:
        raise SystemExit(f"Bill {bill_id} stored attachment has {document.page_count} pages")
    text = ocr_page_bytes(content)
    if row["invoice"] not in text:
        raise SystemExit(f"Stored attachment on {bill_id} does not show {row['invoice']}")
    upper = text.upper()
    for banned in ("PACKING LIST", "BILL OF LADING", "SAIA"):
        if banned in upper:
            raise SystemExit(f"Stored attachment on {bill_id} contains {banned}")
    saved = render_pdf(content, str(bill_id))
    row["qc_pages"] = [str(path.relative_to(ROOT)) for path in saved]
    record = client.get_item("ap_invoices", bill_id)
    values = record.get("values") or {}
    covered, items_used, receipts = line_charge_sum(record)
    header = float(values.get("Invoice_Verification_Amount") or 0)
    notes = note_rows(record)
    note = ""
    if notes:
        note = f"{notes[-1][0]} | {notes[-1][1]}"
    batch = values.get("AP_Invoice_Batch") or {}
    batch_label = f"{batch.get('id')} {lookup_text(batch)}".strip()
    row["readback"] = [
        bill_id,
        lookup_text(values.get("Vendor")),
        values.get("Invoice_Number"),
        f"{header:.2f}",
        f"{covered:.2f}",
        f"{round(header - covered, 2):.2f}",
        "; ".join(items_used),
        "; ".join(receipts) if receipts else "none",
        batch_label,
        note,
        document.page_count,
    ]


def ocr_page_bytes(content: bytes) -> str:
    document = fitz.open(stream=content, filetype="pdf")
    image = Path("/tmp/source-metals-stored.png")
    document[0].get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False).save(str(image))
    return subprocess.check_output(["tesseract", str(image), "stdout"], text=True, stderr=subprocess.DEVNULL)


def replace_rows(rows: list[dict[str, Any]]) -> None:
    path = OUT / "result.json"
    payload = json.loads(path.read_text())
    by_invoice = {row["invoice"]: row for row in rows}
    bills = []
    for bill in payload["bills"]:
        if bill.get("invoice") in by_invoice:
            bills.append(by_invoice.pop(bill["invoice"]))
        else:
            bills.append(bill)
    if by_invoice:
        raise SystemExit(f"Result rows not replaced: {sorted(by_invoice)}")
    payload["bills"] = bills
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    write_xlsx(OUT / "AP-sept-missed-2026-10-07.xlsx", bills)


def append_readback(rows: list[dict[str, Any]]) -> None:
    path = QC / "readback.csv"
    existing = path.read_text().splitlines()
    header = existing[0]
    kept = [header]
    replace = {str(row["bill_id"]) for row in rows}
    for line in existing[1:]:
        if line.split(",", 1)[0] not in replace:
            kept.append(line)
    with path.open("w", newline="") as handle:
        handle.write("\n".join(kept) + "\n")
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(row["readback"])


def main() -> None:
    pdfs = {number: page_one(number) for number in SOURCES}
    require_invoice_page("470223", pdfs["470223"], "1,625.00", "275.00", "59060")
    require_invoice_page("470471", pdfs["470471"], "1,335.00", "285.00", "59216")
    client = login()
    install_401_guard(client)
    require_transfer(client)
    batch = client.get_item("ap_batches", 749)
    values = batch.get("values") or {}
    if str(values.get("AP_Invoice_Batch_ID") or "") != BATCH_NAME or values.get("Status") not in (0, "0"):
        raise SystemExit(f"Batch 749 is {values.get('AP_Invoice_Batch_ID')!r} status {values.get('Status')}. Stopping.")
    terms = latest_terms(client)
    existing = existing_map(client)
    for number in ("470223", "470471"):
        hits = existing.get(number) or []
        if hits:
            LOGGER.info("Existing %s %s", number, hits)
    jobs = build_jobs(pdfs, terms)
    rows = []
    for job in jobs:
        hold_reason = ""
        if not (existing.get(job["number"]) or []):
            hold_reason = receipts_ready(job["number"], live_receipts(client, job["number"]))
        if hold_reason:
            LOGGER.info("HOLD %s %s", job["number"], hold_reason)
            row = hold_without_receipts(client, job, 749, existing, hold_reason)
        else:
            row = enter_job(client, job, 749, existing)
        if int(row.get("bill_id") or 0) <= LAST_ENTERED:
            raise SystemExit(f"{job['number']} came back as bill {row.get('bill_id')}. Stopping.")
        rows.append(row)
        (OUT / "source-metals-progress.json").write_text(json.dumps(rows, indent=2, default=str))
        LOGGER.info("%s %s bill %s", row["result"], row["invoice"], row["bill_id"])
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    archive_id = archive_folder(graph)
    folders: dict[str, dict[str, Any]] = {}
    cache: dict[str, list[dict[str, Any]]] = {}
    ready = {row["invoice"]: row for row in rows if row.get("email_ready")}
    for expected in EMAILS:
        if not all(number in ready for number in expected["invoices"]):
            LOGGER.info("Leave %s until its bill is finished", expected["received"])
            continue
        outcome = move_one(graph, archive_id, folders, expected, cache)
        for number in expected["invoices"]:
            ready[number].update(outcome)
    (OUT / "source-metals-progress.json").write_text(json.dumps(rows, indent=2, default=str))
    for row in rows:
        if row.get("bill_id"):
            store_page(client, row)
    confirm = client.get_item("ap_batches", 749)
    if (confirm.get("values") or {}).get("Status") not in (0, "0"):
        raise SystemExit("Batch 749 is no longer open")
    for row in rows:
        record = client.get_item("ap_invoices", int(row["bill_id"]))
        if (record.get("values") or {}).get("Posted") not in (None, "", False):
            raise SystemExit(f"Bill {row['bill_id']} is posted. Stopping.")
    replace_rows(rows)
    append_readback(rows)
    (OUT / "source-metals-progress.json").write_text(json.dumps(rows, indent=2, default=str))
    for row in rows:
        LOGGER.info(
            "%s %s bill %s batch %s note %s email %s",
            row["result"],
            row["invoice"],
            row["bill_id"],
            row.get("batch_id"),
            row.get("note_id"),
            row.get("email_moved"),
        )


if __name__ == "__main__":
    main()
