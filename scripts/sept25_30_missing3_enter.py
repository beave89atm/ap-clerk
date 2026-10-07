"""Enter the three invoices that were inside proposed archive emails.

Creates only Gas 0040455291, EMJ T609084432, and O'Neal 15487245.
Does not post, close a batch, send mail, or change any message.

Login: one attempt. A later 401 signs in once more and then stops.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials
from ap_clerk.kimco import KimcoError, added_comment_payload
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.rules import (
    ap_clerk_edit_note,
    comments_for,
    due_date_from_terms,
    invoice_number_key,
    kimco_datetime,
    lookup_text,
    money,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("missing3")

BATCH_AGENT = 747
BATCH_TRANSFER = 375
SHOP_SUPPLIES = 31
DEST = ROOT / "runs" / "qc2530-missing3"
SRC = Path("/tmp/missing3")
QC_PATH = ROOT / "qc" / "sept25-30-finish-qc.json"
CONTENTS = ROOT / "runs" / "sept25-30-email-contents-check.csv"
DRYRUN = ROOT / "runs" / "sept25-30-email-move-dryrun.csv"
SKIP_RECEIVED = {
    "2026-09-29T04:48:59Z",
    "2026-09-30T03:11:51Z",
    "2026-10-01T04:29:10Z",
}

_SPEC = importlib.util.spec_from_file_location(
    "sept25_30_finish_2026_10_06",
    ROOT / "scripts" / "sept25_30_finish_2026_10_06.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)
login = _MOD.login
install_401_guard = _MOD.install_401_guard


def slice_page(src: Path, dest: Path, index: int) -> None:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    writer.add_page(reader.pages[index])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)


def prepare_pdfs() -> dict[str, Path]:
    DEST.mkdir(parents=True, exist_ok=True)
    mapping = {
        "gas_full": (
            SRC / "gas_2026-09-29T04-48-59Z_0_billing01_A3050_c.pdf",
            DEST / "Gas_2026-09-29T04:48:59Z.pdf",
            None,
        ),
        "emj_full": (
            SRC / "emj_2026-09-30T03-11-51Z_0_Invoices.pdf",
            DEST / "EMJ_2026-09-30T03:11:51Z.pdf",
            None,
        ),
        "oneal_full": (
            SRC / "oneal_2026-10-01T04-29-10Z_0_O'Neal Steel Invoice 9302026.pdf",
            DEST / "ONeal_2026-10-01T04:29:10Z.pdf",
            None,
        ),
    }
    pages = {
        "gas_bill": (mapping["gas_full"][0], DEST / "bill_0040455291.pdf", 3),
        "emj_bill": (mapping["emj_full"][0], DEST / "bill_T609084432.pdf", 1),
        "oneal_bill": (mapping["oneal_full"][0], DEST / "bill_15487245.pdf", 7),
    }
    out: dict[str, Path] = {}
    for key, (src, dest, index) in {**mapping, **pages}.items():
        if not src.is_file():
            raise SystemExit(f"Missing source PDF {src.name}")
        if index is None:
            dest.write_bytes(src.read_bytes())
        else:
            slice_page(src, dest, index)
        out[key] = dest
    return out


def existing_numbers(client) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {key: [] for key in ("0040455291", "T609084432", "15487245")}
    for item in client.list_items("ap_invoices", page_size=2000):
        number = invoice_number_key(str((item.get("values") or {}).get("Invoice_Number") or ""))
        if number in found:
            found[number].append(int(item["id"]))
    return found


def require_batches(client) -> None:
    agent = client.get_item("ap_batches", BATCH_AGENT)
    transfer = client.get_item("ap_batches", BATCH_TRANSFER)
    agent_name = str((agent.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    transfer_name = str((transfer.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if agent_name != "API Agent - 10/6/26 Sept":
        raise SystemExit("Batch 747 is not the September agent batch. Stopping.")
    if transfer_name != "TRANSFER AP":
        raise SystemExit("Batch 375 is not TRANSFER AP. Stopping.")


def put(client, invoice_id: int, payload: dict[str, Any]) -> tuple[int, str]:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if payload.get("Posted") not in (None, "", False) or values.get("Posted") not in (None, "", False):
        raise SystemExit(f"Refusing to post bill {invoice_id}")
    _body, status, error = client.update("ap_invoices", int(invoice_id), payload)
    LOGGER.info("PUT %s HTTP %s", invoice_id, status)
    return int(status), "" if status < 400 else f"http-{status}"


def create_header(
    client,
    *,
    vendor_id: int,
    number: str,
    amount: float,
    day: date,
    terms_id: int,
    terms_text: str,
    remit_id: int,
    invoice_type: int,
    po_id: int | None,
) -> int:
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": BATCH_AGENT},
        "Vendor": {"id": int(vendor_id)},
        "Invoice_Number": number,
        "Invoice_Type": int(invoice_type),
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": float(amount),
        "Invoice_Due_Date": kimco_datetime(due_date_from_terms(day, terms_text)),
        "Terms_Code": {"id": int(terms_id)},
        "Currency": {"id": 3},
        "Remit_To_Address": {"id": int(remit_id)},
        "Transaction_Date": kimco_datetime(day),
        "Comments": comments_for("live"),
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    if payload.get("Posted") not in (None, "", False):
        raise SystemExit("Refusing to create a posted bill")
    created, _body, status, _error = client.create("ap_invoices", payload)
    if created is None:
        raise SystemExit(f"Header create for {number} failed HTTP {status}")
    created = int(created)
    if created <= 10509:
        raise SystemExit(f"Create for {number} returned existing id {created}. Not editing it.")
    record = client.get_item("ap_invoices", created)
    values = record.get("values") or {}
    if str(values.get("Invoice_Number") or "") != number:
        raise SystemExit(f"Created id {created} is not invoice {number}. Stopping.")
    if values.get("Posted") not in (None, "", False):
        raise SystemExit(f"Created bill {created} is posted. Stopping.")
    LOGGER.info("Created %s as %s", number, created)
    return created


def receipt_fact(client, receipt_id: int) -> dict[str, Any]:
    record = client.get_item("receipts", int(receipt_id))
    values = record.get("values") or {}
    part = values.get("Part_Number")
    pol = values.get("PO_Item_Number")
    qty = money(values.get("Quantity_Received"))
    unit = money(values.get("PO_Item_Number_$_Unit_Price") or values.get("Purchase_Cost") or values.get("Unit_Cost"))
    ext = money(values.get("Extended_Purchase_Cost"))
    if ext is None and qty is not None and unit is not None:
        ext = round(float(qty) * float(unit), 2)
    invoiced = money(values.get("Quantity_Invoiced") or values.get("Quantity_Billed"))
    return {
        "id": int(receipt_id),
        "name": str(values.get("Name") or ""),
        "qty": qty,
        "unit": unit,
        "ext": ext,
        "part": lookup_text(part),
        "desc": str(values.get("PO_Item_Number_$_Part_Description") or "")[:160],
        "po_line": lookup_text(pol),
        "po_line_id": pol.get("id") if isinstance(pol, dict) else None,
        "invoiced": invoiced,
    }


def severe_qty(invoice_qty: float | None, receipt_qty: float | None) -> bool:
    if invoice_qty is None or receipt_qty is None:
        return False
    return invoice_qty <= 2 and receipt_qty >= 20 and abs(invoice_qty - receipt_qty) > 1


def totals(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        item = vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")
        receipt = vals.get("Receipt")
        lines.append(
            {
                "id": line.get("id"),
                "item": lookup_text(item),
                "description": vals.get("Misc_Description") or lookup_text(item),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": vals.get("Extended_Amount"),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
                "gl": lookup_text(vals.get("Purchase_GL_Account")),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        charges.append(
            {
                "id": charge.get("id"),
                "name": vals.get("Name") or lookup_text(vals.get("Additional_Charges")),
                "amount": vals.get("Amount"),
            }
        )
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append(
            {
                "id": tax.get("id"),
                "code": lookup_text(vals.get("Tax_Code")),
                "amount": vals.get("Tax_Amount"),
                "taxable": vals.get("Taxable_Amount"),
                "rate": vals.get("Tax_Rate"),
            }
        )
    notes = []
    for comment in lists.get("Comments_1") or []:
        vals = comment.get("values") or {}
        html = str(vals.get("HtmlValue") or "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        notes.append({"id": comment.get("id"), "text": plain})
    batch = values.get("AP_Invoice_Batch") or {}
    line_sum = round(sum(float(money(row.get("extended")) or 0) for row in lines), 2)
    charge_sum = round(sum(float(money(row.get("amount")) or 0) for row in charges), 2)
    tax_sum = round(sum(float(money(row.get("amount")) or 0) for row in taxes), 2)
    return {
        "bill_id": record.get("id"),
        "invoice": values.get("Invoice_Number"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": lookup_text(batch),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "Invoice_Amount": values.get("Invoice_Amount"),
        "verification": values.get("Invoice_Verification_Amount"),
        "net": values.get("Invoice_Net_Amount"),
        "lines": lines,
        "receipts": [row["receipt_id"] for row in lines if row.get("receipt_id")],
        "charges": charges,
        "taxes": taxes,
        "notes": notes,
        "lines_charges_tax": round(line_sum + charge_sum + tax_sum, 2),
    }


def write_note(client, invoice_id: int, html: str) -> None:
    before = totals(client.get_item("ap_invoices", invoice_id))
    if any(str(note.get("text") or "").startswith("AP Clerk:") for note in before["notes"]):
        raise SystemExit(f"Bill {invoice_id} already has an AP Clerk note. Not writing a second one.")
    status, _err = put(client, invoice_id, added_comment_payload(invoice_id, html))
    if status >= 400:
        raise SystemExit(f"Note on {invoice_id} was not saved HTTP {status}")


def attach(client, invoice_id: int, path: Path) -> str:
    content = path.read_bytes()
    try:
        return client.try_official_attach(
            invoice_id,
            name=path.name,
            content_type="application/pdf",
            size=len(content),
            content=content,
        )
    except KimcoError:
        return "blocked"


def move_batch(client, invoice_id: int, batch_id: int) -> str:
    record = totals(client.get_item("ap_invoices", invoice_id))
    if int(record.get("batch_id") or 0) == int(batch_id):
        return "already"
    status, _err = put(
        client,
        invoice_id,
        {"id": invoice_id, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": int(batch_id)}}},
    )
    after = totals(client.get_item("ap_invoices", invoice_id))
    if int(after.get("batch_id") or 0) != int(batch_id):
        return f"stuck-{status}"
    return "moved"


def shawn(text: str) -> str:
    return ap_clerk_edit_note(text, action="shawn")


def treyce(text: str) -> str:
    return ap_clerk_edit_note(text, action="treyce")


def anthony(text: str) -> str:
    plain = text.strip()
    if not plain.startswith("AP Clerk:"):
        plain = "AP Clerk: " + plain
    if "@Anthony" not in plain:
        plain = plain.replace("AP Clerk:", "AP Clerk: @Anthony", 1)
    if "data-mention-id" in plain:
        raise SystemExit("Refusing to invent a mention id for Anthony")
    return f"<p>{plain}</p>"


def plain_note(text: str) -> str:
    plain = text.strip()
    if not plain.startswith("AP Clerk:"):
        plain = "AP Clerk: " + plain
    if "data-mention-id" in plain:
        raise SystemExit("Gas note must not carry a mention id")
    return f"<p>{plain}</p>"


def money_txt(value: Any) -> str:
    amount = money(value)
    return f"${amount:,.2f}" if amount is not None else "an unread amount"


def enter_gas(client, pdf: Path) -> dict[str, Any]:
    number = "0040455291"
    total = 1403.79
    created = create_header(
        client,
        vendor_id=71,
        number=number,
        amount=total,
        day=date(2026, 9, 28),
        terms_id=4,
        terms_text="F-N60-Net 60",
        remit_id=192,
        invoice_type=4,
        po_id=None,
    )
    payload = misc_add_item_payload(
        [{"description": "OTCU724E02 OTC #8 NOZZLE", "qty": 20, "unit_price": 64.84}],
        invoice_id=created,
        vendor_id=71,
        misc_item={"id": SHOP_SUPPLIES},
    )
    status, _err = put(client, created, payload)
    lines_ok = status < 400
    tax_status = "skipped"
    if lines_ok:
        tax_status = client.try_post_sales_tax(created, 106.99, taxable_amount=1296.80)
    attach_status = attach(client, created, pdf)
    live = totals(client.get_item("ap_invoices", created))
    matched = (
        lines_ok
        and tax_status == "posted"
        and attach_status == "attached"
        and float(live["lines_charges_tax"]) == total
        and float(money(live["verification"]) or 0) == total
        and live["posted"] in (None, "", False)
        and int(live["batch_id"] or 0) == BATCH_AGENT
    )
    if matched:
        note = treyce(
            "AP Clerk: @Treyce Hodges entered Gas and Supply invoice 0040455291 as Shop Supplies - G&S, "
            "20 OTC #8 nozzles at $64.84 ($1,296.80). Sales tax $106.99 is on the Taxes tab. "
            "Customer PO SHOP is not a KIMCO purchase order. "
            "The bill matches the PDF total $1,403.79 and is not posted."
        )
        result, reason = "PASS", "Merchandise is item 31 and sales tax is on the Taxes tab. The live total matches $1,403.79."
    else:
        note = plain_note(
            "AP Clerk: Gas and Supply invoice 0040455291 was entered as Shop Supplies - G&S "
            f"(lines saved: {lines_ok}, tax: {tax_status}, PDF attach: {attach_status}). "
            f"Live lines + charges + tax are {money_txt(live['lines_charges_tax'])} "
            f"against the PDF total $1,403.79. "
            "The bill is on hold on batch 747 and is not posted."
        )
        result = "HOLD"
        reason = f"Gas entry did not match. lines HTTP ok={lines_ok}, tax {tax_status}, attach {attach_status}."
    write_note(client, created, note)
    final = totals(client.get_item("ap_invoices", created))
    final["attachments"] = [row.get("name") or row.get("fileName") for row in client.list_attachments(created)]
    final["pdf_total"] = total
    final["result"] = result
    final["reason"] = reason
    final["note"] = next((row["text"] for row in final["notes"] if str(row["text"]).startswith("AP Clerk:")), "")
    final["vendor"] = "Gas and Supply North Texas, LLC"
    return final


def enter_po_hold_or_match(
    client,
    *,
    number: str,
    vendor_id: int,
    vendor_name: str,
    amount: float,
    day: date,
    terms_id: int,
    terms_text: str,
    remit_id: int,
    po_id: int | None,
    po_text: str,
    pdf: Path,
    invoice_qty: float,
    facts: list[dict[str, Any]],
    line_words: str,
) -> dict[str, Any]:
    open_facts = []
    for fact in facts:
        invoiced = fact.get("invoiced")
        qty = fact.get("qty")
        if invoiced is not None and qty is not None and float(invoiced) >= float(qty) > 0:
            continue
        open_facts.append(fact)
    choice = "missing"
    chosen: dict[str, Any] | None = None
    gap = None
    dollar_hits = [
        fact
        for fact in open_facts
        if fact.get("ext") is not None and abs(float(fact["ext"]) - amount) <= 0.05
    ]
    if len(dollar_hits) == 1:
        chosen = dollar_hits[0]
        gap = round(amount - float(chosen["ext"]), 2)
        choice = "qty" if severe_qty(invoice_qty, money(chosen.get("qty"))) else "select"
    elif len(dollar_hits) > 1:
        choice = "ambiguous"
    elif len(open_facts) == 1 and open_facts[0].get("ext") is not None:
        chosen = open_facts[0]
        gap = round(amount - float(chosen["ext"]), 2)
        if abs(gap) >= 75:
            choice = "price"
        elif severe_qty(invoice_qty, money(chosen.get("qty"))):
            choice = "qty"
        else:
            choice = "select"
    elif open_facts:
        choice = "qty"

    created = create_header(
        client,
        vendor_id=vendor_id,
        number=number,
        amount=amount,
        day=day,
        terms_id=terms_id,
        terms_text=terms_text,
        remit_id=remit_id,
        invoice_type=3 if po_id else 4,
        po_id=po_id,
    )
    select_status = ""
    if choice == "select" and chosen:
        try:
            select_status = client.try_select_receipts(created, [int(chosen["id"])])
        except KimcoError:
            select_status = "blocked"
        if select_status == "selected" and gap is not None and 0.01 <= abs(gap) < 75:
            client.try_post_ppv(created, gap)
    attach_status = attach(client, created, pdf)
    batch_target = BATCH_AGENT
    if choice == "price":
        batch_target = BATCH_TRANSFER
    elif choice == "missing":
        batch_target = BATCH_TRANSFER
    move_batch(client, created, batch_target)
    live = totals(client.get_item("ap_invoices", created))
    receipt_bit = ""
    if chosen:
        receipt_bit = (
            f"Receipt {chosen['id']} is quantity {chosen.get('qty')} at {money_txt(chosen.get('unit'))}, "
            f"extended {money_txt(chosen.get('ext'))}."
        )
    elif open_facts:
        receipt_bit = "Open receipts: " + "; ".join(
            f"{fact['id']} qty {fact.get('qty')} {money_txt(fact.get('ext'))}" for fact in open_facts
        ) + "."
    totals_ok = (
        float(live["lines_charges_tax"]) == amount
        and float(money(live["verification"]) or 0) == amount
        and live["posted"] in (None, "", False)
    )
    matched = choice == "select" and select_status == "selected" and attach_status == "attached" and totals_ok and int(live["batch_id"] or 0) == BATCH_AGENT
    if matched and chosen:
        note = treyce(
            f"AP Clerk: @Treyce Hodges selected receipt {chosen['id']} for {vendor_name} invoice {number}. "
            f"{line_words} {receipt_bit} "
            f"The bill matches the PDF total {money_txt(amount)} and is not posted."
        )
        result, reason = "PASS", f"Receipt {chosen['id']} matches the PDF total."
    elif choice == "price" and chosen:
        note = shawn(
            f"AP Clerk: @Shawn McKibben {vendor_name} invoice {number} on {po_text} cannot be matched. "
            f"{line_words} {receipt_bit} "
            f"The gap is {money_txt(gap)}, which is $75 or more. The receipt was not selected. "
            f"The bill is on hold in Transfer AP and is not posted."
        )
        result, reason = "HOLD", f"Price gap {money_txt(gap)} against receipt {chosen['id']}. Receipt not selected."
    elif choice == "missing":
        note = anthony(
            f"AP Clerk: @Anthony {vendor_name} invoice {number} on {po_text} has no open matching receipt. "
            f"{line_words} The PDF total is {money_txt(amount)}. "
            f"Please receive it. The bill is on hold in Transfer AP and is not posted."
        )
        result, reason = "HOLD", "No open matching receipt."
    elif choice == "select" and select_status != "selected":
        note = shawn(
            f"AP Clerk: @Shawn McKibben {vendor_name} invoice {number} matches receipt {chosen['id'] if chosen else ''} "
            f"but Select Receipts was not saved ({select_status}). {line_words} {receipt_bit} "
            f"The bill is on hold on batch 747 and is not posted."
        )
        result, reason = "HOLD", f"Select Receipts returned {select_status}."
    elif choice == "select":
        note = shawn(
            f"AP Clerk: @Shawn McKibben {vendor_name} invoice {number} selected receipt {chosen['id'] if chosen else ''} "
            f"but the live total is {money_txt(live['lines_charges_tax'])} against the PDF total {money_txt(amount)}. "
            f"PDF attach returned {attach_status}. The bill is on hold on batch 747 and is not posted."
        )
        result, reason = "HOLD", f"Receipt selected but the live total or PDF attach did not match ({attach_status})."
    elif choice == "ambiguous":
        note = shawn(
            f"AP Clerk: @Shawn McKibben {vendor_name} invoice {number} on {po_text} matches more than one open receipt. "
            f"{line_words} {receipt_bit} Nothing was selected. "
            f"The bill is on hold on batch 747 and is not posted."
        )
        result, reason = "HOLD", "More than one open receipt matches the invoice amount."
    else:
        note = shawn(
            f"AP Clerk: @Shawn McKibben {vendor_name} invoice {number} on {po_text} does not match the open receipt. "
            f"{line_words} {receipt_bit} "
            f"The quantity does not match, so the receipt was not selected. "
            f"The PDF total is {money_txt(amount)}. The bill is on hold on batch 747 and is not posted."
        )
        result, reason = "HOLD", "Invoice quantity does not match the open receipt. Receipt not selected."
    write_note(client, created, note)
    final = totals(client.get_item("ap_invoices", created))
    final["attachments"] = [row.get("name") or row.get("fileName") for row in client.list_attachments(created)]
    final["pdf_total"] = amount
    final["result"] = result
    final["reason"] = reason
    final["note"] = next((row["text"] for row in final["notes"] if str(row["text"]).startswith("AP Clerk:")), "")
    final["vendor"] = vendor_name
    final["open_receipts_considered"] = open_facts
    final["decision"] = choice
    return final


def enter_three(client, pdfs: dict[str, Path]) -> list[dict[str, Any]]:
    require_batches(client)
    found = existing_numbers(client)
    blocked = {number: ids for number, ids in found.items() if ids}
    if blocked:
        raise SystemExit(f"A bill already exists for {blocked}. No header created.")
    emj_facts = [receipt_fact(client, 24879)]
    oneal_facts = [receipt_fact(client, rid) for rid in (25179, 25180, 25181, 25182, 25211)]
    three_quarter = [
        fact
        for fact in oneal_facts
        if "59293-05" in str(fact.get("po_line") or "") or "0.75" in str(fact.get("part") or "").lower() or "3/4" in str(fact.get("desc") or "")
    ]
    results = [
        enter_gas(client, pdfs["gas_bill"]),
        enter_po_hold_or_match(
            client,
            number="T609084432",
            vendor_id=208,
            vendor_name="Earle M. Jorgensen Company",
            amount=372.00,
            day=date(2026, 9, 29),
            terms_id=58,
            terms_text="F-0.5/10,N30-0.5% 10, Net 30",
            remit_id=337,
            po_id=7244,
            po_text="PO 59242",
            pdf=pdfs["emj_bill"],
            invoice_qty=24,
            facts=emj_facts,
            line_words="The invoice is 24 ft of 1.250 OD x .083 wall 6061 tubing at $15.50/ft, $372.00.",
        ),
        enter_po_hold_or_match(
            client,
            number="15487245",
            vendor_id=137,
            vendor_name="O'Neal Steel - Dallas (GP)",
            amount=81.52,
            day=date(2026, 9, 30),
            terms_id=4,
            terms_text="F-N60-Net 60",
            remit_id=258,
            po_id=7295,
            po_text="PO 59293 (the PDF customer PO reads 559293, which is not a KIMCO purchase order)",
            pdf=pdfs["oneal_bill"],
            invoice_qty=1,
            facts=three_quarter or oneal_facts,
            line_words=(
                "The invoice is 1 piece of 3/4 inch 1018 cold-finished round, extended $81.52. "
                "The PDF customer PO reads 559293."
            ),
        ),
    ]
    return results


def qc_results() -> dict[int, str]:
    payload = json.loads(QC_PATH.read_text())
    return {int(row["bill_id"]): str(row.get("result") or "") for row in payload.get("bills") or []}


def folder_path(graph: GraphClient, folder_id: str, cache: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen and len(parts) < 8:
        seen.add(current)
        if current not in cache:
            response = graph.request(
                "GET",
                graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{current}"),
                params={"$select": "id,displayName,parentFolderId"},
            )
            if response.status_code != 200:
                break
            cache[current] = response.json() or {}
        folder = cache[current]
        name = str(folder.get("displayName") or "")
        if name and name.lower() not in {"msgfolderroot", "top of information store"}:
            parts.append(name)
        current = str(folder.get("parentFolderId") or "")
    return "/".join(reversed(parts))


def message_view(graph: GraphClient, message_id: str) -> dict[str, Any] | None:
    try:
        return graph.get_message(
            ALLOWED_MAILBOX,
            message_id,
            select="id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime,hasAttachments",
        )
    except Exception:
        return None


def find_messages(graph: GraphClient, received: str, subject: str, invoice: str) -> list[dict[str, Any]]:
    wanted_subject = subject.strip()
    found: dict[str, dict[str, Any]] = {}
    response = graph.request(
        "GET",
        graph._messages_url(ALLOWED_MAILBOX),
        params={
            "$filter": f"receivedDateTime eq {received}",
            "$select": "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime,hasAttachments",
            "$top": 50,
        },
    )
    rows = []
    if response.status_code == 200:
        rows = (response.json() or {}).get("value") or []
    matched = []
    for row in rows:
        if str(row.get("receivedDateTime") or "") == received and str(row.get("subject") or "").strip() == wanted_subject:
            matched.append(row)
    if not matched:
        for needle in (invoice, wanted_subject[:40]):
            for hit in graph.search_messages(ALLOWED_MAILBOX, needle, top=25):
                full = message_view(graph, str(hit.get("id") or ""))
                if full:
                    rows.append(full)
    for row in rows:
        if str(row.get("receivedDateTime") or "") != received:
            continue
        if str(row.get("subject") or "").strip() != wanted_subject:
            continue
        found[str(row.get("id") or "")] = row
    return list(found.values())


def proposed_category(result: str, categories: list[str]) -> str:
    del categories
    if result == "PASS":
        return "Entered in AI"
    return "Entered with issues"


def dry_run(client, graph: GraphClient) -> None:
    results = qc_results()
    cache: dict[str, dict[str, Any]] = {}
    rows = []
    with CONTENTS.open(newline="") as handle:
        for source in csv.DictReader(handle):
            received = source["message received"]
            if received in SKIP_RECEIVED:
                continue
            bill_id = int(source["KIMCO bill id or NONE"])
            invoice = source["invoice #"]
            subject = source["subject"]
            messages = find_messages(graph, received, subject, invoice)
            base_flags = []
            if not messages:
                base_flags.append("MISSING")
                messages = [{}]
            elif len(messages) > 1:
                base_flags.append("DUPLICATED")
            try:
                record = client.get_item("ap_invoices", bill_id)
                values = record.get("values") or {}
                live_number = str(values.get("Invoice_Number") or "")
                batch = values.get("AP_Invoice_Batch") or {}
                batch_text = lookup_text(batch)
                posted = values.get("Posted")
                if live_number != invoice:
                    base_flags.append("BILL_NOT_FOUND")
            except KimcoError:
                live_number = ""
                batch_text = ""
                posted = ""
                base_flags.append("BILL_NOT_FOUND")
            result = results.get(bill_id) or ""
            if result not in {"PASS", "HOLD"}:
                base_flags.append("BILL_NOT_FOUND")
            for message in messages:
                flag_bits = list(base_flags)
                folder = ""
                if message.get("parentFolderId"):
                    folder = folder_path(graph, str(message["parentFolderId"]), cache)
                if "FORT WORTH" in folder.upper():
                    flag_bits.append("ALREADY_IN_ARCHIVE")
                sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""
                categories = message.get("categories") or []
                if isinstance(categories, str):
                    categories = [categories]
                rows.append(
                    {
                        "current message id": message.get("id") or "",
                        "received": received,
                        "sender address": sender,
                        "subject": subject,
                        "current folder path": folder,
                        "categories": " | ".join(str(item) for item in categories),
                        "lastModifiedDateTime": message.get("lastModifiedDateTime") or "",
                        "hasAttachments": message.get("hasAttachments") if message else "",
                        "invoice #": invoice,
                        "bill id": bill_id,
                        "bill batch": batch_text,
                        "bill posted": "yes" if posted is True else "no",
                        "PASS/HOLD": result,
                        "proposed folder": "Inbox/9 - FORT WORTH ARCHIVE",
                        "proposed category": proposed_category(result, list(categories)),
                        "flag": "; ".join(dict.fromkeys(flag_bits)),
                    }
                )
    DRYRUN.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with DRYRUN.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    LOGGER.info("Dry run rows %s", len(rows))


def main() -> None:
    pdfs = prepare_pdfs()
    client = login()
    install_401_guard(client)
    results = enter_three(client, pdfs)
    payload = {"bills": results, "mail_changed": False, "posted": False, "batch_closed": False}
    (DEST / "result.json").write_text(json.dumps(payload, indent=2, default=str))
    LOGGER.info("Wrote result.json")
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    dry_run(client, graph)


if __name__ == "__main__":
    main()
