"""JP Steel September entry: page parse and Quantity_Received choice. No live I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "jpsteel_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "jpsteel_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

parse_jpsteel_layout = _MOD.parse_jpsteel_layout
parse_jpsteel_pages = _MOD.parse_jpsteel_pages
choose_receipts = _MOD.choose_receipts
build_note = _MOD.build_note
html_with_shawn_mention = _MOD.html_with_shawn_mention


def _page(number, po, day, body, total):
    return f"""
JP Steel
Invoice No: {number}
Customer P.O.#: {po}
Invoice Date: {day}
{body}
Total ${total}
"""


INCH = _page(
    "125362",
    "58937",
    "9/17/26",
    """
   1 2,570.00"    10      P     1.250 X 0.120 1020 DOM                                       21.42'            257"    310.16        $0.38     I             $976.60    E
   2    745.00"    3      P     0.750 X 0.065 1020 DOM                                       20.69'       248.3331"     29.53        $0.38     I             $283.10    E
""",
    "1,259.70",
)

FOOT_SHORT = _page(
    "125364",
    "59131",
    "9/17/26",
    """
   1    20.50'     1      P   3.750 X 0.750 A519 HFS                                        20.5'           246"    492.62       $58.94       F         $1,208.27    E
""",
    "1,208.27",
)

EACH_BAR = _page(
    "125365",
    "59152",
    "9/17/26",
    """
   1              9       P     6.5 4130 NQT Round Bar                                                     4.375"    371.53       $82.50     E             $742.50    E
""",
    "742.50",
)

PAGE1 = """
Invoice No: 125366
Customer P.O.#: 59163
Invoice Date: 9/17/26
   1    80.00'     4      P     0.375 A36 Round Bar                                                  20'          240"     30.15        $0.88     F              $70.40    E
   2    43.83'     2      P     1.750 X 0.375 1026 DOM                                           21.92'           263"    241.38       $15.55     F             $681.61    E
   3    80.00'     4      P     1 X 1 X 0.125 A513 TUBE -                                            20'          240"    119.00        $3.33     F             $266.40    E
                                SQUARE/REC
   4    360.00'   18      P     2 X 1 X 0.120 A513                                                   20'          240"    810.72        $3.72     F           $1,339.20    E
"""

PAGE2 = """
Invoice No: 125366
Customer P.O.#: 59163
Invoice Date: 9/17/26
Subtotal Non Taxable                    $2,357.61
Total                   $2,357.61
"""


def test_later_page_total_stays_with_the_invoice():
    bills, credits = parse_jpsteel_pages([PAGE1, PAGE2])
    assert credits == []
    assert len(bills) == 1
    assert bills[0]["invoice_number"] == "125366"
    assert bills[0]["total"] == 2357.61
    assert len(bills[0]["lines"]) == 4
    assert parse_jpsteel_layout(PAGE2) is None


def test_credit_memo_is_not_an_invoice():
    bills, credits = parse_jpsteel_pages(["Credit Memo\nCredit No: CM100\nTotal $-40.00\n"])
    assert bills == []
    assert credits[0]["kind"] == "credit_memo"


def test_inch_lines_select_by_quantity_received_and_ignore_remaining():
    bill = parse_jpsteel_layout(INCH)
    po_lines = [
        {"po": "58937", "line": "PO58937-01", "ordered": 2313, "unit": 0.3785, "part": "RT-1.25 X 1.00 X 0.12-A513", "desc": "Round Tube"},
        {"po": "58937", "line": "PO58937-02", "ordered": 745, "unit": 0.375, "part": "RT-0.75 X 0.625 X 0.063-A513", "desc": "Round Tube"},
        {"po": "58937", "line": "PO58937-03", "ordered": 576, "unit": 0.6598, "part": "FB-0.25 X 1.50-6061", "desc": "Flat Bar"},
    ]
    receipts = [
        {"id": 24288, "po": "58937", "line": "PO58937-01", "qty_received": 2570, "qty_remaining": -257, "unit": 0.3785, "ap": "", "invoiced": None},
        {"id": 24289, "po": "58937", "line": "PO58937-02", "qty_received": 745, "qty_remaining": 0, "unit": 0.375, "ap": "", "invoiced": None},
        {"id": 23559, "po": "58937", "line": "PO58937-03", "qty_received": 576, "unit": 0.6598, "ap": "125051", "invoiced": True},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["merch_gap"] == 7.57
    assert plan["qty_difference"] is False
    assert [row["id"] for row in plan["receipts"]] == [24288, 24289]


def test_quantity_difference_under_75_selects_and_says_so():
    bill = parse_jpsteel_layout(FOOT_SHORT)
    po_lines = [
        {"po": "59131", "line": "PO59131-02", "ordered": 240, "unit": 4.9117, "part": "RT-3.75 X 2.25 X 0.75-A519", "desc": "3.75 tube"},
    ]
    receipts = [
        {"id": 24270, "po": "59131", "line": "PO59131-02", "qty_received": 240, "qty_remaining": 0, "unit": 4.9117, "ap": "", "invoiced": None},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["qty_difference"] is True
    assert plan["merch_gap"] == 29.46
    note = build_note(
        bill, status="Success", action="select", receipt_rows=plan["receipts"],
        amount_entered=bill["total"], ppv=plan["merch_gap"], gap=plan["merch_gap"],
        lines=plan["lines"], qty_difference=True, qty_details=plan["qty_details"],
    )
    assert note.startswith("AP Clerk:")
    assert "quantity difference" in note
    assert "246" in note and "240" in note
    assert "<" not in note and ">" not in note
    assert "@Shawn" not in note


def test_gap_at_least_75_does_not_select():
    bill = parse_jpsteel_layout(FOOT_SHORT)
    bill = {**bill, "total": 2000.00, "subtotal": 2000.00}
    po_lines = [
        {"po": "59131", "line": "PO59131-02", "ordered": 240, "unit": 4.9117, "part": "RT-3.75 X 2.25 X 0.75-A519", "desc": ""},
    ]
    receipts = [
        {"id": 24270, "po": "59131", "line": "PO59131-02", "qty_received": 240, "unit": 4.9117, "ap": "", "invoiced": None},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] in {"price_variance", "quantity_variance"}
    assert plan["receipts"] == []
    assert abs(plan["merch_gap"]) >= 75
    note = build_note(bill, status="HOLD", action=plan["action"], problems=plan["problems"], amount_entered=0)
    assert note.startswith("AP Clerk: @Shawn McKibben")
    assert "Transfer AP" in note
    html = html_with_shawn_mention(note)
    assert 'data-mention-id="104"' in html
    assert "Mention" not in html


def test_piece_cuts_combine_same_item_receipts():
    bill = parse_jpsteel_layout(EACH_BAR)
    po_lines = [
        {"po": "59152", "line": f"PO59152-0{i}", "ordered": qty, "unit": 18.857, "part": "RB-6.50-4130-NQT", "desc": "6.50 DIA 4130 NQT"}
        for i, qty in enumerate([13.125, 8.75, 8.75, 8.75], start=1)
    ]
    receipts = [
        {"id": 24249 + i, "po": "59152", "line": f"PO59152-0{i}", "qty_received": qty, "qty_remaining": 0, "unit": 18.857, "ap": "", "invoiced": None}
        for i, qty in enumerate([13.125, 8.75, 8.75, 8.75], start=1)
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert plan["qty_difference"] is False
    assert [row["id"] for row in plan["receipts"]] == [24250, 24251, 24252, 24253]


def test_square_and_rectangle_do_not_share_a_receipt():
    bills, _credits = parse_jpsteel_pages([PAGE1, PAGE2])
    bill = bills[0]
    po_lines = [
        {"po": "59163", "line": "PO59163-01", "ordered": 960, "unit": 0.0734, "part": "RB-0.375-A36", "desc": "Round Bar 3/8 A36"},
        {"po": "59163", "line": "PO59163-02", "ordered": 526, "unit": 1.2959, "part": "RT-1.75 X 1.00 X 0.375-A513-1026 DOM", "desc": "Round Tube"},
        {"po": "59163", "line": "PO59163-03", "ordered": 960, "unit": 0.2775, "part": "ST-1.00 X 0.125-A513", "desc": 'Square Tube 1" x 1/8" A500'},
        {"po": "59163", "line": "PO59163-04", "ordered": 4320, "unit": 0.31, "part": "RCT-2.00 X 1.00 X 0.13-A513", "desc": 'Rectangular Tube 2" x 1" x 0.13"'},
    ]
    receipts = [
        {"id": 24284, "po": "59163", "line": "PO59163-01", "qty_received": 960, "unit": 0.0734, "ap": "", "invoiced": None},
        {"id": 24285, "po": "59163", "line": "PO59163-02", "qty_received": 526, "unit": 1.2959, "ap": "", "invoiced": None},
        {"id": 24286, "po": "59163", "line": "PO59163-03", "qty_received": 960, "unit": 0.2775, "ap": "", "invoiced": None},
        {"id": 24287, "po": "59163", "line": "PO59163-04", "qty_received": 4320, "unit": 0.31, "ap": "", "invoiced": None},
    ]
    plan = choose_receipts(bill, po_lines, receipts)
    assert plan["action"] == "select"
    assert [row["id"] for row in plan["receipts"]] == [24284, 24285, 24286, 24287]
    assert plan["qty_difference"] is False


def test_missing_receipt_hold_names_shawn():
    bill = parse_jpsteel_layout(
        _page(
            "125499",
            "59230",
            "9/24/26",
            '   1    180.00\'    9      P   6 X 4 X 0.312 A500 B/C                                            20\'          240"   3,434.40      $31.88     F           $5,738.40    E',
            "5,738.40",
        )
    )
    po_lines = [
        {"po": "59230", "line": "PO59230-01", "ordered": 2160, "unit": 2.6567, "part": "RCT-6.00 X 4.00 X 0.313-A500", "desc": ""},
    ]
    plan = choose_receipts(bill, po_lines, [])
    assert plan["action"] == "missing_receipt"
    assert plan["receipts"] == []
    note = build_note(bill, status="HOLD", action="missing_receipt", problems=plan["problems"], amount_entered=0)
    assert "Quantity_Received" in note
    assert "@Shawn McKibben" in note
    assert "<" not in note
