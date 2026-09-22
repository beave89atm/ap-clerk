"""O'Neal plus-10 picker, Anthony missing_receipt, NOTE-51 Outlook."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.graph import ENTERED_IN_AI_CATEGORY, ENTERED_WITH_ISSUES_CATEGORY
from ap_clerk.outlook_finish import parent_outlook_target
from oneal_0921 import CREATED_HEADERS, exact_invoice_number
from oneal_plus10_0922 import (
    ANTHONY_MENTION,
    ANTHONY_MENTION_NAME,
    CAP,
    KNOWN_BATCH_ID,
    PPV_MAX_ABS,
    PREFERRED_BATCH_NAME,
    PREFERRED_LEFTOVERS,
    apply_anthony_missing_receipt,
    pick_plus10,
    ppv_abs_over_gate,
)


def test_plus10_batch_and_locked_headers():
    assert KNOWN_BATCH_ID == 722
    assert PREFERRED_BATCH_NAME == "API Agent - 9/21/26 O'Neal"
    assert CAP == 10
    assert PPV_MAX_ABS == 75.0
    assert CREATED_HEADERS["15469453"] == 10160
    assert CREATED_HEADERS["15464854"] == 10164
    assert ANTHONY_MENTION["id"] is None


def test_pick_plus10_prefers_leftovers_then_newest():
    bills = [
        {"invoice_number": "15469453", "date": "2026-09-10", "amount": 1},
        {"invoice_number": "15460544", "date": "2026-08-12", "amount": 64.5, "po": "58920"},
        {"invoice_number": "15460995", "date": "2026-08-12", "amount": 6954.21, "po": "59085"},
        {"invoice_number": "15461007", "date": "2026-08-12", "amount": 67.38, "po": "59086"},
        {"invoice_number": "15461157", "date": "2026-08-12", "amount": 1978.91, "po": "59092"},
        {"invoice_number": "15470001", "date": "2026-09-15", "amount": 10},
        {"invoice_number": "15470002", "date": "2026-09-14", "amount": 11},
        {"invoice_number": "15470003", "date": "2026-09-13", "amount": 12},
        {"invoice_number": "15470004", "date": "2026-09-12", "amount": 13},
        {"invoice_number": "15470005", "date": "2026-09-11", "amount": 14},
        {"invoice_number": "15470006", "date": "2026-09-10", "amount": 15},
        {"invoice_number": "15450000", "date": "2026-07-15", "amount": 9},
    ]
    chosen, leftover = pick_plus10(bills, already=set(CREATED_HEADERS), cap=10)
    got = [exact_invoice_number(b.get("invoice_number")) for b in chosen]
    assert got[:4] == list(PREFERRED_LEFTOVERS)
    assert "15469453" not in got
    assert "15450000" not in got
    assert got[4:] == [
        "15470001",
        "15470002",
        "15470003",
        "15470004",
        "15470005",
        "15470006",
    ]
    assert len(got) == 10


def test_anthony_overrides_ruben_missing_receipt_owner():
    row = apply_anthony_missing_receipt(
        {
            "Result": "HOLD",
            "Why": "HOLD (receipt): no open leftover. Ruben Perez: receive the PO.",
            "Vendor": "O'Neal Steel - Dallas (GP)",
        }
    )
    assert row["Exception category"] == "missing_receipt"
    assert row["Exception owner"] == ANTHONY_MENTION_NAME
    assert "Ruben" not in row["Why"]


def test_ppv_gate_is_75():
    assert ppv_abs_over_gate(74.99) is False
    assert ppv_abs_over_gate(75.0) is True
    assert ppv_abs_over_gate(-80) is True


def test_note51_parent_outlook_all_vs_partial():
    assert parent_outlook_target(["Success", "Success"]) == ENTERED_IN_AI_CATEGORY
    assert parent_outlook_target(["Success", "HOLD"]) == ENTERED_WITH_ISSUES_CATEGORY
