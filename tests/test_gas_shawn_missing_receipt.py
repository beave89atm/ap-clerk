"""Gas missing_receipt Shawn override — comment text only."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.rules import SHAWN_MCKIBBEN
from gas_0040438052 import SHAWN_MENTION_ID
from gas_shawn_missing_receipt import (
    TARGETS,
    mention_html,
    receipt_comment_items,
    receipt_hold_comment,
)
from gas_supply_0917 import KNOWN_BATCH_ID


def test_targets_are_the_two_holds():
    ids = {t["kimco_id"] for t in TARGETS}
    invs = {t["invoice"] for t in TARGETS}
    assert ids == {10173, 10181}
    assert invs == {"0040438052", "0040437952"}
    assert KNOWN_BATCH_ID == 720
    assert SHAWN_MENTION_ID == 104


def test_receipt_comment_asks_shawn_to_receive_and_stay_720():
    body = receipt_hold_comment(invoice="0040438052", po="59081", amount=5178.55)
    assert body.startswith(SHAWN_MCKIBBEN)
    assert "HOLD (receipt)" in body
    assert "0040438052" in body
    assert "59081" in body
    assert "5178.55" in body
    assert "720" in body
    assert "do not Transfer AP" in body
    assert "receive the PO so AP can Select Receipts" in body
    assert "No email" in body
    assert "Kyle 2026-09-22 override" in body


def test_mention_html_uses_proven_id_104():
    html = mention_html(receipt_hold_comment(invoice="0040437952", po="59006", amount=241.46))
    assert f'data-mention-id="{SHAWN_MENTION_ID}"' in html
    assert 'class="prosemirror-mention-node"' in html
    assert SHAWN_MCKIBBEN in html
    assert "HOLD (receipt)" in html
    assert "0040437952" in html


def test_sheet_merge_keys_results_invoice():
    from gas_shawn_missing_receipt import exact_invoice_number

    results = [{"invoice": "0040438052", "status": "added", "comments_1": [{"id": 940}]}]
    by_inv = {
        exact_invoice_number(r.get("invoice") or r.get("Invoice #")): r for r in results
    }
    assert by_inv[exact_invoice_number("0040438052")]["status"] == "added"


def test_restore_helper_only_targets_720():
    from gas_shawn_missing_receipt import KNOWN_BATCH_ID, move_back_to_720

    assert KNOWN_BATCH_ID == 720
    assert move_back_to_720.__doc__ and "NOTE-53 superseded stay-on-720" in move_back_to_720.__doc__


def test_receipt_comment_items_ignore_price_variance_note():
    comments = [
        {
            "id": 1,
            "html": f'<span data-mention-id="{SHAWN_MENTION_ID}">@Shawn McKibben</span> HOLD (price-does-not-match) on Gas and Supply 0040437952',
        },
        {
            "id": 2,
            "html": f'<span data-mention-id="{SHAWN_MENTION_ID}">@Shawn McKibben</span> HOLD (receipt) on Gas and Supply 0040437952 PO 59006',
        },
    ]
    hits = receipt_comment_items(comments, "0040437952")
    assert [h["id"] for h in hits] == [2]
