"""Plain 1007044 Comments_1 notes. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "three_p_1007044_notes_0925",
    Path(__file__).resolve().parents[1] / "scripts" / "three_p_1007044_notes_0925.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

note_text = _MOD.note_text
comment_payload = _MOD.comment_payload
verify_receipt = _MOD.verify_receipt
verify_spec_math = _MOD.verify_spec_math
unchanged_except_new_comment = _MOD.unchanged_except_new_comment
BILLS = _MOD.BILLS
PO_UNIT = _MOD.PO_UNIT


def _receipt(qty: float, cost: float = PO_UNIT, *, po: str = "PO58925-3P INDUSTRIES", invoiced=None):
    return {
        "id": 23414,
        "values": {
            "Quantity_Received": qty,
            "Unit_Cost": cost,
            "Purchase_Cost": cost,
            "PO_Item_Number_$_Unit_Price": cost,
            "Invoiced": invoiced,
            "AP_Invoice_Number": None,
            "PO_Number": {"id": 1, "text": po},
        },
    }


def _line():
    return {"values": {"Work_Order_Number_$_Part_Number": {"text": "1007044-1 - SUBFROME WELDMENT"}, "Unit_Price": PO_UNIT}}


def test_notes_are_plain_and_bill_specific():
    seen = []
    for spec in BILLS:
        text = note_text(spec)
        assert text.startswith("AP Clerk:")
        assert "@" not in text
        assert "Shawn" not in text
        assert "data-mention" not in text
        assert f"PO {spec['pos'][0]['po']} qty {spec['pos'][0]['qty']}" in text
        assert "$361.46" in text and "$97.50" in text
        assert "21 pcs, $5,543.16 total" in text
        seen.append(text)
    assert len(set(seen)) == 4
    assert "receipt 23414" in note_text(BILLS[0])
    assert "$1,055.84" in note_text(BILLS[0])
    assert "receipt 23920 qty 2 + receipt 23921 qty 1" in note_text(BILLS[3])
    assert "$791.88" in note_text(BILLS[3])
    assert verify_spec_math(BILLS[0]) == []
    assert verify_spec_math(BILLS[2]) == []
    assert verify_spec_math(BILLS[3]) == []


def test_payload_appends_without_mention():
    text = note_text(BILLS[1])
    payload = comment_payload(10308, text)
    assert payload["state"] == "Modified"
    assert "values" not in payload
    child = payload["lists"]["Comments_1"][0]
    assert child["state"] == "Added"
    assert child["values"]["HtmlValue"] == text
    assert "Mention" not in child["values"]
    assert child["values"]["HtmlValue"].startswith("AP Clerk:")


def test_qty_or_cost_mismatch_blocks():
    ok = verify_receipt(_receipt(4), _line(), expected_qty=4, expected_po="58925")
    assert ok == []
    bad_qty = verify_receipt(_receipt(3), _line(), expected_qty=4, expected_po="58925")
    assert any("Quantity_Received" in item for item in bad_qty)
    bad_cost = verify_receipt(_receipt(4, 97.50), _line(), expected_qty=4, expected_po="58925")
    assert any("Unit_Cost" in item for item in bad_cost)


def test_fingerprint_allows_only_the_new_comment():
    before = {
        "invoice": "142179",
        "amount": 1465.8,
        "verification": 1855.8,
        "batch": 375,
        "batch_text": "TRANSFER AP",
        "status": 1,
        "posted": None,
        "posting_hold": False,
        "vendor": "999-3P INDUSTRIES",
        "comments_header": "API Agent",
        "lines": [{"id": 1, "qty": 1, "price": 1, "ext": 1, "receipt": 9}],
        "charges": [],
        "comments": [{"id": 1061, "html": "<p>old</p>", "mention": None}],
    }
    after = dict(before)
    after["comments"] = [
        {"id": 1061, "html": "<p>old</p>", "mention": None},
        {"id": 1100, "html": "AP Clerk: new", "mention": None},
    ]
    assert unchanged_except_new_comment(before, after, 1100) == []
    changed = dict(after)
    changed["amount"] = 1
    assert "amount changed" in unchanged_except_new_comment(before, changed, 1100)
