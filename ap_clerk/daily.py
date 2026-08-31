"""Weekday 5:00am America/Chicago daily run of 30 unprocessed AP bills.

Invoked by the Grok Bot routine (or a human) as:

    python -m ap_clerk daily --live --limit 30

Requires `--live` and live KIMCO creds. Does not register a GitHub Actions
cron that would post live from CI. FIFO from 2026-07-28 toward today with a
persisted cursor. Emails the Excel to Treyce at kannonmfg.com from the AP mailbox.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from ap_clerk.cursor import DailyCursor, cursor_after_messages
from ap_clerk.graph import ALLOWED_MAILBOX, EMAIL_DENIED, REPORT_TO

DEFAULT_DAILY_LIMIT = 30
GROK_BOT_LAUNCH = "python -m ap_clerk daily --live --limit 30"


def email_subject_for(as_of: date) -> str:
    return f"AP run {as_of.isoformat()}"


def email_body_for(rows: list[dict[str, Any]], *, batch_label: str, as_of: date) -> str:
    counts = Counter(str(row.get("Result") or "") for row in rows)
    return (
        f"AP run {as_of.isoformat()} (America/Chicago weekday 5:00am routine).\n"
        f"Batch: {batch_label}\n"
        f"Success: {counts.get('Success', 0)}\n"
        f"Fail: {counts.get('Fail', 0)}\n"
        f"HOLD: {counts.get('HOLD', 0)}\n"
        f"Mailbox: {ALLOWED_MAILBOX}\n"
        f"Report attached.\n"
    )


def result_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("Result") or "") for row in rows)
    return {
        "Success": int(counts.get("Success", 0)),
        "Fail": int(counts.get("Fail", 0)),
        "HOLD": int(counts.get("HOLD", 0)),
        "total": len(rows),
    }


def cursor_from_run(
    invoices: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    *,
    as_of: date,
    batch: str | None,
    mailbox: str = ALLOWED_MAILBOX,
) -> DailyCursor:
    examined: list[dict[str, Any]] = []
    for inv in invoices:
        examined.append(
            {
                "id": inv.get("graph_message_id") or inv.get("id"),
                "receivedDateTime": inv.get("receivedDateTime"),
            }
        )
    for item in skipped:
        examined.append(
            {
                "id": item.get("graph_message_id") or item.get("id"),
                "receivedDateTime": item.get("receivedDateTime"),
            }
        )
    examined.sort(key=lambda m: str(m.get("receivedDateTime") or ""))
    return cursor_after_messages(examined, as_of=as_of, batch=batch, mailbox=mailbox)


def write_email_sidecar(report_path: Path, status: str, *, subject: str, to: str = REPORT_TO) -> Path:
    sidecar = report_path.with_suffix(report_path.suffix + ".email.json")
    sidecar.write_text(
        "{\n"
        f'  "to": "{to}",\n'
        f'  "subject": {subject!r},\n'
        f'  "status": "{status or EMAIL_DENIED}",\n'
        f'  "from": "{ALLOWED_MAILBOX}"\n'
        "}\n"
    )
    return sidecar
