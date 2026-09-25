"""UniFirst First Aid shop supplies, and UniFirst uniforms needs_kyle_review.

API Vendor.id 209 is First Aid. API Vendor.id 189 is uniforms.
API Vendor.id 341 is Tube Supply and is neither rule.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ap_clerk.kimco import replace_comment_payload, shop_supplies_charges_payload
from ap_clerk.pdf_invoice import extract_unifirst_first_aid_bill, parse_invoice_text
from ap_clerk.rules import (
    TUBE_SUPPLY_VENDOR_ID,
    UNIFIRST_FIRST_AID_VENDOR_ID,
    UNIFIRST_UNIFORM_VENDOR_ID,
    air_products_charges_match,
    is_unifirst_first_aid_vendor,
    is_unifirst_uniform_vendor,
    shop_supplies_line_limit,
    standing_vendor_entry_decision,
    unifirst_first_aid_success_comment,
)

PDF_TEXT = """
SALES INVOICE
Invoice Number: IN000037249
UNIFIRST -FIRST AID CORPORATION
Order Number:  42299000082  PO:
AS02 CS  8.25 300.00 6  0  50.0000  0.00Y 6
All Sport ZERO Freezer Bar 3oz Site:  42203
AS38 CS  8.25 706.66 2  0  353.3300  0.00Y 2
All Sport ZERO Powder Stix VAR Site:  42203
Net Invoice $1,006.66
Freight $0.00
$83.05Sales Tax  83.05
Invoice Total $1,089.71
"""


def _batch():
    spec = importlib.util.spec_from_file_location(
        "batch10_enter_unifirst",
        Path(__file__).resolve().parents[1] / "scripts" / "batch10_enter.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _lines(count: int) -> list[dict]:
    return [{"description": f"Item {index}", "amount": 1.00} for index in range(1, count + 1)]


def test_vendor_ids_do_not_mix_first_aid_uniforms_or_tube_supply():
    assert UNIFIRST_FIRST_AID_VENDOR_ID == 209
    assert UNIFIRST_UNIFORM_VENDOR_ID == 189
    assert TUBE_SUPPLY_VENDOR_ID == 341
    assert is_unifirst_first_aid_vendor("1207-UNIFIRST FIRST AID & SAFETY")
    assert is_unifirst_first_aid_vendor("UniFirst Corporation", vendor_id=209)
    assert not is_unifirst_first_aid_vendor("UniFirst First Aid & Safety", vendor_id=189)
    assert not is_unifirst_first_aid_vendor("UniFirst First Aid & Safety", vendor_id=341)
    assert is_unifirst_uniform_vendor("1187-UNIFIRST CORPORATION")
    assert is_unifirst_uniform_vendor("UniFirst")
    assert is_unifirst_uniform_vendor("UniFirst First Aid & Safety", vendor_id=189)
    assert not is_unifirst_uniform_vendor("UniFirst First Aid & Safety")
    assert not is_unifirst_uniform_vendor("1339-Tube Supply")
    assert not is_unifirst_uniform_vendor("UniFirst", vendor_id=341)
    assert not is_unifirst_uniform_vendor("UniFirst", vendor_id=209)


def test_first_aid_under_the_limit_is_shop_supplies_success():
    assert shop_supplies_line_limit() == 10
    decision = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {
            "invoice_number": "IN000037249",
            "total": 1089.71,
            "lines": [
                {"description": "All Sport ZERO Freezer Bar 3oz", "amount": 300.00},
                {"description": "All Sport ZERO Powder Stix VAR", "amount": 706.66},
            ],
            "tax": 83.05,
        },
        vendor_id=209,
    )
    assert decision is not None
    assert decision["vendor_rule"] == "unifirst_first_aid_misc"
    assert decision["mode"] == "shop_supplies"
    assert decision["invoice_type"] == 4
    assert decision["missing_po_hold"] is False
    assert decision["transfer_ap"] is False
    assert decision["post_bill"] is False
    assert decision["finish"] == "Success"
    assert decision["line_count"] == 3
    assert decision["charges_total"] == 1089.71
    assert air_products_charges_match(decision["charges"], 1089.71)
    note = unifirst_first_aid_success_comment(
        invoice_number="IN000037249",
        charges=decision["charges"],
        pdf_total=1089.71,
    )
    assert note.startswith("AP Clerk:")
    assert "UniFirst First Aid & Safety" in note
    assert "shop supplies" in note
    assert "$300.00" in note
    assert "$706.66" in note
    assert "$83.05" in note
    assert "$1,089.71" in note
    assert "not posted" in note
    payload = replace_comment_payload(10357, 1102, note)
    child = payload["lists"]["Comments_1"][0]
    assert child["id"] == 1102
    assert child["state"] == "Modified"
    charges = shop_supplies_charges_payload(decision["charges"], invoice_id=10357)
    assert "Posted" not in charges
    assert round(sum(row["values"]["Amount"] for row in charges["lists"]["InvoiceAdditionalCharges"]), 2) == 1089.71


def test_more_than_ten_lines_is_needs_kyle_review_and_posts_no_charges():
    decision = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {"invoice_number": "IN000099999", "total": 11.00, "lines": _lines(11)},
        vendor_id=209,
    )
    assert decision is not None
    assert decision["hold_reason"] == "needs_kyle_review"
    assert decision["enter_charges"] is False
    assert decision["charges"] == []
    assert decision["line_count"] == 11
    assert decision["line_limit"] == 10
    assert decision["transfer_ap"] is False
    assert decision["post_bill"] is False
    assert "waiting on Kyle's review" in decision["note"]
    assert "11 line items" in decision["note"]
    assert decision["note"].startswith("AP Clerk:")
    ten = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {"invoice_number": "IN10", "total": 10.00, "lines": _lines(10)},
        vendor_id=209,
    )
    assert ten is not None
    assert ten["mode"] == "shop_supplies"
    assert ten["line_count"] == 10
    assert ten["ready"] is True


def test_line_limit_is_configurable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AP_SHOP_SUPPLIES_LINE_LIMIT", "2")
    assert shop_supplies_line_limit() == 2
    decision = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {
            "invoice_number": "IN000037249",
            "total": 1089.71,
            "lines": [
                {"description": "All Sport ZERO Freezer Bar 3oz", "amount": 300.00},
                {"description": "All Sport ZERO Powder Stix VAR", "amount": 706.66},
            ],
            "tax": 83.05,
        },
    )
    assert decision is not None
    assert decision["hold_reason"] == "needs_kyle_review"
    assert decision["line_count"] == 3
    assert decision["line_limit"] == 2
    assert decision["charges"] == []
    air = standing_vendor_entry_decision(
        "Air Products and Chemicals, Inc",
        {
            "total": 2.00,
            "lines": [
                {"description": "Nitrogen", "amount": 1.00},
                {"description": "Delivery", "amount": 1.00},
            ],
        },
    )
    assert air is not None
    assert air["vendor_rule"] == "air_products_misc"
    assert air["mode"] == "shop_supplies"
    assert len(air["charges"]) == 2


def test_uniforms_are_hold_and_tube_supply_is_untouched():
    uniform = standing_vendor_entry_decision(
        "UniFirst Corporation",
        {"invoice_number": "2810791064", "total": 1049.30, "lines": _lines(2)},
        vendor_id=189,
    )
    assert uniform is not None
    assert uniform["vendor_rule"] == "unifirst_uniform_kyle_review"
    assert uniform["hold_reason"] == "needs_kyle_review"
    assert uniform["enter_charges"] is False
    assert uniform["charges"] == []
    assert uniform["transfer_ap"] is False
    assert "uniform" in uniform["note"]
    assert "not UniFirst First Aid" in uniform["note"]
    tube = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {"invoice_number": "01178303", "total": 374.88, "lines": _lines(1), "po": "59093"},
        vendor_id=341,
    )
    assert tube is None
    named = standing_vendor_entry_decision("1339-Tube Supply", {"total": 10, "lines": _lines(1)})
    assert named is None


def test_pdf_lines_for_in000037249_total_the_invoice():
    extracted = extract_unifirst_first_aid_bill(PDF_TEXT)
    assert [row["source"] for row in extracted["charges"]] == [
        "All Sport ZERO Freezer Bar 3oz",
        "All Sport ZERO Powder Stix VAR",
        "Sales Tax",
    ]
    assert air_products_charges_match(extracted["charges"], 1089.71)
    parsed = parse_invoice_text(PDF_TEXT, filename="IN000037249.pdf")
    assert parsed["invoice_number"] == "IN000037249"
    assert parsed["amount"] == 1089.71
    assert parsed["po"] in (None, "")
    assert parsed.get("unifirst_first_aid_misc") is True
    assert len(parsed["shop_supplies_charges"]) == 3
    decision = standing_vendor_entry_decision(parsed["vendor"], parsed, vendor_id=209)
    assert decision is not None
    assert decision["ready"] is True
    assert decision["line_count"] == 3


def test_batch_plan_enters_first_aid_and_holds_uniforms():
    module = _batch()
    bill = next(row for row in module.BILLS if row["invoice_number"] == "IN000037249")
    plan = module.plan_bill(bill, [], [])
    assert plan["action"] == "unifirst_first_aid_misc"
    assert plan["missing_po"] is False
    assert plan["post_bill"] is False
    assert plan["invoice_type"] == 4
    assert plan["transfer_ap"] is False
    assert round(sum(row["amount"] for row in plan["charges"]), 2) == 1089.71
    over = {
        "vendor": "UniFirst First Aid & Safety",
        "vendor_id": 209,
        "invoice_number": "IN-MANY",
        "total": 11.00,
        "po": None,
        "lines": _lines(11),
        "fees": [],
    }
    held = module.plan_bill(over, [], [])
    assert held["action"] == "needs_kyle_review"
    assert held["charges"] == []
    assert held["line_count"] == 11
    assert held["missing_po"] is False
    assert held["transfer_ap"] is False
    uniform = {
        "vendor": "UniFirst Corporation",
        "vendor_id": 189,
        "invoice_number": "2810791064",
        "total": 20.00,
        "po": None,
        "lines": _lines(2),
        "fees": [],
    }
    uniforms = module.plan_bill(uniform, [], [])
    assert uniforms["action"] == "needs_kyle_review"
    assert uniforms["vendor_rule"] == "unifirst_uniform_kyle_review"
    assert uniforms["charges"] == []
    tube = {
        "vendor": "Tube Supply",
        "vendor_id": 341,
        "invoice_number": "01178303",
        "total": 374.88,
        "po": None,
        "lines": _lines(1),
        "fees": [],
    }
    tube_plan = module.plan_bill(tube, [], [])
    assert tube_plan["action"] == "missing_po"
