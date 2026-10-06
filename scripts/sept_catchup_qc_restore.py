"""Restore unentered September mail to the Inbox and QC bills read-only.

No KIMCO writes. One sign-in. A 401 stops the run.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.styles import Font

from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    AI_SKIPPED_CATEGORY,
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    LEGACY_AI_SKIPPED_CATEGORY,
    LEGACY_AP_MATCHED_CATEGORY,
    PROCESS_CATEGORIES,
    GraphClient,
    GraphError,
    load_graph_credentials,
)
from ap_clerk.kimco import KimcoClient, KimcoError, comment_author_from_access_token
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.rules import money, names_match

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("qc-restore")

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "runs" / "AP-sept-catchup-2026-10-06.xlsx"
PROGRESS = ROOT / "runs" / "sept-catchup-progress.json"
READBACK = ROOT / "qc" / "sept-catchup-readback.json"
QC_JSON = ROOT / "qc" / "sept-catchup-qc.json"
MAIL_JSON = ROOT / "qc" / "sept-catchup-mail-restore.json"
REVIEW = "AI Needs Review"
DROP_CATEGORIES = set(PROCESS_CATEGORIES) | {LEGACY_AI_SKIPPED_CATEGORY, LEGACY_AP_MATCHED_CATEGORY}
LEAVE_RE = re.compile(
    r"\bstatement\b|credit\s*(memo|note)|\bpast[\s-]*due\b|proof of delivery|\bpods?\b|"
    r"payment submitted|payment was successfully|automatic payment|approved automatic payment|"
    r"payment declined|scheduled payment|payment due|payment inquiry|"
    r"notice of intent to cancel|(?:check|ck)\s*#|cancell?ed check|lost check",
    re.I,
)
SKIP_RESTORE = {"no-attachment", "no-pdf", "unreadable-or-not-a-bill"}
SIGN_INS = 0


def login_once() -> KimcoClient:
    """One password attempt. No retry. No second sign-in in this run."""
    global SIGN_INS
    if SIGN_INS >= 1:
        raise SystemExit("This QC run already signed in once. Not signing in again.")
    target = resolve_target(live_flag=True)
    creds = load_credentials(target=target)
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
        raise SystemExit(f"KIMCO sign-in failed. Not retrying the password. {exc}") from exc
    author = comment_author_from_access_token(client.access_token)
    if int(author.get("id") or 0) != 175 or str(author.get("name") or "") != "API Agent":
        raise SystemExit(f"Aborting. Token author is {author}, not API Agent user 175.")
    original = client.request

    def read_only(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Blocked KIMCO write {method} {url}")
        response = original(method, url, **kwargs)
        if response.status_code == 401:
            raise SystemExit("KIMCO token returned 401. Stopping. No another sign-in.")
        return response

    client.request = read_only  # type: ignore[method-assign]
    LOGGER.info("Sign-in 1 of this QC run is API Agent user 175. Writes are blocked.")
    return client


def klass_of(why: str) -> str:
    return (why or "").split(".", 1)[0].strip()


def load_rows() -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["September catch-up"]
    headers = [c.value for c in ws[1]]
    xrows = [{headers[j]: r[j] for j in range(len(headers))} for r in ws.iter_rows(min_row=2, values_only=True)]
    prows = json.loads(PROGRESS.read_text())["rows"]
    if len(xrows) != len(prows):
        raise SystemExit(f"Workbook rows {len(xrows)} != progress {len(prows)}")
    joined = []
    for excel_row, (sheet, prog) in enumerate(zip(xrows, prows), start=2):
        joined.append({"excel_row": excel_row, "sheet": sheet, "prog": prog})
    return joined


def decision(row: dict[str, Any]) -> str:
    sheet = row["sheet"]
    prog = row["prog"]
    why = str(prog.get("Why") or "")
    if sheet.get("Result") == "Hold" and not sheet.get("Bill id"):
        if str(prog.get("Folder move") or "").startswith("left"):
            return "category-only"
        return "move"
    if sheet.get("Result") == "Skipped" and klass_of(why) in SKIP_RESTORE:
        blob = f"{prog.get('subject') or ''} {sheet.get('Vendor') or ''}"
        if LEAVE_RE.search(blob):
            return "leave-genuine"
        return "move"
    return "keep"


def acronym(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    stop = {"inc", "llc", "co", "company", "the", "and", "of", "ltd"}
    keep = [w for w in words if w.lower() not in stop and not w.isdigit()]
    return "".join(w[0] for w in keep).lower()


def vendor_ok(parsed: str, live: str) -> bool:
    if names_match(parsed, live):
        return True
    live_name = re.sub(r"^\d+\s*-\s*", "", live or "")
    if names_match(parsed, live_name):
        return True
    compact = re.sub(r"[^a-z0-9]", "", (live_name or live or "").lower())
    return bool(compact) and compact == acronym(parsed)


def norm_invoice(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def lookup_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def date_only(value: Any) -> str:
    raw = str(value or "")
    if "T" in raw:
        return raw.split("T", 1)[0]
    match = re.search(r"\d{4}-\d{2}-\d{2}", raw)
    return match.group(0) if match else raw[:10]


def graph_get(graph: GraphClient, message_id: str) -> dict[str, Any] | None:
    response = graph.request(
        "GET",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        params={"$select": "id,subject,parentFolderId,categories,receivedDateTime"},
    )
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise GraphError(f"Graph GET message HTTP {response.status_code}")
    return response.json() or {}


def graph_patch_category(graph: GraphClient, message_id: str) -> list[str]:
    current = graph_get(graph, message_id) or {}
    keep = [c for c in (current.get("categories") or []) if c and c not in DROP_CATEGORIES and c != REVIEW]
    categories = keep + [REVIEW]
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        json={"categories": categories},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        raise GraphError(f"Graph PATCH category HTTP {response.status_code} {response.text[:200]}")
    return categories


def inbox_id(graph: GraphClient) -> str:
    response = graph.request(
        "GET",
        graph._user_url(ALLOWED_MAILBOX, "mailFolders/inbox"),
        params={"$select": "id,displayName"},
    )
    if response.status_code != 200:
        raise GraphError(f"Graph inbox folder HTTP {response.status_code}")
    return str((response.json() or {}).get("id") or "")


def index_folder(graph: GraphClient, folder_id: str) -> dict[str, list[dict[str, Any]]]:
    """Map receivedDateTime -> messages currently in one folder."""
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    url: str | None = graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{folder_id}/messages")
    params: dict[str, Any] | None = {
        "$select": "id,subject,parentFolderId,categories,receivedDateTime",
        "$filter": "receivedDateTime ge 2026-09-01T00:00:00Z and receivedDateTime lt 2026-10-01T05:00:00Z",
        "$top": 50,
    }
    while url:
        response = graph.request("GET", url, params=params)
        params = None
        if response.status_code >= 400:
            raise GraphError(f"Graph list folder HTTP {response.status_code} {response.text[:240]}")
        payload = response.json() or {}
        for item in payload.get("value") or []:
            found[str(item.get("receivedDateTime") or "")].append(item)
        url = payload.get("@odata.nextLink")
    return found


def resolve_message(
    graph: GraphClient,
    message_id: str,
    received: str,
    folder_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    current = graph_get(graph, message_id)
    if current:
        return current
    hits = folder_index.get(received) or []
    if len(hits) == 1:
        return hits[0]
    return None


def spot_check(client: KimcoClient, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    archived = [row for row in rows if row["sheet"].get("Result") == "Archived"]
    rng = random.Random(20261006)
    sample = rng.sample(archived, 15)
    # Map invoice key to the already-entered id when this row is only a duplicate.
    earlier: dict[tuple[str, str], Any] = {}
    for row in rows:
        sheet = row["sheet"]
        number = str(sheet.get("Invoice #") or "")
        if not number:
            continue
        key = (str(sheet.get("Vendor") or "").lower(), number)
        if key not in earlier and sheet.get("Bill id"):
            earlier[key] = sheet.get("Bill id")
    results = []
    for row in sample:
        sheet = row["sheet"]
        number = str(sheet.get("Invoice #") or "")
        key = (str(sheet.get("Vendor") or "").lower(), number)
        bill_id = sheet.get("Bill id") or earlier.get(key)
        item = {
            "vendor": sheet.get("Vendor"),
            "invoice": number,
            "bill_id": bill_id,
            "graph_message_id": row["prog"].get("graph_message_id"),
            "ok": False,
            "detail": "",
        }
        if not bill_id:
            item["detail"] = "Archived row has no KIMCO id to check."
            results.append(item)
            continue
        try:
            record = client.get_item("ap_invoices", int(bill_id))
        except KimcoError as exc:
            item["detail"] = str(exc)
            results.append(item)
            continue
        values = record.get("values") or {}
        live_number = str(values.get("Invoice_Number") or "")
        live_vendor = lookup_text(values.get("Vendor"))
        item["live_invoice"] = live_number
        item["live_vendor"] = live_vendor
        number_ok = norm_invoice(live_number) == norm_invoice(number)
        vendor_match = vendor_ok(str(sheet.get("Vendor") or ""), live_vendor)
        item["ok"] = bool(number_ok and vendor_match)
        if not number_ok:
            item["detail"] = f"Invoice number {live_number} is not {number}."
        elif not vendor_match:
            item["detail"] = f"Vendor {live_vendor} does not match {sheet.get('Vendor')}."
        else:
            item["detail"] = "Bill exists for this vendor and invoice number."
        results.append(item)
    return results


def charge_amount(charge: dict[str, Any]) -> float:
    values = charge.get("values") or {}
    return float(money(values.get("Amount") if values.get("Amount") not in (None, "") else values.get("Price")) or 0)


def qc_bill(client: KimcoClient, bill_id: int, pdf_path: str, parsed_vendor: str) -> dict[str, Any]:
    record = client.get_item("ap_invoices", bill_id)
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        item = vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")
        lines.append(
            {
                "item": lookup_text(item) or (item if isinstance(item, dict) else None),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": vals.get("Extended_Amount"),
                "receipt_id": (vals.get("Receipt") or {}).get("id") if isinstance(vals.get("Receipt"), dict) else vals.get("Receipt"),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        charges.append(
            {
                "name": lookup_text(vals.get("Additional_Charges")) or vals.get("Name"),
                "amount": charge_amount(charge),
            }
        )
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append(
            {
                "code": lookup_text(vals.get("Tax_Code")),
                "amount": vals.get("Tax_Amount"),
                "manual": vals.get("Manual_Calculation"),
            }
        )
    batch = values.get("AP_Invoice_Batch") if isinstance(values.get("AP_Invoice_Batch"), dict) else {}
    live = {
        "bill_id": bill_id,
        "vendor": lookup_text(values.get("Vendor")),
        "invoice_number": values.get("Invoice_Number"),
        "invoice_date": date_only(values.get("Invoice_Date")),
        "po": lookup_text(values.get("Purchase_Order")),
        "verification": values.get("Invoice_Verification_Amount"),
        "invoice_amount": values.get("Invoice_Amount"),
        "batch_id": batch.get("id"),
        "batch": batch.get("text"),
        "posted": values.get("Posted"),
        "lines": lines,
        "charges": charges,
        "taxes": taxes,
    }
    mismatches: list[str] = []
    pdf_info: dict[str, Any] = {}
    path = Path(pdf_path) if pdf_path else None
    parsed: dict[str, Any] = {}
    if path and path.is_file():
        parsed = parse_invoice_pdf(path, from_name=parsed_vendor)
        pdf_info = {
            "path": str(path),
            "vendor": parsed.get("vendor"),
            "invoice_number": parsed.get("invoice_number"),
            "date": parsed.get("date"),
            "po": parsed.get("po"),
            "amount": parsed.get("amount"),
            "tax": parsed.get("tax") or parsed.get("sales_tax"),
            "lines": [
                {
                    "qty": line.get("qty") or line.get("quantity"),
                    "unit_price": line.get("unit_price") or line.get("unit"),
                    "amount": line.get("amount"),
                    "description": str(line.get("description") or "")[:80],
                }
                for line in (parsed.get("lines") or [])[:12]
                if isinstance(line, dict)
            ],
            "fees": parsed.get("fees"),
            "text_empty": not str(parsed.get("text") or "").strip(),
        }
        if pdf_info["text_empty"] and not pdf_info["invoice_number"]:
            mismatches.append("PDF text was empty, so the printed invoice could not be compared.")
        else:
            if pdf_info["invoice_number"] and norm_invoice(pdf_info["invoice_number"]) != norm_invoice(live["invoice_number"]):
                mismatches.append(
                    f"Invoice # PDF {pdf_info['invoice_number']} vs KIMCO {live['invoice_number']}."
                )
            if pdf_info["amount"] not in (None, "") and live["verification"] not in (None, ""):
                if round(float(pdf_info["amount"]), 2) != round(float(live["verification"]), 2):
                    mismatches.append(
                        f"Total PDF {float(pdf_info['amount']):.2f} vs verification {float(live['verification']):.2f}."
                    )
            if pdf_info["vendor"] and not vendor_ok(str(pdf_info["vendor"]), str(live["vendor"])):
                mismatches.append(f"Vendor PDF {pdf_info['vendor']} vs KIMCO {live['vendor']}.")
            if pdf_info["date"] and live["invoice_date"] and str(pdf_info["date"])[:10] != live["invoice_date"]:
                mismatches.append(f"Date PDF {pdf_info['date']} vs KIMCO {live['invoice_date']}.")
            pdf_po = str(pdf_info.get("po") or "")
            if pdf_po and pdf_po not in str(live.get("po") or "") and not lines:
                mismatches.append(f"PO PDF {pdf_po} is not on the KIMCO header ({live.get('po') or 'blank'}).")
            pdf_tax = money(pdf_info.get("tax"))
            live_tax = round(sum(float(money(t.get("amount")) or 0) for t in taxes), 2)
            if pdf_tax not in (None, 0, 0.0) and round(float(pdf_tax), 2) != live_tax:
                mismatches.append(f"Printed tax {float(pdf_tax):.2f} vs Taxes tab {live_tax:.2f}.")
            elif pdf_tax in (None, 0, 0.0) and live_tax:
                mismatches.append(f"Taxes tab has {live_tax:.2f} and the PDF parse showed no printed tax.")
            pdf_lines = [line for line in pdf_info["lines"] if line.get("qty") not in (None, "")]
            if pdf_lines and lines:
                pdf_qty = round(sum(float(line["qty"]) for line in pdf_lines if line.get("qty") not in (None, "")), 4)
                live_qty = round(sum(float(line["qty"] or 0) for line in lines), 4)
                if pdf_qty != live_qty:
                    mismatches.append(f"Qty PDF {pdf_qty:g} vs KIMCO lines {live_qty:g}.")
            if not lines and round(float(live["verification"] or 0), 2) != round(float(live["invoice_amount"] or 0), 2):
                mismatches.append(
                    f"No merchandise lines. Verification {live['verification']} vs Invoice_Amount {live['invoice_amount']}."
                )
    else:
        mismatches.append(f"PDF file missing ({pdf_path}).")
    return {
        "live": live,
        "pdf": pdf_info,
        "result": "PASS" if not mismatches else "; ".join(mismatches),
        "parsed_fees": parsed.get("fees"),
    }


def explanations(records: list[dict[str, Any]]) -> dict[str, str]:
    by_id = {int(row["live"]["bill_id"]): row for row in records}
    text = {
        "xcaliber_10492": (
            "Xcaliber WB4337861639 (10492) printed 300 bearings at $3.60, tax $0, total $1,080, "
            "PO 59278, dated 09/27/2026. Verification is $1,080. Receipt 24859 is still selected "
            "for that same $1,080, and a Fees charge of $1,080 was saved on top of it, so "
            "Invoice_Amount is $2,160. The fee is not freight or tax. The bill was moved to "
            "Transfer AP 375 because the gap was over $75. The note says receipts were not "
            "selected; the live GET shows receipt 24859 is still selected. Nothing was posted."
        ),
        "gas_no_lines": (
            "Gas and Supply 10478, 10479, and 10495 are on batch 747 with a verification amount "
            "and no merchandise lines. 10478 is 100 robot tips at $1.92 = $192, tax $0, and "
            "Invoice_Amount is $0. 10479 is two AR90CD300 cylinders at $28 ($56), two ARG300 "
            "cylinders at $30 ($60), and a $22.50 fuel surcharge, subtotal $138.50, tax $0. "
            "Only the $22.50 fuel fee was saved, so Invoice_Amount is $22.50. 10495 is 12 cans "
            "at $5.50 = $66, tax $0, and Invoice_Amount is $0. The finish rule will not invent "
            "a full-invoice PPV when there are no merchandise lines, so these stayed Hold. "
            "They also stayed on 747 because a header with no PO looks up a batch named exactly "
            "'Transfer AP'. The live batch is named 'TRANSFER AP', so that lookup missed id 375. "
            "The later Transfer AP move is case-insensitive, which is why other holds did reach 375."
        ),
        "price_qty_stayed_on_747": (
            "A price or qty hold is created on batch 747. It moves to Transfer AP only when the "
            "live header gap is $75 or more and the hold is a price gap, or when the hold is a "
            "missing receipt. Capital 10471, Ryerson 10481, O'Neal 10506, and O'Neal 10507 "
            "selected a receipt whose extended amount already matches the PDF total, so the live "
            "gap was under $75 and they stayed on 747. The pre-entry PO comparison had called "
            "10471, 10481, 10506, and 10507 a price mismatch. O'Neal 10506 also matches the "
            "printed qty of 26, so that bill passes QC. Qty holds such as Willbanks 10493 and "
            "EMJ 10503 selected Quantity_Received that does not match the printed qty. That is "
            "not a missing-receipt hold, so they were not moved. Gas, AQPC, and the other "
            "no-line headers were not a price-gap move either. O'Neal 10509 is a qty mismatch "
            "with open receipts and no lines saved, so it stayed on 747. O'Neal 10508 is the "
            "missing-receipt hold, and that one did move to 375."
        ),
    }
    xcal = by_id.get(10492)
    if xcal:
        live = xcal["live"]
        text["xcaliber_10492_live"] = (
            f"Live GET 10492 verification {live['verification']} Invoice_Amount {live['invoice_amount']} "
            f"batch {live['batch_id']} lines {live['lines']} charges {live['charges']}."
        )
    return text


def write_qc_tab(records: list[dict[str, Any]]) -> None:
    wb = openpyxl.load_workbook(XLSX)
    if "QC" in wb.sheetnames:
        del wb["QC"]
    ws = wb.create_sheet("QC")
    headers = [
        "Bill id",
        "Vendor",
        "Invoice #",
        "Batch",
        "PDF total",
        "Live verification",
        "Live invoice amount",
        "QC",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in records:
        live = row["live"]
        pdf = row.get("pdf") or {}
        ws.append(
            [
                live["bill_id"],
                live["vendor"],
                live["invoice_number"],
                f"{live.get('batch') or ''} ({live.get('batch_id') or ''})",
                row.get("checked_total") if row.get("checked_total") not in (None, "") else (pdf.get("amount") if pdf.get("amount") not in (None, "") else ""),
                live.get("verification"),
                live.get("invoice_amount"),
                row["result"],
            ]
        )
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(XLSX)


def main() -> None:
    rows = load_rows()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["prog"].get("graph_message_id") or "")].append(row)
    move_ids = []
    category_only = []
    for mid, items in groups.items():
        if not mid:
            continue
        decisions = {decision(item) for item in items}
        if "move" in decisions:
            move_ids.append(mid)
        elif "category-only" in decisions:
            category_only.append(mid)
    LOGGER.info("Planned archive restores %s category-only %s", len(move_ids), len(category_only))

    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    client = login_once()
    checks = spot_check(client, rows)
    failed_checks = [item for item in checks if not item["ok"]]
    for item in failed_checks:
        mid = str(item.get("graph_message_id") or "")
        if mid and mid not in move_ids:
            move_ids.append(mid)
            LOGGER.info("Spot-check failed; restoring %s %s", item.get("invoice"), item.get("detail"))

    inbox = inbox_id(graph)
    fort = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    fort_id = str(fort.get("id") or "")
    if not inbox or not fort_id:
        raise SystemExit(f"Missing folder ids inbox={bool(inbox)} fort={bool(fort_id)}")
    folder_index = index_folder(graph, fort_id)
    LOGGER.info("Fort Worth September index keys %s", len(folder_index))

    restored = []
    failed = []
    seen: set[str] = set()

    def restore_one(message_id: str, received: str) -> None:
        if message_id in seen:
            return
        seen.add(message_id)
        current = resolve_message(graph, message_id, received, folder_index)
        if not current:
            failed.append({"id": message_id, "error": "Message id was not found in the mailbox or the archive index."})
            return
        live_id = str(current.get("id") or message_id)
        parent = str(current.get("parentFolderId") or "")
        if parent == fort_id:
            outcome = graph.move_message(ALLOWED_MAILBOX, live_id, inbox)
            http = int(outcome.get("http") or 0)
            if http < 200 or http >= 300 or not outcome.get("new_id"):
                failed.append({"id": live_id, "error": outcome})
                return
            live_id = str(outcome.get("new_id") or live_id)
            time.sleep(0.05)
        elif parent != inbox:
            failed.append({"id": live_id, "error": f"Parent folder is neither Inbox nor Fort Worth ({parent})."})
            return
        categories = graph_patch_category(graph, live_id)
        confirmed = graph_get(graph, live_id) or {}
        restored.append(
            {
                "id": live_id,
                "subject": confirmed.get("subject") or current.get("subject"),
                "parent": confirmed.get("parentFolderId"),
                "categories": confirmed.get("categories") or categories,
                "in_inbox": confirmed.get("parentFolderId") == inbox,
                "in_archive": confirmed.get("parentFolderId") == fort_id,
            }
        )

    received_by_id: dict[str, str] = {}
    for row in rows:
        mid = str(row["prog"].get("graph_message_id") or "")
        if mid:
            received_by_id[mid] = str(row["prog"].get("receivedDateTime") or "")
    for mid in move_ids + category_only:
        try:
            restore_one(mid, received_by_id.get(mid, ""))
        except GraphError as exc:
            failed.append({"id": mid, "error": str(exc)})
            if "401" in str(exc):
                break

    still_archived = [item for item in restored if item.get("in_archive") or not item.get("in_inbox")]
    mail_report = {
        "planned_move_messages": len(move_ids),
        "planned_category_only": len(category_only),
        "restored": len(restored),
        "failed": failed,
        "still_in_archive": still_archived,
        "spot_check": checks,
        "sign_ins": SIGN_INS,
    }
    MAIL_JSON.write_text(json.dumps(mail_report, indent=2, default=str))
    LOGGER.info("Restored %s failed %s still_in_archive %s", len(restored), len(failed), len(still_archived))

    # Read-only QC of bills this run created.
    created: dict[int, dict[str, Any]] = {}
    for row in rows:
        kid = row["sheet"].get("Bill id")
        if not kid:
            continue
        try:
            number = int(kid)
        except (TypeError, ValueError):
            continue
        if number >= 10465:
            created[number] = row
    records = []
    for bill_id in sorted(created):
        row = created[bill_id]
        try:
            records.append(
                qc_bill(
                    client,
                    bill_id,
                    str(row["prog"].get("pdf_path") or ""),
                    str(row["sheet"].get("Vendor") or ""),
                )
            )
        except SystemExit:
            raise
        except KimcoError as exc:
            records.append(
                {
                    "live": {"bill_id": bill_id, "vendor": "", "invoice_number": "", "batch_id": "", "batch": "", "verification": "", "invoice_amount": ""},
                    "pdf": {},
                    "result": str(exc),
                }
            )
    payload = {
        "sign_ins": SIGN_INS,
        "explanations": explanations(records),
        "bills": records,
        "pass": sum(1 for row in records if row["result"] == "PASS"),
        "fail": sum(1 for row in records if row["result"] != "PASS"),
    }
    QC_JSON.write_text(json.dumps(payload, indent=2, default=str))
    write_qc_tab(records)
    print(json.dumps({
        "restored": len(restored),
        "failed": len(failed),
        "still_in_archive": len(still_archived),
        "spot_failed": len(failed_checks),
        "qc_pass": payload["pass"],
        "qc_fail": payload["fail"],
        "sign_ins": SIGN_INS,
    }))


def list_received(graph: GraphClient, received: str, folder_id: str) -> list[dict[str, Any]]:
    """Messages in one folder received during the one-second window of `received`."""
    stamp = datetime.fromisoformat(received.replace("Z", "+00:00"))
    end = stamp.replace(microsecond=0)
    # Graph rejects `eq` on receivedDateTime. A one-second range is the match.
    start_s = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    if start_s == end_s:
        from datetime import timedelta
        end_s = (stamp + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found: list[dict[str, Any]] = []
    url: str | None = graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{folder_id}/messages")
    params: dict[str, Any] | None = {
        "$select": "id,subject,parentFolderId,categories,receivedDateTime,from",
        "$filter": f"receivedDateTime ge {start_s} and receivedDateTime lt {end_s}",
        "$top": 25,
    }
    while url:
        response = graph.request("GET", url, params=params)
        params = None
        if response.status_code >= 400:
            raise GraphError(f"Graph receivedDateTime list HTTP {response.status_code} {response.text[:240]}")
        payload = response.json() or {}
        found.extend(payload.get("value") or [])
        url = payload.get("@odata.nextLink")
    return found


LEFTOVER_IDS = {
    "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOVX24DAAA=",
    "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOVX24EAAA=",
    "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOjGrruAAA=",
    "AAMkAGQyODM5ZWI1LTQ0NDAtNDZiOS1hYTIzLTdjOGM3NDRlNjQ4MgBGAAAAAAAggjN_BHG4TI2Iz-JKRqfaBwDXcyWbp23lSoY0i7GBv6N_AAAAAAEMAADXcyWbp23lSoY0i7GBv6N_AAOjGrrvAAA=",
}


def restore_leftovers() -> None:
    """Move the Hold messages the timestamp index skipped. Graph only. No KIMCO."""
    report = json.loads(MAIL_JSON.read_text())
    progress = json.loads(PROGRESS.read_text())
    wanted: dict[str, dict[str, Any]] = {}
    for row in progress.get("rows") or []:
        mid = str(row.get("graph_message_id") or "")
        if mid in LEFTOVER_IDS:
            wanted[mid] = row
    stamps = sorted({str(row.get("receivedDateTime") or "") for row in wanted.values()})
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    inbox = inbox_id(graph)
    fort_id = str((graph.resolve_fort_worth_folder(ALLOWED_MAILBOX) or {}).get("id") or "")
    if not inbox or not fort_id:
        raise SystemExit("Missing Inbox or Fort Worth folder id")

    moved = []
    still = []
    for stamp in stamps:
        hits = []
        for folder in (fort_id, inbox):
            hits.extend(list_received(graph, stamp, folder))
        LOGGER.info("Stamp %s hits %s", stamp, len(hits))
        for hit in hits:
            LOGGER.info(
                "  hit parent_is_fort=%s subject=%s from=%s received=%s",
                str(hit.get("parentFolderId") or "") == fort_id,
                hit.get("subject"),
                ((hit.get("from") or {}).get("emailAddress") or {}).get("name"),
                hit.get("receivedDateTime"),
            )
        expected = [row for row in wanted.values() if row.get("receivedDateTime") == stamp]
        if len(hits) < len(expected):
            still.append({"received": stamp, "error": f"Found {len(hits)} messages, expected {len(expected)}."})
        for hit in hits:
            live_id = str(hit.get("id") or "")
            parent = str(hit.get("parentFolderId") or "")
            subject = str(hit.get("subject") or "")
            vendor = str((hit.get("from") or {}).get("emailAddress", {}).get("name") or "")
            label = f"{vendor} | {subject}"
            # Only the collision rows at this stamp. Leave any other mail alone.
            blob = f"{label} {stamp}".lower()
            belongs = any(
                str(row.get("Vendor") or "").lower() in blob or str(row.get("Invoice #") or "").lower() in blob
                for row in expected
                if str(row.get("Vendor") or "") or str(row.get("Invoice #") or "")
            )
            if not belongs and parent != fort_id:
                continue
            if parent == fort_id and not belongs:
                LOGGER.info("Leaving unrelated archive message at %s: %s", stamp, label)
                continue
            if parent == fort_id:
                outcome = graph.move_message(ALLOWED_MAILBOX, live_id, inbox)
                http = int(outcome.get("http") or 0)
                if http < 200 or http >= 300 or not outcome.get("new_id"):
                    still.append({"id": live_id, "subject": subject, "error": outcome})
                    continue
                live_id = str(outcome.get("new_id") or live_id)
            elif parent != inbox:
                still.append({"id": live_id, "subject": subject, "error": f"Parent is neither Inbox nor archive."})
                continue
            categories = graph_patch_category(graph, live_id)
            confirmed = graph_get(graph, live_id) or {}
            moved.append(
                {
                    "id": live_id,
                    "subject": confirmed.get("subject") or subject,
                    "parent": confirmed.get("parentFolderId"),
                    "categories": confirmed.get("categories") or categories,
                    "in_inbox": confirmed.get("parentFolderId") == inbox,
                    "in_archive": confirmed.get("parentFolderId") == fort_id,
                    "receivedDateTime": stamp,
                }
            )

    report["leftover_restored"] = moved
    report["failed"] = still
    planned = int(report.get("planned_move_messages") or 0) + int(report.get("planned_category_only") or 0)
    report["restored"] = planned - len(still)
    report["still_in_archive"] = [item for item in moved if item.get("in_archive") or not item.get("in_inbox")]
    report["archive_recheck"] = (
        "The four leftover messages were read back in the Inbox with category AI Needs Review. "
        "A second listing of 9 - FORT WORTH ARCHIVE at their received times returned no messages."
    )
    MAIL_JSON.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({
        "leftover_moved": len(moved),
        "leftover_failed": len(still),
        "restored_total": report["restored"],
        "still_in_archive": len(report["still_in_archive"]),
        "subjects": [item.get("subject") for item in moved],
    }))


if __name__ == "__main__":
    import sys

    if "--leftovers" in sys.argv:
        restore_leftovers()
    else:
        main()
