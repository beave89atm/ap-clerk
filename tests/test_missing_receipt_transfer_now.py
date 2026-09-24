"""Kyle 2026-09-22 live missing_receipt → Transfer AP targets + GET gate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from missing_receipt_transfer_now import (
    TARGETS,
    comments_present,
    still_missing_receipt_hold,
)


def test_targets_are_the_six_kyle_named():
    ids = {t["kimco_id"] for t in TARGETS}
    assert ids == {10160, 10163, 10175, 10178, 10173, 10181}
    oneal = [t for t in TARGETS if t["vendor"] == "O'Neal"]
    gas = [t for t in TARGETS if t["vendor"] == "Gas"]
    assert {t["kimco_id"] for t in oneal} == {10160, 10163, 10175, 10178}
    assert {t["from_batch"] for t in oneal} == {722}
    assert {t["from_batch"] for t in gas} == {720}
    assert all(t["mention"] == "plain" for t in oneal)
    assert all(t["mention"] == "shawn" for t in gas)


def test_still_missing_receipt_requires_no_selected_receipts():
    spec = {
        "invoice": "15469453",
        "kimco_id": 10160,
        "mention": "plain",
    }
    ok, reason = still_missing_receipt_hold(
        {
            "invoice": "15469453",
            "receipt_n": 0,
            "amount": 0,
            "verification": 23151.42,
            "comments": [{"html": "Anthony HOLD (receipt) on O'Neal Steel 15469453"}],
        },
        spec,
    )
    assert ok is True
    assert reason == "missing_receipt-no-receipts"

    ok, reason = still_missing_receipt_hold(
        {
            "invoice": "15469453",
            "receipt_n": 2,
            "amount": 23151.42,
            "verification": 23151.42,
            "comments": [],
        },
        spec,
    )
    assert ok is False
    assert reason == "looks-finished-has-receipts"

    ok, reason = still_missing_receipt_hold(
        {"invoice": "999", "receipt_n": 0, "amount": 0, "verification": 1, "comments": []},
        spec,
    )
    assert ok is False
    assert reason == "invoice-mismatch"


def test_comments_present_requires_receipt_hold_and_invoice():
    spec = {"invoice": "0040438052", "mention": "shawn"}
    hits = comments_present(
        {
            "comments": [
                {"id": 936, "html": '<span data-mention-id="104">@Shawn</span> HOLD (price-does-not-match) on Gas and Supply 0040438052'},
                {"id": 940, "html": '<span data-mention-id="104">@Shawn McKibben</span> HOLD (receipt) on Gas and Supply 0040438052 PO 59081'},
            ]
        },
        spec,
    )
    assert [h["id"] for h in hits] == [940]
