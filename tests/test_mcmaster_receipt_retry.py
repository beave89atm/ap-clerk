"""McMaster receipt retry finishes existing headers only."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from mcmaster_0918 import (  # noqa: E402
    DO_NOT_MUTATE_IDS,
    LEAVE_ALONE_HOLD_IDS,
    RETRY_HEADERS,
)
from mcmaster_receipt_retry import (  # noqa: E402
    RETRY_AMOUNTS,
    RETRY_POS,
    enter_row_from_sheet,
    finish_retry_row,
)


def test_retry_targets_are_existing_headers_not_new_invoices():
    assert set(RETRY_HEADERS) == {
        "71080498",
        "72094446",
        "72087570",
        "72012111",
        "72013304",
        "71839575",
    }
    assert RETRY_HEADERS["71080498"] == 10139
    assert RETRY_HEADERS["72094446"] == 10142
    assert RETRY_POS["72013304"] == "58221"
    assert RETRY_AMOUNTS["71080498"] == 395.31
    assert LEAVE_ALONE_HOLD_IDS == {10140, 10142, 10143, 10146, 10148, 10152, 10154, 10158, 10159}
    assert DO_NOT_MUTATE_IDS == {10138, 10139, 10141, 10144, 10145, 10147, 10149, 10150, 10151, 10153, 10155, 10156, 10157}
    assert 10138 in DO_NOT_MUTATE_IDS


def test_finish_retry_skips_transfer_ap_and_success_leave_alones():
    row, extra = finish_retry_row(
        object(),
        None,
        parsed={"invoice_number": "71001379"},
        enter_row={"Invoice #": "71001379", "KIMCO id": 10140, "Result": "HOLD"},
        kimco_id=10140,
        receipts=[],
    )
    assert extra["status"] == "leave-alone"
    assert row["KIMCO id"] == 10140
    row, extra = finish_retry_row(
        object(),
        None,
        parsed={"invoice_number": "71647463"},
        enter_row={"Invoice #": "71647463", "KIMCO id": 10138, "Result": "Success"},
        kimco_id=10138,
        receipts=[],
    )
    assert extra["status"] == "leave-alone"


def test_enter_row_reuses_sheet_header_id():
    prior = [{"Invoice #": "72087570", "KIMCO id": 10144, "Result": "HOLD", "PO": "59235"}]
    row = enter_row_from_sheet(prior, "72087570", 10144)
    assert row["KIMCO id"] == 10144
    assert row["PO"] == "59235"
