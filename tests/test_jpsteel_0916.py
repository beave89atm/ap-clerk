"""JPSteel 9/16 dedicated-batch discovery gates.

Distinctive JPSteel / JP Steel only. Invoice dates before 2026-08-01
are reported, not entered (AQPC NOTE-28). Never reuse Crosslink batch 715.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.rules import names_match  # noqa: E402
from jpsteel_0916 import (  # noqa: E402
    CAP,
    CROSSLINK_TODAY_NAME,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    MIN_INVOICE_DATE,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    blob_has_jpsteel,
    is_jpsteel_invoice_email,
    is_jpsteel_message,
    is_jpsteel_vendor_text,
    pick_recent,
    _qty_hold,
)


def _msg(*, subject: str, from_name: str = "", categories: list[str] | None = None) -> dict:
    return {
        "id": "AAMk-jpsteel-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": "ap@example.com"}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_jpsteel_distinctive_token_never_other_steel_or_msc():
    assert is_jpsteel_message(_msg(subject="Invoice 125100 from JP Steel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125101 from JPSteel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125102", from_name="JP Steel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125103", from_name="JPSteel Inc"))
    assert not is_jpsteel_message(_msg(subject="Invoice 70762501 from MSC Industrial Supply"))
    assert not is_jpsteel_message(_msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY"))
    assert not is_jpsteel_message(_msg(subject="Invoice 10917 from AMERICAN QUALITY POWDER COATING"))
    assert not is_jpsteel_message(_msg(subject="Invoice #28166 from Crosslink Powder Coating"))
    assert not is_jpsteel_message(_msg(subject="Invoice 15439109 from Morgan Steel"))
    assert not is_jpsteel_message(_msg(subject="Invoice 12345 from Leeco Steel"))
    assert not is_jpsteel_message(_msg(subject="Invoice 999 from Beshert Steel Processing"))
    assert not is_jpsteel_message(_msg(subject="Steel invoice 88"))
    assert blob_has_jpsteel("JPSteel")
    assert blob_has_jpsteel("jp steel")
    assert not blob_has_jpsteel("morgan steel")
    assert not blob_has_jpsteel("industrial steel supply")


def test_jpsteel_names_match_compact_and_spaced():
    assert names_match("JP Steel", "JP Steel")
    assert is_jpsteel_vendor_text("JP Steel")
    assert is_jpsteel_vendor_text("100-JP STEEL")
    assert is_jpsteel_vendor_text("JPSteel")
    assert not is_jpsteel_vendor_text("304-MORGAN STEEL")
    assert not is_jpsteel_vendor_text("Leeco Steel, LLC")
    assert not is_jpsteel_vendor_text("MSC Industrial Supply")


def test_jpsteel_pick_recent_stops_before_aug_2026():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "125200",
                "date": "2026-09-10",
                "receivedDateTime": "2026-09-11T12:00:00Z",
            },
            {
                "invoice_number": "125010",
                "date": "2026-08-03",
                "receivedDateTime": "2026-08-04T12:00:00Z",
            },
            {
                "invoice_number": "124747",
                "date": "2026-07-15",
                "receivedDateTime": "2026-08-14T12:00:00Z",
            },
        ],
        already=set(),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["125200", "125010"]
    assert [b["invoice_number"] for b in older] == ["124747"]
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"
    assert CAP == 5

    recent_only_old, older_only = pick_recent(
        [
            {
                "invoice_number": "124100",
                "date": "2026-07-20",
                "receivedDateTime": "2026-07-21T12:00:00Z",
            }
        ],
        already=set(),
        cap=5,
    )
    assert recent_only_old == []
    assert [b["invoice_number"] for b in older_only] == ["124100"]


def test_jpsteel_already_entered_and_cap():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "125201",
                "date": "2026-09-12",
                "receivedDateTime": "2026-09-12T12:00:00Z",
            },
            {
                "invoice_number": "125202",
                "date": "2026-09-11",
                "receivedDateTime": "2026-09-11T12:00:00Z",
            },
        ],
        already={"125201"},
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["125202"]
    assert older == []

    many = [
        {
            "invoice_number": str(125300 + i),
            "date": "2026-09-01",
            "receivedDateTime": f"2026-09-01T0{i}:00:00Z",
        }
        for i in range(7)
    ]
    recent_capped, _ = pick_recent(many, already=set(), cap=5)
    assert len(recent_capped) == 5


def test_jpsteel_statement_is_not_an_invoice():
    assert not is_jpsteel_invoice_email(
        _msg(subject="Statement from JP Steel")
    )
    assert not is_jpsteel_invoice_email(
        _msg(subject="Past due invoices — JPSteel")
    )
    assert is_jpsteel_invoice_email(
        _msg(subject="Invoice 125200 from JP Steel")
    )


def test_jpsteel_batch_is_dedicated_not_crosslink_715():
    assert PREFERRED_BATCH_NAME == "API Agent - 9/16/26 JPSteel"
    assert FALLBACK_BATCH_NAME == "API Agent - 9/16/26-2"
    assert CROSSLINK_TODAY_NAME == "API Agent - 9/16/26"
    assert 715 in FORBIDDEN_BATCH_IDS
    assert PREFERRED_BATCH_NAME != CROSSLINK_TODAY_NAME
    assert FALLBACK_BATCH_NAME != CROSSLINK_TODAY_NAME
    assert VENDOR_NAME == "JP Steel"


def test_jpsteel_inches_are_not_rolled_qty():
    """Description inches must not equal a leftover rolled qty."""
    parsed = {
        "lines": [
            {"part": "PLATE", "qty": 2.0, "description": '48.000" x 96.000" PLATE'}
        ]
    }
    recs = [{"qty": 48.0, "unit": 1.25}]
    assert _qty_hold(parsed, recs) is True
    recs_ok = [{"qty": 2.0, "unit": 1.25}]
    assert _qty_hold(parsed, recs_ok) is False
    # Explicit length_inches 32 vs receipt qty 32 is allowed (Metal Supermarkets class).
    parsed_len = {"lines": [{"part": "BAR", "qty": 1.0, "length_inches": 32.0}]}
    assert _qty_hold(parsed_len, [{"qty": 32.0}]) is False
