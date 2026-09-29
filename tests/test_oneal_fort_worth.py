"""O'Neal NOTE-52 Fort Worth backfill gates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from oneal_fort_worth_backfill import TARGETS, unique_moves


def test_targets_cover_kyle_list():
    ids = {t["kimco_id"] for t in TARGETS}
    invs = {t["invoice"] for t in TARGETS}
    assert ids == {10160, 10161, 10162, 10163, 10164, 10174, 10175, 10176, 10177, 10178, 10179, 10180, 10079}
    assert "15469453" in invs and "15447737" in invs


def test_unique_moves_one_per_parent():
    rows = [
        {"message_id": "A", "eligible": True, "invoice": "15457895"},
        {"message_id": "A", "eligible": True, "invoice": "15457907"},
        {"message_id": "B", "eligible": True, "invoice": "15469453"},
        {"message_id": "", "eligible": False, "invoice": "x"},
    ]
    assert unique_moves(rows) == ["A", "B"]
