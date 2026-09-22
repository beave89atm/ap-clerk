"""NOTE-50: Kyle receiving-owner map. No live Graph or KIMCO I/O."""

from __future__ import annotations

from ap_clerk.cli import _process_invoice
from ap_clerk.quality_v12 import COL_EXCEPTION_OWNER, apply_exception_category_owner
from ap_clerk.receiving_owners import (
    DATA_PATH,
    NO_DOCK_OWNER,
    PEOPLE,
    UNMAPPED_OWNER,
    load_receiving_owner_map,
    lookup_receiving_owner,
    mention_span,
    missing_receipt_comment_text,
    missing_receipt_comments_1_child,
    missing_receipt_comments_1_html,
    missing_receipt_exception_owner,
    missing_receipt_notes,
    needs_dock_receive,
    should_tag_missing_receipt,
    vendor_entries,
)


def test_map_covers_kyle_sheet_and_notable_blanks():
    payload = load_receiving_owner_map()
    vendors = vendor_entries()
    assert DATA_PATH.is_file()
    assert payload["note"] == "NOTE-50"
    assert payload["mention_ids"]["shawn"]["id"] == 104
    assert payload["mention_ids"]["ruben"]["id"] is None
    assert payload["mention_ids"]["anthony"]["id"] is None
    assert payload["mention_ids"]["monica"]["id"] is None
    assert len(vendors) == 111
    assert sum(1 for v in vendors if v.get("needs_dock_receive")) == 52
    assert sum(1 for v in vendors if not v.get("needs_dock_receive")) == 59
    uncertain = [v["vendor"] for v in vendors if v.get("uncertain")]
    assert uncertain == ["Modern Heat Treat Inc"]

    aqpc = lookup_receiving_owner("AQPC")
    gas = lookup_receiving_owner("Gas and Supply")
    assert aqpc and aqpc["vendor"] == "American Quality Powder Coating"
    assert aqpc["vendor"] != "American Bearing Company"
    assert gas and gas["vendor"] == "Gas and Supply North Texas, LLC"
    assert aqpc["receiving_owner_raw"] == ""
    assert gas["receiving_owner_raw"] == ""
    assert not needs_dock_receive("AQPC")
    assert not needs_dock_receive("Gas & Supply")
    assert not should_tag_missing_receipt("American Quality Powder Coating")
    assert missing_receipt_comment_text("AQPC") == ""
    assert missing_receipt_comments_1_child("Gas and Supply") is None
    assert missing_receipt_exception_owner("AQPC") == NO_DOCK_OWNER


def test_multi_owner_cell_text_is_kept():
    jp = lookup_receiving_owner("JPSteel")
    mc = lookup_receiving_owner("McMaster")
    fastenal = lookup_receiving_owner("Fastenal Company")
    assert jp["receiving_owner_raw"] == "Anthony for raw material, shawn for purchased parts"
    assert mc["receiving_owner_raw"] == "Shawn/Monica/Anthony"
    assert fastenal["receiving_owner_raw"] == "Shawn/Monica"
    assert missing_receipt_exception_owner("JP Steel") == (
        "Anthony for raw material, shawn for purchased parts"
    )
    assert missing_receipt_exception_owner("McMaster-Carr") == "Shawn/Monica/Anthony"
    assert missing_receipt_exception_owner("Fastenal Company") == "Shawn/Monica"
    text = missing_receipt_comment_text("JP Steel")
    assert "@Anthony" in text
    assert "@Shawn McKibben" in text
    assert "Anthony for raw material, shawn for purchased parts" in text


def test_modern_heat_treat_anthony_with_uncertainty():
    entry = lookup_receiving_owner("Modern Heat Treat")
    assert entry["receiving_owner_raw"] == "Anthony?"
    assert entry["uncertain"] is True
    assert missing_receipt_exception_owner("Modern Heat Treat Inc") == "Anthony"
    assert "until Kyle confirms" in missing_receipt_comment_text("Modern Heat")
    assert "until Kyle confirms" in missing_receipt_notes("Modern Heat Treat Inc")
    assert "?" in (entry.get("receiving_owner_raw") or "")


def test_shawn_mention_id_104_no_invented_ids():
    assert PEOPLE["shawn"]["mention_id"] == 104
    assert PEOPLE["ruben"]["mention_id"] is None
    assert PEOPLE["anthony"]["mention_id"] is None
    assert PEOPLE["monica"]["mention_id"] is None
    shawn = mention_span(PEOPLE["shawn"])
    assert 'data-mention-id="104"' in shawn
    assert "@Shawn McKibben" in shawn
    assert mention_span(PEOPLE["ruben"]) == "@Ruben Perez"
    assert 'data-mention-id' not in mention_span(PEOPLE["anthony"])
    assert 'data-mention-id' not in mention_span(PEOPLE["monica"])

    html = missing_receipt_comments_1_html("Fastenal Company")
    assert 'data-mention-id="104"' in html
    assert "@Monica" in html
    assert "data-mention-id=\"None\"" not in html
    child = missing_receipt_comments_1_child("Legacy Wire")
    assert child["state"] == "Added"
    assert "@Ruben Perez" in child["values"]["HtmlValue"]
    assert "data-mention-id" not in child["values"]["HtmlValue"]


def test_legacy_wire_single_owner_is_ruben_perez():
    assert lookup_receiving_owner("Legacy Wire Products")["owner_keys"] == ["ruben"]
    assert missing_receipt_exception_owner("Legacy Wire") == "Ruben Perez"
    stamped = apply_exception_category_owner(
        {
            "Result": "HOLD",
            "Why": "HOLD (receipt): no open receipt on PO 59034.",
            "Vendor": "Legacy Wire Products",
        }
    )
    assert stamped[COL_EXCEPTION_OWNER] == "Ruben Perez"


def test_unmapped_vendor_is_not_tagged():
    assert lookup_receiving_owner("Totally Unknown Vendor") is None
    assert missing_receipt_exception_owner("") == UNMAPPED_OWNER
    assert missing_receipt_comments_1_child("Totally Unknown Vendor") is None
    assert not should_tag_missing_receipt(None)


class _MissingReceiptKimco:
    target = "live"

    def __init__(self):
        self.comments_1 = []

    def create(self, service, values):
        return 8802, {"id": 8802, "values": values}, 200, ""

    def get_item(self, service, item_id):
        return {
            "id": item_id,
            "values": {
                "Remit_To_Address": {"id": 1, "text": "remit"},
                "Terms_Code": {"id": 2, "text": "Net 30"},
                "Vendor": {"id": 9, "text": "Fastenal Company"},
            },
        }

    def try_official_attach(self, *args, **kwargs):
        return "no-pdf-on-vm"

    def try_select_receipts(self, *args, **kwargs):
        return "held-unfinished"

    def try_post_fees(self, *args, **kwargs):
        return "none"

    def try_post_comments_1(self, invoice_id, child):
        self.comments_1.append((invoice_id, child))
        return "posted"


def _hold_row(vendor: str, kimco: _MissingReceiptKimco) -> dict:
    return _process_invoice(
        kimco,
        {
            "vendor": vendor,
            "invoice_number": "TXFT000111",
            "date": "2026-08-26",
            "po": "58700",
            "amount": 40.0,
            "lines": [{"part": "NEED-THIS", "qty": 6}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
            "packing_slip_attached": True,
        },
        batch={"id": 1},
        batch_label="API Agent - 9/22/26 (1)",
        invoice_by_number={},
        vendor_samples=[
            {"vendor_id": 9, "vendor_text": vendor, "invoice_id": 100, "po_text": ""}
        ],
        po_index={
            "58700": {
                "id": 3,
                "text": f"58700-{vendor}",
                "vendor_id": 9,
                "vendor_text": vendor,
                "lines": [],
            }
        },
        receipts=[{"slip": "OTHER", "qty": 6, "part": "DIFFERENT", "po_line": 1}],
        pdf_dir=None,
        graph_client=None,
        flag_outlook=False,
    )


def test_process_invoice_posts_comments_1_for_dock_owner_not_blank():
    dock = _MissingReceiptKimco()
    row = _hold_row("Fastenal Company", dock)
    assert "no receipts" in (row.get("Why") or "").lower()
    assert row[COL_EXCEPTION_OWNER] == "Shawn/Monica"
    assert len(dock.comments_1) == 1
    invoice_id, child = dock.comments_1[0]
    assert invoice_id == 8802
    assert child["state"] == "Added"
    html = child["values"]["HtmlValue"]
    assert 'data-mention-id="104"' in html
    assert "@Monica" in html

    blank = _MissingReceiptKimco()
    aqpc = _hold_row("American Quality Powder Coating", blank)
    assert "No receiving-owner @tag" in (aqpc.get("Why") or "")
    assert aqpc[COL_EXCEPTION_OWNER] == NO_DOCK_OWNER
    assert blank.comments_1 == []

    heat = _MissingReceiptKimco()
    modern = _hold_row("Modern Heat Treat Inc", heat)
    assert modern.get("Notes")
    assert "until Kyle confirms" in modern["Notes"]
    assert heat.comments_1
    assert "@Anthony" in heat.comments_1[0][1]["values"]["HtmlValue"]
