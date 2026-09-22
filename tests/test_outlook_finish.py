"""NOTE-51: Success finish must update accountspayable@ Outlook category."""

from __future__ import annotations

from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_ENTERED_WITH_ISSUES,
    FLAG_FLAGGED,
    FLAG_NO_MESSAGE_ID,
)
from ap_clerk.outlook_finish import (
    parent_outlook_target,
    promote_ap_outlook_after_success,
    sibling_results_for_parent,
)
from ap_clerk.quality_v12 import TREYCE_NOTES_V12, note_by_id


def test_note51_registry():
    note = note_by_id("NOTE-51")
    assert note["slug"] == "outlook-success-promotes-entered-in-ai"
    assert note["never_success"] is False
    assert "NOTE-51" in {n["id"] for n in TREYCE_NOTES_V12}
    assert "Entered in AI" in note["expected"]
    assert "AI HOLD" in note["expected"]
    assert "receiving@" in note["expected"]


def test_all_siblings_success_is_entered_in_ai():
    assert parent_outlook_target(["Success", "Success"]) == ENTERED_IN_AI_CATEGORY


def test_partial_success_keeps_entered_with_issues():
    assert (
        parent_outlook_target(["Success", "HOLD"]) == ENTERED_WITH_ISSUES_CATEGORY
    )
    assert (
        parent_outlook_target(["Success"], any_header=True)
        == ENTERED_IN_AI_CATEGORY
    )
    assert (
        parent_outlook_target(["HOLD", "HOLD"], any_header=True)
        == ENTERED_WITH_ISSUES_CATEGORY
    )


def test_no_header_stays_ai_hold():
    assert parent_outlook_target(["", "parse-error"]) == AI_HOLD_CATEGORY
    assert parent_outlook_target([], any_header=False) == AI_HOLD_CATEGORY


def test_sibling_results_filter_parent_invoices():
    rows = [
        {"Invoice #": "0040438052", "Result": "Success"},
        {"Invoice #": "0040437952", "Result": "HOLD"},
        {"Invoice #": "15460544", "Result": "Success"},
    ]
    assert sibling_results_for_parent(
        rows, invoices=["0040438052", "0040437952"]
    ) == ["Success", "HOLD"]


class _FakeGraph:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def flag_matched(self, mailbox, message_id):
        self.calls.append(("matched", mailbox, message_id))
        return FLAG_FLAGGED

    def flag_issues(self, mailbox, message_id):
        self.calls.append(("issues", mailbox, message_id))
        return FLAG_ENTERED_WITH_ISSUES

    def flag_hold(self, mailbox, message_id):
        raise AssertionError("Success finish must not stamp AI HOLD")


def test_promote_all_success_patches_entered_in_ai():
    graph = _FakeGraph()
    out = promote_ap_outlook_after_success(
        graph, "mid-1", ["Success", "Success"]
    )
    assert out["target"] == ENTERED_IN_AI_CATEGORY
    assert out["status"] == FLAG_FLAGGED
    assert out["mail_send"] is False
    assert graph.calls == [("matched", "accountspayable@kannonmfg.com", "mid-1")]


def test_promote_partial_success_keeps_issues_not_hold():
    graph = _FakeGraph()
    out = promote_ap_outlook_after_success(
        graph, "mid-2", ["Success", "HOLD"], any_header=True
    )
    assert out["target"] == ENTERED_WITH_ISSUES_CATEGORY
    assert out["status"] == FLAG_ENTERED_WITH_ISSUES
    assert graph.calls == [("issues", "accountspayable@kannonmfg.com", "mid-2")]


def test_promote_missing_message_id_does_not_patch():
    graph = _FakeGraph()
    out = promote_ap_outlook_after_success(graph, "", ["Success"])
    assert out["status"] == FLAG_NO_MESSAGE_ID
    assert graph.calls == []
