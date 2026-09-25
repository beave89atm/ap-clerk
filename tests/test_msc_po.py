"""MSC needs a PO. Invoice 77062711 is the one miscellaneous exception."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from ap_clerk.kimco import added_comment_payload, replace_comment_payload
from ap_clerk.pdf_invoice import extract_po_numbers, parse_invoice_text
from ap_clerk.rules import (
    MSC_ONE_TIME_MISC_INVOICE,
    MSC_VENDOR_ID,
    SHAWN_MENTION_HTML,
    is_kimco_po_number,
    is_msc_one_time_misc_exception,
    is_msc_vendor,
    is_vending_po_reference,
    missing_po_owner_note,
    msc_one_time_success_comment,
    shawn_mention_html,
    should_transfer_ap_missing_po,
    standing_vendor_entry_decision,
)

MSC_LINES = [
    {"part": "08654303", "description": "GIP3.98-0.20 IC908 ISCAR CUT-GRIP INSERT", "amount": 424.49},
    {"part": "12082277", "description": "1/4X1/4X3/4X2-1/2 4FL SC TIALCN SQ SEM", "amount": 112.32},
    {"part": "45403011", "description": "1/2 90D 8% COB NC SPOTTING DRILL", "amount": 95.96},
    {"part": "45403045", "description": "3/4 90D 8% COB NC SPOTTING DRILL", "amount": 213.04},
    {"part": "63927537", "description": "CNMG431TF IC907 ISCAR CARB TURNING INSERT", "amount": 97.60},
    {"part": "80692387", "description": "CPMT 3-1-PF IC907 ISCAR CBD 11D TURNING INS", "amount": 90.60},
    {"part": "07772080", "description": "3/4X3/4SHX2-1/4X5 CC ACCUPRO TICN CARB 4FL SEM", "amount": 203.77},
]

PDF_SNIP = """
MSC INDUSTRIAL SUPPLY CO.
Invoice Number Purchase Order No.
77062711 VENDING/1570
Sub-Total: 1,237.78
Sales Tax: 0.00
Total: $1,237.78
"""


def _batch():
    spec = importlib.util.spec_from_file_location(
        "batch10_enter_msc",
        Path(__file__).resolve().parents[1] / "scripts" / "batch10_enter.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_vending_reference_is_not_a_kimco_po():
    assert is_vending_po_reference("VENDING/1570")
    assert not is_kimco_po_number("VENDING/1570")
    assert is_kimco_po_number("58913")
    assert "1570" not in extract_po_numbers(PDF_SNIP)
    parsed = parse_invoice_text("Invoice Number: 77062711\n" + PDF_SNIP, filename="77062711.pdf")
    assert parsed["po"] in (None, "")
    assert parsed["invoice_number"] == "77062711"


def test_one_msc_invoice_is_shop_supplies_and_no_other_msc_is():
    assert MSC_VENDOR_ID == 128
    assert MSC_ONE_TIME_MISC_INVOICE == "77062711"
    assert is_msc_vendor("1126-MSC INDUSTRIAL SUPPLY")
    assert is_msc_one_time_misc_exception("MSC Industrial Supply", "77062711", vendor_id=128)
    assert not is_msc_one_time_misc_exception("Tube Supply", "77062711", vendor_id=341)
    assert not is_msc_one_time_misc_exception("MSC Industrial Supply", "77062712", vendor_id=128)
    decision = standing_vendor_entry_decision(
        "MSC Industrial Supply",
        {"invoice_number": "77062711", "total": 1237.78, "lines": MSC_LINES, "printed_po_not_kimco": "VENDING/1570"},
        vendor_id=128,
    )
    assert decision is not None
    assert decision["vendor_rule"] == "msc_one_time_misc"
    assert decision["mode"] == "shop_supplies"
    assert decision["transfer_ap"] is False
    assert decision["missing_po_hold"] is False
    assert decision["post_bill"] is False
    assert decision["ready"] is True
    assert decision["charges_total"] == 1237.78
    assert all(row["description"] == "shop supplies" for row in decision["charges"])
    note = msc_one_time_success_comment(invoice_number="77062711", charges=decision["charges"], pdf_total=1237.78)
    assert note.startswith("AP Clerk:")
    assert "Kyle approved" in note
    assert "Future MSC invoices need a purchase order" in note
    assert "$1,237.78" in note
    assert "$424.49" in note
    assert "$203.77" in note
    assert "not posted" in note
    payload = replace_comment_payload(10363, 1104, note)
    assert payload["lists"]["Comments_1"][0]["id"] == 1104
    other = standing_vendor_entry_decision(
        "MSC Industrial Supply",
        {"invoice_number": "77069999", "total": 50, "lines": [{"description": "Bit", "amount": 50}], "po": "VENDING/1570"},
        vendor_id=128,
    )
    assert other is not None
    assert other["vendor_rule"] == "msc_needs_po"
    assert other["mode"] == "missing_po"
    assert other["enter_charges"] is False
    assert other["charges"] == []
    assert other["transfer_ap"] is True
    assert "MSC Industrial Supply needs a purchase order" in other["note"]
    assert "77069999" in other["note"]
    html = shawn_mention_html(other["note"])
    assert 'data-mention-id="104"' in html
    assert SHAWN_MENTION_HTML in html
    assert html.count("@Shawn McKibben") == 1
    added = added_comment_payload(10363, html)
    assert added["lists"]["Comments_1"][0]["state"] == "Added"
    assert 'data-mention-id="104"' in added["lists"]["Comments_1"][0]["values"]["HtmlValue"]
    with_po = standing_vendor_entry_decision(
        "MSC Industrial Supply",
        {"invoice_number": "77070000", "total": 10, "po": "59074", "lines": [{"amount": 10}]},
        vendor_id=128,
    )
    assert with_po is None


def test_default_no_po_transfers_and_misc_rules_stay_exempt():
    assert should_transfer_ap_missing_po(vendor="Earle M. Jorgensen Co", printed_pos=[])
    assert should_transfer_ap_missing_po(vendor="ENGIE Resources LLC", printed_pos=[])
    assert not should_transfer_ap_missing_po(
        vendor="ENGIE Resources LLC",
        printed_pos=["59074"],
        resolved={"info": {"id": 1}},
    )
    assert not should_transfer_ap_missing_po(vendor="Air Products and Chemicals, Inc", printed_pos=[])
    assert not should_transfer_ap_missing_po(vendor="UniFirst First Aid & Safety", printed_pos=[])
    assert not should_transfer_ap_missing_po(vendor="UniFirst Corporation", printed_pos=[])
    assert not should_transfer_ap_missing_po(
        vendor="MSC Industrial Supply",
        invoice_number="77062711",
        vendor_id=128,
        printed_pos=["VENDING/1570"],
    )
    assert should_transfer_ap_missing_po(
        vendor="MSC Industrial Supply",
        invoice_number="77069999",
        vendor_id=128,
        printed_pos=["VENDING/1570"],
    )
    assert not should_transfer_ap_missing_po(vendor="Priority 1", printed_pos=[], freight=True)
    assert not should_transfer_ap_missing_po(vendor="Gas and Supply", printed_pos=[], gas_misc=True)
    generic = standing_vendor_entry_decision("ENGIE Resources LLC", {"invoice_number": "9", "total": 10, "lines": []})
    assert generic is None
    note = missing_po_owner_note(vendor="ENGIE Resources LLC", invoice_number="9", pdf_total=10)
    assert "@Shawn McKibben is the owner" in note
    assert note.startswith("AP Clerk:")
    # Vendor 341 is Tube Supply, not the First Aid miscellaneous rule.
    tube = standing_vendor_entry_decision(
        "UniFirst First Aid & Safety",
        {"invoice_number": "01178303", "total": 10, "lines": []},
        vendor_id=341,
    )
    assert tube is None


def test_batch_plan_enters_only_the_approved_msc_invoice():
    module = _batch()
    bill = next(row for row in module.BILLS if row["invoice_number"] == "77062711")
    plan = module.plan_bill(bill, [], [])
    assert plan["action"] == "msc_one_time_misc"
    assert plan["missing_po"] is False
    assert plan["transfer_ap"] is False
    assert plan["post_bill"] is False
    assert round(sum(row["amount"] for row in plan["charges"]), 2) == 1237.78
    other = {
        "vendor": "MSC Industrial Supply",
        "vendor_id": 128,
        "invoice_number": "77069999",
        "total": 40.00,
        "po": None,
        "printed_po_not_kimco": "VENDING/1570",
        "lines": [{"description": "Bit", "amount": 40.00}],
        "fees": [],
    }
    held = module.plan_bill(other, [], [])
    assert held["action"] == "missing_po"
    assert held["transfer_ap"] is True
    assert held["charges"] == []
    assert 'data-mention-id="104"' in module.shawn_mention_html(held["note"]) if hasattr(module, "shawn_mention_html") else True
    from ap_clerk.rules import shawn_mention_html as mention

    assert 'data-mention-id="104"' in mention(held["note"])
    plain = {
        "vendor": "ENGIE Resources LLC",
        "invoice_number": "E-1",
        "total": 12.00,
        "po": None,
        "lines": [],
        "fees": [],
    }
    default = module.plan_bill(plain, [], [])
    assert default["action"] == "missing_po"
    assert default["transfer_ap"] is True
    assert "owner" in default["note"]
