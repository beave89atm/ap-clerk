"""NOTE-47 / Shawn-finish gates for Gas & Supply 10134 and 10137."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from gas_shawn_finish import (  # noqa: E402
    FINISHABLE,
    NOTE47_PPV_MAX_ABS,
    STILL_MISSING_PO,
    SUCCESS_LEAVE_ALONE,
    drop_invoiced,
    match_open_for_bill,
    missing_po_still_blocked,
    parsed_for,
    ppv_abs_over_note47,
)


def _rec(rid: int, qty: float, unit: float, *, po: str = "58948") -> dict:
    return {
        "id": rid,
        "po": po,
        "part": f"PO{po}-XX",
        "qty": qty,
        "unit_price": unit,
        "amount": round(qty * unit, 2),
        "name": f"PO{po}-GAS AND SUPPLY",
    }


def test_note47_gate_abs_75():
    assert NOTE47_PPV_MAX_ABS == 75.00
    assert ppv_abs_over_note47(74.99) is False
    assert ppv_abs_over_note47(-74.99) is False
    assert ppv_abs_over_note47(75.00) is True
    assert ppv_abs_over_note47(-75.00) is True


def test_10134_matches_new_17350_receipt_not_old_invoiced():
    parsed = parsed_for("0040423658")
    open_rows = [_rec(24282, 1.0, 173.5), _rec(24283, 2.0, 146.5)]
    planned = match_open_for_bill(parsed, open_rows)
    assert planned["select_zero"] is False
    assert planned["over_note47"] is False
    assert planned["receipt_ids"] == [24282]
    assert planned["ppv"] == 0.0


def test_10137_matches_new_14650_receipt_not_old_603():
    parsed = parsed_for("0040414821")
    # Old leftover 23271 2@6.03 is over-gate ($280.95). New 24283 matches.
    open_new = [_rec(24282, 1.0, 173.5), _rec(24283, 2.0, 146.5)]
    planned = match_open_for_bill(parsed, open_new)
    assert planned["receipt_ids"] == [24283]
    assert planned["select_zero"] is False
    assert planned["ppv"] == 0.0

    old_only = [_rec(23271, 2.0, 6.03)]
    locked = match_open_for_bill(parsed, old_only)
    assert locked["select_zero"] is True
    assert ppv_abs_over_note47(293.0 - 12.06) is True


def test_do_not_enter_type4_when_po_still_missing():
    receipts = [_rec(24282, 1.0, 173.5, po="58948")]
    assert missing_po_still_blocked("59081", receipts) is True
    assert missing_po_still_blocked("59006", receipts) is True
    assert missing_po_still_blocked("58948", receipts) is False
    assert {inv for inv, _po, _amt in STILL_MISSING_PO} >= {
        "0040430010",
        "0040417672",
        "0040414962",
    }


def test_drop_invoiced_old_58948_leftovers():
    old = {
        "id": 23271,
        "qty": 2.0,
        "unit_price": 6.03,
        "raw": {"Invoiced": True},
    }
    new = {
        "id": 24283,
        "qty": 2.0,
        "unit_price": 146.5,
        "raw": {"Invoiced": None},
    }
    assert [r["id"] for r in drop_invoiced([old, new])] == [24283]


def test_already_success_type4_not_in_finishable():
    assert set(FINISHABLE) == {"0040423658", "0040414821"}
    assert 10134 not in SUCCESS_LEAVE_ALONE.values()
    assert 10137 not in SUCCESS_LEAVE_ALONE.values()
    assert set(SUCCESS_LEAVE_ALONE.values()) == {
        10128, 10129, 10130, 10131, 10132, 10133, 10135, 10136
    }
