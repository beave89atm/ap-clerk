"""O'Neal finish-out inventory helpers."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from oneal_finish_0922 import (
    KNOWN_IDS,
    PPV_MAX_ABS,
    classify_before,
    inventory_ids,
    merge_finish_rows,
    po_from_text,
    rounding_ppv_needed,
)
from oneal_plus10_0922 import KNOWN_BATCH_ID, PREFERRED_BATCH_NAME


def test_known_ids_cover_first_five_and_plus10():
    assert 10160 in KNOWN_IDS and 10164 in KNOWN_IDS
    assert 10174 in KNOWN_IDS and 10180 in KNOWN_IDS
    assert KNOWN_BATCH_ID == 722
    assert PREFERRED_BATCH_NAME == "API Agent - 9/21/26 O'Neal"
    assert PPV_MAX_ABS == 75.0


def test_po_from_kimco_display():
    assert po_from_text("PO59068-ONEAL STEEL, LLC.") == "59068"
    assert po_from_text("59156") == "59156"
    assert po_from_text("") == ""


def test_rounding_ppv_under_75():
    assert rounding_ppv_needed({"amount": 687.36, "verification": 687.26}) == -0.10
    assert rounding_ppv_needed({"amount": 67.38, "verification": 67.38}) == 0.0
    assert rounding_ppv_needed({"amount": 0, "verification": 14543.16}) is None
    assert rounding_ppv_needed({"amount": 100, "verification": 200}) is None


def test_classify_success_vs_hold():
    assert (
        classify_before(
            {"amount": 471.42, "verification": 471.42, "receipt_n": 1},
            {"Result": "Success"},
        )
        == "Success"
    )
    assert (
        classify_before(
            {"amount": 687.36, "verification": 687.26, "receipt_n": 1},
            {"Result": "HOLD"},
        )
        == "HOLD rounding"
    )
    assert (
        classify_before(
            {"amount": 0.0, "verification": 23151.42, "receipt_n": 0},
            {"Result": "HOLD", "Exception category": "missing_receipt"},
        )
        == "HOLD missing_receipt"
    )
    assert (
        classify_before(
            {"amount": 0.0, "verification": 14543.16, "receipt_n": 0},
            {"Result": "HOLD", "Exception category": "price_variance"},
        )
        == "HOLD price_variance"
    )


def test_merge_updates_first_five():
    prior = [{"Invoice #": "15457895", "Result": "HOLD", "KIMCO id": 10161}]
    new = [{"Invoice #": "15457895", "Result": "Success", "KIMCO id": 10161}]
    merged = merge_finish_rows(prior, new)
    assert merged[0]["Result"] == "Success"


def test_inventory_ids_union_known_and_entered():
    ids = inventory_ids({"15457895": 10161, "14748440": 10050})
    assert 10160 in ids and 10180 in ids
    assert 10161 in ids and 10050 in ids
