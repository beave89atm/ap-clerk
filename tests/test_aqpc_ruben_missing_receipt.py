"""AQPC missing_receipt Comments_1 gates. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "aqpc_ruben_missing_receipt",
    Path(__file__).resolve().parents[1] / "scripts" / "aqpc_ruben_missing_receipt.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

classify_header = _MOD.classify_header
ruben_comment_html = _MOD.ruben_comment_html
RUBEN_TAG = _MOD.RUBEN_TAG


def _snap(**overrides):
    base = {
        "kimco_id": 10263,
        "invoice": "11020",
        "vendor_id": 22,
        "vendor": "1020-AMERICAN QUALITY POWDERCOATING",
        "po": "PO59227-AMERICAN QUALITY POWDERCOATING",
        "batch_id": 375,
        "batch": "TRANSFER AP",
        "amount": 0.0,
        "verification": 20.0,
        "posted": None,
        "posted_date": None,
        "void": False,
        "receipt_n": 0,
        "comments": [],
    }
    base.update(overrides)
    return base


def test_11020_is_tag_and_html_is_plain_ruben():
    decision = classify_header(_snap())
    assert decision["action"] == "tag"
    assert decision["mention_id"] is None
    html = ruben_comment_html(
        invoice="11020",
        po="PO59227-AMERICAN QUALITY POWDERCOATING",
        amount=0.0,
        verification=20.0,
    )
    assert RUBEN_TAG in html
    assert "11020" in html
    assert "59227" in html
    assert "20.00" in html
    assert "Dock receive is needed" in html
    assert "data-mention-id" not in html


def test_10264_price_variance_is_not_touched():
    decision = classify_header(
        _snap(
            kimco_id=10264,
            invoice="11021",
            po="PO59237-AMERICAN QUALITY POWDERCOATING",
            verification=75.0,
            comments=[
                {
                    "id": 995,
                    "html": (
                        '<p><span data-mention-id="104" data-mention-name="Shawn McKibben" '
                        'class="prosemirror-mention-node">@Shawn McKibben</span> '
                        "HOLD price_variance on AQPC invoice 11021 PO 59237.</p>"
                    ),
                }
            ],
        )
    )
    assert decision["action"] == "skip"
    assert "10264" not in decision["reason"] or "do-not-touch" in decision["reason"]
    assert decision["reason"].startswith("do-not-touch")


def test_receipts_selected_and_shawn_price_and_test_headers_skip():
    selected = classify_header(_snap(kimco_id=10265, invoice="11012", receipt_n=1, amount=100, verification=100))
    assert selected["action"] == "skip"
    assert selected["reason"] == "receipts-already-selected"

    price = classify_header(
        _snap(
            kimco_id=9791,
            invoice="10875",
            po="PO58797-AMERICAN QUALITY POWDERCOATING",
            verification=500,
            comments=[{"id": 758, "html": "<p>@Shawn McKibben they are charging us $5 each for these</p>"}],
        )
    )
    assert price["action"] == "skip"
    assert price["reason"] == "existing-shawn-price-note"

    test_header = classify_header(
        _snap(kimco_id=4100, invoice="Test 123", po="", amount=0, verification=0, comments=[])
    )
    assert test_header["action"] == "skip"
    assert test_header["reason"] == "no-po-not-dock-hold"


def test_already_tagged_ruben_is_not_retagged():
    decision = classify_header(
        _snap(comments=[{"id": 1, "html": "<p>@Ruben Perez HOLD missing_receipt on AQPC invoice 11020</p>"}])
    )
    assert decision["action"] == "skip"
    assert decision["reason"] == "already-tagged-ruben"
