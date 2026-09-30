"""AFT no-PO Shawn notes. No live KIMCO I/O."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "aft_no_po_shawn_notes",
    Path(__file__).resolve().parents[1] / "scripts" / "aft_no_po_shawn_notes.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)


def _snap(invoice: str, verification: float) -> dict:
    return {
        "invoice": invoice,
        "amount": 0.0,
        "verification": verification,
        "vendor_id": 385,
        "vendor": "1383-AFT Industries",
        "batch_id": 375,
        "batch": "TRANSFER AP",
        "posted": None,
        "status": 1,
        "po_present": False,
        "lines": [],
    }


def test_existing_shawn_mention_is_not_duplicated() -> None:
    rows = [{"id": 1260, "shawn_mentioned": True}]
    action, problems = _MOD.plan_action(_MOD.BILLS[0], _snap("52004", 190.30), rows)
    assert action == "skipped"
    assert problems == []
    action_b, problems_b = _MOD.plan_action(_MOD.BILLS[1], _snap("52005", 363.00), [{"id": 1261, "shawn_mentioned": True}])
    assert action_b == "skipped"
    assert problems_b == []


def test_missing_shawn_mention_would_add_one_note() -> None:
    action, problems = _MOD.plan_action(_MOD.BILLS[0], _snap("52004", 190.30), [{"id": 1, "shawn_mentioned": False}])
    assert action == "add"
    assert problems == []
    html = _MOD.note_html("52004")
    assert _MOD.visible_comment_text(html).startswith("AP Clerk:")
    assert 'data-mention-id="104"' in html
    assert 'data-mention-id="26"' in html
    assert "no KIMCO PO" in html
    assert "needs Shawn" in html


def test_shawn_mention_id_counts_as_already_mentioned() -> None:
    assert _MOD.shawn_mentioned('<span data-mention-id="104">@Shawn McKibben</span>')
    assert not _MOD.shawn_mentioned("<p>AP Clerk: no mention yet.</p>")


def test_zero_invoice_amount_does_not_block_a_matching_verification() -> None:
    problems = _MOD.identity_problems(_MOD.BILLS[0], _snap("52004", 190.30))
    assert problems == []


def test_posted_or_wrong_batch_is_refused() -> None:
    posted = _snap("52004", 190.30)
    posted["posted"] = True
    action, problems = _MOD.plan_action(_MOD.BILLS[0], posted, [])
    assert action == "refused"
    assert problems
