"""Persisted daily-queue cursor. FIFO from 2026-07-28 America/Chicago toward today.

The next weekday continues after the previous 30. Never restarts at 7/28
when a cursor exists. Never logs secrets or message bodies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.rules import CHICAGO

DAILY_START_DATE = date(2026, 7, 28)
DEFAULT_CURSOR_PATH = Path("runs") / "daily-cursor.json"


@dataclass
class DailyCursor:
    last_receivedDateTime: str | None = None
    last_message_id: str | None = None
    mailbox: str = ALLOWED_MAILBOX
    last_run_date: str | None = None
    last_batch: str | None = None
    processed_count: int = 0

    def after_received(self) -> datetime | None:
        if not self.last_receivedDateTime:
            return None
        raw = str(self.last_receivedDateTime).strip()
        try:
            if raw.endswith("Z"):
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


def daily_floor_datetime() -> datetime:
    """2026-07-28 00:00 America/Chicago inclusive."""
    return datetime.combine(DAILY_START_DATE, time.min, tzinfo=CHICAGO)


def load_cursor(path: Path | None = None) -> DailyCursor:
    path = path or DEFAULT_CURSOR_PATH
    if not path.exists():
        return DailyCursor()
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return DailyCursor()
    if not isinstance(payload, dict):
        return DailyCursor()
    return DailyCursor(
        last_receivedDateTime=payload.get("last_receivedDateTime") or None,
        last_message_id=payload.get("last_message_id") or None,
        mailbox=str(payload.get("mailbox") or ALLOWED_MAILBOX),
        last_run_date=payload.get("last_run_date") or None,
        last_batch=payload.get("last_batch") or None,
        processed_count=int(payload.get("processed_count") or 0),
    )


def save_cursor(cursor: DailyCursor, path: Path | None = None) -> Path:
    path = path or DEFAULT_CURSOR_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mailbox": cursor.mailbox or ALLOWED_MAILBOX,
        "start_from": DAILY_START_DATE.isoformat(),
        "last_receivedDateTime": cursor.last_receivedDateTime,
        "last_message_id": cursor.last_message_id,
        "last_run_date": cursor.last_run_date,
        "last_batch": cursor.last_batch,
        "processed_count": cursor.processed_count,
        "notes": (
            "FIFO from 2026-07-28 America/Chicago toward today. "
            "Next weekday continues after this cursor. Do not restart at 7/28."
        ),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def cursor_after_messages(
    messages: list[dict[str, Any]],
    *,
    as_of: date | None = None,
    batch: str | None = None,
    mailbox: str = ALLOWED_MAILBOX,
) -> DailyCursor:
    """Advance the cursor to the last examined message (FIFO toward today)."""
    last = DailyCursor(mailbox=mailbox, last_run_date=as_of.isoformat() if as_of else None, last_batch=batch)
    if not messages:
        return last
    newest = messages[-1]
    last.last_receivedDateTime = str(newest.get("receivedDateTime") or "") or None
    last.last_message_id = str(newest.get("id") or newest.get("graph_message_id") or "") or None
    last.processed_count = len(messages)
    return last


def should_skip_already_seen(message: dict[str, Any], cursor: DailyCursor) -> bool:
    """Skip the exact last-processed message so the next run continues after it."""
    mid = str(message.get("id") or "")
    if cursor.last_message_id and mid and mid == cursor.last_message_id:
        return True
    received = str(message.get("receivedDateTime") or "")
    after = cursor.after_received()
    if after is None or not received:
        return False
    try:
        current = datetime.fromisoformat(received.replace("Z", "+00:00"))
    except ValueError:
        return False
    if current.tzinfo is None:
        current = current.replace(tzinfo=after.tzinfo)
    return current < after
