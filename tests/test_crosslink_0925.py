"""Crosslink September entry: page parse and Quantity_Received choice. No live I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "crosslink_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "crosslink_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

parse_crosslink_pages = _MOD.parse_crosslink_pages
choose_receipts = _MOD.choose_receipts
build_note = _MOD.build_note
html_with_shawn_mention = _MOD.html_with_shawn_mention
open_receipts = _MOD.open_receipts
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "crosslink"


def _pages(name: str) -> list[str]:
    return FIXTURES.joinpath(name).read_text().split("\n----- PAGE -----\n")


def _bill(name: str) -> dict:
    bills, credits = parse_crosslink_pages(_pages(name))
    assert credits == []
    assert len(bills) == 1
    return bills[0]


def _po(po, line, ordered, unit, part):
    return {
        "po": po,
        "po_id": 1,
        "line": line,
        "ordered": ordered,
        "unit": unit,
        "part": part,
        "wo": f"SO000.00.W000 - {part}",
    }


def _rec(rid, po, line, qty, unit, remaining=0):
    return {
        "id": rid,
        "po": po,
        "line": line,
        "qty_received": qty,
        "qty_remaining": remaining,
        "unit": unit,
        "ap": "",
        "invoiced": None,
        "part": "",
    }


def test_second_page_total_stays_with_the_invoice():
    bill = _bill("28273.layout.txt")
    assert bill["invoice_number"] == "28273"
    assert bill["po"] == "59117"
    assert bill["total"] == 2255.98
    assert bill["date"].isoformat() == "2026-09-18"
    assert [(row["part"], row["qty"], row["amount"]) for row in bill["lines"]] == [
        ("1020249-1", 8.0, 1644.64),
        ("1020249-4", 2.0, 589.0),
    ]
    assert bill["fees"][0]["amount"] == 22.34


def test_window_invoices_parse_to_the_penny():
    expect = {
        "28251.layout.txt": ("28251", "59137", 1453.45),
        "28273.layout.txt": ("28273", "59117", 2255.98),
        "28307.layout.txt": ("28307", "59186", 1748.77),
        "28308.layout.txt": ("28308", "59203", 1868.72),
        "28166.layout.txt": ("28166", "59087", 200.0),
    }
    for name, (number, po, total) in expect.items():
        bill = _bill(name)
        assert bill["invoice_number"] == number
        assert bill["po"] == po
        assert bill["total"] == total
        assert bill["type"] == "parts"


def test_sh_invoice_number_drops_the_prefix_and_keeps_the_fee():
    bill = _bill("27756.layout.txt")
    assert bill["invoice_number"] == "27756"
    assert bill["printed_number"] == "SH:27756"
    assert bill["po"] == "58723"
    assert bill["total"] == 1252.18
    assert bill["fees"][0]["amount"] == 12.40
    assert [(row["part"], row["qty"]) for row in bill["lines"]] == [
        ("1020249-1", 3.0),
        ("1020249-2", 3.0),
    ]


def test_statement_is_not_an_invoice():
    bills, credits = parse_crosslink_pages(_pages("statement.layout.txt"))
    assert bills == []
    assert credits == []


def test_credit_memo_is_not_an_invoice():
    bills, credits = parse_crosslink_pages(["Credit Memo\nCredit No: CM100\nTOTAL:\n$-40.00\n"])
    assert bills == []
    assert credits[0]["kind"] == "credit_memo"


def test_quantity_remaining_zero_does_not_drop_an_open_receipt():
    bill = _bill("28308.layout.txt")
    po_lines = [_po("59203", "PO59203-01", 9, 193.94, "1020249-1")]
    receipts = [_rec(24508, "59203", "PO59203-01", 9, 193.94, remaining=0)]
    assert open_receipts(receipts)[0]["id"] == 24508
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "price_variance"
    assert plan["qty_difference"] is False
    assert plan["merch_gap"] == 104.76
    assert plan["receipts"] == []
    assert [row["id"] for row in plan["considered"]] == [24508]


def test_combined_receipts_match_qty_and_gap_over_75_does_not_select():
    bill = _bill("28251.layout.txt")
    po_lines = [_po("59137", "PO59137-01", 7, 193.94, "1020249-1")]
    receipts = [
        _rec(24136, "59137", "PO59137-01", 5, 193.94, remaining=2),
        _rec(24558, "59137", "PO59137-01", 2, 193.94, remaining=0),
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "price_variance"
    assert plan["merch_gap"] == 81.48
    assert plan["receipts"] == []
    assert sorted(row["id"] for row in plan["considered"]) == [24136, 24558]
    note = build_note(
        bill, status="HOLD", action=plan["action"], gap=plan["merch_gap"],
        line_details=plan["line_details"], fee_total=plan["fee_total"],
        received_ext=plan["received_ext"], amount_entered=0,
    )
    assert note.startswith("AP Clerk:")
    assert "@Shawn McKibben" in note
    assert "81.48" in note
    assert "Quantity_Received" in note
    assert "not selected" in note
    assert "Transfer AP" in note
    assert "<" not in note and ">" not in note
    assert "category=" not in note
    html = html_with_shawn_mention(note)
    assert 'data-mention-id="104"' in html
    assert html.count("@Shawn McKibben") == 1
    assert 'data-mention-id="104"' in html.split("@Shawn McKibben", 1)[0]


def test_two_part_price_gap_is_a_hold():
    bill = _bill("28307.layout.txt")
    po_lines = [
        _po("59186", "PO59186-01", 5, 193.94, "1020249-3"),
        _po("59186", "PO59186-02", 2, 205.58, "1020249-1"),
    ]
    receipts = [
        _rec(24509, "59186", "PO59186-01", 5, 193.94),
        _rec(24510, "59186", "PO59186-02", 2, 205.58),
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "price_variance"
    assert plan["merch_gap"] == 350.60
    assert plan["qty_difference"] is False


def test_missing_receipt_when_no_open_row_on_the_part():
    bill = _bill("28251.layout.txt")
    po_lines = [_po("59137", "PO59137-01", 7, 193.94, "1020249-1")]
    receipts = [
        {
            "id": 1,
            "po": "59137",
            "line": "PO59137-01",
            "qty_received": 7,
            "qty_remaining": 7,
            "unit": 193.94,
            "ap": "28100",
            "invoiced": True,
        }
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "missing_receipt"
    assert plan["receipts"] == []


def test_gap_under_75_selects_even_when_quantity_differs():
    bill = _bill("28166.layout.txt")
    po_lines = [_po("59087", "PO59087-01", 8, 25.0, "Customer Touchup")]
    po_lines[0]["part"] = "Customer Touchup"
    receipts = [_rec(23924, "59087", "PO59087-01", 8, 25.0, remaining=0)]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["merch_gap"] == 0.0
    assert plan["receipts"][0]["id"] == 23924
