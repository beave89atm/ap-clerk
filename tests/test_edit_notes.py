"""NOTE-59: every KIMCO edit leaves an AP Clerk note and tags the owner."""

from __future__ import annotations

import pytest

from ap_clerk.rules import (
    RUBEN_MENTION_ID,
    SHAWN_MENTION_HTML,
    TREYCE_MENTION_HTML,
    TREYCE_MENTION_ID,
    ap_clerk_edit_note,
    edit_note_problems,
    edit_owner,
)


def test_transfer_ap_edit_must_mention_treyce():
    owner = edit_owner(batch_id=375, batch_name="TRANSFER AP", action="select_receipts")
    assert owner["mention_id"] == TREYCE_MENTION_ID == 33
    assert owner["name"] == "Treyce Hodges"
    bare = "AP Clerk: Selected the receipt. The bill is not posted."
    assert edit_note_problems(bare, owner)
    with pytest.raises(ValueError, match="AP Clerk:"):
        ap_clerk_edit_note("Selected the receipt.", batch_id=375)
    note = ap_clerk_edit_note(bare, batch_id=375)
    assert note.startswith("<p>AP Clerk:")
    assert TREYCE_MENTION_HTML in note
    assert 'data-mention-id="33"' in note
    assert "prosemirror-mention-node" in note
    assert edit_note_problems(note, owner) == []
    named = ap_clerk_edit_note(
        "AP Clerk: @Treyce Hodges sales tax was added. The bill is not posted.",
        batch_name="Transfer AP",
    )
    assert 'data-mention-id="33"' in named
    assert named.count("data-mention-id") == 1


def test_po_or_receiving_mentions_shawn():
    owner = edit_owner(action="po", batch_id=720)
    assert owner["mention_id"] == 104
    note = ap_clerk_edit_note(
        "AP Clerk: Asked purchasing to correct the PO price. The bill is not posted.",
        action="receiving",
    )
    assert note.startswith("<p>AP Clerk:")
    assert SHAWN_MENTION_HTML in note
    assert 'data-mention-id="104"' in note
    assert edit_note_problems(note, owner) == []
    with pytest.raises(ValueError, match="owner"):
        ap_clerk_edit_note("AP Clerk: Changed a line.", batch_id=720)


def test_aqpc_receiving_names_ruben_without_an_invented_id():
    assert RUBEN_MENTION_ID is None
    owner = edit_owner(action="receiving", vendor="American Quality Powder Coating", batch_id=375)
    assert owner["name"] == "Ruben Perez"
    assert owner["mention_id"] is None
    note = ap_clerk_edit_note(
        "AP Clerk: Receiving still needs the dock receipt. The bill is not posted.",
        action="aqpc_receiving",
        vendor="AQPC",
        batch_id=375,
    )
    assert note.startswith("<p>AP Clerk:")
    assert "@Ruben Perez" in note
    assert "data-mention-id" not in note
    assert edit_note_problems(note, owner) == []
    invented = note.replace("@Ruben Perez", '<span data-mention-id="999">@Ruben Perez</span>', 1)
    assert edit_note_problems(invented, owner)
