"""Crosslink 9/16 focused-batch discovery gates.

Distinctive Crosslink only. NOTE-30 reminders stay already-entered.
Invoice dates before 2026-08-01 are reported, not entered (AQPC lesson).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from crosslink_0916 import (  # noqa: E402
    MIN_INVOICE_DATE,
    NOTE30_REMINDERS,
    VENDOR_NAME,
    is_crosslink_message,
    pick_recent,
)


def _msg(*, subject: str, from_name: str = "", categories: list[str] | None = None) -> dict:
    return {
        "id": "AAMk-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": "ap@example.com"}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_crosslink_distinctive_token_never_msc_rmp():
    assert is_crosslink_message(
        _msg(subject="Invoice #27943 for 58888 from Crosslink Powder Coating")
    )
    assert is_crosslink_message(
        _msg(subject="Invoice 28001", from_name="Crosslink Powder Coating of TX, LLC")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 70762501 from MSC Industrial Supply")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 10917 from AMERICAN QUALITY POWDER COATING")
    )


def test_crosslink_pick_recent_stops_before_aug_2026():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "28010",
                "date": "2026-08-12",
                "receivedDateTime": "2026-08-13T12:00:00Z",
            },
            {
                "invoice_number": "27999",
                "date": "2026-08-03",
                "receivedDateTime": "2026-08-04T12:00:00Z",
            },
            {
                "invoice_number": "27447",
                "date": "2026-07-07",
                "receivedDateTime": "2026-08-20T01:00:00Z",
            },
        ],
        already=set(NOTE30_REMINDERS),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["28010", "27999"]
    assert older == []
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"

    recent_only_old, older_only = pick_recent(
        [
            {
                "invoice_number": "27001",
                "date": "2026-07-15",
                "receivedDateTime": "2026-07-16T12:00:00Z",
            }
        ],
        already=set(),
        cap=5,
    )
    assert recent_only_old == []
    assert [b["invoice_number"] for b in older_only] == ["27001"]


def test_crosslink_note30_reminders_not_recreated():
    assert NOTE30_REMINDERS == {"27447": 9382, "27448": 9384, "27591": 9587}
    recent, older = pick_recent(
        [
            {
                "invoice_number": "27447",
                "date": "2026-08-20",
                "receivedDateTime": "2026-08-20T01:00:00Z",
            },
            {
                "invoice_number": "27591",
                "date": "2026-08-20",
                "receivedDateTime": "2026-08-20T02:00:00Z",
            },
        ],
        already=set(NOTE30_REMINDERS),
        cap=5,
    )
    assert recent == []
    assert older == []
    assert VENDOR_NAME == "Crosslink Powder Coating"
