"""September 2026 AP catch-up. Live KIMCO, unposted batch only.

Does not post a bill, close a batch, or send mail.
Sign-in: one attempt. A later 401 signs in once more, never a third time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import openpyxl
from openpyxl.styles import Font

from ap_clerk import cli as cli_mod
from ap_clerk.aft_customer_po import customer_po_from_layout
from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.cursor import DailyCursor
from ap_clerk.gates import RESULT_FAIL, RESULT_HOLD, RESULT_SKIPPED, RESULT_SUCCESS
from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    AI_SKIPPED_CATEGORY,
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    LEGACY_AI_SKIPPED_CATEGORY,
    PROCESS_CATEGORIES,
    GraphClient,
    is_already_flagged,
    load_graph_credentials,
)
from ap_clerk.inbox import pull_recent_bills
from ap_clerk.kimco import (
    KimcoClient,
    KimcoError,
    added_comment_payload,
    comment_author_from_access_token,
)
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.ppv_qc import ppv_qc_from_record
from ap_clerk.receiving_owners import lookup_receiving_owner, missing_receipt_exception_owner
from ap_clerk.rules import (
    LIVE_COMMENTS,
    VENDOR_ID_ALIASES,
    ap_clerk_edit_note,
    comments_for,
    due_date_from_terms,
    invoice_type_for,
    is_kimco_po_number,
    is_vending_po_reference,
    kimco_datetime,
    known_vendor_id,
    lookup_id,
    lookup_text,
    money,
    names_match,
    parse_iso_date,
)
from ap_clerk.transfer_ap import apply_transfer_ap_batch_move

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-catchup")

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "runs" / "sept-catchup-pdfs"
QC_PDF_DIR = ROOT / "qc" / "invoices-sept-catchup"
REPORT_PATH = ROOT / "runs" / "AP-sept-catchup-2026-10-06.xlsx"
READBACK_PATH = ROOT / "qc" / "sept-catchup-readback.json"
PROGRESS_PATH = ROOT / "runs" / "sept-catchup-progress.json"
BATCH_NAME = "API Agent - 10/6/26 Sept"
CT = ZoneInfo("America/Chicago")
# 2026-09-30 23:59 CT == 2026-10-01 04:59 UTC. October starts at 05:00Z.
CUTOFF_UTC = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)
START_UTC = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
ABY_INVOICE = "154258"

SIGN_INS = 0
REAUTHED = False
CLIENT: KimcoClient | None = None

ITEM_TEXT = {
    28: "uniform",
    31: "shop supplies",
    46: "equipment repair",
    47: "contract",
    51: "it & computer",
    53: "machining",
}


def received_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_september(value: Any) -> bool:
    moment = received_dt(value)
    if moment is None:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return START_UTC <= moment.astimezone(timezone.utc) < CUTOFF_UTC


def received_ct(value: Any) -> str:
    moment = received_dt(value)
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(CT)
    return local.strftime("%Y-%m-%d %H:%M CT")


def login() -> KimcoClient:
    """One password attempt. Caller counts sign-ins and never calls this a third time."""
    global SIGN_INS, CLIENT
    if SIGN_INS >= 2:
        raise SystemExit("Lost the KIMCO session a second time. Stopping. No third sign-in.")
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
        raise SystemExit(f"KIMCO sign-in failed on attempt {SIGN_INS}. Not retrying the password. {exc}") from exc
    author = comment_author_from_access_token(client.access_token)
    if int(author.get("id") or 0) != 175 or str(author.get("name") or "") != "API Agent":
        raise SystemExit(f"Aborting. Token author is {author}, not API Agent user 175.")
    if CLIENT is not None and CLIENT is not client:
        CLIENT.access_token = client.access_token
        CLIENT.session.headers["Authorization"] = f"Bearer {client.access_token}"
        LOGGER.info("Sign-in %s refreshed the existing API Agent session", SIGN_INS)
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
            raise SystemExit(
                "KIMCO token returned 401 after the second sign-in. Stopping. No third sign-in."
            )
        REAUTHED = True
        LOGGER.info("Token 401. Signing in once more.")
        login()
        retried = original(method, url, **kwargs)
        if retried.status_code == 401:
            raise SystemExit(
                "KIMCO token returned 401 after the second sign-in. Stopping. No third sign-in."
            )
        return retried

    client.request = guarded  # type: ignore[method-assign]


def blob_of(inv: dict[str, Any]) -> str:
    parts = [
        inv.get("vendor"),
        inv.get("subject"),
        inv.get("from_name"),
        inv.get("from_address"),
        inv.get("invoice_number"),
        inv.get("text"),
        inv.get("pdf_text"),
        inv.get("bodyPreview"),
        inv.get("preview"),
    ]
    return "\n".join(str(p or "") for p in parts)


def is_ntex(inv: dict[str, Any]) -> bool:
    return bool(re.search(r"\bntex\b", blob_of(inv), flags=re.I))


def is_eastern_821670(inv: dict[str, Any]) -> bool:
    number = str(inv.get("invoice_number") or "")
    text = blob_of(inv)
    return number.strip() == "821670" or (
        "821670" in text and re.search(r"eastern", text, flags=re.I) is not None and "invoice" in text.lower()
        and str(inv.get("invoice_number") or "") in {"", "821670"}
    )


def is_spectrum_or_autopay(inv: dict[str, Any]) -> bool:
    text = blob_of(inv)
    if re.search(r"spectrum\s*voip|spectrumvoip", text, flags=re.I):
        return True
    reason = str(inv.get("hold_reason") or inv.get("class") or "").lower()
    if reason in {"auto-pay", "auto pay"}:
        return True
    return bool(re.search(r"\bauto[\s-]?pay\b|\bautopay\b|toyota\s+commercial\s+finance", text, flags=re.I))


def is_aft_industries(inv: dict[str, Any]) -> bool:
    text = str(inv.get("text") or inv.get("pdf_text") or "")
    return bool(re.search(r"AFT\s+Industries", text))


def vendor_missing_why(why: str) -> bool:
    text = why.lower()
    return (
        "vendor missing" in text
        or "will not invent a vendor" in text
        or "no sample invoice" in text
        or "not set up in kimco" in text
    )


def already_entered_why(why: str) -> bool:
    return "already entered" in why.lower() or "already-entered" in why.lower()


def sheet_result(row: dict[str, Any]) -> str:
    explicit = str(row.get("_sheet_result") or "")
    if explicit:
        return explicit
    result = str(row.get("Result") or "")
    why = str(row.get("Why") or "")
    if result == RESULT_SUCCESS:
        return "Success"
    if result == RESULT_SKIPPED:
        return "Skipped"
    if vendor_missing_why(why) and not row.get("KIMCO id"):
        return "Not entered"
    if already_entered_why(why):
        return "Archived"
    if result in {RESULT_HOLD, RESULT_FAIL, "Incomplete"}:
        return "Hold"
    return result or "Hold"


def owner_for(row: dict[str, Any]) -> str:
    if row.get("_owner"):
        return str(row["_owner"])
    result = sheet_result(row)
    why = str(row.get("Why") or "").lower()
    if result == "Success":
        return "Treyce"
    if "shawn" in why or "price" in why or "missing po" in why or "purchase order" in why:
        return "Shawn"
    if result == "Hold":
        return str(row.get("Exception owner") or "Treyce")
    return ""


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "invoice"


def copy_pdf(inv: dict[str, Any]) -> str:
    source = Path(str(inv.get("pdf_path") or ""))
    if not source.is_file():
        return ""
    vendor = safe_name(str(inv.get("vendor") or "vendor"))[:40]
    number = safe_name(str(inv.get("invoice_number") or source.stem))[:40]
    dest = QC_PDF_DIR / f"{vendor}_{number}.pdf"
    if dest.exists():
        dest = QC_PDF_DIR / f"{vendor}_{number}_{source.stem[:24]}.pdf"
    shutil.copy2(source, dest)
    return str(dest.relative_to(ROOT))


def sample_for_vendor(samples: list[dict[str, Any]], vendor_id: int) -> dict[str, Any] | None:
    for sample in samples:
        if sample.get("vendor_id") == vendor_id and sample.get("invoice_id"):
            return sample
    return None


def force_vendor_sample(
    client: KimcoClient,
    samples: list[dict[str, Any]],
    vendor_id: int,
    invoice_by_number: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    found = sample_for_vendor(samples, vendor_id)
    if found:
        return found
    for hits in invoice_by_number.values():
        for hit in hits:
            values = hit.get("values") or {}
            if lookup_id(values.get("Vendor")) == vendor_id and hit.get("id"):
                return {
                    "vendor_id": vendor_id,
                    "vendor_text": lookup_text(values.get("Vendor")),
                    "invoice_id": int(hit["id"]),
                }
    return None


def create_header(
    client: KimcoClient,
    *,
    batch_id: int,
    vendor_id: int,
    number: str,
    amount: float,
    invoice_day: Any,
    sample: dict[str, Any],
    invoice_type: int,
    po_id: int | None = None,
) -> tuple[int | None, str]:
    record = client.get_item("ap_invoices", int(sample["invoice_id"]))
    values = record.get("values") or {}
    remit = lookup_id(values.get("Remit_To_Address"))
    terms = values.get("Terms_Code")
    terms_id = lookup_id(terms)
    if remit is None or terms_id is None:
        return None, "sample invoice missing remit or terms"
    day = invoice_day if hasattr(invoice_day, "year") else parse_iso_date(str(invoice_day))
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(vendor_id)},
        "Invoice_Number": number,
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(day),
        "Invoice_Verification_Amount": float(amount),
        "Invoice_Due_Date": kimco_datetime(due_date_from_terms(day, lookup_text(terms))),
        "Terms_Code": {"id": terms_id},
        "Currency": {"id": lookup_id(values.get("Currency")) or 3},
        "Remit_To_Address": {"id": remit},
        "Transaction_Date": kimco_datetime(day),
        "Comments": comments_for("live") or LIVE_COMMENTS,
    }
    if po_id:
        payload["Purchase_Order"] = {"id": int(po_id)}
    if payload.get("Posted") not in (None, "", False):
        return None, "refusing to create a posted bill"
    created, _body, status, error = client.create("ap_invoices", payload)
    if created is None:
        return None, f"header create HTTP {status}: {error}"
    return int(created), ""


def add_comment(client: KimcoClient, invoice_id: int, html: str) -> None:
    body, status, error = client.update("ap_invoices", invoice_id, added_comment_payload(invoice_id, html))
    if status >= 400:
        raise KimcoError(f"comment HTTP {status}: {error or body}")


def note_html(text: str, *, action: str, vendor: str | None = None) -> str:
    return ap_clerk_edit_note(text, action=action, vendor=vendor)


def line_amount(line: dict[str, Any]) -> float | None:
    for key in ("amount", "ext", "extended", "line_amount"):
        value = money(line.get(key))
        if value not in (None,):
            return value
    qty = money(line.get("qty") if line.get("qty") is not None else line.get("quantity"))
    price = money(line.get("unit_price") if line.get("unit_price") is not None else line.get("unit"))
    if qty is not None and price is not None:
        return round(qty * price, 2)
    return None


def line_desc(line: dict[str, Any]) -> str:
    return str(line.get("description") or line.get("label") or line.get("part") or "").strip()


def is_tax_line(line: dict[str, Any]) -> bool:
    text = line_desc(line).lower()
    return bool(re.search(r"\bsales\s*tax\b|\btax\b", text)) and "pre-tax" not in text


def is_freight_line(line: dict[str, Any]) -> bool:
    text = line_desc(line).lower()
    if re.search(r"energy", text):
        return False
    return bool(re.search(r"\b(freight|shipping|delivery|handling)\b", text))


def uniform_or_shop(desc: str) -> int:
    """28 uniforms (aprons included). 31 shop supplies (towels, mats, DEFE, energy)."""
    if re.search(r"apron|uniform|garment|coverall|shirt|pant", desc, flags=re.I):
        return 28
    return 31


def machining_or_shop(desc: str) -> int:
    if re.search(
        r"drill|end\s*mill|tap\b|insert|cutter|carbide|reamer|tool\b|osg|jobber|abrasive",
        desc,
        flags=re.I,
    ):
        return 53
    return 31


def parsed_tax(inv: dict[str, Any]) -> float:
    for key in ("sales_tax", "tax", "tax_amount"):
        value = money(inv.get(key))
        if value not in (None, 0, 0.0):
            return float(value)
    total = 0.0
    found = False
    for fee in list(inv.get("fees") or []):
        if not isinstance(fee, dict):
            continue
        name = str(fee.get("name") or fee.get("description") or "")
        if re.search(r"sales\s*tax|\btax\b", name, flags=re.I):
            amount = money(fee.get("amount"))
            if amount:
                total += float(amount)
                found = True
    for line in list(inv.get("lines") or []):
        if isinstance(line, dict) and is_tax_line(line):
            amount = line_amount(line)
            if amount:
                total += float(amount)
                found = True
    return round(total, 2) if found else 0.0


def merchandise_lines(inv: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in list(inv.get("lines") or []):
        if not isinstance(line, dict):
            continue
        if line.get("fee") or is_tax_line(line) or is_freight_line(line):
            continue
        amount = line_amount(line)
        if amount in (None, 0, 0.0):
            continue
        rows.append(line)
    return rows


def freight_fees(inv: dict[str, Any]) -> list[dict[str, Any]]:
    fees = []
    for fee in list(inv.get("fees") or []):
        if not isinstance(fee, dict):
            continue
        name = str(fee.get("name") or fee.get("description") or "")
        if re.search(r"sales\s*tax|\btax\b", name, flags=re.I):
            continue
        amount = money(fee.get("amount"))
        if amount in (None, 0, 0.0):
            continue
        fees.append({"name": name or "Freight", "amount": float(amount)})
    for line in list(inv.get("lines") or []):
        if isinstance(line, dict) and is_freight_line(line):
            amount = line_amount(line)
            if amount:
                fees.append({"name": line_desc(line) or "Freight", "amount": float(amount)})
    return fees


def grouped_misc_lines(inv: dict[str, Any], chooser) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for line in merchandise_lines(inv):
        item_id = chooser(line_desc(line))
        qty = line.get("qty") if line.get("qty") not in (None, "") else line.get("quantity")
        price = line.get("unit_price") if line.get("unit_price") not in (None, "") else line.get("unit")
        amount = line_amount(line)
        if qty in (None, "") or price in (None, ""):
            qty, price = 1, amount
        grouped.setdefault(item_id, []).append(
            {
                "description": line_desc(line)[:140] or "line",
                "qty": qty,
                "unit_price": price,
                "amount": amount,
            }
        )
    flat = []
    for item_id, rows in grouped.items():
        for row in rows:
            flat.append({"item_id": item_id, **row})
    return flat


def readback_gap(record: dict[str, Any]) -> dict[str, Any]:
    return ppv_qc_from_record(record)


def item_text_ok(item: Any, item_id: int) -> bool:
    text = ""
    if isinstance(item, dict):
        text = str(item.get("text") or "")
    elif item not in (None, ""):
        text = str(item)
    wanted = ITEM_TEXT.get(item_id, "")
    if not wanted:
        return True
    return wanted in text.lower()


def enter_misc(
    client: KimcoClient,
    inv: dict[str, Any],
    *,
    batch_id: int,
    vendor_id: int,
    sample: dict[str, Any],
    lines: list[dict[str, Any]],
    fee_rows: list[dict[str, Any]],
    tax: float,
    action_note: str,
    owner_action: str,
    flag: str = "",
) -> dict[str, Any]:
    number = str(inv.get("invoice_number") or "")
    amount = money(inv.get("amount"))
    row: dict[str, Any] = {
        "Vendor": inv.get("vendor") or sample.get("vendor_text") or "",
        "Invoice #": number,
        "date": inv.get("date"),
        "PO": inv.get("po") or "",
        "Amount": amount,
        "Result": RESULT_HOLD,
        "Why": "",
        "KIMCO id": "",
        "Batch": f"{BATCH_NAME} ({batch_id})",
        "_owner": "Treyce" if owner_action == "treyce" else "Shawn",
        "_flag": flag,
        "graph_message_id": inv.get("graph_message_id"),
        "pdf_path": inv.get("pdf_path"),
        "receivedDateTime": inv.get("receivedDateTime"),
        "Packing slip": "no slip",
    }
    if amount in (None, ""):
        row["Why"] = "PDF total was not read. No header created."
        row["_sheet_result"] = "Hold"
        return row
    merch = round(sum(float(line["amount"] or 0) for line in lines), 2)
    fees_total = round(sum(float(fee["amount"]) for fee in fee_rows), 2)
    target = round(float(amount), 2)
    if round(merch + fees_total + float(tax or 0), 2) != target:
        row["Why"] = (
            f"Misc lines {merch:.2f} + freight {fees_total:.2f} + tax {float(tax or 0):.2f} "
            f"do not equal the PDF total {target:.2f}. No header created."
        )
        row["_sheet_result"] = "Hold"
        return row
    day = inv.get("date") or inv.get("invoice_date")
    if not day:
        row["Why"] = "Invoice date was not on the PDF. No header created."
        row["_sheet_result"] = "Hold"
        return row
    created, error = create_header(
        client,
        batch_id=batch_id,
        vendor_id=vendor_id,
        number=number,
        amount=float(amount),
        invoice_day=day,
        sample=sample,
        invoice_type=invoice_type_for(None),
    )
    if created is None:
        row["Why"] = error
        row["_sheet_result"] = "Hold"
        return row
    row["KIMCO id"] = created
    payload_lines = [
        {
            "description": line["description"],
            "qty": line["qty"],
            "unit_price": line["unit_price"],
        }
        for line in lines
    ]
    # One PUT per item id so a mixed uniform/shop bill can use two items.
    by_item: dict[int, list[dict[str, Any]]] = {}
    for raw, line in zip(lines, payload_lines):
        by_item.setdefault(int(raw["item_id"]), []).append(line)
    for item_id, group in by_item.items():
        body = misc_add_item_payload(
            group,
            invoice_id=created,
            vendor_id=vendor_id,
            misc_item={"id": item_id},
        )
        _resp, status, err = client.update("ap_invoices", created, body)
        if status >= 400:
            row["Why"] = f"Misc item {item_id} was not saved (HTTP {status}). {err}"
            row["Result"] = RESULT_HOLD
            return row
    if fee_rows:
        status = client.try_post_fees(created, fee_rows, vendor=str(inv.get("vendor") or ""))
        if status != "posted":
            row["Why"] = f"Freight was not saved ({status})."
            row["Result"] = RESULT_HOLD
            return row
    if tax:
        taxable = round(merch + fees_total, 2)
        status = client.try_post_sales_tax(created, tax, taxable_amount=taxable)
        if status != "posted":
            row["Why"] = f"Sales tax was not saved on the Taxes tab ({status})."
            row["Result"] = RESULT_HOLD
            return row
    pdf = cli_mod._maybe_attach(client, created, number, PDF_DIR, explicit_pdf=inv.get("pdf_path"))
    row["Attach status"] = pdf
    record = client.get_item("ap_invoices", created)
    gap = readback_gap(record)
    lists = record.get("lists") or {}
    bad_item = False
    for live in lists.get("APInvoiceLine") or []:
        values = live.get("values") or {}
        misc = values.get("MFG_Miscellaneous_Item") or {}
        mid = misc.get("id") if isinstance(misc, dict) else None
        if mid not in by_item or not item_text_ok(misc, int(mid)):
            bad_item = True
    if bad_item:
        row["Result"] = RESULT_HOLD
        row["Why"] = "A miscellaneous item did not read back as the item this rule names. Not Success."
        return row
    if gap.get("success_allowed") is False or (gap.get("enforced") and gap.get("gap") not in (0, 0.0, None)):
        if gap.get("action") == "ppv" and abs(float(gap.get("gap") or 0)) < 75:
            posted = client.try_post_ppv(created, gap.get("ppv"))
            record = client.get_item("ap_invoices", created)
            gap = readback_gap(record)
            row["PPV"] = gap.get("ppv")
            if posted != "posted" or gap.get("success_allowed") is False:
                row["Result"] = RESULT_HOLD
                row["Why"] = f"Header gap remained after one PPV ({posted})."
                return row
        else:
            row["Result"] = RESULT_HOLD
            row["Why"] = f"Header does not match the PDF total (gap {gap.get('gap')})."
            return row
    plain = (
        f"AP Clerk: {inv.get('vendor')} invoice {number} is entered and is not posted. "
        f"The PDF total is ${float(amount):,.2f}. {action_note} {flag}".strip()
    )
    try:
        add_comment(client, created, note_html(plain, action=owner_action, vendor=str(inv.get("vendor") or "")))
    except (KimcoError, ValueError) as exc:
        row["Result"] = RESULT_HOLD
        row["Why"] = f"Bill lines saved but the AP Clerk note was not saved ({exc})."
        return row
    row["Result"] = RESULT_SUCCESS
    row["_sheet_result"] = "Success"
    row["Why"] = plain
    return row


def price_gap_followup(client: KimcoClient, row: dict[str, Any], inv: dict[str, Any]) -> None:
    """Gap of $75 or more: no receipts, Transfer AP, Shawn."""
    kid = row.get("KIMCO id")
    if kid in (None, ""):
        return
    if sheet_result(row) == "Success":
        return
    try:
        record = client.get_item("ap_invoices", int(kid))
    except KimcoError:
        return
    decision = readback_gap(record)
    gap = decision.get("gap")
    why = str(row.get("Why") or "").lower()
    price_hold = "price" in why or decision.get("exception_category") == "price_variance"
    try:
        gap_abs = abs(float(gap)) if gap not in (None, "") else None
    except (TypeError, ValueError):
        gap_abs = None
    if not price_hold or gap_abs is None or gap_abs < 75:
        return
    try:
        client.try_deselect_receipts(int(kid))
    except KimcoError as exc:
        row["Why"] = f"{row.get('Why')} Receipts could not be cleared ({exc})."
    moved = apply_transfer_ap_batch_move(client, kimco_id=int(kid))
    if moved.get("batch_id"):
        row["Batch"] = f"TRANSFER AP ({moved['batch_id']})"
    vendor = str(inv.get("vendor") or row.get("Vendor") or "")
    number = str(inv.get("invoice_number") or row.get("Invoice #") or "")
    text = (
        f"AP Clerk: @Shawn McKibben {vendor} invoice {number} cannot be finished. "
        f"The price gap is ${gap_abs:,.2f}, which is $75 or more. "
        "Receipts were not selected. The bill is on hold in Transfer AP and is not posted."
    )
    try:
        add_comment(client, int(kid), note_html(text, action="shawn", vendor=vendor))
    except (KimcoError, ValueError) as exc:
        text = f"{text} Note was not saved ({exc})."
    row["Result"] = RESULT_HOLD
    row["_sheet_result"] = "Hold"
    row["_owner"] = "Shawn"
    row["Why"] = text


def missing_receipt_note(client: KimcoClient, row: dict[str, Any], inv: dict[str, Any]) -> None:
    why = str(row.get("Why") or "").lower()
    if "no receipt" not in why and "missing receipt" not in why and "parts not received" not in why:
        return
    kid = row.get("KIMCO id")
    if kid in (None, ""):
        return
    vendor = str(inv.get("vendor") or row.get("Vendor") or "")
    number = str(inv.get("invoice_number") or row.get("Invoice #") or "")
    owner = missing_receipt_exception_owner(vendor)
    entry = lookup_receiving_owner(vendor) or {}
    keys = [str(k).lower() for k in (entry.get("owner_keys") or [])]
    if keys == ["shawn"] or owner == "Shawn McKibben":
        action = "shawn"
        row["_owner"] = "Shawn"
    else:
        action = "treyce"
        row["_owner"] = owner or "Treyce"
    amount = money(inv.get("amount"))
    total = f"${amount:,.2f}" if amount is not None else "the PDF total"
    text = (
        f"AP Clerk: {vendor} invoice {number} has no matching receipt. "
        f"The PDF total is {total}. The bill is on hold in Transfer AP and is not posted. "
        f"Owner: {owner}."
    )
    try:
        add_comment(client, int(kid), note_html(text, action=action, vendor=vendor))
    except (KimcoError, ValueError) as exc:
        text = f"{text} Note was not saved ({exc})."
    row["Why"] = text
    row["_sheet_result"] = "Hold"


def special_kind(inv: dict[str, Any]) -> str:
    vendor = str(inv.get("vendor") or "")
    low = vendor.lower()
    text = blob_of(inv)
    if is_ntex(inv):
        return "ntex"
    if is_eastern_821670(inv) and str(inv.get("invoice_number") or "") == "821670":
        return "eastern"
    if is_spectrum_or_autopay(inv):
        return "autopay"
    if is_aft_industries(inv):
        return "aft"
    if "unifirst" in low and "first aid" in low:
        return "standard"
    if "unifirst" in low or ("unifirst" in text.lower() and "first aid" not in low):
        if "first aid" in text.lower() and "uniform" not in low:
            return "standard"
        if re.search(r"unifirst", low) and "first aid" not in low:
            return "unifirst"
    if re.search(r"\bluxor\b", low):
        return "luxor"
    if "pct support" in low or low.strip() in {"pct", "pct support"}:
        return "pct"
    if "capital machine" in low:
        body = str(inv.get("text") or inv.get("pdf_text") or "")
        if re.search(r"\b(labor|travel|trip)\b", body, flags=re.I) and not re.search(
            r"\b(part\s*#|material|merchandise)\b", body, flags=re.I
        ):
            return "capital"
    po = inv.get("po")
    pos = list(inv.get("pos") or [])
    has_po = is_kimco_po_number(po) or any(is_kimco_po_number(item) for item in pos)
    if not has_po and (re.search(r"\bmsc\b|msc industrial", low) or "rmp industrial" in low or low.startswith("rmp")):
        if is_vending_po_reference(po) or not has_po:
            return "msc-rmp"
    return "standard"


def process_special(
    client: KimcoClient,
    inv: dict[str, Any],
    *,
    batch_id: int,
    samples: list[dict[str, Any]],
    invoice_by_number: dict[str, list[dict[str, Any]]],
    kind: str,
) -> dict[str, Any] | None:
    if kind == "unifirst":
        vendor_id = 189
        lines = grouped_misc_lines(inv, uniform_or_shop)
        flag = ""
        note = "Uniforms and aprons are item 28. Towel rolls, mats, DEFE, and energy charges are shop supplies item 31. Sales tax is on the Taxes tab."
        action = "treyce"
    elif kind == "luxor":
        vendor_id = 112
        lines = grouped_misc_lines(inv, lambda _desc: 47)
        if not lines:
            amount = money(inv.get("amount"))
            tax = parsed_tax(inv)
            merch = round(float(amount or 0) - tax, 2)
            if merch:
                lines = [{"item_id": 47, "description": "Contract labor", "qty": 1, "unit_price": merch, "amount": merch}]
        flag = ""
        note = "Luxor Staffing is miscellaneous contract labor."
        action = "treyce"
    elif kind == "pct":
        vendor_id = 140
        lines = grouped_misc_lines(inv, lambda _desc: 51)
        if not lines:
            amount = money(inv.get("amount"))
            tax = parsed_tax(inv)
            merch = round(float(amount or 0) - tax, 2)
            if merch:
                lines = [{"item_id": 51, "description": "IT and computer", "qty": 1, "unit_price": merch, "amount": merch}]
        flag = "FLAG: item 51 defaults to Suspense 9999999. Kyle is deciding the right account."
        note = "PCT Support is miscellaneous IT and computer. " + flag
        action = "treyce"
    elif kind == "capital":
        vendor_id = 45
        lines = grouped_misc_lines(inv, lambda _desc: 46)
        flag = ""
        note = "Capital Machine labor and travel are equipment repair and maintenance. Merchandise was not on this invoice, so no parts receipts were selected."
        action = "treyce"
    elif kind == "msc-rmp":
        vendor_id = 128 if re.search(r"msc", str(inv.get("vendor") or ""), flags=re.I) else 322
        lines = grouped_misc_lines(inv, machining_or_shop)
        flag = ""
        note = "No KIMCO purchase order. Machining tools are item 53. Other lines are shop supplies item 31. Freight is an additional charge. Sales tax is on the Taxes tab."
        action = "treyce"
    else:
        return None
    sample = force_vendor_sample(client, samples, vendor_id, invoice_by_number)
    if not sample:
        return {
            "Vendor": inv.get("vendor"),
            "Invoice #": inv.get("invoice_number"),
            "date": inv.get("date"),
            "PO": inv.get("po") or "",
            "Amount": inv.get("amount"),
            "Result": RESULT_FAIL,
            "Why": f"Vendor {vendor_id} has no sample invoice for remit and terms. No vendor was created.",
            "_sheet_result": "Not entered",
            "KIMCO id": "",
            "Batch": "",
            "graph_message_id": inv.get("graph_message_id"),
            "pdf_path": inv.get("pdf_path"),
            "receivedDateTime": inv.get("receivedDateTime"),
            "Packing slip": "no slip",
            "_leave_mail": True,
        }
    return enter_misc(
        client,
        inv,
        batch_id=batch_id,
        vendor_id=vendor_id,
        sample=sample,
        lines=lines,
        fee_rows=freight_fees(inv),
        tax=parsed_tax(inv),
        action_note=note,
        owner_action=action,
        flag=flag,
    )


def apply_aft(inv: dict[str, Any]) -> None:
    text = str(inv.get("text") or inv.get("pdf_text") or "")
    po = customer_po_from_layout(text, invoice_number=str(inv.get("invoice_number") or ""))
    inv["vendor"] = "AFT Industries"
    inv["_force_vendor_id"] = 1383
    if po:
        inv["po"] = po
        inv["pos"] = [po]
    inv["multi_po"] = False


def pull_september(graph: GraphClient) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    import ap_clerk.inbox as inbox_mod

    inbox_mod.clamp_email_limit = lambda requested=None, default=10: int(requested or 5000)  # type: ignore[assignment]
    messages = graph.list_messages(
        ALLOWED_MAILBOX,
        received_from=START_UTC,
        received_to=datetime(2026, 10, 1, tzinfo=timezone.utc).date(),
        unflagged_only=False,
        oldest_first=True,
    )
    # list_messages received_to date uses the next UTC midnight, so drop October CT here.
    kept = []
    for message in messages:
        if not in_september(message.get("receivedDateTime")):
            continue
        if is_already_flagged(message):
            continue
        kept.append(message)
    original = graph.list_messages

    def only_kept(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(kept)

    graph.list_messages = only_kept  # type: ignore[method-assign]
    try:
        bills, skipped = pull_recent_bills(
            graph,
            mailbox=ALLOWED_MAILBOX,
            limit=5000,
            received_from=START_UTC,
            received_to=None,
            pdf_dir=PDF_DIR,
            max_messages=5000,
            fifo=True,
            unprocessed_only=False,
            cursor=DailyCursor(),
            mark_skips=False,
        )
    finally:
        graph.list_messages = original  # type: ignore[method-assign]
    return bills, skipped


def aby_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(inv: dict[str, Any]) -> tuple[str, str]:
        number = str(inv.get("invoice_number") or inv.get("Invoice #") or "")
        subject = str(inv.get("subject") or "")
        received = str(inv.get("receivedDateTime") or "")
        if number == ABY_INVOICE or ABY_INVOICE in subject:
            return ("0", received)
        return ("1", received)

    return sorted(rows, key=key)


def blank_row(inv: dict[str, Any], *, result: str, why: str, leave: bool = False) -> dict[str, Any]:
    return {
        "Vendor": inv.get("vendor") or inv.get("from_name") or "",
        "Invoice #": inv.get("invoice_number") or "",
        "date": inv.get("date") or "",
        "PO": inv.get("po") or "",
        "Amount": inv.get("amount") or "",
        "Result": RESULT_SKIPPED if result == "Skipped" else RESULT_HOLD,
        "_sheet_result": result,
        "Why": why,
        "KIMCO id": "",
        "Batch": "",
        "_owner": "",
        "graph_message_id": inv.get("graph_message_id") or inv.get("id") or "",
        "pdf_path": inv.get("pdf_path") or "",
        "receivedDateTime": inv.get("receivedDateTime") or "",
        "subject": inv.get("subject") or "",
        "Packing slip": "no slip",
        "_leave_mail": leave,
    }


def write_sheet(rows: list[dict[str, Any]]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "September catch-up"
    headers = [
        "Vendor",
        "Invoice #",
        "Date received",
        "PO",
        "Total",
        "Result",
        "Bill id",
        "Batch",
        "Reason",
        "Owner",
        "Packing slip",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(
            [
                row.get("Vendor") or "",
                row.get("Invoice #") or "",
                received_ct(row.get("receivedDateTime")) or str(row.get("date") or ""),
                row.get("PO") or "",
                row.get("Amount") if row.get("Amount") not in (None, "") else "",
                sheet_result(row),
                row.get("KIMCO id") or "",
                row.get("Batch") or "",
                row.get("Why") or "",
                owner_for(row),
                row.get("Packing slip") or "no slip",
            ]
        )
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb.save(REPORT_PATH)


def slim_lookup(value: Any) -> Any:
    if isinstance(value, dict):
        return {"id": value.get("id"), "text": value.get("text")}
    return value


def write_readback(client: KimcoClient, rows: list[dict[str, Any]], batch: dict[str, Any]) -> None:
    bills = []
    for row in rows:
        kid = row.get("KIMCO id")
        if kid in (None, ""):
            continue
        if sheet_result(row) == "Archived":
            continue
        record = client.get_item("ap_invoices", int(kid))
        values = record.get("values") or {}
        lists = record.get("lists") or {}
        lines = []
        for line in lists.get("APInvoiceLine") or []:
            vals = line.get("values") or {}
            lines.append(
                {
                    "item": slim_lookup(vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")),
                    "qty": vals.get("Quantity"),
                    "unit_price": vals.get("Unit_Price"),
                    "extended": vals.get("Extended_Amount"),
                    "receipt_id": slim_lookup(vals.get("Receipt")),
                    "ppv": vals.get("Purchase_Price_Variance") or vals.get("PPV"),
                    "gl": slim_lookup(vals.get("Purchase_GL_Account")),
                }
            )
        charges = []
        for charge in lists.get("InvoiceAdditionalCharges") or []:
            vals = charge.get("values") or {}
            charges.append(
                {
                    "name": vals.get("Name") or slim_lookup(vals.get("Additional_Charges")),
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
                    "code": slim_lookup(vals.get("Tax_Code")),
                    "manual": vals.get("Manual_Calculation"),
                    "taxable": vals.get("Taxable_Amount"),
                    "rate": vals.get("Tax_Rate"),
                    "amount": vals.get("Tax_Amount"),
                }
            )
        bills.append(
            {
                "kimco_id": int(kid),
                "invoice_number": values.get("Invoice_Number") or row.get("Invoice #"),
                "vendor": slim_lookup(values.get("Vendor")),
                "header_invoice_amount": values.get("Invoice_Amount"),
                "header_verification_amount": values.get("Invoice_Verification_Amount"),
                "batch": slim_lookup(values.get("AP_Invoice_Batch")),
                "posted": values.get("Posted"),
                "lines": lines,
                "taxes": taxes,
                "additional_charges": charges,
            }
        )
    payload = {
        "batch_name": batch.get("name"),
        "batch_id": batch.get("id"),
        "sign_ins": SIGN_INS,
        "bills": bills,
    }
    READBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    READBACK_PATH.write_text(json.dumps(payload, indent=2, default=str))


def save_progress(rows: list[dict[str, Any]]) -> None:
    slim = []
    for row in rows:
        slim.append({k: v for k, v in row.items() if not str(k).startswith("_ppv")})
    PROGRESS_PATH.write_text(json.dumps({"sign_ins": SIGN_INS, "rows": slim}, indent=2, default=str))


def main() -> None:
    # AFT Industries is vendor 1383. Never the Automated Finishing alias.
    VENDOR_ID_ALIASES["aft industries"] = 1383
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    QC_PDF_DIR.mkdir(parents=True, exist_ok=True)
    client = login()
    install_401_guard(client)
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        raise SystemExit(graph_creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(graph_creds.tenant_id, graph_creds.client_id, graph_creds.client_secret)
    LOGGER.info("Loading KIMCO invoices, batches, purchase lines, and receipts")
    existing = client.list_items("ap_invoices")
    batches = client.list_items("ap_batches")
    purchase_lines = client.list_items("purchase_lines")
    receipts = [cli_mod.normalize_receipt(item) for item in client.list_items("receipts")]
    invoice_by_number = cli_mod._index_invoices(existing)
    vendor_samples = cli_mod._vendor_samples(existing)
    po_index = cli_mod._index_purchase_orders(purchase_lines)
    batch = cli_mod._find_or_create_batch(client, batches, BATCH_NAME)
    if int(batch["id"]) == 746 or batch["name"] != BATCH_NAME:
        raise SystemExit(f"Refusing batch {batch}. Kyle is processing batch 746.")
    LOGGER.info("Using batch %s (%s) created=%s", batch["name"], batch["id"], batch.get("created"))
    bills, skipped = pull_september(graph)
    bills = [bill for bill in bills if in_september(bill.get("receivedDateTime"))]
    skipped = [item for item in skipped if in_september(item.get("receivedDateTime"))]
    bills = aby_first(bills)
    LOGGER.info("September bills %s skipped %s", len(bills), len(skipped))
    rows: list[dict[str, Any]] = []
    seen_numbers: set[tuple[str, str]] = set()
    for inv in bills:
        if CLIENT is not None:
            client = CLIENT
        kind = special_kind(inv)
        label = f"{inv.get('vendor')} {inv.get('invoice_number')} {kind}"
        LOGGER.info("QUEUE %s received %s", label, inv.get("receivedDateTime"))
        if kind == "ntex" or is_ntex(inv):
            rows.append(blank_row(inv, result="Not entered", why="NTEX left alone. Kyle is handling it.", leave=True))
            save_progress(rows)
            continue
        if kind == "eastern":
            rows.append(
                blank_row(
                    inv,
                    result="Not entered",
                    why="Eastern Metal 821670 was not re-raised.",
                    leave=True,
                )
            )
            save_progress(rows)
            continue
        if kind == "autopay" or is_spectrum_or_autopay(inv):
            rows.append(
                blank_row(
                    inv,
                    result="Skipped",
                    why="Autopay or Spectrum. Not entered. Moved to AutoPay Archive.",
                )
            )
            rows[-1]["_autopay"] = True
            save_progress(rows)
            continue
        key = (str(inv.get("vendor") or "").lower(), str(inv.get("invoice_number") or ""))
        if key[1] and key in seen_numbers:
            rows.append(blank_row(inv, result="Archived", why="Duplicate of an invoice already handled in this run."))
            save_progress(rows)
            continue
        amount = money(inv.get("amount"))
        subject = str(inv.get("subject") or "")
        if (amount is not None and amount < 0) or re.search(r"credit\s*(memo|note)", subject, flags=re.I):
            rows.append(blank_row(inv, result="Skipped", why="Credit memo. Not entered."))
            save_progress(rows)
            continue
        if inv.get("pdf_path"):
            copy_pdf(inv)
        try:
            if kind == "aft":
                apply_aft(inv)
                if not inv.get("po"):
                    rows.append(
                        blank_row(
                            inv,
                            result="Hold",
                            why="AFT Industries Customer P.O. No. was not read. No header created and it was not linked to Automated Finishing Technology.",
                            leave=True,
                        )
                    )
                    save_progress(rows)
                    continue
                kind = "standard"
            if kind != "standard":
                row = process_special(
                    client,
                    inv,
                    batch_id=int(batch["id"]),
                    samples=vendor_samples,
                    invoice_by_number=invoice_by_number,
                    kind=kind,
                )
            else:
                row = cli_mod._process_invoice(
                    client,
                    inv,
                    batch=batch,
                    batch_label=f"{BATCH_NAME} ({batch['id']})",
                    invoice_by_number=invoice_by_number,
                    vendor_samples=vendor_samples,
                    po_index=po_index,
                    pdf_dir=PDF_DIR,
                    receipts=receipts,
                    graph_client=None,
                    mailbox=ALLOWED_MAILBOX,
                    flag_outlook=False,
                )
                row["graph_message_id"] = inv.get("graph_message_id")
                row["pdf_path"] = inv.get("pdf_path")
                row["receivedDateTime"] = inv.get("receivedDateTime")
                row["Packing slip"] = "no slip"
                if vendor_missing_why(str(row.get("Why") or "")) and not row.get("KIMCO id"):
                    row["_sheet_result"] = "Not entered"
                    row["_leave_mail"] = True
                    row["Why"] = (
                        f"{inv.get('vendor')} is not set up in KIMCO. No vendor was created. The mail was left in the inbox."
                    )
                elif already_entered_why(str(row.get("Why") or "")):
                    row["_sheet_result"] = "Archived"
                    row["Why"] = f"{row.get('Why')} Already in KIMCO. No duplicate header. The bill was not edited."
                else:
                    price_gap_followup(client, row, inv)
                    missing_receipt_note(client, row, inv)
                    if sheet_result(row) == "Success" and row.get("KIMCO id"):
                        vendor = str(inv.get("vendor") or "")
                        number = str(inv.get("invoice_number") or "")
                        amount = money(inv.get("amount"))
                        total = f"${amount:,.2f}" if amount is not None else "the PDF total"
                        text = (
                            f"AP Clerk: {vendor} invoice {number} is entered and is not posted. "
                            f"The PDF total is {total}."
                        )
                        try:
                            add_comment(client, int(row["KIMCO id"]), note_html(text, action="treyce", vendor=vendor))
                        except (KimcoError, ValueError) as exc:
                            row["Result"] = RESULT_HOLD
                            row["_sheet_result"] = "Hold"
                            row["Why"] = f"Bill saved but the AP Clerk note was not saved ({exc})."
            if row is None:
                row = blank_row(inv, result="Hold", why="No entry path.", leave=True)
        except SystemExit:
            save_progress(rows)
            write_sheet(rows)
            raise
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Invoice failed %s\n%s", label, traceback.format_exc())
            row = blank_row(inv, result="Hold", why=f"Stopped on this invoice: {exc}", leave=True)
            if "third sign-in" in str(exc).lower() or "second time" in str(exc).lower():
                rows.append(row)
                save_progress(rows)
                write_sheet(rows)
                raise SystemExit(str(exc)) from exc
        rows.append(row)
        if key[1]:
            seen_numbers.add(key)
        LOGGER.info("RESULT %s %s id=%s", sheet_result(row), label, row.get("KIMCO id"))
        save_progress(rows)
    for item in skipped:
        if str(item.get("class") or "") == "already-flagged":
            continue
        if is_ntex(item):
            rows.append(blank_row(item, result="Not entered", why="NTEX left alone. Kyle is handling it.", leave=True))
            continue
        if "821670" in str(item.get("subject") or ""):
            rows.append(blank_row(item, result="Not entered", why="Eastern Metal 821670 was not re-raised.", leave=True))
            continue
        klass = str(item.get("class") or item.get("hold_reason") or "not-a-bill")
        if klass in {"auto-pay", "auto pay"} or is_spectrum_or_autopay(item):
            row = blank_row(item, result="Skipped", why=f"{klass}. Autopay or Spectrum. Not entered.")
            row["_autopay"] = True
            rows.append(row)
            continue
        rows.append(
            blank_row(
                item,
                result="Skipped",
                why=f"{klass}. Credit memo, statement, past-due notice, or not an invoice. Not entered.",
            )
        )
    finalize_mail(graph, rows)
    if CLIENT is not None:
        client = CLIENT
    write_sheet(rows)
    write_readback(client, rows, batch)
    save_progress(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[sheet_result(row)] = counts.get(sheet_result(row), 0) + 1
    print(json.dumps({"batch": batch, "sign_ins": SIGN_INS, "counts": counts, "rows": len(rows)}, default=str))


def patch_category(graph: GraphClient, message_id: str, category: str) -> str:
    current = graph.get_message(ALLOWED_MAILBOX, message_id, select="id,categories,flag")
    drop = set(PROCESS_CATEGORIES) | {LEGACY_AI_SKIPPED_CATEGORY, "AP Matched"}
    keep = [c for c in (current.get("categories") or []) if c and c not in drop]
    if category not in keep:
        keep.append(category)
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        json={"categories": keep},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        return f"category-http-{response.status_code}"
    return category


def finalize_mail(graph: GraphClient, rows: list[dict[str, Any]]) -> None:
    folders = graph.list_inbox_child_folders(ALLOWED_MAILBOX)
    autopay = next((f for f in folders if str(f.get("displayName") or "") == "AutoPay Archive"), None)
    fort = graph.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        mid = str(row.get("graph_message_id") or "")
        if not mid:
            continue
        grouped.setdefault(mid, []).append(row)
    for mid, group in grouped.items():
        if any(row.get("_leave_mail") for row in group):
            for row in group:
                row["Folder move"] = "left in inbox"
            continue
        results = {sheet_result(row) for row in group}
        if any(row.get("_autopay") for row in group):
            status = patch_category(graph, mid, LEGACY_AI_SKIPPED_CATEGORY)
            dest = str((autopay or {}).get("id") or "")
            folder_name = "AutoPay Archive"
        elif results <= {"Success", "Archived"}:
            status = patch_category(graph, mid, ENTERED_IN_AI_CATEGORY)
            dest = str(fort.get("id") or "")
            folder_name = "9 - FORT WORTH ARCHIVE"
        elif "Hold" in results or "Fail" in results:
            status = patch_category(graph, mid, ENTERED_WITH_ISSUES_CATEGORY if any(row.get("KIMCO id") for row in group) else AI_HOLD_CATEGORY)
            dest = str(fort.get("id") or "")
            folder_name = "9 - FORT WORTH ARCHIVE"
        else:
            status = patch_category(graph, mid, AI_SKIPPED_CATEGORY)
            dest = str(fort.get("id") or "")
            folder_name = "9 - FORT WORTH ARCHIVE"
        moved = "not-moved"
        if dest:
            outcome = graph.move_message(ALLOWED_MAILBOX, mid, dest)
            moved = f"{folder_name} {outcome.get('status')}"
        for row in group:
            row["Flag status"] = status
            row["Folder move"] = moved


if __name__ == "__main__":
    main()
