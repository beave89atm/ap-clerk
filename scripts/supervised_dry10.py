"""Supervised 10-invoice LIVE dry run (QUALITY V1.1).

Uses the weekday FIFO cursor. Does not launch the daily 30.
Does not email until --email (after UI finish). Never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import format_presence, load_credentials, resolve_target
from ap_clerk.cli import (
    ROOT as CLI_ROOT,
    _optional_graph_client,
    _print_summary,
    run_enter,
)
from ap_clerk.cursor import DEFAULT_CURSOR_PATH, load_cursor, save_cursor
from ap_clerk.daily import cursor_from_run, result_counts, write_email_sidecar
from ap_clerk.gates import RESULT_INCOMPLETE, RESULT_SUCCESS, finish_gate
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    EMAIL_DENIED,
    EMAIL_SENT,
    FLAG_AI_HOLD,
    FLAG_FLAGGED,
    GraphError,
    MailboxRejected,
    REPORT_TO,
    apply_flag_after_match,
    assert_allowed_mailbox,
)
from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.report import write_report
from ap_clerk.rules import batch_name_for, chicago_today, parse_iso_date

LOGGER = logging.getLogger("ap_clerk.dry10")
DRY_LIMIT = 10
DRY_SUBJECT = "AP dry run 10 — quality V1.1"


def dry_report_path(as_of: date) -> Path:
    daily = CLI_ROOT / "runs" / f"AP-run-{as_of.isoformat()}.xlsx"
    if daily.exists():
        return CLI_ROOT / "runs" / f"AP-run-{as_of.isoformat()}-dry10.xlsx"
    return daily


def dry_email_body(rows: list[dict[str, Any]], *, batch_label: str) -> str:
    counts = result_counts(rows)
    return (
        "Supervised 10-invoice LIVE dry run for Treyce review (QUALITY V1.1).\n"
        "This is not the weekday daily 30 and does not re-arm that routine.\n"
        f"Batch: {batch_label}\n"
        f"Success: {counts['Success']}\n"
        f"Incomplete: {counts['Incomplete']}\n"
        f"HOLD: {counts['HOLD']}\n"
        f"Fail: {counts['Fail']}\n"
        f"Mailbox: {ALLOWED_MAILBOX}\n"
        "Success = finished bill (header + Select Receipts when PO + PDF attached).\n"
        "Header-only remains Incomplete + AI HOLD, not Entered in AI.\n"
        "Report attached.\n"
    )


def _safe_invoice(inv: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "vendor",
        "invoice_number",
        "date",
        "po",
        "pos",
        "amount",
        "fees",
        "lines",
        "multi_po",
        "pdf_path",
        "graph_message_id",
        "subject",
        "receivedDateTime",
        "field_sources",
        "is_purchase_order_doc",
    )
    return {k: inv.get(k) for k in keep}


def _safe_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("Notes", None)
    return out


def write_sidecar(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path


def apply_finish_updates(
    rows: list[dict[str, Any]],
    invoices: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    graph_client,
    mailbox: str,
) -> list[dict[str, Any]]:
    """Re-apply finish_gate + Outlook after live UI Select Receipts / attach."""
    by_id = {str(u.get("kimco_id")): u for u in updates if u.get("kimco_id") not in (None, "")}
    inv_by_number = {
        str(inv.get("invoice_number") or ""): inv for inv in invoices if inv.get("invoice_number")
    }
    for row in rows:
        kid = str(row.get("KIMCO id") or "")
        update = by_id.get(kid)
        if not update:
            continue
        if update.get("attach_status"):
            row["Attach status"] = update["attach_status"]
        receipts_selected = bool(update.get("receipts_selected"))
        po = row.get("PO") or None
        if str(po).strip() in {"", "None", "null"}:
            po = None
        result, why = finish_gate(
            header_created=True,
            attach_status=row.get("Attach status"),
            po=po,
            multi_po=bool(update.get("multi_po")),
            receipts_selected=receipts_selected or not po,
            kimco_id=row.get("KIMCO id"),
        )
        if update.get("force_hold"):
            result = "HOLD"
            why = str(update.get("why") or why)
        row["Result"] = result
        extra = str(update.get("why") or "").strip()
        if result == RESULT_SUCCESS:
            row["Why"] = (
                f"Finished bill via live UI path (QUALITY V1.1). {extra} "
                f"Attach status={row.get('Attach status')}."
            ).strip()
        elif result == RESULT_INCOMPLETE:
            row["Why"] = f"{why} {extra}".strip()
        elif extra:
            row["Why"] = extra
        inv = inv_by_number.get(str(row.get("Invoice #") or ""))
        if inv is None:
            inv = {"graph_message_id": update.get("graph_message_id") or row.get("graph_message_id")}
        apply_flag_after_match(row, inv, graph_client, mailbox=mailbox)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervised 10-invoice LIVE dry run (V1.1)")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=DRY_LIMIT)
    parser.add_argument("--report", default=None)
    parser.add_argument("--cursor", default=str(CLI_ROOT / DEFAULT_CURSOR_PATH))
    parser.add_argument("--mailbox", default=ALLOWED_MAILBOX)
    parser.add_argument("--email-to", default=REPORT_TO)
    parser.add_argument("--email", action="store_true", help="Send the Treyce report (after UI finish)")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--sidecar", default=None)
    parser.add_argument(
        "--apply-finish",
        default=None,
        help="JSON list of {kimco_id, attach_status, receipts_selected, why} from the UI pass",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    mailbox = assert_allowed_mailbox(args.mailbox)
    if int(args.limit) > DRY_LIMIT:
        print(f"Refusing limit {args.limit}. Supervised dry run is {DRY_LIMIT} bills, not the daily 30.", flush=True)
        return 2

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    batch_name = batch_name_for(as_of)
    report_path = Path(args.report) if args.report else dry_report_path(as_of)
    sidecar_path = Path(args.sidecar) if args.sidecar else report_path.with_suffix(".json")
    cursor_path = Path(args.cursor)
    cursor = load_cursor(cursor_path)

    if args.apply_finish:
        payload = json.loads(Path(args.apply_finish).read_text() if args.apply_finish != "-" else sidecar_path.read_text())
        if isinstance(payload, dict) and "rows" in payload:
            rows = payload["rows"]
            invoices = payload.get("invoices") or []
            updates = payload.get("ui_updates") or []
        else:
            raise SystemExit("--apply-finish needs the sidecar JSON with rows + ui_updates")
        graph_client = _optional_graph_client()
        rows = apply_finish_updates(rows, invoices, updates, graph_client, mailbox)
        write_report(report_path, rows)
        payload["rows"] = [_safe_row(r) for r in rows]
        payload["counts"] = result_counts(rows)
        write_sidecar(sidecar_path, payload)
        print(f"Wrote {report_path} after UI finish.", flush=True)
        _print_summary(rows)
        if args.email:
            return _send_email(graph_client, report_path, rows, batch_label=str(rows[0].get("Batch") or batch_name) if rows else batch_name, to=args.email_to)
        return 0

    target = resolve_target(live_flag=True)
    creds = load_credentials(target=target)
    print(format_presence(creds.presence), flush=True)
    print(f"Target: {creds.target}", flush=True)
    print(f"Instance host: {creds.instance_url}", flush=True)
    print(
        f"Supervised dry run {args.limit} (QUALITY V1.1). "
        f"Cursor last_received={cursor.last_receivedDateTime or 'none'} "
        f"last_message_id={'set' if cursor.last_message_id else 'none'}. "
        "Skip Entered in AI. Do not restart at 7/28. No daily 30.",
        flush=True,
    )

    graph_client = _optional_graph_client()
    if graph_client is None:
        print("Graph credentials missing or authenticate failed.", flush=True)
        return 2
    try:
        category_status = graph_client.ensure_ai_hold_category(mailbox)
    except (GraphError, MailboxRejected):
        category_status = "category-denied"
    print(f"AI HOLD master category: {category_status}", flush=True)

    pdf_dir = CLI_ROOT / "runs" / "inbox-pdfs"
    invoices, skipped = pull_recent_bills(
        graph_client,
        mailbox=mailbox,
        limit=max(1, int(args.limit)),
        received_from=None,
        received_to=as_of,
        pdf_dir=pdf_dir,
        max_messages=max(400, int(args.limit) * 20),
        fifo=True,
        unprocessed_only=True,
        cursor=cursor,
        mark_skips=True,
    )
    print(
        f"Selected {len(invoices)} bill(s); skipped {len(skipped)} non-bill(s).",
        flush=True,
    )
    skip_rows = skip_rows_for_report(skipped, batch_name)
    rows: list[dict[str, Any]] = []
    batch_label = batch_name

    if not creds.ready:
        print(creds.error or "Live credentials not ready.", flush=True)
        return 2
    if not invoices and not skip_rows:
        print("No unprocessed vendor invoices in the FIFO window.", flush=True)
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
                mailbox=mailbox,
                flag_outlook=True,
            )
            if rows:
                batch_label = str(rows[0].get("Batch") or batch_name)
    except KimcoError as exc:
        print(f"Live call failed: {exc}", flush=True)
        return 1

    rows.extend(skip_rows)
    write_report(report_path, rows)
    advanced = cursor_from_run(invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor)
    save_cursor(advanced, cursor_path)
    notes = (
        f"FIFO from 2026-07-28 America/Chicago toward today. "
        f"2026-09-08 supervised dry10 processed {len(invoices)} bills + {len(skipped)} skips "
        f"after Legacy PS-INV103976 / {cursor.last_receivedDateTime}. "
        f"Batch {batch_label}. Next weekday continues AFTER this cursor. Do not restart at 7/28."
    )
    payload = json.loads(cursor_path.read_text())
    payload["notes"] = notes
    cursor_path.write_text(json.dumps(payload, indent=2) + "\n")

    sidecar = {
        "kind": "supervised-dry10",
        "quality": "V1.1",
        "mailbox": mailbox,
        "as_of": as_of.isoformat(),
        "batch": batch_label,
        "limit": int(args.limit),
        "daily_30_ran": False,
        "counts": result_counts(rows),
        "invoices": [_safe_invoice(inv) for inv in invoices],
        "skipped": [
            {
                "subject": item.get("subject"),
                "class": item.get("class"),
                "hold_reason": item.get("hold_reason"),
                "receivedDateTime": item.get("receivedDateTime"),
                "graph_message_id": item.get("graph_message_id"),
            }
            for item in skipped
        ],
        "rows": [_safe_row(r) for r in rows],
        "ui_updates": [],
        "cursor": {
            "last_receivedDateTime": advanced.last_receivedDateTime,
            "last_message_id_set": bool(advanced.last_message_id),
            "processed_count": advanced.processed_count,
        },
    }
    write_sidecar(sidecar_path, sidecar)
    print(f"Wrote {report_path}", flush=True)
    print(f"Wrote {sidecar_path}", flush=True)
    print(f"Advanced cursor to {advanced.last_receivedDateTime} (message id not printed).", flush=True)
    _print_summary(rows)
    if args.email:
        return _send_email(graph_client, report_path, rows, batch_label=batch_label, to=args.email_to)
    print("Email not sent yet (UI finish first). Use --email after attach/Select Receipts.", flush=True)
    return 0


def _send_email(graph_client, report_path: Path, rows: list[dict[str, Any]], *, batch_label: str, to: str) -> int:
    body = dry_email_body(rows, batch_label=batch_label)
    if graph_client is None:
        status = EMAIL_DENIED
    else:
        try:
            status = graph_client.send_run_report(
                ALLOWED_MAILBOX,
                to=to,
                subject=DRY_SUBJECT,
                body=body,
                attachment_path=report_path,
            )
        except MailboxRejected:
            raise
        except GraphError:
            status = EMAIL_DENIED
    write_email_sidecar(report_path, status, subject=DRY_SUBJECT, to=to)
    counts = result_counts(rows)
    print(
        f"Email status={status} to={to} subject={DRY_SUBJECT} "
        f"Success={counts['Success']} Incomplete={counts['Incomplete']} "
        f"Fail={counts['Fail']} HOLD={counts['HOLD']}",
        flush=True,
    )
    return 0 if status == EMAIL_SENT else 1


if __name__ == "__main__":
    raise SystemExit(main())
