"""NOTE-58 / NOTE-59: qty/UOM over the PPV limit, and plain-English HOLD notes."""

from __future__ import annotations

from ap_clerk.quality_v12 import (
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
    note_by_id,
)
from ap_clerk.receiving_owners import missing_receipt_comment_text
from ap_clerk.rules import (
    HOLD_NOTE_CATEGORIES,
    SHAWN_USER_ID,
    assess_bill_qty_uom,
    comments_1_html,
    plain_hold_note,
    ppv_limit,
    qty_uom_disconnect_over_ppv,
    sheet_note_problems,
)
from ap_clerk.transfer_ap import apply_qty_uom_transfer_ap, should_transfer_ap_qty_uom


def test_notes_registered():
    qty = note_by_id("NOTE-58")
    style = note_by_id("NOTE-59")
    assert qty["slug"] == "qty-uom-over-ppv-transfer-shawn"
    assert "Shawn" in qty["expected"]
    assert "buyer" in qty["expected"]
    assert style["slug"] == "plain-english-hold-notes"
    assert "category=" in style["expected"]
    assert "AP Clerk:" in style["expected"]


def test_oneal_14400_vs_1200_is_shawn_transfer_not_buyer():
    decision = qty_uom_disconnect_over_ppv(
        invoice_qty=1200,
        receipt_qty=14400,
        po_qty=1200,
        invoice_uom="IN",
        receipt_uom="IN",
        po_uom="IN",
        unit_price=0.1665,
        invoice_extended=199.73,
        receipt_extended=2397.60,
        invoice_total=257.46,
        amount_entered=57.72,
        line="2",
        po="59059",
        receipt="23563",
        invoice_number="15455478",
    )
    assert decision["hold"] is True
    assert decision["transfer_ap"] is True
    assert should_transfer_ap_qty_uom(decision) is True
    assert decision["category"] == "quantity_variance"
    assert decision["owner"] == "Shawn McKibben"
    assert decision["owner"] != "buyer"
    assert decision["mention_id"] == SHAWN_USER_ID == 104
    assert decision["gap"] >= ppv_limit()
    assert decision["gap"] == 2197.87
    note = decision["note"]
    assert sheet_note_problems(note) == []
    assert "category=" not in note
    assert "owner=" not in note
    assert "<" not in note
    assert "buyer" not in note.lower()
    assert "257.46" in note
    assert "57.72" in note
    assert "59059" in note
    assert "23563" in note
    assert "14,400" in note
    assert "1,200" in note
    row = apply_exception_category_owner({"Result": "HOLD", "Why": note})
    assert row[COL_EXCEPTION_CATEGORY] == "quantity_variance"
    assert row[COL_EXCEPTION_OWNER] == "Shawn McKibben"
    assert "category=" not in row["Why"]


def test_under_limit_qty_mismatch_stays_with_buyer():
    decision = qty_uom_disconnect_over_ppv(
        invoice_qty=10,
        receipt_qty=9,
        po_qty=10,
        unit_price=1.0,
        invoice_total=10,
        amount_entered=9,
    )
    assert decision["gap"] == 1.0
    assert decision["hold"] is False
    assert decision["transfer_ap"] is False
    assert decision["owner"] == ""
    assert should_transfer_ap_qty_uom(decision) is False
    under = plain_hold_note(
        "quantity_variance",
        over_ppv=False,
        qty_received=9,
        qty_invoiced=10,
        qty_ordered=10,
        gap=1,
        invoice_total=10,
        amount_entered=9,
        po="100",
        receipt="5",
        line="1",
        invoice_number="1",
    )
    assert sheet_note_problems(under) == []
    assert "buyer" in under.lower()
    row = apply_exception_category_owner(
        {"Result": "HOLD", "Why": "invoice qty does not match PO/receipt qty (line invoice qty 10 vs PO/receipt qty 9)."}
    )
    assert row[COL_EXCEPTION_OWNER] == "buyer"


def test_limit_boundary_and_uom_disconnect():
    at_limit = qty_uom_disconnect_over_ppv(
        invoice_qty=1,
        receipt_qty=2,
        po_qty=1,
        unit_price=75,
    )
    assert at_limit["gap"] == 75.0
    assert at_limit["hold"] is True
    assert at_limit["owner"] == "Shawn McKibben"
    under = qty_uom_disconnect_over_ppv(
        invoice_qty=1,
        receipt_qty=2,
        po_qty=1,
        unit_price=74.99,
    )
    assert under["hold"] is False
    uom = qty_uom_disconnect_over_ppv(
        invoice_qty=10,
        receipt_qty=10,
        po_qty=10,
        invoice_uom="IN",
        receipt_uom="FT",
        invoice_extended=100,
        receipt_extended=1200,
        po="59059",
        receipt="23563",
    )
    assert uom["uom_disconnect"] is True
    assert uom["qty_disconnect"] is False
    assert uom["hold"] is True
    assert uom["gap"] == 1100.0
    assert "unit of measure" in uom["note"].lower() or "quantity does not match" in uom["note"].lower()


def test_matching_receipt_is_not_this_hold():
    decision = assess_bill_qty_uom(
        invoice_lines=[{"part": "FAST-A", "qty": 35, "unit_price": 30.99, "amount": 1084.65}],
        po_lines=[{"part": "FAST-A", "qty": 36, "unit_price": 30.99}],
        receipts=[
            {"id": 3601, "part": "FAST-A", "qty": 36, "unit_price": 30.99, "amount": 1115.64},
            {"id": 3501, "part": "FAST-A", "qty": 35, "unit_price": 30.99, "amount": 1084.65},
        ],
        invoice_total=1084.65,
        po="58700",
    )
    assert decision["hold"] is False


def test_oneal_lines_route_to_transfer_ap_and_shawn_note():
    decision = assess_bill_qty_uom(
        invoice_lines=[
            {"part": "ANG-125-125", "qty": 240, "unit_price": 0.2405, "amount": 57.72, "line": 1},
            {"part": "ANG-316", "qty": 1200, "unit_price": 0.1665, "amount": 199.73, "line": 2},
        ],
        po_lines=[
            {"part": "ANG-125-125", "qty": 240, "unit_price": 0.2405, "po": "59059"},
            {"part": "ANG-316", "qty": 1200, "unit_price": 0.1665, "po": "59059"},
        ],
        receipts=[
            {"id": 23562, "part": "ANG-125-125", "qty": 240, "unit_price": 0.2405, "amount": 57.72, "po": "59059"},
            {"id": 23563, "part": "ANG-316", "qty": 14400, "unit_price": 0.1665, "amount": 2397.60, "po": "59059"},
        ],
        invoice_total=257.46,
        amount_entered=57.72,
        po="59059",
        invoice_number="15455478",
    )
    assert decision["hold"] is True
    assert decision["owner"] == "Shawn McKibben"
    assert "23563" in decision["note"]

    class Client:
        def __init__(self):
            self.updates = []

        def list_items(self, service):
            assert service == "ap_batches"
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": "Transfer AP"}}]

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {"AP_Invoice_Batch": {"id": 730, "text": "API Agent"}},
                "lists": {"Comments_1": []},
            }

        def update(self, service, item_id, payload):
            self.updates.append(payload)
            return {}, 200, ""

    client = Client()
    routed = apply_qty_uom_transfer_ap(client, kimco_id=10317, note=decision["note"])
    assert routed["move"]["status"] == "moved"
    assert routed["move"]["batch_id"] == 375
    assert routed["comment"]["status"] == "persisted"
    assert routed["comment"]["mention_id"] == 104
    html = client.updates[-1]["lists"]["Comments_1"][0]["values"]["HtmlValue"]
    assert 'data-mention-id="104"' in html
    assert html.startswith("<p>AP Clerk:")
    assert "category=" not in decision["note"]
    sheet = comments_1_html(decision["note"], mention_id=None)
    assert "<span" not in decision["note"]
    assert "data-mention-id" not in decision["note"]
    assert "<p>" in sheet


def test_every_hold_note_rejects_key_value_and_html():
    facts = {
        "invoice_number": "15455478",
        "vendor": "O'Neal",
        "posted_vendor": "Someone Else",
        "invoice_total": 257.46,
        "amount_entered": 57.72,
        "line": "2",
        "po": "59059",
        "receipt": "23563",
        "qty_received": 14400,
        "qty_invoiced": 1200,
        "qty_ordered": 1200,
        "uom": "IN",
        "gap": 199.74,
        "mention": "@Ruben Perez",
        "extra": "Sheet owner: Anthony for raw material, shawn for purchased parts",
    }
    assert set(HOLD_NOTE_CATEGORIES) == set(plain_hold_note.__globals__["_HOLD_NOTE_BUILDERS"])
    for category in HOLD_NOTE_CATEGORIES:
        text = plain_hold_note(category, **facts)
        assert sheet_note_problems(text) == [], text
        assert "category=" not in text
        assert "owner=" not in text
        assert "<" not in text
        assert text.startswith("AP Clerk:")
        assert text.endswith(".")
    bad = "AP Clerk: category=quantity_variance; owner=buyer. Fix the receipt."
    assert "key=value shorthand" in sheet_note_problems(bad)
    html = "AP Clerk: <b>Fix the receipt.</b>"
    assert "html" in sheet_note_problems(html)
    shawn_html = comments_1_html(plain_hold_note("price_variance", **facts), mention_id=104)
    assert 'data-mention-id="104"' in shawn_html
    assert "category=" not in plain_hold_note("price_variance", **facts)


def test_missing_receipt_comment_is_plain_english():
    text = missing_receipt_comment_text("JP Steel")
    assert text.startswith("AP Clerk:")
    assert sheet_note_problems(text) == []
    assert "@Anthony" in text
    assert "@Shawn McKibben" in text
    assert "category=" not in text
    assert "<" not in text
    modern = missing_receipt_comment_text("Modern Heat")
    assert "until Kyle confirms" in modern
    assert sheet_note_problems(modern) == []
