"""Receiving-check Comments_1 notes. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "three_p_1007044_receiving_notes_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "three_p_1007044_receiving_notes_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

mismatches = _MOD.mismatches
note_text = _MOD.note_text
po_number = _MOD.po_number


def test_only_unaligned_receipts_are_noted():
    rows = [
        {"id": 23414, "po": "58925", "quantity_received": 4.0, "quantity_remaining": 3.0},
        {"id": 23543, "po": "58925", "quantity_received": 2.0, "quantity_remaining": 2.0},
        {"id": 23528, "po": "58925", "quantity_received": 1.0, "quantity_remaining": 2.0},
        {"id": 23530, "po": "59002", "quantity_received": 4.0, "quantity_remaining": 0.0},
    ]
    bad = mismatches(rows)
    assert [row["id"] for row in bad] == [23414, 23528, 23530]
    text = note_text(bad)
    assert text.startswith("AP Clerk:")
    assert "@" not in text
    assert text.count("AP Clerk:") == 3
    assert "receipt 23414 shows Quantity_Received 4 but Quantity_Remaining 3 on PO 58925" in text
    assert "receipt 23528 shows Quantity_Received 1 but Quantity_Remaining 2 on PO 58925" in text
    assert "receipt 23530 shows Quantity_Received 4 but Quantity_Remaining 0 on PO 59002" in text
    assert "These should line up." in text
    assert po_number("PO58925-3P INDUSTRIES") == "58925"
