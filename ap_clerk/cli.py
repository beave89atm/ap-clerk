"""Daily-runnable AP Clerk CLI. Default target is KIMCO prototype."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from ap_clerk.auth import format_presence, load_credentials, resolve_target
from ap_clerk.cursor import DEFAULT_CURSOR_PATH, load_cursor, save_cursor
from ap_clerk.daily import (
    DEFAULT_DAILY_LIMIT,
    cursor_from_run,
    email_body_for,
    email_subject_for,
    result_counts,
    write_email_sidecar,
)
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    EMAIL_DENIED,
    FLAG_NO_MESSAGE_ID,
    FLAG_SKIPPED,
    REPORT_TO,
    GraphClient,
    GraphError,
    MailboxRejected,
    apply_flag_after_match,
    assert_allowed_mailbox,
    attach_message_ids,
    format_graph_presence,
    load_graph_credentials,
)
from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.report import write_report
from ap_clerk.rules import (
    CURRENCY_USD_ID,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_BATCH_NAMES,
    FORBIDDEN_INVOICE_IDS,
    PRICE_DOES_NOT_MATCH,
    PRICE_MISMATCH_PO_COMMENT,
    batch_name_for,
    chicago_today,
    comments_for,
    due_date_from_terms,
    evaluate_bill_price_variance,
    extract_po_number,
    flag_in_outlook_for,
    format_fees,
    format_ppv,
    invoice_number_key,
    invoice_type_for,
    kimco_datetime,
    known_vendor_id,
    lookup_id,
    lookup_text,
    match_receipts,
    money,
    names_match,
    normalize_receipt,
    should_create_header,
    vendor_match_score,
    parse_iso_date,
)

LOGGER = logging.getLogger("ap_clerk")
ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kannon AP Clerk (KIMCO prototype by default)")
    parser.add_argument(
        "command",
        choices=["enter", "pull", "daily", "probe"],
        help=(
            "enter: fixture or inbox invoices as header-only AP bills. "
            "pull: list unflagged AP mailbox messages (no category write). "
            "daily: weekday 5am America/Chicago FIFO of 30 from 2026-07-28 (requires --live). "
            "probe: Graph category + Mail.Send draft check on the AP mailbox (does not send mail)."
        ),
    )
    parser.add_argument("--fixture", default=str(ROOT / "fixtures" / "testrun-727-803.json"))
    parser.add_argument("--report", default=None, help="Output xlsx path. Default: runs/AP-run-YYYY-MM-DD.xlsx")
    parser.add_argument("--as-of", default=None, help="Override Chicago calendar date YYYY-MM-DD")
    parser.add_argument("--pdf-dir", default=None, help="Optional directory of invoice PDFs to attach")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live.kimcoerp.com + live GUIDs. Requires KIMCO_LIVE_*. Kyle said go for live writes.",
    )
    parser.add_argument(
        "--from-inbox",
        action="store_true",
        help="Pull vendor-invoice PDFs from accountspayable@kannonmfg.com. Success→Entered in AI; HOLD/Fail/skips→AI HOLD.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Bills to attempt. enter --from-inbox default 20 (most recent). daily default 30 (FIFO from cursor).",
    )
    parser.add_argument(
        "--cursor",
        default=str(ROOT / DEFAULT_CURSOR_PATH),
        help="daily: persisted FIFO cursor JSON (last received datetime / message id).",
    )
    parser.add_argument(
        "--email-to",
        default=REPORT_TO,
        help=f"daily: send the Excel report to this address FROM {ALLOWED_MAILBOX}.",
    )
    parser.add_argument(
        "--mailbox",
        default=ALLOWED_MAILBOX,
        help=f"Must be {ALLOWED_MAILBOX}. Any other mailbox is rejected.",
    )
    parser.add_argument(
        "--match-inbox",
        action="store_true",
        help="Attach Graph message ids from the AP mailbox onto invoices, then flag after Success only.",
    )
    parser.add_argument("--inbox-from", default=None, help="Inbox window start YYYY-MM-DD (inclusive)")
    parser.add_argument("--inbox-to", default=None, help="Inbox window end YYYY-MM-DD (inclusive)")
    parser.add_argument("--out", default=None, help="pull: write unflagged message queue JSON here")
    parser.add_argument(
        "--match-fixture",
        action="store_true",
        help="pull: attach graph_message_id onto the fixture invoices (does not flag).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        assert_allowed_mailbox(args.mailbox)
    except MailboxRejected as exc:
        print(str(exc), flush=True)
        return 2

    if args.command == "pull":
        return _run_pull(args)
    if args.command == "probe":
        return _run_probe(args)
    if args.command == "daily":
        return _run_daily(args)

    target = resolve_target(live_flag=args.live)
    creds = load_credentials(target=target)
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(f"Target: {creds.target}", flush=True)
    if creds.key_source:
        print(f"Using credential pair: {creds.key_source}", flush=True)
    print(f"Instance host: {creds.instance_url}", flush=True)
    if creds.target == "live":
        print(
            "Live target. Kyle said go. Writes use live host + live GUIDs only. "
            "Success gets Entered in AI; HOLD/Fail get AI HOLD. No follow-up flag.",
            flush=True,
        )

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    batch_name = batch_name_for(as_of)
    report_path = Path(args.report) if args.report else ROOT / "runs" / f"AP-run-{as_of.isoformat()}.xlsx"
    fixture_path = Path(args.fixture)
    graph_client = None
    invoices: list[dict[str, Any]]
    inbox_skip_rows: list[dict[str, Any]] = []
    category_status = "not-attempted"
    if args.from_inbox:
        graph_client = _optional_graph_client()
        if graph_client is None:
            print("Graph credentials missing or authenticate failed. Cannot pull inbox.", flush=True)
            return 2
        try:
            category_status = graph_client.ensure_ai_hold_category(args.mailbox)
            print(f"AI HOLD master category: {category_status}", flush=True)
        except (GraphError, MailboxRejected):
            print("AI HOLD master category: category-denied", flush=True)
        from datetime import timedelta

        start = parse_iso_date(args.inbox_from) if args.inbox_from else as_of - timedelta(days=45)
        end = parse_iso_date(args.inbox_to) if args.inbox_to else as_of
        pdf_dir = Path(args.pdf_dir) if args.pdf_dir else ROOT / "runs" / "inbox-pdfs"
        invoices, skipped = pull_recent_bills(
            graph_client,
            mailbox=args.mailbox,
            limit=max(1, int(args.limit or 20)),
            received_from=start,
            received_to=end,
            pdf_dir=pdf_dir,
            unprocessed_only=True,
            mark_skips=True,
        )
        inbox_skip_rows = skip_rows_for_report(skipped, batch_name)
        print(
            f"Inbox selected {len(invoices)} bill(s) from {args.mailbox} "
            f"({start.isoformat()} to {end.isoformat()}); skipped {len(skipped)} non-bill(s). "
            "Success→Entered in AI; unable-to-process→AI HOLD.",
            flush=True,
        )
        if not invoices:
            print("No vendor invoices selected from inbox.", flush=True)
            write_report(report_path, inbox_skip_rows)
            return 2
    else:
        invoices = _load_fixture(fixture_path)
        needs_graph = bool(args.match_inbox) or any(
            inv.get("graph_message_id") or inv.get("graphMessageId") for inv in invoices
        )
        graph_client = _optional_graph_client() if needs_graph else None
        if graph_client is not None:
            try:
                category_status = graph_client.ensure_ai_hold_category(args.mailbox)
                print(f"AI HOLD master category: {category_status}", flush=True)
            except (GraphError, MailboxRejected):
                print("AI HOLD master category: category-denied", flush=True)
        if args.match_inbox:
            invoices = _attach_inbox_ids(
                invoices,
                mailbox=args.mailbox,
                start=args.inbox_from,
                end=args.inbox_to,
                graph_client=graph_client,
                fixture_path=fixture_path,
            )

    if not creds.ready:
        print(creds.error or "Credentials not ready. Writing HOLD report and stopping.", flush=True)
        rows = [_offline_row(inv, batch_name, creds.error or "credentials missing") for inv in invoices]
        rows.extend(inbox_skip_rows)
        write_report(report_path, rows)
        print(f"Wrote {report_path}", flush=True)
        return 2

    try:
        client = KimcoClient.authenticate(
            creds.instance_url,
            creds.key or "",
            creds.password or "",
            target=creds.target,
        )
        if creds.target == "live":
            print("Live auth success (token not printed). Proceeding with live writes.", flush=True)
        pdf_dir = Path(args.pdf_dir) if args.pdf_dir else None
        if args.from_inbox and pdf_dir is None:
            pdf_dir = ROOT / "runs" / "inbox-pdfs"
        elif args.from_inbox:
            pdf_dir = Path(args.pdf_dir) if args.pdf_dir else ROOT / "runs" / "inbox-pdfs"
        rows = run_enter(
            client,
            invoices,
            batch_name=batch_name,
            pdf_dir=pdf_dir,
            graph_client=graph_client,
            mailbox=args.mailbox,
            flag_outlook=True,
        )
    except KimcoError as exc:
        label = "Live" if creds.target == "live" else "Prototype"
        print(f"{label} call failed: {exc}", flush=True)
        rows = [_offline_row(inv, batch_name, f"Fail: {exc}") for inv in invoices]
        rows.extend(inbox_skip_rows)
        write_report(report_path, rows)
        return 1

    rows.extend(inbox_skip_rows)
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)
    return 0


def run_enter(
    client: KimcoClient,
    invoices: list[dict[str, Any]],
    *,
    batch_name: str,
    pdf_dir: Path | None = None,
    graph_client: GraphClient | None = None,
    mailbox: str = ALLOWED_MAILBOX,
    flag_outlook: bool = True,
) -> list[dict[str, Any]]:
    LOGGER.info("Loading %s lists (invoices, batches, purchase lines)", client.target)
    existing_invoices = client.list_items("ap_invoices")
    batches = client.list_items("ap_batches")
    purchase_lines = client.list_items("purchase_lines")

    invoice_by_number = _index_invoices(existing_invoices)
    vendor_samples = _vendor_samples(existing_invoices)
    _seed_vendor_samples(client, vendor_samples, invoice_by_number, invoices)
    po_index = _index_purchase_orders(purchase_lines)
    receipts: list[dict[str, Any]] | None
    try:
        receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
        LOGGER.info("Loaded %s receipts for Select Receipts matching", len(receipts))
    except KimcoError as exc:
        LOGGER.info("Receipts list unavailable; will not HOLD-no-receipts without a search: %s", type(exc).__name__)
        receipts = None
    batch = _find_or_create_batch(client, batches, batch_name)
    batch_label = f"{batch_name} ({batch['id']})"
    LOGGER.info("Using batch %s", batch_label)

    rows = []
    for inv in invoices:
        rows.append(
            _process_invoice(
                client,
                inv,
                batch=batch,
                batch_label=batch_label,
                invoice_by_number=invoice_by_number,
                vendor_samples=vendor_samples,
                po_index=po_index,
                receipts=receipts,
                pdf_dir=pdf_dir,
                graph_client=graph_client,
                mailbox=mailbox,
                flag_outlook=flag_outlook,
            )
        )
    return rows


def _load_fixture(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text())
    invoices = payload.get("invoices") or []
    if not invoices:
        raise SystemExit(f"Fixture {path} has no invoices")
    return invoices


def _offline_row(inv: dict[str, Any], batch_name: str, why: str) -> dict[str, Any]:
    result = "HOLD"
    if why.lower().startswith("fail"):
        result = "Fail"
    return {
        "Vendor": inv.get("vendor"),
        "Invoice #": inv.get("invoice_number"),
        "date": inv.get("date"),
        "PO": inv.get("po") or "",
        "Amount": inv.get("amount"),
        "Result": result,
        "Why": why,
        "KIMCO id": "",
        "Batch": batch_name,
        "Fees and surcharges": format_fees(inv.get("fees")),
        "PPV": "none",
        "Attach status": "no-pdf-on-vm",
        "Flag status": FLAG_SKIPPED,
        "Flag in Outlook": flag_in_outlook_for(result),
    }


def _index_invoices(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        values = item.get("values") or {}
        key = invoice_number_key(values.get("Invoice_Number"))
        if key:
            index.setdefault(key, []).append(item)
    return index


def _find_existing_invoice(
    invoice_by_number: dict[str, list[dict[str, Any]]],
    number: str,
    vendor: str,
) -> dict[str, Any] | None:
    """Match same vendor + invoice #. A unique invoice # on live is already-exists
    even when the mailbox sender name does not match (NoreplyMV / Leeco).
    Do not treat a different vendor as a dup when two vendors share a number.
    """
    items = invoice_by_number.get(invoice_number_key(number)) or []
    if not items:
        return None
    if not vendor or len(items) == 1:
        return items[0]
    for item in items:
        values = item.get("values") or {}
        text = lookup_text(values.get("Vendor") or values.get("Vendor_$_Display_Name"))
        if names_match(vendor, text) or vendor_match_score(vendor, text):
            return item
    return None


def _remember_invoice(
    invoice_by_number: dict[str, list[dict[str, Any]]],
    number: str,
    item: dict[str, Any],
) -> None:
    invoice_by_number.setdefault(invoice_number_key(number), []).append(item)


def _vendor_samples(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples = []
    seen: set[int] = set()
    for item in items:
        values = item.get("values") or {}
        vendor = values.get("Vendor") or values.get("Purchase_Order_$_Vendor")
        vendor_id = lookup_id(vendor)
        vendor_text = lookup_text(vendor) or lookup_text(values.get("Vendor_$_Display_Name"))
        if vendor_id is None:
            continue
        if vendor_id in seen:
            continue
        seen.add(vendor_id)
        samples.append(
            {
                "vendor_id": vendor_id,
                "vendor_text": vendor_text,
                "invoice_id": item.get("id"),
                "po_text": lookup_text(values.get("Purchase_Order") or values.get("Purchase_Order_$_Display_Name")),
            }
        )
    return samples


def _seed_vendor_samples(
    client: KimcoClient,
    samples: list[dict[str, Any]],
    invoice_by_number: dict[str, dict[str, Any]],
    invoices: list[dict[str, Any]],
) -> None:
    """GET a bounded set of existing invoices so no-PO vendor/remit/terms can be matched by name."""
    needed = []
    for inv in invoices:
        if not should_create_header(inv)[0]:
            continue
        if _best_vendor_sample(inv.get("vendor") or "", "", samples):
            continue
        alias_id = known_vendor_id(inv.get("vendor") or "")
        if alias_id and _sample_by_vendor_id(samples, alias_id):
            continue
        needed.append(inv)
    if not needed:
        return

    prefixes: set[str] = set()
    for inv in needed:
        key = invoice_number_key(str(inv.get("invoice_number") or ""))
        for n in (4, 5, 6, 7):
            if len(key) >= n:
                prefixes.add(key[:n])

    ranked: list[int] = []
    seen = {int(s["invoice_id"]) for s in samples if s.get("invoice_id") is not None}
    for key, items in invoice_by_number.items():
        for item in items:
            item_id = item.get("id")
            if item_id is None or int(item_id) in seen:
                continue
            if any(key.startswith(prefix) for prefix in prefixes if len(prefix) >= 4):
                ranked.append(int(item_id))
    extras = sorted(
        (
            int(item["id"])
            for items in invoice_by_number.values()
            for item in items
            if item.get("id") is not None
        ),
        reverse=True,
    )
    # Mix recent ids with a stride through older invoices so utility/staffing
    # vendors (Luxor, Priority 1, etc.) are not missed.
    stride = extras[:: max(1, len(extras) // 200)]
    for item_id in extras[:200] + stride:
        if item_id not in seen and item_id not in ranked:
            ranked.append(item_id)
        if len(ranked) >= 600:
            break

    LOGGER.info("Seeding vendor samples from %s existing invoices for %s unmatched vendors", min(len(ranked), 350), len(needed))
    probed = 0
    for item_id in ranked:
        if probed >= 350:
            break
        try:
            item = client.get_item("ap_invoices", item_id)
        except KimcoError:
            continue
        probed += 1
        values = item.get("values") or {}
        vendor_field = values.get("Vendor")
        vendor_id = lookup_id(vendor_field)
        if vendor_id is None:
            continue
        samples.append(
            {
                "vendor_id": vendor_id,
                "vendor_text": lookup_text(vendor_field),
                "invoice_id": item.get("id"),
                "po_text": lookup_text(values.get("Purchase_Order")),
            }
        )
        seen.add(item_id)


def _index_purchase_orders(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in lines:
        values = item.get("values") or {}
        po = (
            values.get("Purchase_Order_Number")
            or values.get("Purchase_Order")
            or values.get("Name")
            or values.get("Display_Name")
        )
        po_id = lookup_id(po)
        text = lookup_text(po) or lookup_text(values.get("Display_Name")) or lookup_text(values.get("Name"))
        if not text and po is not None and not isinstance(po, dict):
            text = str(po)
        number = extract_po_number(text)
        if not number:
            continue
        if not po_id:
            continue
        vendor = values.get("Vendor") or values.get("Purchase_Order_$_Vendor") or values.get("PO_Vendor")
        vendor_id = lookup_id(vendor)
        vendor_text = lookup_text(vendor) or lookup_text(values.get("Vendor_$_Display_Name"))
        if not vendor_text and "-" in (text or ""):
            vendor_text = text.split("-", 1)[1].strip()
        line_no = values.get("Purchase_Line_Number") or values.get("Line_Number")
        if isinstance(line_no, dict):
            line_no = line_no.get("text") or line_no.get("id")
        part = lookup_text(values.get("Item_Number") or values.get("Item") or values.get("PO_Item_Number")) or values.get("Item_Number")
        qty = money(values.get("Quantity") or values.get("Qty"))
        unit_price = money(values.get("Unit_Price") or values.get("Price") or values.get("Unit_Cost"))
        amount = money(values.get("Amount") or values.get("Line_Amount") or values.get("Extended_Price"))
        if amount is None and unit_price is not None and qty is not None:
            amount = round(unit_price * qty, 2)
        wo = lookup_text(values.get("Work_Order") or values.get("WO"))
        line_row = {
            "id": item.get("id"),
            "line_no": line_no,
            "po_line": line_no,
            "line": line_no,
            "part": str(part or "").strip(),
            "qty": qty,
            "quantity": qty,
            "unit_price": unit_price,
            "amount": amount,
            "wo": wo,
        }
        current = index.get(number)
        if current is None:
            index[number] = {
                "id": po_id,
                "text": text,
                "line_ids": [item.get("id")],
                "vendor_id": vendor_id,
                "vendor_text": vendor_text,
                "lines": [line_row],
            }
        elif current["id"] == po_id:
            current["line_ids"].append(item.get("id"))
            current["lines"].append(line_row)
            if current.get("vendor_id") is None and vendor_id is not None:
                current["vendor_id"] = vendor_id
            if not current.get("vendor_text") and vendor_text:
                current["vendor_text"] = vendor_text
    return index


def _find_or_create_batch(client: KimcoClient, batches: list[dict[str, Any]], batch_name: str) -> dict[str, Any]:
    if batch_name in FORBIDDEN_BATCH_NAMES:
        raise KimcoError(f"Refusing forbidden batch name {batch_name}")
    for item in batches:
        values = item.get("values") or {}
        name = values.get("AP_Invoice_Batch_ID")
        batch_id = item.get("id")
        if batch_id in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_BATCH_NAMES:
            continue
        if name == batch_name:
            if batch_id in FORBIDDEN_BATCH_IDS:
                raise KimcoError("Matched a forbidden batch id")
            LOGGER.info("Found existing batch id=%s name=%s", batch_id, name)
            return {"id": batch_id, "name": name, "created": False}
    LOGGER.info("Creating batch %s", batch_name)
    values: dict[str, Any] = {
        "AP_Invoice_Batch_ID": batch_name,
        "Description": comments_for(client.target),
        "Status": 0,
    }
    owner_id = _discover_batch_owner(client, batches)
    if owner_id is not None:
        values["Batch_Owner"] = {"id": owner_id}
    elif client.target != "live":
        values["Batch_Owner"] = {"id": 173}
    created_id, body, status, error = client.create("ap_batches", values)
    if created_id is None and "Batch_Owner" not in values:
        # Retry with a looked-up owner only if create said owner is required.
        owner_id = _discover_batch_owner(client, batches, any_owner=True)
        if owner_id is not None:
            values["Batch_Owner"] = {"id": owner_id}
            created_id, body, status, error = client.create("ap_batches", values)
    if created_id is None:
        raise KimcoError(f"Batch create failed HTTP {status}: {error}")
    if created_id in FORBIDDEN_BATCH_IDS:
        raise KimcoError("Create returned forbidden batch id; aborting")
    LOGGER.info("Created batch id=%s", created_id)
    return {"id": created_id, "name": batch_name, "created": True, "raw": body}


def _discover_batch_owner(
    client: KimcoClient,
    batches: list[dict[str, Any]],
    *,
    any_owner: bool = False,
) -> int | None:
    """Reuse an API Agent batch owner. Do not invent a person or reuse Mark Brown."""
    for item in batches:
        values = item.get("values") or {}
        name = str(values.get("AP_Invoice_Batch_ID") or "")
        batch_id = item.get("id")
        if batch_id in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_BATCH_NAMES:
            continue
        owner = lookup_id(values.get("Batch_Owner"))
        if owner is None:
            continue
        if name.startswith("API Agent"):
            return owner
    if not any_owner:
        return None
    for item in batches:
        values = item.get("values") or {}
        name = str(values.get("AP_Invoice_Batch_ID") or "")
        if item.get("id") in FORBIDDEN_BATCH_IDS or name in FORBIDDEN_BATCH_NAMES:
            continue
        owner = lookup_id(values.get("Batch_Owner"))
        if owner is not None:
            return owner
    return None


def _finish_row(
    row: dict[str, Any],
    inv: dict[str, Any],
    graph_client: GraphClient | None,
    mailbox: str,
    *,
    flag_outlook: bool,
) -> dict[str, Any]:
    row["Flag in Outlook"] = flag_in_outlook_for(str(row.get("Result") or ""))
    if flag_outlook:
        apply_flag_after_match(row, inv, graph_client, mailbox=mailbox)
    else:
        row["Flag status"] = FLAG_SKIPPED
    return row


def _process_invoice(
    client: KimcoClient,
    inv: dict[str, Any],
    *,
    batch: dict[str, Any],
    batch_label: str,
    invoice_by_number: dict[str, list[dict[str, Any]]],
    vendor_samples: list[dict[str, Any]],
    po_index: dict[str, dict[str, Any]],
    pdf_dir: Path | None,
    receipts: list[dict[str, Any]] | None = None,
    graph_client: GraphClient | None = None,
    mailbox: str = ALLOWED_MAILBOX,
    flag_outlook: bool = True,
) -> dict[str, Any]:
    vendor = inv.get("vendor") or ""
    number = str(inv.get("invoice_number") or "")
    po = inv.get("po")
    po_display = "" if po is None else str(po)
    amount = inv.get("amount")
    fees = format_fees(inv.get("fees"))
    attach = "no-pdf-on-vm"
    row = {
        "Vendor": vendor,
        "Invoice #": number,
        "date": inv.get("date"),
        "PO": po_display,
        "Amount": amount,
        "Result": "HOLD",
        "Why": "",
        "KIMCO id": "",
        "Batch": batch_label,
        "Fees and surcharges": fees,
        "PPV": "none",
        "Attach status": attach,
        "Flag status": FLAG_NO_MESSAGE_ID,
        "Flag in Outlook": "No",
    }

    create_ok, hold_reason = should_create_header(inv)
    if not create_ok:
        row["Why"] = f"HOLD: {hold_reason}. Do not create a header."
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    existing = _find_existing_invoice(invoice_by_number, number, vendor)
    if existing:
        existing_id = existing.get("id")
        row["KIMCO id"] = existing_id
        row["Result"] = "Fail"
        if existing_id in FORBIDDEN_INVOICE_IDS:
            row["Why"] = f"Fail/already exists (do not recreate id {existing_id})"
        else:
            row["Why"] = f"Fail/already exists (id {existing_id})"
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    pos = [str(p) for p in (inv.get("pos") or ([po] if po else [])) if p]
    multi_po = bool(inv.get("multi_po")) or len(pos) > 1
    po_info = None
    po_missing_note = ""
    if multi_po:
        po_missing_note = "Multi-PO bill: header Purchase Order left blank; Select Receipts per PO. "
    elif po:
        po_info = po_index.get(str(po))
        if not po_info:
            po_missing_note = (
                f"PO {po} is not in {client.target}; Purchase Order left blank (will not invent a PO). "
            )
        else:
            po_vendor_text = str(po_info.get("vendor_text") or po_info.get("text") or "")
            po_vendor_id = po_info.get("vendor_id")
            invoice_vendor_id = known_vendor_id(vendor)
            known_conflict = (
                invoice_vendor_id is not None
                and po_vendor_id is not None
                and int(invoice_vendor_id) != int(po_vendor_id)
                and not names_match(vendor, po_vendor_text)
                and not vendor_match_score(vendor, po_vendor_text)
            )
            if known_conflict:
                po_missing_note = (
                    f"PO {po} is {po_vendor_text}, not {vendor}; Purchase Order left blank. "
                )
                po_info = None

    vendor_info = _resolve_vendor(
        client,
        vendor,
        po_info,
        vendor_samples,
        invoice_by_number=invoice_by_number,
        invoice_number=number,
    )
    if not vendor_info:
        hint = f" (PO {po} exists as {po_info['text']})" if po_info else " (looked up by vendor name)"
        row["Result"] = "Fail"
        row["Why"] = (
            f"Fail: vendor missing on {client.target} for {vendor}{hint}. "
            "Will not invent a vendor/remit-to ID."
        )
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    if not vendor_info.get("invoice_id"):
        row["Result"] = "Fail"
        row["Why"] = (
            f"Fail: vendor {vendor_info.get('vendor_id')} is known "
            f"({vendor_info.get('vendor_text') or vendor}) but no sample invoice "
            "for remit/terms. Not a vendor-missing fail."
        )
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    try:
        sample = client.get_item("ap_invoices", int(vendor_info["invoice_id"]))
    except KimcoError as exc:
        row["Result"] = "Fail"
        row["Why"] = f"Fail: could not load vendor sample invoice: {exc}"
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    sample_values = sample.get("values") or {}
    remit = sample_values.get("Remit_To_Address")
    terms = sample_values.get("Terms_Code")
    if lookup_id(remit) is None or lookup_id(terms) is None:
        row["Result"] = "Fail"
        row["Why"] = "Fail: sample invoice missing remit or terms; will not invent them."
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    if not inv.get("date"):
        row["Result"] = "Fail"
        row["Why"] = "Fail: invoice date missing from PDF/email; will not invent a date."
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)
    if amount in (None, ""):
        row["Result"] = "Fail"
        row["Why"] = "Fail: invoice amount missing from PDF; will not invent an amount."
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    invoice_lines = list(inv.get("lines") or [])
    po_lines = list((po_info or {}).get("lines") or [])
    if not invoice_lines and po_lines and len(po_lines) == 1 and amount not in (None, ""):
        invoice_lines = [{"amount": amount, "qty": po_lines[0].get("qty"), "part": po_lines[0].get("part"), "po_line": po_lines[0].get("po_line")}]
    price = evaluate_bill_price_variance(invoice_lines, po_lines, invoice_total=float(amount) if amount not in (None, "") else None)
    if price["hold"]:
        row["Result"] = "HOLD"
        row["PPV"] = "none"
        row["Why"] = price["why"] or f"HOLD: {PRICE_DOES_NOT_MATCH}."
        if PRICE_MISMATCH_PO_COMMENT not in row["Why"]:
            row["Why"] = f"{row['Why']} {PRICE_MISMATCH_PO_COMMENT}"
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)
    if price["ppv_total"]:
        row["PPV"] = format_ppv(price["ppv_total"])

    receipt_note = ""
    if receipts is not None and po_info:
        receipt_result = match_receipts(
            invoice_number=number,
            invoice_lines=invoice_lines,
            receipts=receipts,
            po_number=str(po) if po else None,
        )
        if receipt_result["hold_no_receipts"]:
            row["Result"] = "HOLD"
            row["Why"] = (
                "HOLD: no receipts after slip # / part / qty / PO line search "
                f"(invoice {number}). Will not guess a qty-only slip."
            )
            return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)
        receipt_note = receipt_result["why"] + " "

    invoice_day = parse_iso_date(str(inv["date"]))
    due = due_date_from_terms(invoice_day, lookup_text(terms))
    invoice_type = invoice_type_for(po if po_info else None)
    currency = sample_values.get("Currency")
    currency_id = lookup_id(currency) or CURRENCY_USD_ID
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": batch["id"]},
        "Vendor": {"id": vendor_info["vendor_id"]},
        "Invoice_Number": number,
        "Invoice_Type": invoice_type,
        "Invoice_Date": kimco_datetime(invoice_day),
        "Invoice_Verification_Amount": float(amount),
        "Invoice_Due_Date": kimco_datetime(due),
        "Terms_Code": {"id": lookup_id(terms)},
        "Currency": {"id": currency_id},
        "Remit_To_Address": {"id": lookup_id(remit)},
        "Transaction_Date": kimco_datetime(invoice_day),
        "Comments": comments_for(client.target),
    }
    if po_info and not multi_po:
        payload["Purchase_Order"] = {"id": po_info["id"]}
    created_id, _body, status, error = client.create("ap_invoices", payload)
    if created_id is None:
        row["Result"] = "Fail"
        row["Why"] = f"Fail: header create HTTP {status}: {error}"
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)
    if created_id in FORBIDDEN_INVOICE_IDS:
        row["Result"] = "Fail"
        row["KIMCO id"] = created_id
        row["Why"] = "Fail: create returned a forbidden existing invoice id; will not edit it."
        return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)

    _remember_invoice(
        invoice_by_number,
        number,
        {"id": created_id, "values": {"Invoice_Number": number, "Vendor": {"text": vendor}}},
    )
    pdf_status = _maybe_attach(client, created_id, number, pdf_dir, explicit_pdf=inv.get("pdf_path"))
    edit_hint = ""
    probe = getattr(client, "try_put_probe_rejected", None)
    if probe:
        try:
            edit_hint = probe("ap_invoices", created_id)
        except KimcoError:
            edit_hint = "options-failed"
    row["Result"] = "Success"
    row["KIMCO id"] = created_id
    row["Attach status"] = pdf_status
    if po_info and not multi_po:
        if "editable" in edit_hint:
            line_note = (
                "Header PO set. Select Receipts via API not implemented without an Editable receipt action; "
                "do not type Add Item. "
            )
        else:
            line_note = (
                f"Lines blocked ({edit_hint}): API cannot Select Receipts until Editable is on; "
                "do not type Add Item. Live UI needed if PUT is 405. "
            )
    else:
        line_note = (
            "Purchase Order left blank (no-PO or multi-PO or PO not on target). "
            "Do not type Add Item. "
        )
    ppv_note = ""
    if price["ppv_total"]:
        ppv_note = (
            f"Post Additional Charge Purchase Price Variance {format_ppv(price['ppv_total'])} "
            "(signed; negative allowed). "
        )
        for item in price.get("items") or []:
            if item.get("reason") and item.get("action") == "ppv":
                ppv_note += item["reason"] + " "
    row["Why"] = (
        f"Header created (Invoice_Type {invoice_type}). {po_missing_note}{line_note}{receipt_note}"
        f"{ppv_note}"
        "Fees go to Additional Charge Fees and surcharges / F-Fees & Surcharges (not PPV). "
        f"Attach status={pdf_status}."
    )
    LOGGER.info("Created invoice %s id=%s vendor=%s po=%s type=%s", number, created_id, vendor, po, invoice_type)
    return _finish_row(row, inv, graph_client, mailbox, flag_outlook=flag_outlook)


def _sample_by_vendor_id(samples: list[dict[str, Any]], vendor_id: int | None) -> dict[str, Any] | None:
    if vendor_id is None:
        return None
    for sample in samples:
        if sample.get("vendor_id") == vendor_id:
            return sample
    return None


def _resolve_vendor(
    client: KimcoClient,
    fixture_vendor: str,
    po_info: dict[str, Any] | None,
    samples: list[dict[str, Any]],
    *,
    invoice_by_number: dict[str, dict[str, Any]],
    invoice_number: str,
) -> dict[str, Any] | None:
    """Name match, then known alias, then the vendor on the live PO.

    Do not Fail vendor-missing when the PO has a vendor (Treyce 2026-08-28).
    Aliases: National Specialty Alloys=1386, Coherent Corp.=1410.
    """
    po_text = (po_info or {}).get("text") or ""
    po_vendor_id = (po_info or {}).get("vendor_id")
    po_vendor_text = (po_info or {}).get("vendor_text") or ""
    alias_id = known_vendor_id(fixture_vendor) or known_vendor_id(po_vendor_text)

    match = _best_vendor_sample(fixture_vendor, po_text, samples)
    if match:
        return match
    if alias_id:
        aliased = _sample_by_vendor_id(samples, alias_id)
        if aliased:
            return aliased
    if po_vendor_id:
        from_po = _sample_by_vendor_id(samples, int(po_vendor_id))
        if from_po:
            return from_po
    if po_vendor_text:
        from_po_name = _best_vendor_sample(po_vendor_text, po_text, samples)
        if from_po_name:
            return from_po_name

    discovered = _discover_vendor_from_invoices(
        client,
        fixture_vendor,
        samples,
        invoice_by_number=invoice_by_number,
        invoice_number=invoice_number,
        prefer_vendor_id=alias_id or po_vendor_id,
    )
    if discovered:
        return discovered

    # Vendor is known from alias or the live PO; remit/terms sample may still be missing.
    known_id = alias_id or po_vendor_id
    if known_id:
        return {
            "vendor_id": int(known_id),
            "vendor_text": fixture_vendor or po_vendor_text,
            "invoice_id": None,
            "po_text": po_text,
            "from_po": True,
        }
    return _best_vendor_sample(fixture_vendor, po_text, samples)


def _best_vendor_sample(fixture_vendor: str, po_text: str, samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    po_vendor = po_text.split("-", 1)[1] if "-" in po_text else po_text
    scored: list[tuple[int, dict[str, Any]]] = []
    for sample in samples:
        score = max(
            vendor_match_score(fixture_vendor, sample.get("vendor_text")),
            vendor_match_score(po_vendor, sample.get("vendor_text")),
            vendor_match_score(fixture_vendor, sample.get("po_text")),
            vendor_match_score(po_vendor, sample.get("po_text")),
        )
        if score:
            scored.append((score, sample))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


def _discover_vendor_from_invoices(
    client: KimcoClient,
    fixture_vendor: str,
    samples: list[dict[str, Any]],
    *,
    invoice_by_number: dict[str, dict[str, Any]],
    invoice_number: str,
    prefer_vendor_id: int | None = None,
) -> dict[str, Any] | None:
    """GET existing invoices and match Vendor text or a known vendor id."""
    needle = invoice_number_key(invoice_number)
    prefixes = {needle[:n] for n in (4, 5, 6, 7) if len(needle) >= n}
    ranked_ids: list[int] = []
    seen_ids: set[int] = {s.get("invoice_id") for s in samples if s.get("invoice_id") is not None}
    for key, items in invoice_by_number.items():
        for item in items:
            item_id = item.get("id")
            if item_id is None or item_id in seen_ids:
                continue
            if any(key.startswith(prefix) for prefix in prefixes if len(prefix) >= 4):
                ranked_ids.append(int(item_id))
    extras = sorted(
        (
            int(item.get("id"))
            for items in invoice_by_number.values()
            for item in items
            if item.get("id") is not None
        ),
        reverse=True,
    )
    for item_id in extras:
        if item_id not in seen_ids and item_id not in ranked_ids:
            ranked_ids.append(item_id)
        if len(ranked_ids) >= 400:
            break

    LOGGER.info("Vendor name lookup for %s: probing %s existing invoices", fixture_vendor, min(len(ranked_ids), 250))
    probed = 0
    for item_id in ranked_ids:
        if probed >= 250:
            break
        if item_id in seen_ids:
            continue
        try:
            item = client.get_item("ap_invoices", item_id)
        except KimcoError:
            continue
        probed += 1
        values = item.get("values") or {}
        vendor_field = values.get("Vendor")
        vendor_id = lookup_id(vendor_field)
        vendor_text = lookup_text(vendor_field)
        if vendor_id is None:
            continue
        sample = {
            "vendor_id": vendor_id,
            "vendor_text": vendor_text,
            "invoice_id": item.get("id"),
            "po_text": lookup_text(values.get("Purchase_Order")),
        }
        samples.append(sample)
        seen_ids.add(item_id)
        if prefer_vendor_id is not None and vendor_id == int(prefer_vendor_id):
            LOGGER.info("Matched vendor id %s from existing invoice id=%s", vendor_id, item.get("id"))
            return sample
        if _best_vendor_sample(fixture_vendor, "", [sample]):
            LOGGER.info("Matched vendor %s from existing invoice id=%s", vendor_text, item.get("id"))
            return sample
    return None


def _maybe_attach(
    client: KimcoClient,
    invoice_id: int,
    invoice_number: str,
    pdf_dir: Path | None,
    explicit_pdf: str | None = None,
) -> str:
    pdf: Path | None = None
    if explicit_pdf:
        candidate = Path(explicit_pdf)
        if candidate.exists() and candidate.suffix.lower() == ".pdf":
            pdf = candidate
    if pdf is None:
        if pdf_dir is None or not pdf_dir.exists() or not invoice_number:
            return "no-pdf-on-vm"
        matches = list(pdf_dir.glob(f"*{invoice_number}*.pdf"))
        named = [p for p in matches if invoice_number.lower() in p.name.lower()]
        if not named:
            return "no-pdf-on-vm"
        pdf = named[0]
    try:
        content = pdf.read_bytes()
        return client.try_official_attach(
            invoice_id,
            name=pdf.name,
            content_type="application/pdf",
            size=len(content),
            content=content,
        )
    except Exception as exc:  # noqa: BLE001 - attach must not fail the run
        LOGGER.info("Attach attempt failed without raising run: %s", type(exc).__name__)
        return "blocked-405"


def _optional_graph_client() -> GraphClient | None:
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        LOGGER.info("Graph client not ready; inbox match/flag will record no-message-id or graph-denied")
        return None
    try:
        return GraphClient.authenticate(
            graph_creds.tenant_id or "",
            graph_creds.client_id or "",
            graph_creds.client_secret or "",
        )
    except GraphError as exc:
        LOGGER.info("Graph authenticate failed: %s", type(exc).__name__)
        return None


def _fixture_window(invoices_or_path: Path | list[dict[str, Any]]) -> tuple[Any, Any]:
    if isinstance(invoices_or_path, Path):
        payload = json.loads(invoices_or_path.read_text())
        window = payload.get("window") or {}
        start = window.get("from")
        end = window.get("to")
        return start, end
    return None, None


def _attach_inbox_ids(
    invoices: list[dict[str, Any]],
    *,
    mailbox: str,
    start: str | None,
    end: str | None,
    graph_client: GraphClient | None,
    fixture_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Keep Graph message ids on invoices. Does not flag."""
    mailbox = assert_allowed_mailbox(mailbox)
    if graph_client is None:
        LOGGER.info("Skipping inbox match: Graph client unavailable")
        return invoices
    if not start or not end:
        fixture_start, fixture_end = _fixture_window(fixture_path) if fixture_path else (None, None)
        start = start or fixture_start
        end = end or fixture_end
    if not start or not end:
        LOGGER.info("Skipping inbox match: --inbox-from/--inbox-to (or fixture window) required")
        return invoices
    messages = graph_client.list_messages(
        mailbox,
        received_from=parse_iso_date(str(start)),
        received_to=parse_iso_date(str(end)),
        unflagged_only=False,
        include_attachment_names=True,
    )
    enriched = attach_message_ids(invoices, messages)
    matched = sum(1 for inv in enriched if inv.get("graph_message_id"))
    LOGGER.info("Attached Graph message ids to %s/%s invoices (flag happens after match, not now)", matched, len(enriched))
    return enriched


def _run_probe(args: argparse.Namespace) -> int:
    """Graph-only: AI HOLD category + Mail.Send draft check. Never sendMail. Never KIMCO."""
    print(format_graph_presence(), flush=True)
    mailbox = assert_allowed_mailbox(args.mailbox)
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        print(graph_creds.error or "Graph credentials missing.", flush=True)
        return 2
    try:
        client = GraphClient.authenticate(
            graph_creds.tenant_id or "",
            graph_creds.client_id or "",
            graph_creds.client_secret or "",
        )
        category_status = client.ensure_ai_hold_category(mailbox)
        probe = client.probe_send_authorization(mailbox)
    except MailboxRejected as exc:
        print(str(exc), flush=True)
        return 2
    except GraphError as exc:
        print(f"Graph probe failed: {exc}", flush=True)
        return 1

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    out_path = Path(args.out) if args.out else ROOT / "runs" / f"graph-send-probe-{as_of.isoformat()}.json"
    payload = {
        "mailbox": mailbox,
        "other_mailboxes_used": [],
        "kimco_writes": False,
        "send_mail_invoked": False,
        "mail_sent_to_anyone": False,
        "ai_hold_category": category_status,
        "probe": probe,
        "notes": (
            "Draft created on the AP mailbox and deleted. sendMail was not called. "
            "Daily still emails Treyce only after a real --live enter."
        ),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"AI HOLD master category: {category_status}", flush=True)
    print(
        f"Mail.Send role={'present' if probe.get('mail_send_role') else 'absent'} "
        f"draft={probe.get('draft_status')} draft_http={probe.get('draft_http')} "
        f"deleted_http={probe.get('draft_deleted_http')} send_mail_invoked=false",
        flush=True,
    )
    print(f"Wrote {out_path}. No mail was sent. No KIMCO writes.", flush=True)
    return 0


def _run_daily(args: argparse.Namespace) -> int:
    """Weekday 5am America/Chicago FIFO of 30. Requires --live. Does not start a CI cron."""
    if not args.live:
        print(
            "daily requires --live (live KIMCO + live creds). "
            "Refusing to run without the live flag so CI/prototype cannot post.",
            flush=True,
        )
        return 2

    target = resolve_target(live_flag=True)
    creds = load_credentials(target=target)
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(f"Target: {creds.target}", flush=True)
    if creds.key_source:
        print(f"Using credential pair: {creds.key_source}", flush=True)
    print(f"Instance host: {creds.instance_url}", flush=True)
    print(
        "Daily FIFO from 2026-07-28 America/Chicago toward today. "
        "Skip Entered in AI. Replace not-a-bill. Cursor persists. "
        "Success→Entered in AI + flag.flagStatus=flagged; HOLD/Fail→AI HOLD (not flagged).",
        flush=True,
    )

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    batch_name = batch_name_for(as_of)
    report_path = Path(args.report) if args.report else ROOT / "runs" / f"AP-run-{as_of.isoformat()}.xlsx"
    limit = max(1, int(args.limit or DEFAULT_DAILY_LIMIT))
    cursor_path = Path(args.cursor)
    cursor = load_cursor(cursor_path)
    print(
        f"Cursor: last_received={cursor.last_receivedDateTime or 'none (start 2026-07-28)'} "
        f"last_message_id={'set' if cursor.last_message_id else 'none'}",
        flush=True,
    )

    graph_client = _optional_graph_client()
    if graph_client is None:
        print("Graph credentials missing or authenticate failed. Cannot run daily inbox FIFO.", flush=True)
        write_report(report_path, [])
        write_email_sidecar(report_path, EMAIL_DENIED, subject=email_subject_for(as_of), to=args.email_to)
        return 2

    category_status = "not-attempted"
    try:
        category_status = graph_client.ensure_ai_hold_category(args.mailbox)
    except (GraphError, MailboxRejected):
        category_status = "category-denied"
    print(f"AI HOLD master category: {category_status}", flush=True)

    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else ROOT / "runs" / "inbox-pdfs"
    try:
        invoices, skipped = pull_recent_bills(
            graph_client,
            mailbox=args.mailbox,
            limit=limit,
            received_from=None,
            received_to=as_of,
            pdf_dir=pdf_dir,
            max_messages=max(400, limit * 20),
            fifo=True,
            unprocessed_only=True,
            cursor=cursor,
            mark_skips=True,
        )
    except MailboxRejected as exc:
        print(str(exc), flush=True)
        return 2
    except GraphError as exc:
        print(f"Graph daily pull failed: {exc}", flush=True)
        write_report(report_path, [])
        write_email_sidecar(report_path, EMAIL_DENIED, subject=email_subject_for(as_of), to=args.email_to)
        return 1

    print(
        f"Daily selected {len(invoices)} bill(s) from {args.mailbox} "
        f"(FIFO from 2026-07-28 / cursor; skipped {len(skipped)} non-bill(s)).",
        flush=True,
    )

    skip_rows = skip_rows_for_report(skipped, batch_name)
    rows: list[dict[str, Any]] = []
    batch_label = batch_name

    if not creds.ready:
        print(creds.error or "Live credentials not ready. Writing HOLD report and stopping.", flush=True)
        rows = [_offline_row(inv, batch_name, creds.error or "credentials missing") for inv in invoices]
        rows.extend(skip_rows)
        write_report(report_path, rows)
        save_cursor(
            cursor_from_run(invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor),
            cursor_path,
        )
        print(f"Wrote {report_path}", flush=True)
        _email_daily_report(graph_client, report_path, rows, batch_label=batch_label, as_of=as_of, to=args.email_to)
        return 2

    if not invoices and not skip_rows:
        write_report(report_path, [])
        save_cursor(
            cursor_from_run(invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor),
            cursor_path,
        )
        print(f"No unprocessed vendor invoices in the FIFO window. Wrote {report_path}", flush=True)
        _email_daily_report(graph_client, report_path, [], batch_label=batch_label, as_of=as_of, to=args.email_to)
        return 0

    try:
        client = KimcoClient.authenticate(
            creds.instance_url,
            creds.key or "",
            creds.password or "",
            target=creds.target,
        )
        print("Live auth success (token not printed). Proceeding with live writes.", flush=True)
        if invoices:
            rows = run_enter(
                client,
                invoices,
                batch_name=batch_name,
                pdf_dir=pdf_dir,
                graph_client=graph_client,
                mailbox=args.mailbox,
                flag_outlook=True,
            )
            if rows:
                batch_label = str(rows[0].get("Batch") or batch_name)
        else:
            rows = []
    except KimcoError as exc:
        print(f"Live call failed: {exc}", flush=True)
        rows = [_offline_row(inv, batch_name, f"Fail: {exc}") for inv in invoices]
        rows.extend(skip_rows)
        write_report(report_path, rows)
        save_cursor(
            cursor_from_run(invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor),
            cursor_path,
        )
        print(f"Wrote {report_path}", flush=True)
        _email_daily_report(graph_client, report_path, rows, batch_label=batch_label, as_of=as_of, to=args.email_to)
        return 1

    rows.extend(skip_rows)
    write_report(report_path, rows)
    save_cursor(
            cursor_from_run(invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor),
            cursor_path,
        )
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)
    _email_daily_report(graph_client, report_path, rows, batch_label=batch_label, as_of=as_of, to=args.email_to)
    return 0


def _email_daily_report(
    graph_client: GraphClient | None,
    report_path: Path,
    rows: list[dict[str, Any]],
    *,
    batch_label: str,
    as_of,
    to: str,
) -> str:
    subject = email_subject_for(as_of)
    body = email_body_for(rows, batch_label=batch_label, as_of=as_of)
    if graph_client is None:
        status = EMAIL_DENIED
    else:
        try:
            status = graph_client.send_run_report(
                ALLOWED_MAILBOX,
                to=to,
                subject=subject,
                body=body,
                attachment_path=report_path,
            )
        except MailboxRejected:
            raise
        except GraphError:
            status = EMAIL_DENIED
    write_email_sidecar(report_path, status, subject=subject, to=to)
    counts = result_counts(rows)
    print(
        f"Email status={status} to={to} subject={subject} "
        f"Success={counts['Success']} Fail={counts['Fail']} HOLD={counts['HOLD']}",
        flush=True,
    )
    return status


def _run_pull(args: argparse.Namespace) -> int:
    print(format_graph_presence(), flush=True)
    mailbox = assert_allowed_mailbox(args.mailbox)
    graph_creds = load_graph_credentials()
    if not graph_creds.ready:
        print(graph_creds.error or "Graph credentials missing.", flush=True)
        return 2
    start = args.inbox_from
    end = args.inbox_to
    if (not start or not end) and args.fixture:
        start = start or _fixture_window(Path(args.fixture))[0]
        end = end or _fixture_window(Path(args.fixture))[1]
    if not start or not end:
        print("pull requires --inbox-from and --inbox-to (or a fixture with window.from/to).", flush=True)
        return 2
    try:
        client = GraphClient.authenticate(
            graph_creds.tenant_id or "",
            graph_creds.client_id or "",
            graph_creds.client_secret or "",
        )
        messages = client.list_messages(
            mailbox,
            received_from=parse_iso_date(str(start)),
            received_to=parse_iso_date(str(end)),
            unflagged_only=True,
            include_attachment_names=bool(args.match_fixture),
        )
    except MailboxRejected as exc:
        print(str(exc), flush=True)
        return 2
    except GraphError as exc:
        print(f"Graph pull failed: {exc}", flush=True)
        return 1

    queue = []
    for message in messages:
        flag = ((message.get("flag") or {}).get("flagStatus") or "notFlagged")
        queue.append(
            {
                "graph_message_id": message.get("id"),
                "subject": message.get("subject"),
                "receivedDateTime": message.get("receivedDateTime"),
                "hasAttachments": message.get("hasAttachments"),
                "flagStatus": flag,
                "attachment_names": message.get("attachment_names") or [],
            }
        )
    payload: dict[str, Any] = {
        "mailbox": mailbox,
        "window": {"from": start, "to": end},
        "notes": "Mail without category Entered in AI is the work queue. pull does not write categories. After enter Success the CLI adds Entered in AI.",
        "messages": queue,
    }
    if args.match_fixture:
        invoices = attach_message_ids(_load_fixture(Path(args.fixture)), messages)
        payload["invoices"] = [
            {
                "vendor": inv.get("vendor"),
                "invoice_number": inv.get("invoice_number"),
                "date": inv.get("date"),
                "graph_message_id": inv.get("graph_message_id") or "",
            }
            for inv in invoices
        ]
    out_path = Path(args.out) if args.out else ROOT / "runs" / f"inbox-unflagged-{start}-to-{end}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"Pulled {len(queue)} unflagged message(s) from {mailbox} ({start} to {end}). "
        f"Wrote {out_path}. No messages were flagged.",
        flush=True,
    )
    return 0


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print("\nRun summary (no secrets):", flush=True)
    for row in rows:
        print(
            f"  {row['Vendor']} | {row['Invoice #']} | {row['Result']} | "
            f"id={row['KIMCO id'] or '-'} | flag={row.get('Flag status') or '-'} | {row['Why']}",
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
