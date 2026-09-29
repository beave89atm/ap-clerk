"""Type 4 Misc Lines-K payload. No live writes."""

from __future__ import annotations

import pytest

from ap_clerk.kimco import KimcoError
from ap_clerk.misc_lines import (
    collect_shop_supplies_gs_from_records,
    existing_misc_lines_match_pdf,
    is_shop_supplies_gs_name,
    misc_add_item_payload,
    misc_line_description,
    misc_line_snapshot,
    payload_has_receipt,
    type4_shop_supplies_lines_ok,
)


def test_shop_supplies_gs_name_matches_live_texts() -> None:
    assert is_shop_supplies_gs_name("Shop Supplies - G&S-.")
    assert is_shop_supplies_gs_name("5081100 - Shop Supplies - G&S")
    assert is_shop_supplies_gs_name("shop supplies - g&s")
    assert not is_shop_supplies_gs_name("Uniforms & Safety-.")
    assert not is_shop_supplies_gs_name("Tooling/Fixtures-.")
    assert not is_shop_supplies_gs_name("")


def test_collect_unique_shop_supplies_from_live_shaped_records() -> None:
    records = [
        {
            "id": 9966,
            "lists": {
                "APInvoiceLine": [
                    {
                        "values": {
                            "MFG_Miscellaneous_Item": {
                                "id": 31,
                                "text": "Shop Supplies - G&S-.",
                            },
                            "Purchase_GL_Account": {
                                "id": 200,
                                "text": "5081100 - Shop Supplies - G&S",
                            },
                        }
                    }
                ]
            },
        },
        {
            "id": 9965,
            "lists": {
                "APInvoiceLine": [
                    {
                        "values": {
                            "MFG_Miscellaneous_Item": {
                                "id": 28,
                                "text": "Uniforms & Safety-.",
                            },
                            "Purchase_GL_Account": {
                                "id": 57,
                                "text": "6035100 - Uniforms & Safety Equip",
                            },
                        }
                    }
                ]
            },
        },
    ]
    found = collect_shop_supplies_gs_from_records(records)
    assert found["misc_item"] == {"id": 31, "text": "Shop Supplies - G&S-."}
    assert found["gl_account"] == {"id": 200, "text": "5081100 - Shop Supplies - G&S"}


def test_collect_aborts_when_two_shop_supplies_ids() -> None:
    records = [
        {
            "lists": {
                "APInvoiceLine": [
                    {
                        "values": {
                            "MFG_Miscellaneous_Item": {"id": 31, "text": "Shop Supplies - G&S-."}
                        }
                    },
                    {
                        "values": {
                            "MFG_Miscellaneous_Item": {"id": 99, "text": "Shop Supplies - G&S"}
                        }
                    },
                ]
            }
        }
    ]
    with pytest.raises(KimcoError, match="not unique"):
        collect_shop_supplies_gs_from_records(records)


def test_misc_add_item_payload_no_receipt_fuel_not_a_line() -> None:
    payload = misc_add_item_payload(
        [
            {
                "part": "AR90CD300",
                "description": "300 COMP.GAS N.O.S. 2.2 UN1956",
                "qty": 8.0,
                "unit_price": 28.0,
            },
            {
                "part": "ARG300",
                "description": "300 SZ ARGON UN1006 HAZARDOUS",
                "qty": 3.0,
                "unit_price": 30.0,
            },
        ],
        invoice_id=10135,
        vendor_id=71,
        misc_item={"id": 31, "text": "Shop Supplies - G&S-."},
        gl_account={"id": 200, "text": "5081100 - Shop Supplies - G&S"},
    )
    assert payload["id"] == 10135
    assert payload["state"] == "Modified"
    children = payload["lists"]["APInvoiceLine"]
    assert len(children) == 2
    assert payload_has_receipt(payload) is False
    first = children[0]["values"]
    assert first["MFG_Miscellaneous_Item"] == {"id": 31}
    assert first["Purchase_GL_Account"] == {"id": 200}
    assert first["Quantity"] == 8.0
    assert first["Unit_Price"] == 28.0
    assert first["Misc_Description"].startswith("AR90CD300")
    assert first["Invoice_Number"] == {"id": 10135}
    assert first["Vendor"] == {"id": 71}
    assert "Receipt" not in first
    with pytest.raises(KimcoError, match="Receipt"):
        misc_add_item_payload(
            [{"qty": 1, "unit_price": 1, "description": "x", "Receipt": {"id": 1}}],
            invoice_id=10135,
            vendor_id=71,
            misc_item={"id": 31},
        )


def test_misc_line_description_includes_part() -> None:
    assert misc_line_description(
        {"part": "AR90CD300", "description": "300 COMP.GAS N.O.S. 2.2 UN1956"}
    ) == "AR90CD300 300 COMP.GAS N.O.S. 2.2 UN1956"


def test_type4_shop_supplies_lines_ok_rejects_header_only() -> None:
    assert type4_shop_supplies_lines_ok([]) is False
    assert type4_shop_supplies_lines_ok(None) is False
    assert type4_shop_supplies_lines_ok(
        [{"desc": "AR90CD300", "qty": 8, "unit": 28, "misc": {"id": 31, "text": "Shop Supplies - G&S-."}}]
    )
    assert type4_shop_supplies_lines_ok(
        [{"desc": "AR90CD300", "qty": 8, "unit": 28, "misc": {"id": 28, "text": "Uniforms & Safety-."}}]
    ) is False
    record = {
        "lists": {
            "APInvoiceLine": [
                {
                    "id": 20694,
                    "values": {
                        "Misc_Description": "AR90CD300 300 COMP.GAS",
                        "Quantity": 8.0,
                        "Unit_Price": 28.0,
                        "MFG_Miscellaneous_Item": {"id": 31, "text": "Shop Supplies - G&S-."},
                    },
                }
            ]
        }
    }
    snap = misc_line_snapshot(record)
    assert type4_shop_supplies_lines_ok(snap)
    assert existing_misc_lines_match_pdf(snap, [{"qty": 8.0, "unit_price": 28.0}])
    assert not existing_misc_lines_match_pdf(snap, [{"qty": 8.0, "unit_price": 28.0}, {"qty": 1, "unit_price": 1}])
