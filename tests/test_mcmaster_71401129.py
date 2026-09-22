"""McMaster 71401129 / 10152 NOTE-47 finish is this bill only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.kimco import KimcoError  # noqa: E402
from mcmaster_0918 import DO_NOT_MUTATE_IDS, LEAVE_ALONE_HOLD_IDS  # noqa: E402
from mcmaster_receipt_retry import finish_retry_row  # noqa: E402
from mcmaster_71401129 import (  # noqa: E402
    ALLOWED_WRITE_IDS,
    KNOWN_RECEIPT_IDS,
    NOTE47_PPV_MAX_ABS,
    TARGET_AMOUNT,
    TARGET_ID,
    TARGET_INVOICE,
    TARGET_PO,
    ppv_abs_over_note47,
    refuse_other_header,
)


def test_finalize_targets_10152_only():
    assert TARGET_INVOICE == "71401129"
    assert TARGET_ID == 10152
    assert TARGET_PO == "59125"
    assert TARGET_AMOUNT == 257.72
    assert ALLOWED_WRITE_IDS == {10152}
    assert KNOWN_RECEIPT_IDS == (23939, 23940, 23941)
    assert 10152 in LEAVE_ALONE_HOLD_IDS
    assert 10152 not in DO_NOT_MUTATE_IDS


def test_note47_14_60_in_gate_75_over():
    assert NOTE47_PPV_MAX_ABS == 75.00
    assert ppv_abs_over_note47(-14.60) is False
    assert ppv_abs_over_note47(14.60) is False
    assert ppv_abs_over_note47(74.99) is False
    assert ppv_abs_over_note47(75.00) is True
    assert ppv_abs_over_note47(-75.00) is True
    assert round(abs(272.32 - 257.72), 2) == 14.60


def test_refuse_other_headers():
    refuse_other_header(10152, "71401129")
    with pytest.raises(KimcoError, match="10152 only"):
        refuse_other_header(10139, "71080498")
    with pytest.raises(KimcoError, match="71401129 only"):
        refuse_other_header(10152, "71080498")


def test_generic_retry_still_skips_10152():
    row, extra = finish_retry_row(
        object(),
        None,
        parsed={"invoice_number": "71401129"},
        enter_row={"Invoice #": "71401129", "KIMCO id": 10152, "Result": "HOLD"},
        kimco_id=10152,
        receipts=[],
    )
    assert extra["status"] == "leave-alone"
    assert row["KIMCO id"] == 10152
