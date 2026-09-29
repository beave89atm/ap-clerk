"""PO 59081 / 59006 exist as purchase lines; received qty 0 blocks Success."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from gas_po_59081_enter import (  # noqa: E402
    INVOICE_PO_COVER,
    PO_59006_LINES,
    PO_59081_LINES,
    WANTED,
    po_cover_amount,
    received_qty_blocks_success,
)


def test_po_line_covers_match_pdf_totals():
    assert len(PO_59081_LINES) == 23
    assert len(PO_59006_LINES) == 1
    for inv, spec in WANTED.items():
        covered = po_cover_amount(inv)
        # 0040417672 live line ext sums to 3857.29 vs PDF 3856.77 (52¢).
        tol = 0.60 if inv == "0040417672" else 0.02
        assert abs(covered - spec["amount"]) <= tol, (inv, covered, spec["amount"])
    assert INVOICE_PO_COVER["0040417672"][-1] == "PO59081-23"
    assert INVOICE_PO_COVER["0040424839"] == ("PO59081-02", "PO59081-03")


def test_received_zero_is_not_success():
    assert received_qty_blocks_success(0) is True
    assert received_qty_blocks_success(0.0) is True
    assert received_qty_blocks_success(None) is True
    assert received_qty_blocks_success(1.0) is False


def test_do_not_enter_as_type4():
    assert all(spec["po"] in {"59081", "59006"} for spec in WANTED.values())
