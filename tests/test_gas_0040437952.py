"""Gas 0040437952 leftover enter gates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.graph import ENTERED_IN_AI_CATEGORY, ENTERED_WITH_ISSUES_CATEGORY
from ap_clerk.outlook_finish import parent_outlook_target
from gas_0040438052 import _blank_gas_missing_receipt
from gas_0040437952 import EXPECTED_AMOUNT, EXPECTED_PO, TARGET
from gas_supply_0917 import KNOWN_BATCH_ID, PREFERRED_BATCH_NAME


def test_target_is_the_9_17_leftover():
    assert TARGET == "0040437952"
    assert EXPECTED_PO == "59006"
    assert EXPECTED_AMOUNT == 241.46
    assert KNOWN_BATCH_ID == 720
    assert PREFERRED_BATCH_NAME == "API Agent - 9/17/26 Gas & Supply"


def test_gas_missing_receipt_does_not_tag():
    row = _blank_gas_missing_receipt(
        {
            "Result": "HOLD",
            "Why": "HOLD (receipt): no open receipt leftover. @Ruben Perez: receive.",
            "Vendor": "Gas and Supply North Texas, LLC",
        }
    )
    assert row["Exception category"] == "missing_receipt"
    assert row["Exception owner"] == ""
    assert "@Ruben" not in row["Why"]
    assert "blank" in row["Why"].lower()


def test_note51_parent_stays_issues_while_8052_hold():
    results = [
        "Success",
        "Success",
        "Success",  # this invoice
        "HOLD",  # 0040438052 missing_receipt
        "Success",
        "Success",
        "Success",
    ]
    assert parent_outlook_target(results) == ENTERED_WITH_ISSUES_CATEGORY
    assert parent_outlook_target(["Success"] * 7) == ENTERED_IN_AI_CATEGORY
