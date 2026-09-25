"""Air Products invoices are miscellaneous shop-supplies bills. Never missing_po."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from ap_clerk.kimco import replace_comment_payload, shop_supplies_charges_payload
from ap_clerk.pdf_invoice import extract_air_products_bill, parse_invoice_text
from ap_clerk.rules import (
    INVOICE_TYPE_NO_PO,
    air_products_charges_match,
    air_products_entry_decision,
    air_products_success_comment,
    is_air_products_vendor,
    should_transfer_ap_missing_po,
)

PDF_TEXT = """
Invoice
 Air Products and Chemicals, Inc
Invoice No.: 436333415
Date: 09/04/2026
 Invoice Summary
Product Price 1,456.99
Delivery Charge 135.00
Hazmat Charge 120.00
Product Surcharge 6.07
Net value 1,718.06
State Tax 6.25% 107.38
City Tax 2.00% 34.36
Total to be paid USD 1,859.80
Purchase Order Number: NONE
0010 29575 75,884.637 FTS
Nitrogen Liquid
Product Price           19.20  USD 1000  FTS 1,456.99
Delivery Charge            135.00
Hazmat Charge            120.00
Product Surcharge              6.07
State Tax 6.25%            107.38
City Tax 2.00%             34.36
Total to be Paid             1,859.80
"""


def _batch_plan():
    spec = importlib.util.spec_from_file_location(
        "batch10_enter",
        Path(__file__).resolve().parents[1] / "scripts" / "batch10_enter.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_air_products_is_always_miscellaneous_and_never_missing_po():
    assert is_air_products_vendor("Air Products and Chemicals, Inc")
    assert is_air_products_vendor("1011-AIR PRODUCTS", vendor_id=13)
    assert not is_air_products_vendor("UniFirst First Aid & Safety")
    decision = air_products_entry_decision(
        "Air Products and Chemicals, Inc",
        {
            "total": 1859.80,
            "lines": [{"description": "Nitrogen Liquid", "amount": 1456.99}],
            "fees": [
                {"name": "Delivery Charge", "amount": 135.00},
                {"name": "Hazmat Charge", "amount": 120.00},
                {"name": "Product Surcharge", "amount": 6.07},
            ],
            "taxes": [
                {"name": "State Tax", "amount": 107.38},
                {"name": "City Tax", "amount": 34.36},
            ],
        },
    )
    assert decision is not None
    assert decision["invoice_type"] == INVOICE_TYPE_NO_PO == 4
    assert decision["missing_po_hold"] is False
    assert decision["transfer_ap"] is False
    assert decision["select_receipts"] is False
    assert decision["post_bill"] is False
    assert decision["finish"] == "Success"
    assert decision["ready"] is True
    assert decision["charges_total"] == 1859.80
    assert all(row["description"] == "shop supplies" for row in decision["charges"])
    assert [row["source"] for row in decision["charges"]] == [
        "Nitrogen Liquid",
        "Delivery Charge",
        "Hazmat Charge",
        "Product Surcharge",
        "State Tax",
        "City Tax",
    ]
    assert not should_transfer_ap_missing_po(vendor="Air Products and Chemicals, Inc", printed_pos=[])
    assert air_products_entry_decision("UniFirst First Aid & Safety", {"total": 10, "lines": []}) is None


def test_pdf_lines_total_the_invoice_to_the_penny():
    extracted = extract_air_products_bill(PDF_TEXT)
    assert air_products_charges_match(extracted["charges"], 1859.80)
    parsed = parse_invoice_text(PDF_TEXT, filename="436333415.pdf")
    assert parsed["invoice_number"] == "436333415"
    assert parsed["amount"] == 1859.80
    assert parsed["po"] in (None, "")
    assert parsed.get("air_products_misc") is True
    charges = parsed["air_products_charges"]
    assert len(charges) == 6
    assert air_products_charges_match(charges, parsed["amount"])
    assert {row["source"] for row in charges} == {
        "Nitrogen Liquid",
        "Delivery Charge",
        "Hazmat Charge",
        "Product Surcharge",
        "State Tax",
        "City Tax",
    }


def test_additional_charge_payload_is_shop_supplies_and_does_not_post():
    charges = extract_air_products_bill(PDF_TEXT)["charges"]
    payload = shop_supplies_charges_payload(charges, invoice_id=10356)
    assert payload["state"] == "Modified"
    assert payload["id"] == 10356
    assert "Posted" not in payload
    assert "values" not in payload
    children = payload["lists"]["InvoiceAdditionalCharges"]
    assert len(children) == 6
    amounts = []
    for child in children:
        assert child["state"] == "Added"
        assert "Receipt" not in child["values"]
        assert child["values"]["Name"] == "shop supplies"
        assert child["values"]["Quantity"] == 1.0
        assert child["values"]["Price"] == child["values"]["Amount"]
        amounts.append(child["values"]["Amount"])
    assert round(sum(amounts), 2) == 1859.80


def test_success_comment_overwrites_in_place():
    charges = extract_air_products_bill(PDF_TEXT)["charges"]
    note = air_products_success_comment(invoice_number="436333415", charges=charges, pdf_total=1859.80)
    assert note.startswith("AP Clerk:")
    assert "miscellaneous" in note
    assert "standing rule" in note
    assert "shop supplies" in note
    assert "$1,456.99" in note
    assert "$135.00" in note
    assert "$120.00" in note
    assert "$6.07" in note
    assert "$107.38" in note
    assert "$34.36" in note
    assert "$1,859.80" in note
    assert "matches the PDF total" in note
    assert "not posted" in note
    payload = replace_comment_payload(10356, 1101, note)
    child = payload["lists"]["Comments_1"][0]
    assert child["id"] == 1101
    assert child["state"] == "Modified"
    assert child["values"]["HtmlValue"].startswith("<p>AP Clerk:")
    assert "Posted" not in payload


def test_batch_entry_path_does_not_hold_air_products_as_missing_po():
    module = _batch_plan()
    air = next(bill for bill in module.BILLS if bill["invoice_number"] == "436333415")
    plan = module.plan_bill(air, [], [])
    assert plan["action"] == "air_products_misc"
    assert plan["missing_po"] is False
    assert plan["post_bill"] is False
    assert plan["invoice_type"] == 4
    assert round(sum(row["amount"] for row in plan["charges"]), 2) == 1859.80
    other = next(bill for bill in module.BILLS if bill["invoice_number"] == "IN000037249")
    held = module.plan_bill(other, [], [])
    assert held["action"] == "missing_po"
