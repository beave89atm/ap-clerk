"""NOTE-47 / Shawn-complete retry for Gas 10165–10172 on PO 59081 / 59006."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from gas_59081_retry import (  # noqa: E402
    CATALOG_BY_DISPLAY,
    FINISH_ORDER,
    HEADERS,
    KNOWN_RECEIPT_IDS,
    SHAWN_MENTION_ID,
    annotate_catalog_part,
    match_open_for_bill,
    merch_lines_for,
    parsed_for,
)
from gas_po_59081_enter import INVOICE_PO_COVER, WANTED, po_cover_amount  # noqa: E402
from gas_shawn_finish import NOTE47_PPV_MAX_ABS, SUCCESS_LEAVE_ALONE, ppv_abs_over_note47  # noqa: E402


def _rec(rid: int, display: str, qty: float, unit: float, *, po: str = "59081") -> dict:
    return {
        "id": rid,
        "po": po,
        "part": display,
        "name": f"PO{po}-GAS AND SUPPLY - 2026/9/22",
        "qty": qty,
        "unit_price": unit,
        "amount": round(qty * unit, 2),
    }


def _pool() -> list[dict]:
    """Shawn 2026-09-22 leftovers: 24332 = 59006-01; 24333–24355 = 59081-01..23."""
    rows = [_rec(24332, "PO59006-01", 880.0, 8.91, po="59006")]
    rid = 24333
    from gas_po_59081_enter import PO_59081_LINES

    for _lid, display, qty, unit, _part in PO_59081_LINES:
        rows.append(_rec(rid, display, qty, unit))
        rid += 1
    return rows


def test_note47_gate_and_17672_under():
    assert NOTE47_PPV_MAX_ABS == 75.00
    assert SHAWN_MENTION_ID == 104
    assert ppv_abs_over_note47(0.52) is False
    assert ppv_abs_over_note47(-0.52) is False
    assert ppv_abs_over_note47(75.00) is True
    cover = po_cover_amount("0040417672")
    pdf = WANTED["0040417672"]["amount"]
    assert round(abs(cover - pdf), 2) == 0.52
    assert abs(cover - pdf) < NOTE47_PPV_MAX_ABS


def test_headers_type3_not_type4_and_order():
    assert HEADERS == {
        "0040430010": 10165,
        "0040424839": 10166,
        "0040438057": 10167,
        "0040438056": 10168,
        "0040438055": 10169,
        "0040438053": 10170,
        "0040417672": 10171,
        "0040414962": 10172,
    }
    assert FINISH_ORDER.index("0040438057") < FINISH_ORDER.index("0040417672")
    assert all(WANTED[inv]["po"] in {"59081", "59006"} for inv in FINISH_ORDER)
    assert set(SUCCESS_LEAVE_ALONE.values()).isdisjoint(HEADERS.values())


def test_annotate_joins_po_line_display_to_catalog_part():
    rec = annotate_catalog_part(_rec(24333, "PO59081-01", 1.0, 14.0))
    assert rec["part"] == "MLW49-56-7240"
    assert rec["po_line"] == "PO59081-01"
    assert CATALOG_BY_DISPLAY["PO59081-04"] == CATALOG_BY_DISPLAY["PO59081-20"] == "PFXPXTW1425R"


def test_0010_matches_unique_1_at_14():
    planned = match_open_for_bill(parsed_for("0040430010"), _pool())
    assert planned["receipt_ids"] == [24333]
    assert planned["select_zero"] is False
    assert planned["over_note47"] is False
    assert planned["ppv"] == 0.0


def test_8057_takes_one_identical_36_12_then_17672_takes_the_other():
    pool = _pool()
    first = match_open_for_bill(parsed_for("0040438057"), pool)
    assert first["select_zero"] is False
    assert first["over_note47"] is False
    assert len(first["receipt_ids"]) == 1
    assert first["receipt_ids"][0] in {24336, 24352}
    taken = set(first["receipt_ids"])
    remain = [r for r in pool if r["id"] not in taken]
    second = match_open_for_bill(parsed_for("0040417672"), remain)
    assert second["select_zero"] is False
    assert second["over_note47"] is False
    assert round(abs(second["ppv"] or 0.0), 2) == 0.52
    leftover_3612 = {24336, 24352} - taken
    assert leftover_3612 <= set(second["receipt_ids"])
    assert taken.isdisjoint(set(second["receipt_ids"]))
    assert len(second["receipt_ids"]) == len(INVOICE_PO_COVER["0040417672"]) == 15


def test_14962_matches_59006_only():
    planned = match_open_for_bill(parsed_for("0040414962"), _pool())
    assert planned["receipt_ids"] == [24332]
    assert planned["ppv"] == 0.0
    assert planned["over_note47"] is False


def test_24839_two_unique_lines():
    planned = match_open_for_bill(parsed_for("0040424839"), _pool())
    assert set(planned["receipt_ids"]) == {24334, 24335}
    assert planned["ppv"] == 0.0


def test_merch_lines_cover_pdf_parts():
    lines = merch_lines_for("0040430010")
    assert lines == [
        {
            "part": "MLW49-56-7240",
            "qty": 1.0,
            "unit_price": 14.0,
            "amount": 14.0,
            "po_line": "PO59081-01",
            "label": "MLW49-56-7240",
            "description": "MLW49-56-7240",
        }
    ]
    assert [ln["part"] for ln in merch_lines_for("0040438056")] == ["MIL269767", "MIL269771"]
    assert KNOWN_RECEIPT_IDS[0] == 24332
    assert KNOWN_RECEIPT_IDS[-1] == 24355
