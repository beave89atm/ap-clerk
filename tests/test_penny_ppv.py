"""NOTE-56: penny PPV when stored lines + charges miss the header total.

Gas 0040443847 / KIMCO 10284: PDF unit 1.2076 is stored as 1.21, KIMCO
extends 12×1.21=14.52 vs printed 14.49, and verification stays 29.25.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.kimco import (  # noqa: E402
    PPV_CHARGE_CODE,
    PPV_CHARGE_LOOKUP_ID,
    header_penny_ppv_from_record,
    post_header_penny_ppv,
)
from ap_clerk.pdf_invoice import extract_gas_item_lines
from ap_clerk.rules import decide_ppv, penny_ppv_for_header_gap


def test_gas_0040443847_unit_rounds_and_header_gap_is_three_cents():
    text = (
        "PIP250-01-0900 12 0 PIP ZENON Z12 CLEAR LENS PR 1.23 14.76 N\n"
        "PIP250-01-0001 12 0 PIP ZENON Z12 GRAY LENS PR 1.2076 14.49 N\n"
    )
    lines = extract_gas_item_lines(text)
    assert [line["part"] for line in lines] == ["PIP250-01-0900", "PIP250-01-0001"]
    gray = lines[1]
    assert gray["unit_price"] == 1.21
    assert gray["amount"] == 14.49
    assert round(gray["qty"] * gray["unit_price"], 2) == 14.52

    decision = penny_ppv_for_header_gap(
        header_total=29.25,
        line_amounts=[14.76, round(12 * 1.21, 2)],
        charge_amounts=[],
    )
    assert decision["action"] == "ppv"
    assert decision["ppv"] == -0.03
    assert decision["gap"] == -0.03
    assert "Purchase Price Variance" in decision["reason"]


def test_gas_0040446744_positive_three_cent_gap_is_not_a_negative_ppv():
    """0040446744 / KIMCO 10283: verification 1891.88 vs extensions 1891.85.

    The matching PPV is +0.03. A reported -0.03 charge leaves 1891.82.
    """
    lines = [275.50, 42.60, 563.00, 511.00, 11.35, 12.95, 39.34, 20.61, 12.00, 403.50]
    decision = penny_ppv_for_header_gap(header_total=1891.88, line_amounts=lines)
    assert decision["action"] == "ppv"
    assert decision["lines"] == 1891.85
    assert decision["ppv"] == 0.03
    assert decision["gap"] == 0.03

    widened = penny_ppv_for_header_gap(
        header_total=1891.88,
        line_amounts=lines,
        charge_amounts=[-0.03],
    )
    assert widened["action"] == "ppv"
    assert widened["ppv"] == 0.06
    assert round(widened["lines"] + widened["charges"], 2) == 1891.82

    closed = penny_ppv_for_header_gap(
        header_total=1891.88,
        line_amounts=lines,
        charge_amounts=[0.03],
    )
    assert closed["action"] == "match"
    assert closed["ppv"] == 0.0


def test_penny_header_gap_is_not_waived_at_one_or_two_cents():
    one = penny_ppv_for_header_gap(header_total=192.85, line_amounts=[192.86])
    assert one["action"] == "ppv"
    assert one["ppv"] == -0.01
    two = penny_ppv_for_header_gap(header_total=10.02, line_amounts=[10.00])
    assert two["action"] == "ppv"
    assert two["ppv"] == 0.02
    # Exactly two cents stays a per-line match (NOTE-23). One cent does not.
    per_line = decide_ppv(invoice_line_amount=10.02, po_line_amount=10.00, invoice_total=10.02)
    assert per_line["action"] == "match"
    assert per_line["ppv"] == 0.0
    oneal = decide_ppv(invoice_line_amount=192.85, po_line_amount=192.86, invoice_total=192.85)
    assert oneal["action"] == "ppv"
    assert oneal["ppv"] == -0.01


def test_penny_ppv_matches_when_closed_and_holds_at_75():
    closed = penny_ppv_for_header_gap(
        header_total=29.25,
        line_amounts=[29.28],
        charge_amounts=[-0.03],
    )
    assert closed["action"] == "match"
    assert closed["ppv"] == 0.0
    over = penny_ppv_for_header_gap(header_total=100.0, line_amounts=[25.0])
    assert over["action"] == "hold"
    assert over["ppv"] == 0.0
    under = penny_ppv_for_header_gap(header_total=100.0, line_amounts=[25.01])
    assert under["action"] == "ppv"
    assert under["ppv"] == 74.99


def test_record_reader_uses_verification_and_ppv_lookup():
    record = {
        "values": {
            "Invoice_Amount": 29.28,
            "Invoice_Verification_Amount": 29.25,
        },
        "lists": {
            "APInvoiceLine": [
                {"values": {"Quantity": 12, "Unit_Price": 1.23, "Extended_Amount": 14.76}},
                {"values": {"Quantity": 12, "Unit_Price": 1.21, "Extended_Amount": 14.52}},
            ],
            "InvoiceAdditionalCharges": [],
        },
    }
    decision = header_penny_ppv_from_record(record)
    assert decision["action"] == "ppv"
    assert decision["ppv"] == -0.03

    class Client:
        def __init__(self):
            self.ppv = []

        def get_item(self, service, item_id):
            assert service == "ap_invoices"
            assert item_id == 10284
            return record

        def try_post_ppv(self, invoice_id, amount):
            self.ppv.append((invoice_id, amount))
            return "posted"

    posted = post_header_penny_ppv(Client(), 10284)
    assert posted["ppv_status"] == "posted"
    assert posted["mutated"] is True
    assert posted["ppv"] == -0.03
    assert PPV_CHARGE_LOOKUP_ID == 13
    assert PPV_CHARGE_CODE == "Purchase Price Variance"


def test_gas_quality_withholds_success_until_penny_ppv():
    from ap_clerk.quality_v12 import COL_EXCEPTION_CATEGORY
    from gas_supply_0917 import quality_gas_row

    misc = [
        {
            "desc": "PIP ZENON Z12 CLEAR LENS",
            "qty": 12.0,
            "unit": 1.23,
            "ext": 14.76,
            "misc": {"id": 31, "text": "Shop Supplies - G&S-."},
        },
        {
            "desc": "PIP ZENON Z12 GRAY LENS",
            "qty": 12.0,
            "unit": 1.21,
            "ext": 14.52,
            "misc": {"id": 31, "text": "Shop Supplies - G&S-."},
        },
    ]
    parsed = {
        "invoice_number": "0040443847",
        "amount": 29.25,
        "po": None,
        "fees": [],
        "lines": [],
        "graph_message_id": "",
    }
    enter = {
        "Vendor": "Gas and Supply North Texas, LLC",
        "Invoice #": "0040443847",
        "PO": "",
        "Amount": 29.25,
        "Result": "Success",
        "Why": "",
        "KIMCO id": 10284,
    }
    open_gap = quality_gas_row(
        None,
        parsed=parsed,
        enter_row=enter,
        proof={
            "id": 10284,
            "invoice_number": "0040443847",
            "invoice_amount": 29.28,
            "verification_amount": 29.25,
            "invoice_type": 4,
            "vendor_id": 71,
            "attachments": ["0040443847.pdf"],
            "receipt_lines": [],
            "misc_lines": misc,
            "fee_amounts": [],
            "ppv_amounts": [],
        },
        finish={"select_status": "no-po-misc", "select_zero": False, "skipped_over_ppv": False},
        vendor_id=71,
    )
    assert open_gap["Result"] != "Success"

    closed = quality_gas_row(
        None,
        parsed=parsed,
        enter_row=enter,
        proof={
            "id": 10284,
            "invoice_number": "0040443847",
            "invoice_amount": 29.25,
            "verification_amount": 29.25,
            "invoice_type": 4,
            "vendor_id": 71,
            "attachments": ["0040443847.pdf"],
            "receipt_lines": [],
            "misc_lines": misc,
            "fee_amounts": [],
            "ppv_amounts": [-0.03],
        },
        finish={"select_status": "no-po-misc", "select_zero": False, "skipped_over_ppv": False, "ppv_amount": -0.03},
        vendor_id=71,
    )
    assert closed["Result"] == "Success"
    assert closed[COL_EXCEPTION_CATEGORY] == ""
    assert "-0.03" in str(closed.get("PPV"))
