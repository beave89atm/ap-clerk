"""Kyle override: only 72013304 / 70759737 move to Transfer AP + @Shawn 104."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.comments_tab import SHAWN_MENTION_ID, mention_html  # noqa: E402
from ap_clerk.kimco import KimcoError  # noqa: E402
from ap_clerk.rules import SHAWN_MCKIBBEN  # noqa: E402
from mcmaster_0918 import DO_NOT_MUTATE_IDS, LEAVE_ALONE_HOLD_IDS  # noqa: E402
from mcmaster_transfer_two import (  # noqa: E402
    ALLOWED_WRITE_IDS,
    COMMENT_NEEDLE,
    TARGETS,
    merch_summary,
    refuse_other_header,
    shawn_action_note,
)


def test_targets_are_the_two_kyle_asked():
    assert set(TARGETS) == {"72013304", "70759737"}
    assert TARGETS["72013304"]["kimco_id"] == 10146
    assert TARGETS["70759737"]["kimco_id"] == 10158
    assert TARGETS["72013304"]["po"] == "58221"
    assert TARGETS["70759737"]["po"] == "59014"
    assert TARGETS["72013304"]["amount"] == 443.49
    assert TARGETS["70759737"]["amount"] == 1037.59
    assert ALLOWED_WRITE_IDS == {10146, 10158}
    assert ALLOWED_WRITE_IDS <= LEAVE_ALONE_HOLD_IDS
    assert ALLOWED_WRITE_IDS.isdisjoint(DO_NOT_MUTATE_IDS)


def test_refuse_other_headers():
    refuse_other_header(10146, "72013304")
    refuse_other_header(10158, "70759737")
    with pytest.raises(KimcoError, match="72013304/70759737 only"):
        refuse_other_header(10152, "71401129")
    with pytest.raises(KimcoError, match="expected KIMCO 10146"):
        refuse_other_header(10158, "72013304")


def test_action_notes_tell_shawn_what_to_do():
    note_46 = shawn_action_note(
        invoice="72013304",
        parsed={
            "po": "58221",
            "amount": 443.49,
            "lines": [{"part": "4082T15", "qty": 24.0, "unit_price": 17.71}],
            "fees": [{"name": "Shipping", "amount": 18.45}],
        },
        live={"receipt_lines": [], "po_text": "PO58221-AMERICAN QUALITY POWDERCOATING"},
    )
    assert COMMENT_NEEDLE in note_46
    assert "72013304" in note_46
    assert "10146" in note_46
    assert "443.49" in note_46
    assert "58221" in note_46
    assert "4082T15" in note_46
    assert "Do not Select those AQPC leftovers" in note_46
    assert "Transfer AP" in note_46
    assert SHAWN_MCKIBBEN in note_46
    html = mention_html(note_46)
    assert f'data-mention-id="{SHAWN_MENTION_ID}"' in html
    assert "Entity" not in html

    note_58 = shawn_action_note(
        invoice="70759737",
        parsed={
            "po": "59014",
            "amount": 1037.59,
            "lines": [
                {"part": "98935A744", "qty": 12.0, "unit_price": 12.1},
                {"part": "6698K17", "qty": 24.0, "unit_price": 21.24},
            ],
            "fees": [{"name": "Shipping", "amount": 34.39}],
        },
        live={"receipt_lines": [], "po_text": "PO59014-MCMASTER-CARR"},
    )
    assert "70759737" in note_58
    assert "10158" in note_58
    assert "1037.59" in note_58
    assert "59014" in note_58
    assert "Receive the PO 59014 lines" in note_58
    assert merch_summary(
        {"lines": [{"part": "4082T15", "qty": 24.0, "unit_price": 17.71}]}
    ) == "4082T15 24.0@17.71"
