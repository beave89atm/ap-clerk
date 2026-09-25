"""Fastenal receipt selection and plain notes. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "fastenal_five_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "fastenal_five_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

parse_fastenal_text = _MOD.parse_fastenal_text
choose_receipts = _MOD.choose_receipts
build_note = _MOD.build_note
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fastenal"


def _invoice_from(name: str, number: str) -> dict:
    text = (FIXTURES / name).read_text()
    bills = {bill["invoice_number"]: bill for bill in parse_fastenal_text(text)}
    return bills[number]


def test_pages_balance_to_the_penny():
    expected = {
        "sep04-0.txt": {
            "TXFT4100349": (1150.21, 54.77, "59055"),
            "TXFT4100376": (2334.07, 111.15, "59054"),
        },
        "sep19-0.txt": {
            "TXFT4100503": (268.14, 22.14, "59190"),
            "TXFT4100537": (2265.41, 107.87, "59179"),
        },
        "sep17-0.txt": {
            "TXFT499930": (72.85, 21.98, "58835"),
        },
    }
    for filename, invoices in expected.items():
        text = (FIXTURES / filename).read_text()
        bills = {bill["invoice_number"]: bill for bill in parse_fastenal_text(text)}
        assert set(bills) == set(invoices)
        for number, (total, shipping, po) in invoices.items():
            bill = bills[number]
            assert bill["total"] == total
            assert bill["shipping"] == shipping
            assert bill["po"] == po
            assert round(bill["subtotal"] + bill["shipping"] - bill["total"], 2) == 0


def test_quantity_remaining_zero_still_selects_quantity_received():
    invoice = _invoice_from("sep17-0.txt", "TXFT499930")
    po_lines = [
        {
            "po": "58835",
            "line": "PO58835-01",
            "ordered": 30,
            "fastenal": "94404",
            "desc": "Fastenal 94404",
            "unit": 1.6955,
        }
    ]
    receipts = [
        {
            "id": 22921,
            "po": "58835",
            "line": "PO58835-01",
            "qty_received": 30,
            "qty_remaining": 0,
            "unit": 1.6955,
            "ap": "",
            "invoiced": None,
        }
    ]
    plan = choose_receipts(invoice, po_lines, receipts)
    assert plan["action"] == "select"
    assert [row["id"] for row in plan["receipts"]] == [22921]
    assert abs(plan["merch_gap"]) < 75


def test_partial_receipts_sum_quantity_received():
    invoice = _invoice_from("sep04-0.txt", "TXFT4100376")
    parts = {
        "PO59054-01": ("4203563", 34, 7.4),
        "PO59054-02": ("99472056", 34, 2.46),
        "PO59054-03": ("466232", 34, 2.53),
        "PO59054-04": ("65348", 34, 27.79),
        "PO59054-05": ("0472471", 102, 8.4),
    }
    splits = {
        "PO59054-01": [2, 13, 2, 8, 8, 1],
        "PO59054-02": [2, 13, 2, 8, 8, 1],
        "PO59054-03": [2, 13, 2, 8, 8, 1],
        "PO59054-04": [2, 13, 2, 8, 8, 1],
        "PO59054-05": [6, 39, 24, 24, 3, 6],
    }
    po_lines = []
    receipts = []
    rid = 23842
    for line, (part, ordered, unit) in parts.items():
        po_lines.append(
            {
                "po": "59054",
                "line": line,
                "ordered": ordered,
                "fastenal": part,
                "desc": f"Fastenal {part}",
                "unit": unit,
            }
        )
        for qty in splits[line]:
            receipts.append(
                {
                    "id": rid,
                    "po": "59054",
                    "line": line,
                    "qty_received": qty,
                    "qty_remaining": 0,
                    "unit": unit,
                    "ap": "",
                    "invoiced": None,
                }
            )
            rid += 1
    plan = choose_receipts(invoice, po_lines, receipts)
    assert plan["action"] == "select"
    assert len(plan["receipts"]) == 30
    assert abs(plan["merch_gap"]) < 0.05


def test_zero_quantity_received_is_missing_receipt():
    invoice = _invoice_from("sep19-0.txt", "TXFT4100503")
    po_lines = [
        {
            "po": "59190",
            "line": "PO59190-01",
            "ordered": 250,
            "fastenal": "33819",
            "desc": "Fastenal 33819",
            "unit": 0.624,
        },
        {
            "po": "59190",
            "line": "PO59190-02",
            "ordered": 50,
            "fastenal": "15323",
            "desc": "Fastenal 15323",
            "unit": 1.8,
        },
    ]
    plan = choose_receipts(invoice, po_lines, [])
    assert plan["action"] == "missing_receipt"
    assert plan["receipts"] == []
    assert {row["received"] for row in plan["problems"]} == {0}
    note = build_note(invoice, status="HOLD", action="missing_receipt", problems=plan["problems"], amount_entered=0)
    assert note.startswith("AP Clerk: @Shawn McKibben")
    assert "category=" not in note
    assert "<" not in note
    assert "Quantity_Received is 0" in note
    assert "receive the invoiced quantity" in note
    assert "Transfer AP" in note
    assert "$268.14" in note


def test_over_ppv_does_not_select():
    invoice = _invoice_from("sep17-0.txt", "TXFT499930")
    po_lines = [
        {
            "po": "58835",
            "line": "PO58835-01",
            "ordered": 30,
            "fastenal": "94404",
            "desc": "Fastenal 94404",
            "unit": 20,
        }
    ]
    receipts = [
        {
            "id": 22921,
            "po": "58835",
            "line": "PO58835-01",
            "qty_received": 30,
            "qty_remaining": 99,
            "unit": 20,
            "ap": "",
            "invoiced": None,
        }
    ]
    plan = choose_receipts(invoice, po_lines, receipts)
    assert plan["action"] == "price_variance"
    assert plan["receipts"] == []
    assert abs(plan["merch_gap"]) >= 75


def test_success_note_is_plain_english():
    invoice = _invoice_from("sep17-0.txt", "TXFT499930")
    note = build_note(
        invoice,
        status="Success",
        action="select",
        receipt_rows=[{"id": 22921, "qty_received": 30, "unit": 1.6955}],
        amount_entered=72.85,
        ppv=0,
    )
    assert note.startswith("AP Clerk:")
    assert "category=" not in note
    assert "owner=" not in note
    assert "<" not in note
    assert "22921 qty 30 @ 1.6955" in note
    assert "$0.00" in note
    assert "not posted" in note
