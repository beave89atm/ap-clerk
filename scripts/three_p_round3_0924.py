"""Enter exactly five 3P invoices on live KIMCO. invent=false. Do not post.

142240, 142241, 142281 are freight-only: Miscellaneous Invoice_Type 4, header
PO blank, no lines, one Freight External charge (lookup id 1, GL 6032100)
equal to that invoice's own PDF total. 142240 is freight for 142188 and gets
its own bill. Do not touch KIMCO 10308.

142216 and 142421 are parts: Invoice_Type 3, header PO blank (PDF says SEE
BELOW), select open receipts whose part, qty, and PO price match the invoice
line. PPV is invoice total minus selected receipt extended. |PPV| under $75
is an Additional Charge. $75 or more, or a unit-price gap on a receipt already
extended at PO price, is HOLD price_variance: do not select that receipt, do
not stack a unit-price PPV, Comments_1 @Shawn (mention id 104), then Transfer AP.

Every edit gets one Comments_1 note starting with "AP Clerk:".
Packing-slip Success gate is suspended. No Mail.Send.
After header + PDF attach, the source accountspayable@ message is moved to
Inbox folder 9 - FORT WORTH ARCHIVE when it is not already there.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.graph import ALLOWED_MAILBOX, FORT_WORTH_FOLDER_DISPLAY_NAME, GraphClient
from ap_clerk.kimco import (
    KimcoClient,
    fees_payload,
    receipt_line_values_from_records,
    select_receipts_payload,
)
from ap_clerk.rules import kimco_datetime, lookup_id, lookup_text, money
from ap_clerk.transfer_ap import apply_transfer_ap_batch_move
from gas_59081_retry import SHAWN_MENTION_ID, comments_1_shawn
from missing_receipt_transfer_now import comments_1_plain

BATCH_ID = 724
VENDOR_ID = 1
TERMS_ID = 4
CURRENCY_ID = 3
REMIT_ID = 120
DO_NOT_TOUCH_IDS = {10186, 10308}
PPV_HOLD = 75.0
PDF_DIR = Path("/tmp/3p-round3/enter-pdfs")
OUT_JSON = ROOT / "artifacts" / "3p-round3-2026-09-24.json"
OUT_XLSX = ROOT / "artifacts" / "AP-3P-round3-2026-09-24.xlsx"

# Pages are 0-based and are only that invoice's own page.
# Confirmed by OCR of every page of each source PDF on 2026-09-25.
# Pack 09092026_001.pdf is 19 pages: odd pages after each invoice are photos.
# 142281 page 2 is a photo. 142216 / 142421 later pages are packing lists and
# the Chris/Ruben email thread, not the invoice.
BILLS: list[dict[str, Any]] = [
    {
        "invoice": "142240",
        "kind": "freight",
        "date": date(2026, 8, 27),
        "due": date(2026, 10, 26),
        "total": 500.00,
        "pdf_file": "09092026_001.pdf",
        "pages": [16],
        "page_count": 19,
        "pos": [],
        "po_on_pdf": "FREIGHT",
        "freight_for": "142188",
        "pdf_line": "1 FREIGHT FOR INVOICE # 142188 @ 500.00 = 500.00",
        "message_subject_needle": "142240",
        "receipts": [],
        "skip_receipts": [],
    },
    {
        "invoice": "142241",
        "kind": "freight",
        "date": date(2026, 8, 31),
        "due": date(2026, 10, 30),
        "total": 500.00,
        "pdf_file": "09092026_001.pdf",
        "pages": [17],
        "page_count": 19,
        "pos": [],
        "po_on_pdf": "FREIGHT",
        "freight_for": "142216",
        "pdf_line": "1 FREIGHT FOR INVOICE # 142216 @ 500.00 = 500.00",
        "message_subject_needle": "142241",
        "receipts": [],
        "skip_receipts": [],
    },
    {
        "invoice": "142281",
        "kind": "freight",
        "date": date(2026, 8, 28),
        "due": date(2026, 10, 27),
        "total": 500.00,
        "pdf_file": "09102026_008.pdf",
        "pages": [0],
        "page_count": 2,
        "pos": [],
        "po_on_pdf": "FREIGHT",
        "freight_for": "142280",
        "pdf_line": "1 FREIGHT FOR INVOICE 142280 @ 500.00 = 500.00",
        "message_subject_needle": "INV # 142281",
        "receipts": [],
        "skip_receipts": [],
    },
    {
        "invoice": "142216",
        "kind": "parts",
        "date": date(2026, 8, 31),
        "due": date(2026, 10, 30),
        "total": 1481.33,
        "pdf_file": "09042026_007.pdf",
        "pages": [0],
        "page_count": 8,
        "pos": ["58925", "58952", "58972", "59002", "59031"],
        "po_on_pdf": "SEE BELOW",
        "lines": [
            {"po": "58925", "part": "1007044-1", "qty": 1, "unit": 97.50, "amount": 97.50},
            {"po": "58952", "part": "1007044-1", "qty": 3, "unit": 97.50, "amount": 292.50},
            {"po": "58972", "part": "1007044-1", "qty": 2, "unit": 97.50, "amount": 195.00},
            {"po": "59002", "part": "1007044-1", "qty": 4, "unit": 97.50, "amount": 390.00},
            {"po": "58972", "part": "1008270-1", "qty": 5, "unit": 83.02, "amount": 415.10},
            {"po": "59031", "part": "21678-1", "qty": 1, "unit": 91.23, "amount": 91.23},
        ],
        "receipts": [23526, 23525],
        "receipt_expect": {
            23526: {"qty": 5.0, "price": 83.02},
            23525: {"qty": 1.0, "price": 91.23},
        },
        "skip_receipts": [
            {
                "id": 23528,
                "part": "1007044-1",
                "po": "58925",
                "qty": 1,
                "po_unit": 361.46,
                "po_ext": 361.46,
                "invoice_unit": 97.50,
                "invoice_ext": 97.50,
            },
            {
                "id": 23529,
                "part": "1007044-1",
                "po": "58952",
                "qty": 3,
                "po_unit": 361.46,
                "po_ext": 1084.38,
                "invoice_unit": 97.50,
                "invoice_ext": 292.50,
            },
            {
                "id": 23527,
                "part": "1007044-1",
                "po": "58972",
                "qty": 2,
                "po_unit": 361.46,
                "po_ext": 722.92,
                "invoice_unit": 97.50,
                "invoice_ext": 195.00,
            },
            {
                "id": 23530,
                "part": "1007044-1",
                "po": "59002",
                "qty": 4,
                "po_unit": 361.46,
                "po_ext": 1445.84,
                "invoice_unit": 97.50,
                "invoice_ext": 390.00,
            },
        ],
        "message_subject_needle": "INV # 142216",
    },
    {
        "invoice": "142421",
        "kind": "parts",
        "date": date(2026, 9, 11),
        "due": date(2026, 11, 10),
        "total": 1929.61,
        "pdf_file": "09212026_017.pdf",
        "pages": [0],
        "page_count": 16,
        "pos": ["59124", "59107", "59090", "59031"],
        "po_on_pdf": "SEE BELOW",
        "lines": [
            {"po": "59124", "part": "21678-1", "qty": 5, "unit": 91.23, "amount": 456.15},
            {"po": "59107", "part": "21678-1", "qty": 2, "unit": 91.23, "amount": 182.46},
            {"po": "59124", "part": "21678-1", "qty": 10, "unit": 91.23, "amount": 912.30},
            {"po": "59090", "part": "21913-1", "qty": 14, "unit": 18.48, "amount": 258.72},
            {"po": "59031", "part": "1008270-1", "qty": 1, "unit": 83.02, "amount": 83.02},
            {"po": "59031", "part": "21913-1", "qty": 2, "unit": 18.48, "amount": 36.96},
        ],
        "receipts": [24038, 24036, 24037, 24041, 24040, 24039],
        "receipt_expect": {
            24038: {"qty": 5.0, "price": 91.23},
            24036: {"qty": 2.0, "price": 91.23},
            24037: {"qty": 10.0, "price": 91.23},
            24041: {"qty": 14.0, "price": 18.48},
            24040: {"qty": 1.0, "price": 83.02},
            24039: {"qty": 2.0, "price": 18.48},
        },
        "skip_receipts": [],
        "message_subject_needle": "INV # 142421",
    },
]


def ensure_pdfs(graph: GraphClient) -> None:
    """Download each source PDF from accountspayable@ via Graph."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {spec["pdf_file"] for spec in BILLS}
    for name in sorted(wanted):
        dest = PDF_DIR / name
        if dest.exists() and dest.stat().st_size > 1000:
            continue
        needle = next(spec["message_subject_needle"] for spec in BILLS if spec["pdf_file"] == name)
        hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=8)
        saved = False
        for msg in hits:
            subject = str(msg.get("subject") or "")
            if needle not in subject:
                continue
            downloaded = graph.download_pdf_attachments(ALLOWED_MAILBOX, str(msg.get("id") or ""))
            for att_name, content in downloaded:
                if att_name == name or name in att_name:
                    dest.write_bytes(content)
                    saved = True
                    break
            if saved:
                break
        if not saved:
            raise SystemExit(f"Graph PDF {name} not found for {needle}")


def page_bytes(spec: dict[str, Any]) -> bytes:
    path = PDF_DIR / spec["pdf_file"]
    reader = PdfReader(str(path))
    if len(reader.pages) != spec["page_count"]:
        raise SystemExit(f"{spec['invoice']} PDF page count {len(reader.pages)} != {spec['page_count']}")
    writer = PdfWriter()
    for index in spec["pages"]:
        writer.add_page(reader.pages[index])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def existing_3p(client: KimcoClient) -> dict[str, int]:
    found: dict[str, int] = {}
    url = client._url("ap_invoices")
    offset = 0
    total = None
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise SystemExit(f"AP list HTTP {response.status_code}")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            values = item.get("values") or {}
            vendor = str(values.get("Vendor_$_Display_Name") or "")
            number = str(values.get("Invoice_Number") or "").strip()
            if "3P" not in vendor.upper():
                continue
            if number and item.get("id") not in (None, ""):
                found[number] = int(item["id"])
        if not items:
            break
        offset += len(items)
    return found


def snapshot(client: KimcoClient, kimco_id: int) -> dict[str, Any]:
    rec = client.get_item("ap_invoices", kimco_id)
    values = rec.get("values") or {}
    lists = rec.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        lines.append(
            {
                "id": line.get("id"),
                "qty": lv.get("Quantity"),
                "price": money(lv.get("Unit_Price")),
                "ext": money(lv.get("Extended_Amount")),
                "receipt": lookup_id(lv.get("Receipt")),
                "po": lookup_text(lv.get("Purchase_Order_Number")),
                "po_line": lookup_text(lv.get("Purchase_Order_Line")),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        cv = charge.get("values") or {}
        charges.append(
            {
                "id": charge.get("id"),
                "kind": cv.get("Additional_Charges"),
                "amount": money(cv.get("Amount")),
                "price": money(cv.get("Price")),
                "qty": cv.get("Quantity"),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        comments.append(
            {
                "id": comment.get("id"),
                "html": ((comment.get("values") or {}).get("HtmlValue") or "")[:2000],
            }
        )
    try:
        atts = client.list_attachments(kimco_id)
    except Exception:
        atts = []
    return {
        "id": kimco_id,
        "invoice": values.get("Invoice_Number"),
        "type": values.get("Invoice_Type"),
        "date": values.get("Invoice_Date"),
        "due": values.get("Invoice_Due_Date"),
        "amount": money(values.get("Invoice_Amount")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "status": values.get("Status"),
        "posted": values.get("Posted"),
        "po": lookup_text(values.get("Purchase_Order")),
        "po_id": lookup_id(values.get("Purchase_Order")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "vendor": lookup_text(values.get("Vendor")),
        "lines": lines,
        "charges": charges,
        "comments_1": comments,
        "attachments": [a.get("name") for a in atts],
    }


def header_payload(spec: dict[str, Any]) -> dict[str, Any]:
    invoice_type = 4 if spec["kind"] == "freight" else 3
    return {
        "AP_Invoice_Batch": {"id": BATCH_ID},
        "Vendor": {"id": VENDOR_ID},
        "Invoice_Number": spec["invoice"],
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(spec["date"]),
        "Invoice_Verification_Amount": float(spec["total"]),
        "Invoice_Due_Date": kimco_datetime(spec["due"]),
        "Terms_Code": {"id": TERMS_ID},
        "Currency": {"id": CURRENCY_ID},
        "Remit_To_Address": {"id": REMIT_ID},
        "Transaction_Date": kimco_datetime(spec["date"]),
        "Comments": "API Agent",
    }


def prove_receipt(client: KimcoClient, row: dict[str, Any]) -> None:
    rec = client.get_item("receipts", int(row["id"]))
    values = rec.get("values") or {}
    ap = values.get("AP_Invoice_Number")
    ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "").strip()
    if values.get("Invoiced") is True or ap_text:
        raise SystemExit(f"receipt {row['id']} already billed on {ap_text or 'an invoice'}")
    qty = money(values.get("Quantity_Received"))
    price = money(values.get("PO_Item_Number_$_Unit_Price"))
    if qty != float(row["qty"]) or price != float(row["po_unit"] if "po_unit" in row else row["price"]):
        raise SystemExit(
            f"receipt {row['id']} live qty {qty} price {price} != expected {row}"
        )


def prove_spec_receipts(client: KimcoClient, spec: dict[str, Any]) -> None:
    expect = spec.get("receipt_expect") or {}
    for rid in spec.get("receipts") or []:
        wanted = expect.get(int(rid)) or expect.get(rid)
        if not wanted:
            raise SystemExit(f"receipt {rid} has no expected qty/price")
        prove_receipt(client, {"id": rid, "qty": wanted["qty"], "po_unit": wanted["price"]})
    for row in spec.get("skip_receipts") or []:
        prove_receipt(client, row)


def select_open_receipts(client: KimcoClient, kimco_id: int, receipt_ids: list[int]) -> dict[str, Any]:
    invoice = client.get_item("ap_invoices", kimco_id)
    lines = []
    for rid in receipt_ids:
        receipt = client.get_item("receipts", int(rid))
        values = receipt.get("values") or {}
        ap = values.get("AP_Invoice_Number")
        ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "").strip()
        if values.get("Invoiced") is True or ap_text:
            raise SystemExit(f"receipt {rid} already billed on {ap_text or 'an invoice'}")
        lines.append(receipt_line_values_from_records(invoice, receipt))
    payload = select_receipts_payload(lines, invoice_id=kimco_id)
    put = client.request("PUT", client._record_url("ap_invoices", kimco_id), json=payload)
    return {
        "http": put.status_code,
        "error": "" if put.status_code < 400 else put.text[:600],
        "count": len(receipt_ids),
    }


def price_note(spec: dict[str, Any]) -> str:
    bits = []
    for row in spec.get("skip_receipts") or []:
        gap = round(float(row["invoice_ext"]) - float(row["po_ext"]), 2)
        bits.append(
            f"qty {row['qty']} of part {row['part']} on PO {row['po']} is "
            f"${row['invoice_unit']:.2f} on the invoice (${row['invoice_ext']:.2f}) "
            f"and receipt {row['id']} is already at PO price ${row['po_unit']:.2f} "
            f"(${row['po_ext']:.2f}). |PPV| ${abs(gap):.2f} is $75 or more, so that "
            "receipt was not selected and the unit price was not changed. "
            f"Unreceive, set PO {row['po']} part {row['part']} to ${row['invoice_unit']:.2f}, "
            f"and re-receive qty {row['qty']}."
        )
    selected = ", ".join(str(rid) for rid in spec.get("receipts") or []) or "none"
    return (
        "AP Clerk: @Shawn McKibben HOLD price_variance on 3P "
        f"{spec['invoice']}. Matching receipts selected: {selected}. " + " ".join(bits)
    )


def price_html(spec: dict[str, Any]) -> str:
    text = price_note(spec)
    mention = (
        f'<span data-mention-id="{SHAWN_MENTION_ID}" '
        'data-mention-name="Shawn McKibben" '
        'data-mention-email="Shawn.McKibben@kannonmfg.com" '
        'class="prosemirror-mention-node">@Shawn McKibben</span>'
    )
    body = text.replace("@Shawn McKibben", mention, 1)
    return f"<p>{body}</p>"


def plain_html(text: str) -> str:
    return f"<p>{text}</p>"


def comment_id_matching(live: dict[str, Any], needle: str) -> int | None:
    for comment in live.get("comments_1") or []:
        if needle in str(comment.get("html") or ""):
            if comment.get("id") not in (None, ""):
                return int(comment["id"])
    return None


def archive_email(graph: GraphClient, needle: str, *, move: bool = True) -> dict[str, Any]:
    hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=5)
    chosen = None
    for msg in hits:
        subject = str(msg.get("subject") or "")
        if needle in subject:
            chosen = msg
            break
    if chosen is None:
        return {"status": "not-found", "folder": "", "moved_this_run": False, "email_moved": "no"}
    message_id = str(chosen.get("id") or "")
    full = graph.get_message(
        ALLOWED_MAILBOX,
        message_id,
        select="id,subject,parentFolderId,categories",
    )
    folder = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(folder.get("id") or "")
    parent = str(full.get("parentFolderId") or "")
    display = folder.get("displayName") or FORT_WORTH_FOLDER_DISPLAY_NAME
    if fort_id and parent == fort_id:
        return {
            "status": "already",
            "folder": display,
            "moved_this_run": False,
            "email_moved": "yes",
            "subject": full.get("subject"),
        }
    if not move:
        return {
            "status": "lookup-only",
            "folder": "",
            "moved_this_run": False,
            "email_moved": "no",
            "subject": full.get("subject"),
        }
    if not fort_id:
        return {
            "status": "no-folder",
            "folder": "",
            "moved_this_run": False,
            "email_moved": "no",
            "subject": full.get("subject"),
        }
    moved = graph.move_message(ALLOWED_MAILBOX, message_id, fort_id)
    new_id = str(moved.get("new_id") or message_id)
    after = graph.get_message(
        ALLOWED_MAILBOX,
        new_id,
        select="id,subject,parentFolderId,categories",
    )
    in_folder = str(after.get("parentFolderId") or "") == fort_id
    return {
        "status": "moved" if in_folder else moved.get("status"),
        "folder": display if in_folder else "",
        "moved_this_run": bool(in_folder),
        "email_moved": "yes" if in_folder else "no",
        "http": moved.get("http"),
        "subject": after.get("subject") or full.get("subject"),
    }


def success_note(spec: dict[str, Any], live: dict[str, Any], ppv: float | None) -> str:
    if spec["kind"] == "freight":
        return (
            f"AP Clerk: Freight-only 3P {spec['invoice']} for invoice {spec.get('freight_for')}. "
            "Miscellaneous Invoice_Type 4, header PO blank, no lines. "
            f"Freight External lookup id 1 GL 6032100 for this invoice's PDF total "
            f"${spec['total']:.2f}. Not posted."
        )
    receipt_ids = ", ".join(str(line.get("receipt")) for line in live.get("lines") or []) or "none"
    ppv_bit = "No PPV." if not ppv else f"PPV {ppv:.2f}."
    return (
        f"AP Clerk: Parts 3P {spec['invoice']}. Selected open receipts {receipt_ids}. "
        f"Invoice_Amount {live.get('amount')} matches the PDF total {spec['total']:.2f}. "
        f"{ppv_bit} Header PO blank (PDF SEE BELOW). Not posted."
    )


def enter_one(client: KimcoClient, graph: GraphClient, spec: dict[str, Any]) -> dict[str, Any]:
    number = spec["invoice"]
    prove_spec_receipts(client, spec)
    pdf = page_bytes(spec)
    created_id, _body, status, error = client.create("ap_invoices", header_payload(spec))
    out: dict[str, Any] = {
        "invoice": number,
        "kind": spec["kind"],
        "pdf_date": spec["date"].isoformat(),
        "pdf_due": spec["due"].isoformat(),
        "pdf_total": spec["total"],
        "po": None,
        "po_on_pdf": spec.get("po_on_pdf"),
        "pos": spec.get("pos") or [],
        "create_http": status,
        "create_error": error,
        "kimco_id": created_id,
        "ppv": None,
        "transfer_ap": "no",
        "comments_1": "",
        "comments_1_id": None,
    }
    if created_id is None:
        out["status"] = "Fail"
        out["why"] = f"header create HTTP {status}"
        return out
    if int(created_id) in DO_NOT_TOUCH_IDS:
        raise SystemExit(f"refusing to edit KIMCO {created_id}")
    attach = client.try_official_attach(
        created_id,
        name=f"{number}.pdf",
        content_type="application/pdf",
        size=len(pdf),
        content=pdf,
    )
    out["attach"] = attach
    email = archive_email(graph, spec["message_subject_needle"])
    out["email"] = email
    if spec["kind"] == "freight":
        fee_body = fees_payload(
            [{"amount": spec["total"], "freight_external": True}],
            invoice_id=created_id,
            freight_external=True,
        )
        put = client.request("PUT", client._record_url("ap_invoices", created_id), json=fee_body)
        out["charge_http"] = put.status_code
        out["charge_error"] = "" if put.status_code < 400 else put.text[:400]
        out["freight_for"] = spec.get("freight_for")
        out["pdf_line"] = spec.get("pdf_line")
        live = snapshot(client, created_id)
        charge_sum = round(sum(c["amount"] or 0 for c in live["charges"]), 2)
        freight_ok = charge_sum == spec["total"] and any(
            "freight external" in str((c.get("kind") or {}).get("text") or "").lower()
            for c in live["charges"]
        )
        amount_ok = live["amount"] == spec["total"] and live["verification"] == spec["total"]
        lines_ok = not live["lines"]
        posted_ok = live["posted"] in (None, "", False)
        batch_ok = live["batch_id"] == BATCH_ID
        if amount_ok and freight_ok and lines_ok and posted_ok and batch_ok and attach == "attached":
            note = success_note(spec, live, None)
            comment = comments_1_plain(client, created_id, plain_html(note))
            live = snapshot(client, created_id)
            out["comments_1_put"] = comment
            out["comments_1"] = note
            out["comments_1_id"] = comment_id_matching(live, "AP Clerk:")
            out["status"] = "Success" if out["comments_1_id"] else "HOLD"
            out["why"] = note if out["comments_1_id"] else f"{note} Comments_1 did not persist."
        else:
            out["status"] = "HOLD"
            out["why"] = (
                f"freight check failed amount={live['amount']} charges={charge_sum} "
                f"lines={len(live['lines'])} attach={attach} batch={live['batch_id']}"
            )
        out["live"] = live
        out["receipts_selected"] = []
        return out

    selected = select_open_receipts(client, created_id, list(spec["receipts"]))
    out["select"] = selected
    live = snapshot(client, created_id)
    if selected["http"] >= 400:
        out["status"] = "Fail"
        out["why"] = f"select receipts HTTP {selected['http']}"
        out["live"] = live
        return out
    receipt_ext = round(sum(line["ext"] or 0 for line in live["lines"]), 2)
    selected_ids = {line["receipt"] for line in live["lines"]}
    out["receipts_selected"] = [
        {
            "id": line["receipt"],
            "qty": line["qty"],
            "price": line["price"],
            "ext": line["ext"],
            "po": line["po"],
            "po_line": line["po_line"],
        }
        for line in live["lines"]
    ]
    out["receipt_extended"] = receipt_ext
    skipped = spec.get("skip_receipts") or []
    if skipped:
        gap = round(spec["total"] - receipt_ext, 2)
        note = price_note(spec)
        expected_ids = set(spec["receipts"])
        ids_ok = selected_ids == expected_ids
        over = abs(gap) >= PPV_HOLD
        comment = comments_1_shawn(client, created_id, price_html(spec))
        live = snapshot(client, created_id)
        comment_id = comment_id_matching(live, "AP Clerk:")
        mention_ok = any('data-mention-id="104"' in str(c.get("html") or "") for c in live["comments_1"])
        moved: dict[str, Any] = {"status": "not-moved"}
        if comment.get("status") == "persisted" and comment_id and mention_ok:
            moved = apply_transfer_ap_batch_move(client, kimco_id=created_id)
            live = snapshot(client, created_id)
        out["live"] = live
        out["comments_1"] = note
        out["comments_1_id"] = comment_id
        out["comments_1_put"] = comment
        out["transfer_ap"] = "yes" if live.get("batch_id") not in (None, BATCH_ID) and moved.get("status") in {
            "moved",
            "already-on-transfer-ap",
        } else "no"
        out["transfer_ap_detail"] = moved
        out["ppv"] = None
        out["price_gap"] = gap
        if (
            over
            and ids_ok
            and live.get("batch_id") not in (None, BATCH_ID)
            and mention_ok
            and attach == "attached"
            and live["posted"] in (None, "", False)
        ):
            out["status"] = "HOLD"
            out["exception"] = "price_variance"
            out["why"] = note
        else:
            out["status"] = "HOLD"
            out["exception"] = "price_variance"
            out["why"] = (
                f"{note} transfer={moved.get('status')} comment={comment.get('status')} "
                f"mention={mention_ok} ids_ok={ids_ok} batch={live.get('batch_id')} "
                f"gap={gap} attach={attach}"
            )
        return out

    ppv = round(spec["total"] - receipt_ext, 2)
    out["ppv"] = ppv if ppv else None
    if abs(ppv) >= PPV_HOLD:
        out["status"] = "HOLD"
        out["exception"] = "price_variance"
        out["why"] = f"unexpected |PPV| {ppv} on a bill with no skipped line"
        out["live"] = live
        return out
    if ppv and abs(ppv) >= 0.01:
        out["ppv_post"] = client.try_post_ppv(created_id, ppv)
        live = snapshot(client, created_id)
    amount_ok = live["amount"] == spec["total"] and live["verification"] == spec["total"]
    lines_ok = len(live["lines"]) == len(spec["receipts"]) and selected_ids == set(spec["receipts"])
    posted_ok = live["posted"] in (None, "", False)
    if amount_ok and lines_ok and posted_ok and attach == "attached" and live["type"] == 3 and live["batch_id"] == BATCH_ID:
        note = success_note(spec, live, ppv if ppv else None)
        comment = comments_1_plain(client, created_id, plain_html(note))
        live = snapshot(client, created_id)
        out["comments_1_put"] = comment
        out["comments_1"] = note
        out["comments_1_id"] = comment_id_matching(live, "AP Clerk:")
        out["status"] = "Success" if out["comments_1_id"] else "HOLD"
        out["why"] = note if out["comments_1_id"] else f"{note} Comments_1 did not persist."
    else:
        out["status"] = "HOLD"
        out["why"] = (
            f"parts check failed amount={live['amount']} verification={live['verification']} "
            f"lines={len(live['lines'])} expected={len(spec['receipts'])} ppv={ppv} attach={attach}"
        )
    out["live"] = live
    out["transfer_ap"] = "no"
    return out


def already_row(client: KimcoClient, graph: GraphClient, spec: dict[str, Any], kimco_id: int) -> dict[str, Any]:
    if int(kimco_id) in DO_NOT_TOUCH_IDS:
        raise SystemExit(f"refusing to touch KIMCO {kimco_id}")
    live = snapshot(client, kimco_id)
    email = archive_email(graph, spec["message_subject_needle"], move=False)
    return {
        "invoice": spec["invoice"],
        "kind": spec["kind"],
        "pdf_date": spec["date"].isoformat(),
        "pdf_due": spec["due"].isoformat(),
        "pdf_total": spec["total"],
        "po": live.get("po"),
        "po_on_pdf": spec.get("po_on_pdf"),
        "pos": spec.get("pos") or [],
        "kimco_id": kimco_id,
        "status": "already_entered",
        "why": f"already_entered on KIMCO {kimco_id}. Not duplicated.",
        "live": live,
        "email": {
            "status": "lookup-only",
            "folder": email.get("folder") or "",
            "moved_this_run": False,
            "email_moved": "yes" if email.get("folder") else "no",
            "subject": email.get("subject"),
        },
        "receipts_selected": [
            {
                "id": line["receipt"],
                "qty": line["qty"],
                "price": line["price"],
                "ext": line["ext"],
                "po": line["po"],
                "po_line": line["po_line"],
            }
            for line in live["lines"]
        ],
        "ppv": None,
        "transfer_ap": "yes" if live.get("batch_id") not in (None, BATCH_ID) else "no",
        "comments_1": "",
        "comments_1_id": None,
        "freight_for": spec.get("freight_for"),
    }


def receipt_label(row: dict[str, Any]) -> str:
    bits = []
    for line in row.get("receipts_selected") or []:
        bits.append(f"{line.get('id')} qty {line.get('qty')} @ {line.get('price')}")
    return "; ".join(bits) if bits else "none"


def status_cell(row: dict[str, Any]) -> str:
    status = row.get("status") or ""
    if status == "HOLD" and row.get("exception"):
        status = f"HOLD {row['exception']}"
    why = row.get("why") or ""
    if why and why not in status:
        return f"{status}. {why}" if status else why
    return status


def write_outputs(rows: list[dict[str, Any]], *, batch_name: str) -> None:
    payload = {
        "run": "3p-round3-2026-09-24",
        "host": "https://live.kimcoerp.com",
        "vendor": "999-3P INDUSTRIES",
        "vendor_id": VENDOR_ID,
        "batch_id": BATCH_ID,
        "batch_name": batch_name,
        "posted": False,
        "emails_sent": False,
        "invent": False,
        "packing_slip_gate": "suspended",
        "login": "API Agent key. Bills are not posted. Treyce Hodges (id 33) posts.",
        "untouched": [{"invoice": "142188", "kimco_id": 10308}, {"invoice": "142231", "kimco_id": 10186}],
        "selection": (
            "Exactly 142240, 142241, 142281, 142216, and 142421. "
            "Freight-only bills are their own headers. 142240 does not touch KIMCO 10308."
        ),
        "invoices": [],
    }
    for row in rows:
        live = row.get("live") or {}
        email = row.get("email") or {}
        payload["invoices"].append(
            {
                "invoice": row["invoice"],
                "pdf_date": row.get("pdf_date"),
                "pdf_total": row.get("pdf_total"),
                "type": row.get("kind"),
                "po": row.get("po"),
                "po_on_pdf": row.get("po_on_pdf"),
                "pos": row.get("pos") or [],
                "kimco_id": row.get("kimco_id"),
                "status": row.get("status"),
                "exception": row.get("exception"),
                "why": row.get("why"),
                "receipts": row.get("receipts_selected") or [],
                "ppv": row.get("ppv"),
                "price_gap": row.get("price_gap"),
                "transfer_ap": row.get("transfer_ap"),
                "charges": [
                    {
                        "code": (c.get("kind") or {}).get("text") if isinstance(c.get("kind"), dict) else c.get("kind"),
                        "id": c.get("id"),
                        "amount": c.get("amount"),
                        "lookup_id": (c.get("kind") or {}).get("id") if isinstance(c.get("kind"), dict) else None,
                        "gl": "6032100" if row.get("kind") == "freight" else None,
                    }
                    for c in (live.get("charges") or [])
                ],
                "invoice_amount_after": live.get("amount"),
                "verification_amount": live.get("verification"),
                "comments_1": row.get("comments_1") or "",
                "comments_1_id": row.get("comments_1_id"),
                "email_folder": email.get("folder") or "",
                "email_moved": email.get("email_moved"),
                "email_moved_this_run": email.get("moved_this_run"),
                "email_subject": email.get("subject"),
                "batch_id": live.get("batch_id"),
                "batch": live.get("batch"),
                "posted": live.get("posted"),
                "invoice_type": live.get("type"),
                "attachments": live.get("attachments"),
                "freight_for": row.get("freight_for"),
            }
        )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    wb = Workbook()
    first = wb.active
    first.title = "This run (5)"
    headers = [
        "Invoice #",
        "Date",
        "Type",
        "Amount",
        "KIMCO bill #",
        "Batch",
        "Status",
        "Receipts selected",
        "PPV",
        "Transfer AP",
        "Comments_1 id",
        "Email moved",
    ]
    first.append(headers)
    for row in rows:
        live = row.get("live") or {}
        batch_id = live.get("batch_id")
        batch = live.get("batch") or ""
        batch_cell = f"{batch} ({batch_id})" if batch_id else ""
        first.append(
            [
                row["invoice"],
                row.get("pdf_date"),
                row.get("kind"),
                row.get("pdf_total"),
                row.get("kimco_id"),
                batch_cell,
                status_cell(row),
                receipt_label(row),
                row.get("ppv") if row.get("ppv") not in (None, "") else None,
                row.get("transfer_ap") or "no",
                row.get("comments_1_id"),
                (row.get("email") or {}).get("email_moved") or "no",
            ]
        )
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    for cell in first[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for excel_row in first.iter_rows(min_row=2):
        for cell in excel_row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    widths = [14, 14, 12, 12, 16, 32, 72, 36, 12, 14, 16, 14]
    for i, width in enumerate(widths, 1):
        first.column_dimensions[get_column_letter(i)].width = width
    first.auto_filter.ref = f"A1:L{first.max_row}"
    first.freeze_panes = "A2"
    first.row_dimensions[1].height = 22
    for r in range(2, first.max_row + 1):
        first.row_dimensions[r].height = 64

    lines_ws = wb.create_sheet("Invoice PDF lines")
    lines_ws.append(
        ["Invoice #", "PDF page", "Date", "Due", "PO", "Part", "Qty", "Price", "Line amount", "PDF total", "KIMCO bill #"]
    )
    for row in rows:
        spec = next(b for b in BILLS if b["invoice"] == row["invoice"])
        if row.get("kind") == "freight":
            lines_ws.append(
                [
                    row["invoice"],
                    spec["pages"][0] + 1,
                    row.get("pdf_date"),
                    row.get("pdf_due"),
                    "FREIGHT",
                    spec.get("pdf_line"),
                    1,
                    row.get("pdf_total"),
                    row.get("pdf_total"),
                    row.get("pdf_total"),
                    row.get("kimco_id"),
                ]
            )
            continue
        for line in spec.get("lines") or []:
            lines_ws.append(
                [
                    row["invoice"],
                    spec["pages"][0] + 1,
                    row.get("pdf_date"),
                    row.get("pdf_due"),
                    line["po"],
                    line["part"],
                    line["qty"],
                    line["unit"],
                    line["amount"],
                    row.get("pdf_total"),
                    row.get("kimco_id"),
                ]
            )
    wb.save(OUT_XLSX)
    print("WROTE", OUT_JSON)
    print("WROTE", OUT_XLSX)


def main() -> int:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    graph = _optional_graph_client()
    if graph is None:
        raise SystemExit("Graph client required")
    ensure_pdfs(graph)
    batch = client.get_item("ap_batches", BATCH_ID)
    bvals = batch.get("values") or {}
    batch_name = str(bvals.get("AP_Invoice_Batch_ID") or "")
    print("BATCH", BATCH_ID, batch_name, flush=True)
    if batch_name != "API Agent - 9/22/26 3P":
        raise SystemExit(f"batch 724 name changed: {batch_name}")
    already = existing_3p(client)
    if already.get("142188") != 10308:
        raise SystemExit(f"142188 id changed: {already.get('142188')}")
    if already.get("142231") != 10186:
        raise SystemExit(f"142231 id changed: {already.get('142231')}")
    results = []
    for spec in BILLS:
        number = spec["invoice"]
        if number in already:
            print("ALREADY", number, already[number], flush=True)
            results.append(already_row(client, graph, spec, already[number]))
            continue
        print("ENTER", number, spec["kind"], spec["total"], flush=True)
        row = enter_one(client, graph, spec)
        results.append(row)
        live = row.get("live") or {}
        print(
            number,
            row.get("status"),
            row.get("kimco_id"),
            "amount",
            live.get("amount"),
            "batch",
            live.get("batch_id"),
            "comment",
            row.get("comments_1_id"),
            "email",
            (row.get("email") or {}).get("email_moved"),
            flush=True,
        )
        if row.get("status") not in {"Success", "HOLD", "already_entered"}:
            print("STOP", number, row.get("why"), flush=True)
            break
    path = Path("/tmp/3p-round3/enter-results.json")
    path.write_text(json.dumps(results, indent=2, default=str) + "\n")
    if len(results) == len(BILLS):
        write_outputs(results, batch_name=batch_name)
    else:
        print("PARTIAL", len(results), flush=True)
    ok = len(results) == len(BILLS) and all(
        r.get("status") in {"Success", "HOLD", "already_entered"} for r in results
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
