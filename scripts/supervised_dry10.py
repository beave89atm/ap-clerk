"""Supervised 10-email LIVE dry run (QUALITY V1.1, API finish).

Hard email cap 10 until further notice (Kyle 2026-09-11). Cap is mailbox
messages touched, not bill attempts. Bill-attempt mode is suspended.
Already-flagged mail is walked past and does not consume the cap.

`--from-date` starts FIFO at that America/Chicago day (Kyle: 2026-08-16)
and skips already-finished paused headers. Does not open the KIMCO UI.
Does not launch the daily 30. Mail.Send is off unless `--email` /
`--email-only` after the spreadsheet is locked. Never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import format_presence, load_credentials, resolve_target
from ap_clerk.cli import ROOT as CLI_ROOT, _optional_graph_client, _print_summary, run_enter
from ap_clerk.cursor import DEFAULT_CURSOR_PATH, DailyCursor, load_cursor, save_cursor
from ap_clerk.daily import cursor_from_run, result_counts, write_email_sidecar
from ap_clerk.finish import (
    KIND_FINISH_UP,
    KIND_NEW,
    KIND_PRIOR,
    apply_grouped_outlook_flags,
    dry_email_body,
    dry_subject_for,
    finish_existing_header,
)
from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    EMAIL_DENIED,
    EMAIL_SENT,
    REPORT_TO,
    GraphError,
    MailboxRejected,
    assert_allowed_mailbox,
)
from ap_clerk.inbox import HARD_EMAIL_CAP, clamp_email_limit, pull_recent_bills, skip_rows_for_report
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.report import write_report
from ap_clerk.rules import batch_name_for, chicago_today, normalize_receipt, parse_iso_date

LOGGER = logging.getLogger("ap_clerk.dry10")
DRY_LIMIT = HARD_EMAIL_CAP


def dry_report_path(as_of) -> Path:
    daily = CLI_ROOT / "runs" / f"AP-run-{as_of.isoformat()}.xlsx"
    if daily.exists():
        return CLI_ROOT / "runs" / f"AP-run-{as_of.isoformat()}-dry10.xlsx"
    return daily


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


def _inv_for_row(row: dict[str, Any], invoices: list[dict[str, Any]]) -> dict[str, Any]:
    number = str(row.get("Invoice #") or "")
    for inv in invoices:
        if str(inv.get("invoice_number") or "") == number:
            return inv
    return {
        "invoice_number": number,
        "vendor": row.get("Vendor"),
        "po": row.get("PO") or None,
        "pos": [row["PO"]] if row.get("PO") else [],
        "amount": row.get("Amount"),
        "date": row.get("date"),
        "graph_message_id": row.get("graph_message_id"),
        "pdf_path": row.get("pdf_path"),
        "lines": [],
        "multi_po": False,
        "fees": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervised 10-invoice LIVE dry run (V1.1 API finish)")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=DRY_LIMIT)
    parser.add_argument("--report", default=None)
    parser.add_argument("--cursor", default=str(CLI_ROOT / DEFAULT_CURSOR_PATH))
    parser.add_argument("--mailbox", default=ALLOWED_MAILBOX)
    parser.add_argument("--email-to", default=REPORT_TO)
    parser.add_argument("--email", action="store_true", help="Send the Treyce report after API finish")
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--sidecar", default=None)
    parser.add_argument(
        "--from-date",
        default=None,
        help="FIFO start YYYY-MM-DD America/Chicago (Kyle: 2026-08-16). Skips paused finish-ups.",
    )
    parser.add_argument(
        "--paused-sidecar",
        default=str(CLI_ROOT / "runs" / "AP-run-2026-09-08-dry10-paused.json"),
        help="Paused dry-run sidecar with Incomplete headers to finish first.",
    )
    parser.add_argument(
        "--skip-paused",
        action="store_true",
        help="Do not API-finish paused Incomplete headers; FIFO new bills only.",
    )
    parser.add_argument(
        "--finish-paused-only",
        action="store_true",
        help="Only API-finish paused Incomplete headers; do not FIFO new bills.",
    )
    parser.add_argument(
        "--email-only",
        action="store_true",
        help="Send the locked spreadsheet once. Refuses if already emailed. No KIMCO writes.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    mailbox = assert_allowed_mailbox(args.mailbox)
    if int(args.limit) > DRY_LIMIT:
        print(
            f"Refusing limit {args.limit}. Supervised dry run is {DRY_LIMIT} emails max "
            "(Kyle 2026-09-11 hard email cap). Bill-attempt mode is suspended.",
            flush=True,
        )
        return 2

    as_of = parse_iso_date(args.as_of) if args.as_of else chicago_today()
    from_date = parse_iso_date(args.from_date) if args.from_date else None
    skip_paused = bool(args.skip_paused or from_date is not None)
    batch_name = batch_name_for(as_of)
    report_path = Path(args.report) if args.report else dry_report_path(as_of)
    sidecar_path = Path(args.sidecar) if args.sidecar else report_path.with_suffix(".json")
    cursor_path = Path(args.cursor)
    cursor = load_cursor(cursor_path)
    paused_path = Path(args.paused_sidecar)
    email_subject = dry_subject_for(from_date)

    if args.email_only:
        return _send_email_only(
            report_path=report_path,
            sidecar_path=sidecar_path,
            to=args.email_to,
            subject=email_subject,
            from_date=from_date,
        )

    target = resolve_target(live_flag=True)
    creds = load_credentials(target=target)
    print(format_presence(creds.presence), flush=True)
    print(f"Target: {creds.target}", flush=True)
    print(f"Instance host: {creds.instance_url}", flush=True)
    print(
        f"Supervised dry run {args.limit} emails (QUALITY V1.1 API finish). "
        f"Hard email cap {HARD_EMAIL_CAP} until further notice (Kyle 2026-09-11). "
        f"FIFO from={from_date.isoformat() if from_date else 'cursor'} "
        f"Cursor last_received={cursor.last_receivedDateTime or 'none'} "
        f"last_message_id={'set' if cursor.last_message_id else 'none'}. "
        f"{'Skip paused finish-ups. ' if skip_paused else 'Finish paused Incomplete first. '}"
        "Skip already-flagged. Do not restart at 7/28. No daily 30. No KIMCO UI. "
        "Email is off unless --email after the spreadsheet is locked.",
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

    if not creds.ready:
        print(creds.error or "Live credentials not ready.", flush=True)
        return 2

    paused: dict[str, Any] = {}
    paused_invoices: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    prior_rows: list[dict[str, Any]] = []
    if skip_paused:
        print("Skipping paused finish-ups. New FIFO bills only.", flush=True)
    elif not paused_path.exists():
        print(f"Paused sidecar missing: {paused_path}. FIFO new bills only.", flush=True)
    else:
        paused = json.loads(paused_path.read_text())
        paused_invoices = list(paused.get("invoices") or [])
        paused_rows = list(paused.get("rows") or [])
        incomplete = [row for row in paused_rows if str(row.get("Result") or "") == "Incomplete" and row.get("KIMCO id")]
        prior_rows = [row for row in paused_rows if str(row.get("Result") or "") != "Incomplete"]
        for row in prior_rows:
            row.setdefault("kind", KIND_PRIOR)
            row["Notes"] = ""
        print(
            f"Paused dry-run Incomplete headers to finish: {len(incomplete)}. "
            f"Prior Fail/HOLD rows kept: {len(prior_rows)}.",
            flush=True,
        )

    try:
        client = KimcoClient.authenticate(
            creds.instance_url,
            creds.key or "",
            creds.password or "",
            target=creds.target,
        )
        print("Live auth success (token not printed). API finish only. No KIMCO UI.", flush=True)
    except KimcoError as exc:
        print(f"Live call failed: {exc}", flush=True)
        return 1

    receipts: list[dict[str, Any]] | None
    try:
        receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
        LOGGER.info("Loaded %s receipts for Select Receipts matching", len(receipts))
    except KimcoError as exc:
        LOGGER.info("Receipts list unavailable: %s", type(exc).__name__)
        receipts = None

    pdf_dir = CLI_ROOT / "runs" / "inbox-pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    finished: list[dict[str, Any]] = []
    for row in incomplete:
        inv = _inv_for_row(row, paused_invoices)
        finished.append(
            finish_existing_header(
                client,
                row,
                inv,
                receipts=receipts,
                pdf_dir=pdf_dir,
                graph_client=graph_client,
                mailbox=mailbox,
                flag_outlook=False,
            )
        )

    rows: list[dict[str, Any]] = list(finished)
    new_invoices: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    batch_label = str((finished[0].get("Batch") if finished else None) or paused.get("batch") or batch_name)

    # Bill-attempt fill is suspended (Kyle 2026-09-11). FIFO is a hard email cap.
    email_limit = 0 if args.finish_paused_only else clamp_email_limit(args.limit)
    need = email_limit

    if need:
        print(
            f"FIFO up to {need} mailbox message(s) (hard email cap; noise consumes the slot).",
            flush=True,
        )
        new_invoices, skipped = pull_recent_bills(
            graph_client,
            mailbox=mailbox,
            limit=need,
            received_from=from_date,
            received_to=as_of,
            pdf_dir=pdf_dir,
            max_messages=max(80, HARD_EMAIL_CAP * 8),
            fifo=True,
            unprocessed_only=True,
            cursor=cursor,
            mark_skips=True,
        )
        skip_rows = skip_rows_for_report(skipped, batch_name)
        if new_invoices:
            created = run_enter(
                client,
                new_invoices,
                batch_name=batch_name,
                pdf_dir=pdf_dir,
                graph_client=graph_client,
                mailbox=mailbox,
                flag_outlook=False,
            )
            for row in created:
                row["kind"] = KIND_NEW
                row["Notes"] = ""
            rows.extend(created)
            if created:
                batch_label = str(created[0].get("Batch") or batch_label)
        for row in skip_rows:
            row.setdefault("kind", KIND_PRIOR)
        rows.extend(skip_rows)
    else:
        print(
            "No new FIFO fill (finish-paused-only, or email cap already applied).",
            flush=True,
        )

    rows.extend(prior_rows)
    apply_grouped_outlook_flags(rows, paused_invoices + new_invoices, graph_client, mailbox=mailbox)

    write_report(report_path, rows)
    paused_cursor = paused.get("cursor") or {}
    if paused_cursor.get("last_receivedDateTime"):
        advanced = DailyCursor(
            last_receivedDateTime=paused_cursor.get("last_receivedDateTime") or cursor.last_receivedDateTime,
            last_message_id=paused_cursor.get("last_message_id") or cursor.last_message_id,
            mailbox=ALLOWED_MAILBOX,
            last_run_date=as_of.isoformat(),
            last_batch=batch_label,
            processed_count=int(paused_cursor.get("processed_count") or cursor.processed_count or 0),
        )
        if new_invoices or skipped:
            advanced = cursor_from_run(
                new_invoices,
                skipped,
                as_of=as_of,
                batch=batch_label,
                previous=advanced,
            )
    else:
        advanced = cursor_from_run(new_invoices, skipped, as_of=as_of, batch=batch_label, previous=cursor)
    start_note = from_date.isoformat() if from_date else "2026-07-28"
    notes = (
        f"FIFO from {start_note} America/Chicago toward today. "
        f"{as_of.isoformat()} supervised dry10 API-finished {len(finished)} paused Incomplete header(s) "
        f"on {batch_label}; {len(new_invoices)} new FIFO bill(s). "
        "Next weekday continues AFTER this cursor. Do not restart at 7/28."
    )
    persist_dry10_cursor(advanced, cursor_path, notes=notes)

    sidecar = {
        "kind": "supervised-dry10-api-finish",
        "quality": "V1.1",
        "mailbox": mailbox,
        "as_of": as_of.isoformat(),
        "from_date": from_date.isoformat() if from_date else None,
        "batch": batch_label,
        "limit": int(args.limit),
        "daily_30_ran": False,
        "kimco_ui_opened": False,
        "email_sent": False,
        "finish_path": "api-record-put-and-attach",
        "counts": result_counts(rows),
        "finish_ups": [
            {
                "vendor": row.get("Vendor"),
                "invoice_number": row.get("Invoice #"),
                "kimco_id": row.get("KIMCO id"),
                "result": row.get("Result"),
                "attach_status": row.get("Attach status"),
                "kind": row.get("kind"),
            }
            for row in rows
            if row.get("kind") == KIND_FINISH_UP
        ],
        "new_fifo": [
            {
                "vendor": row.get("Vendor"),
                "invoice_number": row.get("Invoice #"),
                "kimco_id": row.get("KIMCO id"),
                "result": row.get("Result"),
                "kind": row.get("kind"),
            }
            for row in rows
            if row.get("kind") == KIND_NEW
        ],
        "invoices": [_safe_invoice(inv) for inv in paused_invoices + new_invoices],
        "rows": [_safe_row(r) for r in rows],
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
        return _send_email(
            graph_client,
            report_path,
            rows,
            batch_label=batch_label,
            to=args.email_to,
            subject=email_subject,
            from_date=from_date,
            sidecar_path=sidecar_path,
        )
    print("Email not sent (pass --email or --email-only after the spreadsheet is locked).", flush=True)
    return 0


def persist_dry10_cursor(cursor: DailyCursor, cursor_path: Path, notes: str) -> None:
    save_cursor(cursor, cursor_path)
    payload = json.loads(cursor_path.read_text())
    payload["notes"] = notes
    cursor_path.write_text(json.dumps(payload, indent=2) + "\n")


def _mark_sidecar_email(sidecar_path: Path, status: str, subject: str) -> None:
    if not sidecar_path.exists():
        return
    try:
        payload = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["email_sent"] = status == EMAIL_SENT
    payload["email_status"] = status
    payload["email_subject"] = subject
    sidecar_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _send_email_only(
    *,
    report_path: Path,
    sidecar_path: Path,
    to: str,
    subject: str,
    from_date,
) -> int:
    if not report_path.exists():
        print(f"Email-only refused: locked spreadsheet missing ({report_path}).", flush=True)
        return 2
    if sidecar_path.exists():
        try:
            payload = json.loads(sidecar_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("email_sent") or payload.get("email_status") == EMAIL_SENT:
            print("Email-only refused: Mail.Send already recorded. Will not send twice.", flush=True)
            return 2
        rows = list(payload.get("rows") or [])
        batch_label = str(payload.get("batch") or "")
        if payload.get("from_date") and from_date is None:
            from_date = parse_iso_date(str(payload["from_date"]))
            subject = dry_subject_for(from_date)
    else:
        rows = []
        batch_label = ""
    email_sidecar = report_path.with_suffix(report_path.suffix + ".email.json")
    if email_sidecar.exists():
        try:
            prior = json.loads(email_sidecar.read_text())
        except (OSError, json.JSONDecodeError):
            prior = {}
        if prior.get("status") == EMAIL_SENT:
            print("Email-only refused: Mail.Send already recorded. Will not send twice.", flush=True)
            return 2
    graph_client = _optional_graph_client()
    return _send_email(
        graph_client,
        report_path,
        rows,
        batch_label=batch_label,
        to=to,
        subject=subject,
        from_date=from_date,
        sidecar_path=sidecar_path,
    )


def _send_email(
    graph_client,
    report_path: Path,
    rows: list[dict[str, Any]],
    *,
    batch_label: str,
    to: str,
    subject: str | None = None,
    from_date=None,
    sidecar_path: Path | None = None,
) -> int:
    subject = subject or dry_subject_for(from_date)
    if sidecar_path and sidecar_path.exists():
        try:
            payload = json.loads(sidecar_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("email_sent") or payload.get("email_status") == EMAIL_SENT:
            print("Mail.Send refused: already recorded. Will not send twice.", flush=True)
            return 2
    body = dry_email_body(rows, batch_label=batch_label, from_date=from_date)
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
    if sidecar_path is not None:
        _mark_sidecar_email(sidecar_path, status, subject)
    counts = result_counts(rows)
    print(
        f"Email status={status} to={to} subject={subject} "
        f"Success={counts['Success']} Incomplete={counts['Incomplete']} "
        f"Fail={counts['Fail']} HOLD={counts['HOLD']}",
        flush=True,
    )
    return 0 if status == EMAIL_SENT else 1


if __name__ == "__main__":
    raise SystemExit(main())
