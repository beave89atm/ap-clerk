"""EMJ layout parse and receipt choice. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "emj_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "emj_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

parse_emj_layout = _MOD.parse_emj_layout
parse_emj_credits = _MOD.parse_emj_credits
choose_receipts = _MOD.choose_receipts
build_note = _MOD.build_note
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emj"


def _all_bills() -> dict:
    bills = []
    for path in sorted(FIXTURES.glob("*.layout.txt")):
        bills.extend(parse_emj_layout(path.read_text()))
    return {bill["invoice_number"]: bill for bill in bills}


def test_window_parses_fifteen_invoices_and_every_pdf_has_a_po():
    bills = _all_bills()
    assert len(bills) == 15
    # The starting list called these four no-PO. The PDF header has the PO.
    for number, po, total in (
        ("B154229432", "59208", 907.20),
        ("S816419432", "59257", 276.60),
        ("T607753432", "59108", 4756.01),
        ("T608354432", "59198", 993.60),
    ):
        assert bills[number]["po"] == po
        assert bills[number]["total"] == total
        assert bills[number]["type"] == "parts"
    assert round(sum(bill["total"] for bill in bills.values()), 2) == 13582.00


def test_second_page_invoices_are_not_dropped():
    bills = _all_bills()
    assert bills["T607834432"]["po"] == "59122"
    assert bills["T607834432"]["total"] == 607.04
    assert bills["S816212432"]["total"] == 261.66
    assert bills["T608428432"]["total"] == 1263.28
    assert bills["S816504432"]["total"] == 312.00


def test_credit_memo_is_not_an_invoice():
    text = (FIXTURES / "10-Invoices.pdf.layout.txt").read_text()
    assert parse_emj_layout(text) == []
    credits = parse_emj_credits(text)
    assert credits[0]["invoice_number"] == "C120102432"
    assert credits[0]["amount"] == -475.06
    assert credits[0]["original_invoice"] == "T607621"


def test_feet_invoice_selects_inch_receipts_when_dollars_match():
    bill = _all_bills()["B154229432"]
    po_lines = [
        {"po": "59208", "line": "PO59208-01", "line_no": 1, "ordered": 576, "unit": 0.7875,
         "part": "RT-1.25 X 1.00 X 0.13-6061", "desc": "Round Tube 1.25 x 1 x .13 6061"},
        {"po": "59208", "line": "PO59208-02", "line_no": 2, "ordered": 576, "unit": 0.7875,
         "part": "RT-1.25 X 1.00 X 0.13-6061", "desc": "Round Tube 1.25 x 1 x .13 6061"},
    ]
    receipts = [
        {"id": 24281, "po": "59208", "line": "PO59208-01", "qty_received": 576, "unit": 0.7875, "ap": "", "invoiced": None, "qty_remaining": 0},
        {"id": 24280, "po": "59208", "line": "PO59208-02", "qty_received": 576, "unit": 0.7875, "ap": "", "invoiced": None, "qty_remaining": 0},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["merch_gap"] == 0.0
    assert sorted(row["id"] for row in plan["receipts"]) == [24280, 24281]


def test_quantity_remaining_does_not_drop_an_open_receipt():
    bill = _all_bills()["S815368432"]
    po_lines = [
        {"po": "59122", "line": "PO59122-02", "line_no": 2, "ordered": 228.6, "unit": 1.5835,
         "part": "RT-2.50 X 2.00 X 0.25-A513-DOM", "desc": "2.500 OD X 2.000 ID"},
    ]
    receipts = [
        {"id": 23878, "po": "59122", "line": "PO59122-02", "qty_received": 228.6, "unit": 1.5835,
         "ap": "", "invoiced": None, "qty_remaining": 0},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["merch_gap"] == 37.75
    assert plan["receipts"][0]["id"] == 23878


def test_gap_at_least_75_does_not_select():
    bill = _all_bills()["S815368432"]
    bill = {**bill, "total": 500.00, "subtotal": 500.00}
    po_lines = [
        {"po": "59122", "line": "PO59122-02", "line_no": 2, "ordered": 228.6, "unit": 1.5835,
         "part": "RT-2.50 X 2.00 X 0.25-A513-DOM", "desc": "2.500 OD X 2.000 ID"},
    ]
    receipts = [
        {"id": 23878, "po": "59122", "line": "PO59122-02", "qty_received": 228.6, "unit": 1.5835, "ap": "", "invoiced": None},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] in {"price_variance", "quantity_variance"}
    assert plan["receipts"] == []
    assert abs(plan["merch_gap"]) >= 75
    note = build_note(bill, status="HOLD", action=plan["action"], problems=plan["problems"], amount_entered=0, gap=plan["merch_gap"])
    assert note.startswith("AP Clerk:")
    assert "@Shawn McKibben" in note
    assert "<" not in note and ">" not in note
    assert "Transfer AP" in note


def test_missing_receipt_hold_names_shawn():
    bill = _all_bills()["T608428432"]
    po_lines = [
        {"po": "59207", "line": "PO59207-01", "line_no": 1, "ordered": 240, "unit": 4.67,
         "part": "RT-5.00 X 4.00 X 0.50-1026-DOM", "desc": "5 inch tube"},
    ]
    plan = choose_receipts(bill, po_lines, [])
    assert plan["action"] == "missing_receipt"
    assert plan["receipts"] == []
    note = build_note(bill, status="HOLD", action="missing_receipt", problems=plan["problems"], amount_entered=0)
    assert note.startswith("AP Clerk: @Shawn McKibben")
    assert "Quantity_Received" in note
    assert "<" not in note


def test_already_invoiced_receipt_is_not_selected():
    bill = _all_bills()["T608354432"]
    po_lines = [
        {"po": "59198", "line": "PO59198-01", "line_no": 1, "ordered": 960, "unit": 1.035,
         "part": "RCT-3.00 X 2.00 X 0.188-A500", "desc": "3 x 2 x .188 A500"},
    ]
    receipts = [
        {"id": 24460, "po": "59198", "line": "PO59198-01", "qty_received": 960, "unit": 1.035, "ap": "OLD", "invoiced": True},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "missing_receipt"
