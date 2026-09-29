"""Enter the 5 oldest unentered 3P freight-only invoices on batch 724.

Kyle 2026-09-24: freight-only (no parts) is Miscellaneous Invoice_Type 4,
no lines, no Select Receipts. Additional Charge is the live outside-freight
code Freight External (id 1, GL 6032100). The Additional Charges list has
no code named OUTSIDE FREIGHT. invent=false. No Mail.Send. Do not post.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials
from ap_clerk.cli import _optional_graph_client
from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient
from ap_clerk.kimco import KimcoClient, fees_payload
from ap_clerk.rules import kimco_datetime, lookup_id, lookup_text, money

BATCH_ID = 724
VENDOR_ID = 1
TERMS_ID = 4
CURRENCY_ID = 3
REMIT_ID = 120
FREIGHT_PDF = Path("/tmp/3p-next5/pdfs/2026-09-09_09092026_001.pdf")
MAIL_JSON = Path("/tmp/3p-next5/mail.json")

# Page index is 0-based. Confirmed by OCR of each page.
BILLS = [
    {
        "invoice": "142232",
        "date": date(2026, 8, 6),
        "due": date(2026, 10, 5),
        "total": 500.00,
        "page": 2,
        "line": "1 FREIGHT FOR INVOICE # 142041 @ 500.00 = 500.00",
        "for_invoice": "142041",
    },
    {
        "invoice": "142233",
        "date": date(2026, 8, 7),
        "due": date(2026, 10, 6),
        "total": 500.00,
        "page": 4,
        "line": "1 FREIGHT FOR INVOICE # 141942 @ 500.00 = 500.00",
        "for_invoice": "141942",
    },
    {
        "invoice": "142234",
        "date": date(2026, 8, 14),
        "due": date(2026, 10, 13),
        "total": 1000.00,
        "page": 6,
        "line": "2 FREIGHT FOR INVOICE # 142043 & 142044 @ 500.00 = 1,000.00",
        "for_invoice": "142043 & 142044",
    },
    {
        "invoice": "142235",
        "date": date(2026, 8, 17),
        "due": date(2026, 10, 16),
        "total": 500.00,
        "page": 8,
        "line": "1 FREIGHT FOR INVOICE # 142173 @ 500.00. Total $500.00",
        "for_invoice": "142173",
    },
    {
        "invoice": "142236",
        "date": date(2026, 8, 18),
        "due": date(2026, 10, 17),
        "total": 500.00,
        "page": 10,
        "line": "1 FREIGHT FOR INVOICE # 142174 & 142175 @ 500.00 = 500.00",
        "for_invoice": "142174 & 142175",
    },
]


def existing_3p(client: KimcoClient) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in client.list_items("ap_invoices", page_size=2000):
        values = item.get("values") or {}
        vendor = str(values.get("Vendor_$_Display_Name") or "")
        if "3P" not in vendor.upper():
            continue
        number = str(values.get("Invoice_Number") or "").strip()
        if number and item.get("id") not in (None, ""):
            found[number] = int(item["id"])
    return found


def page_bytes(page_index: int) -> bytes:
    reader = PdfReader(str(FREIGHT_PDF))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def snapshot(client: KimcoClient, kimco_id: int) -> dict:
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
                "price": lv.get("Unit_Price"),
                "ext": lv.get("Extended_Amount"),
                "receipt": lookup_id(lv.get("Receipt")),
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
                "html": ((comment.get("values") or {}).get("HtmlValue") or "")[:500],
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
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "vendor": lookup_text(values.get("Vendor")),
        "lines": lines,
        "charges": charges,
        "comments_1": comments,
        "attachments": [a.get("name") for a in atts],
    }


def enter_one(client: KimcoClient, spec: dict) -> dict:
    number = spec["invoice"]
    pdf = page_bytes(spec["page"])
    payload = {
        "AP_Invoice_Batch": {"id": BATCH_ID},
        "Vendor": {"id": VENDOR_ID},
        "Invoice_Number": number,
        "Invoice_Type": 4,
        "Invoice_Date": kimco_datetime(spec["date"]),
        "Invoice_Verification_Amount": float(spec["total"]),
        "Invoice_Due_Date": kimco_datetime(spec["due"]),
        "Terms_Code": {"id": TERMS_ID},
        "Currency": {"id": CURRENCY_ID},
        "Remit_To_Address": {"id": REMIT_ID},
        "Transaction_Date": kimco_datetime(spec["date"]),
        "Comments": "API Agent",
    }
    created_id, _body, status, error = client.create("ap_invoices", payload)
    out = {
        "invoice": number,
        "pdf_date": spec["date"].isoformat(),
        "pdf_due": spec["due"].isoformat(),
        "pdf_total": spec["total"],
        "po": None,
        "po_on_pdf": "FREIGHT",
        "freight_for": spec["for_invoice"],
        "pdf_line": spec["line"],
        "create_http": status,
        "create_error": error,
        "kimco_id": created_id,
    }
    if created_id is None:
        out["status"] = "Fail"
        out["why"] = f"header create HTTP {status}"
        return out
    attach = client.try_official_attach(
        created_id,
        name=f"{number}.pdf",
        content_type="application/pdf",
        size=len(pdf),
        content=pdf,
    )
    out["attach"] = attach
    fee_body = fees_payload(
        [{"amount": spec["total"], "freight_external": True}],
        invoice_id=created_id,
        freight_external=True,
    )
    put = client.request("PUT", client._record_url("ap_invoices", created_id), json=fee_body)
    out["charge_http"] = put.status_code
    out["charge_error"] = "" if put.status_code < 400 else put.text[:400]
    live = snapshot(client, created_id)
    out["live"] = live
    charge_sum = round(sum(c["amount"] or 0 for c in live["charges"]), 2)
    amount_ok = live["amount"] == spec["total"] and live["verification"] == spec["total"]
    lines_ok = len(live["lines"]) == 0
    charge_ok = charge_sum == spec["total"] and any(
        "freight external" in str((c.get("kind") or {}).get("text") or "").lower() for c in live["charges"]
    )
    posted_ok = live["posted"] in (None, "", False)
    batch_ok = live["batch_id"] == BATCH_ID
    attach_ok = attach == "attached"
    if amount_ok and lines_ok and charge_ok and posted_ok and batch_ok and attach_ok:
        out["status"] = "Success"
    else:
        out["status"] = "HOLD"
        out["why"] = (
            f"amount_ok={amount_ok} lines_ok={lines_ok} charge_ok={charge_ok} "
            f"posted_ok={posted_ok} batch_ok={batch_ok} attach_ok={attach_ok}"
        )
    return out


def main() -> int:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    batch = client.get_item("ap_batches", BATCH_ID)
    bvals = batch.get("values") or {}
    print("BATCH", BATCH_ID, bvals.get("Name") or bvals.get("Batch_Name") or bvals.get("Display_Name"), flush=True)
    already = existing_3p(client)
    results = []
    for spec in BILLS:
        number = spec["invoice"]
        if number in already:
            results.append(
                {
                    "invoice": number,
                    "status": "skipped-already-in-kimco",
                    "kimco_id": already[number],
                }
            )
            print("SKIP already", number, already[number], flush=True)
            continue
        print("ENTER", number, flush=True)
        row = enter_one(client, spec)
        results.append(row)
        print(
            number,
            row.get("status"),
            row.get("kimco_id"),
            (row.get("live") or {}).get("amount"),
            flush=True,
        )
        if row.get("status") != "Success":
            print("STOP after non-success", number, row.get("why") or row.get("charge_error"), flush=True)
            break
    path = Path("/tmp/3p-next5/enter-results.json")
    path.write_text(json.dumps(results, indent=2, default=str) + "\n")
    print("WROTE", path, flush=True)
    return 0 if results and all(r.get("status") == "Success" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
