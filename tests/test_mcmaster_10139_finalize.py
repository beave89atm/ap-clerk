"""10139 / 71080498 finalize is this bill only."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.kimco import KimcoError  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    DO_NOT_MUTATE_IDS,
    LEAVE_ALONE_HOLD_IDS,
    apply_over_ppv_transfer_ap,
)
from mcmaster_10139_finalize import (  # noqa: E402
    ALLOWED_WRITE_IDS,
    FIRST_OPEN_DO_NOT_USE,
    TARGET_AMOUNT,
    TARGET_ID,
    TARGET_INVOICE,
    TARGET_PO,
    finish_10139_row,
    refuse_other_header,
    usable_po_receipts,
)
from mcmaster_receipt_retry import finish_retry_row  # noqa: E402


def test_finalize_targets_10139_only():
    assert TARGET_INVOICE == "71080498"
    assert TARGET_ID == 10139
    assert TARGET_PO == "59056"
    assert TARGET_AMOUNT == 395.31
    assert ALLOWED_WRITE_IDS == {10139}
    assert 10139 in LEAVE_ALONE_HOLD_IDS
    assert 10139 not in DO_NOT_MUTATE_IDS
    other_holds = {10140, 10142, 10143, 10146, 10148, 10152, 10154, 10158, 10159}
    assert other_holds <= LEAVE_ALONE_HOLD_IDS
    assert 23841 in FIRST_OPEN_DO_NOT_USE


def test_refuse_other_headers():
    refuse_other_header(10139, "71080498")
    with pytest.raises(KimcoError, match="10139 only"):
        refuse_other_header(10142, "72094446")
    with pytest.raises(KimcoError, match="71080498 only"):
        refuse_other_header(10139, "72094446")


def test_finish_retry_still_skips_10139_and_other_holds():
    row, extra = finish_retry_row(
        object(),
        None,
        parsed={"invoice_number": "71080498"},
        enter_row={"Invoice #": "71080498", "KIMCO id": 10139, "Result": "HOLD"},
        kimco_id=10139,
        receipts=[],
    )
    assert extra["status"] == "leave-alone"
    assert row["KIMCO id"] == 10139
    row, extra = finish_retry_row(
        object(),
        None,
        parsed={"invoice_number": "72094446"},
        enter_row={"Invoice #": "72094446", "KIMCO id": 10142, "Result": "HOLD"},
        kimco_id=10142,
        receipts=[],
    )
    assert extra["status"] == "leave-alone"


def test_usable_receipts_skip_zero_remaining_and_first_open():
    usable, skipped = usable_po_receipts(
        [
            {"id": 23841, "qty": 18, "unit_price": 20.72, "part": "9565K38", "raw": {"Quantity_Remaining": 18}},
            {"id": 24247, "qty": 18, "unit_price": 20.72, "part": "9565K38", "raw": {"Quantity_Remaining": 0}},
            {"id": 25001, "qty": 18, "unit_price": 20.72, "part": "9565K38", "raw": {"Quantity_Remaining": 18}},
        ]
    )
    assert [r["id"] for r in usable] == [25001]
    reasons = {s["id"]: s["reason"] for s in skipped}
    assert reasons[23841] == "first-open-23841"
    assert reasons[24247] == "zero-remaining"


def test_apply_over_ppv_10139_needs_allow_ids():
    comment = "@Shawn McKibben HOLD"

    class _Fake:
        def list_items(self, _svc):
            raise AssertionError("must not list batches when leave-alone")

    assert apply_over_ppv_transfer_ap(_Fake(), kimco_id=10139, comment=comment)["status"] == "leave-alone"
    assert apply_over_ppv_transfer_ap(_Fake(), kimco_id=10140, comment=comment, allow_ids={10139})[
        "status"
    ] == "leave-alone"


def test_finish_10139_refuses_other_id():
    with pytest.raises(KimcoError, match="10139 only"):
        finish_10139_row(
            object(),
            None,
            parsed={"invoice_number": "72094446"},
            enter_row={"Invoice #": "72094446", "KIMCO id": 10142},
            kimco_id=10142,
            receipts=[],
        )
