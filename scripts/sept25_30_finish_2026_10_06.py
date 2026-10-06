"""Finish existing unposted AP bills 10478-10509. Never creates a bill.

Reads the invoice PDFs already saved for the September catch-up, then edits
only those 32 KIMCO records. Does not post, auto-pay, close a batch, send
mail, or move / re-tag / mark-read any message.
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

from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.kimco import KimcoClient, KimcoError, comment_author_from_access_token
from ap_clerk.pdf_invoice import extract_pdf_text, parse_invoice_pdf
from ap_clerk.rules import money

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept25-30-finish")

BILL_IDS = list(range(10478, 10510))
PROGRESS = ROOT / "runs" / "sept-catchup-progress.json"
QC_PDFS = ROOT / "qc" / "invoices-sept-catchup"
SNAPSHOT = Path("/tmp/sept25-30-live.json")
ALLOWED = set(BILL_IDS)

SIGN_INS = 0
REAUTHED = False
CLIENT: KimcoClient | None = None


def login() -> KimcoClient:
    global SIGN_INS, CLIENT
    if SIGN_INS >= 2:
        raise SystemExit("Lost the KIMCO session a second time. Stopping. No third sign-in.")
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error or "Live credentials missing")
    SIGN_INS += 1
    try:
        client = KimcoClient.authenticate(
            creds.instance_url,
            creds.key or "",
            creds.password or "",
            target="live",
        )
    except KimcoError as exc:
        raise SystemExit(f"KIMCO sign-in failed on attempt {SIGN_INS}. Not retrying the password.") from exc
    author = comment_author_from_access_token(client.access_token)
    if int(author.get("id") or 0) != 175 or str(author.get("name") or "") != "API Agent":
        raise SystemExit("Aborting. Token author is not API Agent user 175.")
    if CLIENT is not None and CLIENT is not client:
        CLIENT.access_token = client.access_token
        CLIENT.session.headers["Authorization"] = f"Bearer {client.access_token}"
        return CLIENT
    CLIENT = client
    LOGGER.info("Sign-in %s is API Agent user 175", SIGN_INS)
    return client


def install_401_guard(client: KimcoClient) -> None:
    original = client.request

    def guarded(method: str, url: str, **kwargs: Any):
        global REAUTHED
        response = original(method, url, **kwargs)
        if response.status_code != 401:
            return response
        if REAUTHED or SIGN_INS >= 2:
            raise SystemExit("KIMCO token returned 401 after the second sign-in. Stopping.")
        REAUTHED = True
        LOGGER.info("Token 401. Signing in once more.")
        login()
        retried = original(method, url, **kwargs)
        if retried.status_code == 401:
            raise SystemExit("KIMCO token returned 401 after the second sign-in. Stopping.")
        return retried

    client.request = guarded  # type: ignore[method-assign]


def progress_rows() -> dict[int, dict[str, Any]]:
    payload = json.loads(PROGRESS.read_text())
    rows = payload["rows"] if isinstance(payload, dict) else payload
    found: dict[int, dict[str, Any]] = {}
    for row in rows:
        raw = row.get("KIMCO id")
        if raw in (None, ""):
            continue
        kid = int(raw)
        if kid in ALLOWED:
            found[kid] = row
    missing = [kid for kid in BILL_IDS if kid not in found]
    if missing:
        raise SystemExit(f"Catch-up progress is missing bills {missing}")
    return found


def pdf_for(invoice_number: str) -> Path:
    hits = sorted(QC_PDFS.glob(f"*{invoice_number}*.pdf"))
    exact = [path for path in hits if path.stem.endswith(invoice_number) or f"_{invoice_number}" in path.stem]
    path = (exact or hits)[0] if hits else None
    if path is None or not path.is_file():
        raise SystemExit(f"No QC PDF for invoice {invoice_number}")
    return path


def shoppa_lines(text: str) -> dict[str, Any]:
    materials = re.search(r"Materials\s+([\d,]+\.\d{2})", text)
    labor = re.search(r"Labor\s+([\d,]+\.\d{2})", text)
    tax = re.search(r"Sales Tax\s+([\d,]+\.\d{2})", text)
    total = re.search(r"Total Due\s+([\d,]+\.\d{2})", text)
    lines = []
    if materials:
        amount = money(materials.group(1))
        lines.append({"description": "Materials", "qty": 1, "unit_price": amount, "amount": amount})
    if labor:
        amount = money(labor.group(1))
        lines.append({"description": "Labor", "qty": 1, "unit_price": amount, "amount": amount})
    return {
        "lines": lines,
        "fees": [],
        "sales_tax": money(tax.group(1)) if tax else None,
        "amount": money(total.group(1)) if total else None,
        "po": None,
    }


def telecom_lines(text: str, invoice_number: str) -> dict[str, Any]:
    qty = re.search(
        r"(\d+)\s+([A-Z][A-Z0-9 ,./-]{3,40}?)\s+\$?([\d,]+\.\d{2})\s+\$?([\d,]+\.\d{2})",
        text,
    )
    po = re.search(r"PO:\s*(\d{5})", text)
    total = re.search(r"Invoice Total:\s*\$?([\d,]+\.\d{2})", text)
    part = re.search(r"Part:\s*([A-Z0-9-]+)", text)
    lines = []
    if qty:
        lines.append(
            {
                "part": part.group(1) if part else "",
                "description": re.sub(r"\s+", " ", qty.group(2)).strip(" ,"),
                "qty": money(qty.group(1)),
                "unit_price": money(qty.group(3)),
                "amount": money(qty.group(4)),
            }
        )
    return {
        "lines": lines,
        "fees": [],
        "sales_tax": 0.0,
        "amount": money(total.group(1)) if total else None,
        "po": po.group(1) if po else None,
        "invoice_number": invoice_number,
    }


def xcaliber_lines(text: str) -> dict[str, Any]:
    match = re.search(
        r"(6005-2RS[^\n]*)\n(?:.*\n){0,3}?(\d+)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
        text,
    )
    lines = []
    if match:
        lines.append(
            {
                "part": "6005-2RS C3",
                "description": "6005-2RS C3 IMPORT BEARING",
                "qty": money(match.group(2)),
                "unit_price": money(match.group(3)),
                "amount": money(match.group(4)),
            }
        )
    total = re.search(r"BALANCE DUE\s+\$?([\d,]+\.\d{2})", text)
    po = re.search(r"P\.O\.\s*NO\.\s*\n?\s*(?:\d{2}/\d{2}/\d{4}\s+DELIVERY\s+)?(\d{5})", text)
    if po is None:
        po = re.search(r"DELIVERY\s+(\d{5})", text)
    return {
        "lines": lines,
        "fees": [],
        "sales_tax": 0.0,
        "amount": money(total.group(1)) if total else None,
        "po": po.group(1) if po else "59278",
    }


def ryerson_lines(text: str) -> dict[str, Any]:
    row = re.search(
        r"(\d+)\s+PC\s+[\d,.]+\s+LB\s+([\d,]+\.\d{2})\s*/\s*PC\s+([\d,]+\.\d{2})",
        text,
    )
    fuel = re.search(r"Fuel Surcharge\s+([\d,]+\.\d{2})", text)
    total = re.search(r"Total Amount\s+([\d,]+\.\d{2})", text)
    po = re.search(r"Customer PO\s+(\d{5})", text)
    lines = []
    if row:
        qty = money(row.group(1))
        unit = money(row.group(2))
        amount = money(row.group(3))
        lines.append(
            {
                "description": "Alloy Tube RD 4140 QT",
                "qty": qty,
                "unit_price": unit,
                "amount": amount,
            }
        )
    fees = []
    if fuel:
        fees.append({"name": "Fuel Surcharge", "amount": money(fuel.group(1))})
    return {
        "lines": lines,
        "fees": fees,
        "sales_tax": 0.0,
        "amount": money(total.group(1)) if total else None,
        "po": po.group(1) if po else None,
    }


def oneal_section(text: str, invoice_number: str) -> str:
    hits = [match.start() for match in re.finditer(rf"\b{invoice_number}\b", text)]
    if not hits:
        return text
    # The invoice number is printed in the header after the prior invoice's tail.
    start = hits[0]
    later = [match.start() for match in re.finditer(r"\b15\d{6}\b", text) if match.start() > start + 20]
    # Skip a second print of the same number.
    end = len(text)
    for pos in later:
        window = text[pos : pos + 12]
        if invoice_number not in window:
            end = pos
            break
    return text[max(0, start - 400) : end]


def parse_target(row: dict[str, Any]) -> dict[str, Any]:
    number = str(row.get("Invoice #") or "")
    path = pdf_for(number)
    text = extract_pdf_text(path)
    vendor = str(row.get("Vendor") or "")
    special = None
    if "shoppa" in vendor.lower():
        special = shoppa_lines(text)
    elif "telecom" in vendor.lower():
        special = telecom_lines(text, number)
    elif "xcaliber" in vendor.lower():
        special = xcaliber_lines(text)
    elif "ryerson" in vendor.lower():
        special = ryerson_lines(text)
    elif "o'neal" in vendor.lower() or "oneal" in vendor.lower():
        text = oneal_section(text, number)
    parsed = parse_invoice_pdf(path, subject=number, from_name=vendor)
    siblings = [parsed] + list(parsed.get("siblings") or [])
    chosen = next((item for item in siblings if str(item.get("invoice_number") or "") == number), parsed)
    if "o'neal" in vendor.lower() or "oneal" in vendor.lower():
        from ap_clerk.pdf_invoice import extract_fees, extract_invoice_lines, parse_invoice_text

        section_parsed = parse_invoice_text(text, from_name=vendor, filename=path.name)
        chosen = dict(chosen)
        chosen["lines"] = extract_invoice_lines(text) or section_parsed.get("lines") or []
        chosen["fees"] = [fee for fee in extract_fees(text) if not re.search(r"sales\s*tax", str(fee.get("name") or ""), re.I)]
        chosen["amount"] = section_parsed.get("amount") or chosen.get("amount")
        chosen["po"] = section_parsed.get("po") or chosen.get("po")
        chosen["invoice_number"] = number
    if special:
        chosen = dict(chosen)
        chosen.update(special)
    lines = []
    for line in chosen.get("lines") or []:
        if not isinstance(line, dict):
            continue
        lines.append(
            {
                "part": line.get("part") or "",
                "description": line.get("description") or line.get("label") or "",
                "qty": line.get("qty") if line.get("qty") is not None else line.get("quantity"),
                "unit_price": line.get("unit_price"),
                "amount": line.get("amount") if line.get("amount") is not None else line.get("line_amount"),
                "fee": bool(line.get("fee")),
            }
        )
    fees = []
    for fee in chosen.get("fees") or []:
        if isinstance(fee, dict):
            fees.append({"name": fee.get("name") or fee.get("description") or "", "amount": fee.get("amount")})
    return {
        "pdf": path.name,
        "invoice_number": number,
        "vendor": vendor,
        "amount": chosen.get("amount") or row.get("Amount"),
        "po": chosen.get("po") or row.get("PO") or None,
        "sales_tax": chosen.get("sales_tax") or chosen.get("tax") or chosen.get("tax_amount"),
        "lines": lines,
        "fees": fees,
    }


def slim_lookup(value: Any) -> Any:
    if isinstance(value, dict):
        return {"id": value.get("id"), "text": value.get("text")}
    return value


def slim_bill(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        lines.append(
            {
                "id": line.get("id"),
                "item": slim_lookup(vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")),
                "desc": vals.get("Misc_Description"),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": vals.get("Extended_Amount"),
                "receipt": slim_lookup(vals.get("Receipt")),
                "gl": slim_lookup(vals.get("Purchase_GL_Account")),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        charges.append(
            {
                "id": charge.get("id"),
                "name": vals.get("Name") or slim_lookup(vals.get("Additional_Charges")),
                "kind": slim_lookup(vals.get("Additional_Charges")),
                "qty": vals.get("Quantity"),
                "price": vals.get("Price"),
                "amount": vals.get("Amount"),
            }
        )
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append(
            {
                "id": tax.get("id"),
                "code": slim_lookup(vals.get("Tax_Code")),
                "amount": vals.get("Tax_Amount"),
                "taxable": vals.get("Taxable_Amount"),
                "rate": vals.get("Tax_Rate"),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        vals = comment.get("values") or {}
        html = str(vals.get("HtmlValue") or "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        comments.append({"id": comment.get("id"), "text": plain[:240]})
    batch = values.get("AP_Invoice_Batch")
    return {
        "id": record.get("id"),
        "invoice": values.get("Invoice_Number"),
        "vendor": slim_lookup(values.get("Vendor")),
        "po": slim_lookup(values.get("Purchase_Order")),
        "type": values.get("Invoice_Type"),
        "batch": slim_lookup(batch),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "invoice_amount": values.get("Invoice_Amount"),
        "verification": values.get("Invoice_Verification_Amount"),
        "net": values.get("Invoice_Net_Amount"),
        "lines": lines,
        "charges": charges,
        "taxes": taxes,
        "comments": comments,
        "attachment_count": len(lists.get("Attachments") or lists.get("APInvoiceAttachment") or []),
    }


def snapshot() -> None:
    client = login()
    install_401_guard(client)
    rows = progress_rows()
    bills = []
    for kid in BILL_IDS:
        if kid not in ALLOWED:
            raise SystemExit(f"Refusing to read outside 10478-10509: {kid}")
        record = client.get_item("ap_invoices", kid)
        live = slim_bill(record)
        target = parse_target(rows[kid])
        bills.append({"id": kid, "live": live, "pdf": target, "sheet_po": rows[kid].get("PO"), "sheet_amount": rows[kid].get("Amount")})
        LOGGER.info(
            "GET %s inv=%s batch=%s lines=%s charges=%s tax=%s amt=%s ver=%s",
            kid,
            live.get("invoice"),
            (live.get("batch") or {}).get("id") if isinstance(live.get("batch"), dict) else live.get("batch"),
            len(live["lines"]),
            len(live["charges"]),
            len(live["taxes"]),
            live.get("invoice_amount"),
            live.get("verification"),
        )
    SNAPSHOT.write_text(json.dumps({"sign_ins": SIGN_INS, "bills": bills}, indent=2, default=str))
    LOGGER.info("Wrote %s", SNAPSHOT)


def po_numbers(bill: dict[str, Any]) -> list[str]:
    raw = str(bill.get("sheet_po") or bill["pdf"].get("po") or "")
    return [part.strip() for part in raw.split(",") if part.strip() and part.strip().lower() not in {"none", "null"}]


def plan() -> None:
    from ap_clerk.cli import _index_purchase_orders, normalize_receipt
    from ap_clerk.receiving_owners import lookup_receiving_owner
    from ap_clerk.rules import filter_matches_outside_ppv_gate, match_receipts

    client = login()
    install_401_guard(client)
    data = json.loads(SNAPSHOT.read_text())
    receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
    purchase_lines = client.list_items("purchase_lines")
    po_index = _index_purchase_orders(purchase_lines)
    LOGGER.info("receipts %s purchase lines %s", len(receipts), len(purchase_lines))
    # Read-only sample of an older Shoppa bill so misc item is not invented.
    sample = client.get_item("ap_invoices", 10228)
    sample_lines = slim_bill(sample)["lines"]
    LOGGER.info("Shoppa 10228 line items %s", [line.get("item") for line in sample_lines])
    decisions = []
    for bill in data["bills"]:
        kid = int(bill["id"])
        pos = po_numbers(bill)
        vendor = str(bill["pdf"].get("vendor") or "")
        owner = lookup_receiving_owner(vendor)
        lines = [line for line in bill["pdf"].get("lines") or [] if not line.get("fee")]
        one = None
        lock = None
        if pos:
            one = match_receipts(
                invoice_number=str(bill["pdf"].get("invoice_number") or ""),
                invoice_lines=lines,
                receipts=receipts,
                po_number=pos[0] if len(pos) == 1 else None,
                po_numbers=pos,
                invoice_amount=bill["pdf"].get("amount") if not lines else None,
            )
            lock = filter_matches_outside_ppv_gate(one.get("matched"), invoice_total=bill["pdf"].get("amount"))
        picked = []
        for hit in (lock or {}).get("selectable") or []:
            rec = hit.get("receipt") or {}
            picked.append(
                {
                    "id": rec.get("id"),
                    "po": rec.get("po"),
                    "qty": hit.get("select_qty") if hit.get("select_qty") is not None else rec.get("qty"),
                    "amount": rec.get("amount"),
                    "part": str(rec.get("part") or "")[:60],
                    "how": hit.get("how"),
                }
            )
        skipped = []
        for hit in (lock or {}).get("skipped") or []:
            rec = hit.get("receipt") or {}
            skipped.append({"id": rec.get("id"), "reason": hit.get("ppv_skip_reason"), "amount": rec.get("amount")})
        open_on_po = [
            {"id": rec.get("id"), "qty": rec.get("qty"), "amount": rec.get("amount"), "part": str(rec.get("part") or "")[:40]}
            for rec in receipts
            if str(rec.get("po") or "") in pos
        ][:8]
        decisions.append(
            {
                "id": kid,
                "pos": pos,
                "owner": (owner or {}).get("receiving_owner_raw"),
                "owner_keys": (owner or {}).get("owner_keys"),
                "matched": len((one or {}).get("matched") or []),
                "hold_no_receipts": (one or {}).get("hold_no_receipts"),
                "why": (one or {}).get("why"),
                "picked": picked,
                "skipped": skipped,
                "open_on_po": open_on_po,
                "po_in_index": {po: bool(po_index.get(po)) for po in pos},
            }
        )
        LOGGER.info(
            "PLAN %s po=%s matched=%s picked=%s skipped=%s open=%s",
            kid,
            pos,
            len((one or {}).get("matched") or []),
            [item["id"] for item in picked],
            [item["id"] for item in skipped],
            [item["id"] for item in open_on_po],
        )
    Path("/tmp/sept25-30-plan.json").write_text(json.dumps(decisions, indent=2, default=str))
    LOGGER.info("Wrote plan")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    if command == "snapshot":
        snapshot()
    elif command == "plan":
        plan()
    else:
        raise SystemExit(f"Unknown command {command}")
