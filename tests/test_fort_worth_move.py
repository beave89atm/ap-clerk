"""NOTE-52: move AP email to Inbox 9 - FORT WORT… after header + PDF attach."""

from __future__ import annotations

from unittest.mock import Mock

from ap_clerk.graph import (
    MOVE_MOVED,
    MOVE_SKIPPED_ALREADY,
    MOVE_SKIPPED_NO_ATTACH,
    MOVE_SKIPPED_NO_HEADER,
    MOVE_SKIPPED_NO_MESSAGE,
    _MOVED_MESSAGE_IDS,
    apply_fort_worth_move_after_enter,
    is_fort_worth_inbox_folder,
    should_move_to_fort_worth,
)
from ap_clerk.quality_v12 import TREYCE_NOTES_V12, note_by_id


def test_note52_is_registered():
    note = note_by_id("NOTE-52")
    assert "FORT WORT" in note["expected"]
    assert "NOTE-52" in {n["id"] for n in TREYCE_NOTES_V12}


def test_folder_name_match_prefix_and_fort_wort():
    assert is_fort_worth_inbox_folder("9 - FORT WORTH AP")
    assert is_fort_worth_inbox_folder("9 - FORT WORT…")
    assert is_fort_worth_inbox_folder("9-Fort Worth")
    assert is_fort_worth_inbox_folder("9-Fort Worth invoices")
    assert is_fort_worth_inbox_folder("Processed — Fort Worth")
    assert not is_fort_worth_inbox_folder("Inbox")
    assert not is_fort_worth_inbox_folder("8 - Dallas")
    assert not is_fort_worth_inbox_folder("")


def test_do_not_move_without_attach_or_header():
    common = {"result": "HOLD", "kimco_id": 10173, "message_id": "mid-1"}
    assert (
        should_move_to_fort_worth(**common, attach_status="no-pdf-on-vm")
        == MOVE_SKIPPED_NO_ATTACH
    )
    assert (
        should_move_to_fort_worth(**common, attach_status="blocked-405")
        == MOVE_SKIPPED_NO_ATTACH
    )
    assert (
        should_move_to_fort_worth(
            result="Success",
            kimco_id="",
            attach_status="attached",
            message_id="mid-1",
        )
        == MOVE_SKIPPED_NO_HEADER
    )
    assert (
        should_move_to_fort_worth(
            result="Fail",
            kimco_id="",
            attach_status="no-pdf-on-vm",
            message_id="mid-1",
        )
        == MOVE_SKIPPED_NO_HEADER
    )
    assert (
        should_move_to_fort_worth(
            result="Success",
            kimco_id=10164,
            attach_status="attached",
            message_id="",
        )
        == MOVE_SKIPPED_NO_MESSAGE
    )


def test_move_when_header_and_attach_success_or_hold():
    assert (
        should_move_to_fort_worth(
            result="Success",
            kimco_id=10164,
            attach_status="attached",
            message_id="mid-ok",
        )
        == MOVE_MOVED
    )
    assert (
        should_move_to_fort_worth(
            result="HOLD",
            kimco_id=10173,
            attach_status="attached",
            message_id="mid-hold",
        )
        == MOVE_MOVED
    )


def test_multi_invoice_parent_moves_once():
    _MOVED_MESSAGE_IDS.clear()
    _MOVED_MESSAGE_IDS.add("parent-mid")
    assert (
        should_move_to_fort_worth(
            result="HOLD",
            kimco_id=10181,
            attach_status="attached",
            message_id="parent-mid",
        )
        == MOVE_SKIPPED_ALREADY
    )
    _MOVED_MESSAGE_IDS.clear()


def test_apply_does_not_call_move_without_attach():
    graph = Mock()
    row = {
        "Result": "HOLD",
        "KIMCO id": 10173,
        "Attach status": "no-pdf-on-vm",
        "Notes": "",
    }
    invoice = {"graph_message_id": "mid-no-attach"}
    status = apply_fort_worth_move_after_enter(row, invoice, graph)
    assert status == MOVE_SKIPPED_NO_ATTACH
    graph.move_message.assert_not_called()
    graph.resolve_fort_worth_folder.assert_not_called()
    assert "no Fort Worth move" in row["Notes"]


def test_apply_moves_when_header_and_attach():
    _MOVED_MESSAGE_IDS.clear()
    graph = Mock()
    graph.resolve_fort_worth_folder.return_value = {
        "id": "folder-fw",
        "displayName": "9 - FORT WORTH AP",
    }
    graph.move_message.return_value = {
        "status": MOVE_MOVED,
        "http": 201,
        "new_id": "mid-after",
    }
    row = {
        "Result": "Success",
        "KIMCO id": 10164,
        "Attach status": "attached",
        "Notes": "",
    }
    invoice = {"graph_message_id": "mid-before"}
    status = apply_fort_worth_move_after_enter(row, invoice, graph)
    assert status == MOVE_MOVED
    graph.move_message.assert_called_once_with(
        "accountspayable@kannonmfg.com", "mid-before", "folder-fw"
    )
    assert row["Folder move"] == MOVE_MOVED
    assert "NOTE-52" in row["Notes"]
    assert "mid-after" in row["Notes"]
    assert invoice["graph_message_id"] == "mid-after"
    _MOVED_MESSAGE_IDS.clear()
