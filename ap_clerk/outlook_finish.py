"""NOTE-51: when a KIMCO bill is finished to Success, update AP Outlook.

accountspayable@ process marker must match the finished bill:
Success → Entered in AI (never leave Success sitting on AI HOLD /
Entered with issues).

Multi-invoice parent emails: flip the parent to Entered in AI only when
EVERY sibling invoice from that PDF is Success. Otherwise keep
Entered with issues (header exists) or AI HOLD (no header).

receiving@ packing-slip emails still use AI Completed after a verified
slip attach (NOTE-49). That is a separate mailbox and a separate rule.

No Mail.Send.
"""

from __future__ import annotations

from typing import Any

from ap_clerk.graph import (
    AI_HOLD_CATEGORY,
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_AI_HOLD,
    FLAG_ENTERED_WITH_ISSUES,
    FLAG_FLAGGED,
    FLAG_NO_MESSAGE_ID,
)

RESULT_SUCCESS = "Success"

# Outcomes that mean a header exists and the parent is not fully finished.
_HEADER_RESULTS = frozenset(
    {"HOLD", "Incomplete", "Fail", "Entered with issues", "Success"}
)


def parent_outlook_target(
    sibling_results: list[str] | tuple[str, ...],
    *,
    any_header: bool | None = None,
) -> str:
    """Exact accountspayable@ process category for a parent email.

    All siblings Success → Entered in AI.
    Any entered-but-unfinished sibling, or any_header → Entered with issues.
    No header at all → AI HOLD.
    """
    results = [str(r or "").strip() for r in sibling_results]
    if results and all(r == RESULT_SUCCESS for r in results):
        return ENTERED_IN_AI_CATEGORY
    header = bool(any_header) if any_header is not None else any(
        r in _HEADER_RESULTS for r in results
    )
    if header:
        return ENTERED_WITH_ISSUES_CATEGORY
    return AI_HOLD_CATEGORY


def sibling_results_for_parent(
    rows: list[dict[str, Any]],
    *,
    invoice_key: str = "Invoice #",
    result_key: str = "Result",
    invoices: list[str] | None = None,
) -> list[str]:
    """Results for the sibling invoice numbers on one parent PDF."""
    wanted = {str(n).strip() for n in (invoices or []) if str(n).strip()}
    out: list[str] = []
    for row in rows:
        inv = str(row.get(invoice_key) or row.get("invoice_number") or "").strip()
        if wanted and inv not in wanted:
            continue
        out.append(str(row.get(result_key) or ""))
    return out


def promote_ap_outlook_after_success(
    graph,
    message_id: str,
    sibling_results: list[str] | tuple[str, ...],
    *,
    mailbox: str = ALLOWED_MAILBOX,
    any_header: bool | None = None,
) -> dict[str, Any]:
    """PATCH accountspayable@ after finishing a bill. Never Mail.Send.

    Call this when an existing KIMCO header is repaired to Success (HOLD
    finish, packing-slip complete that finishes the bill, leftover enter
    that lands Success). Do not leave that Success on AI HOLD.

    receiving@ AI Completed is not this function.
    """
    if graph is None:
        return {
            "target": parent_outlook_target(sibling_results, any_header=any_header),
            "status": "no-graph",
            "mailbox": mailbox,
            "message_id": message_id or "",
            "mail_send": False,
        }
    mid = str(message_id or "").strip()
    target = parent_outlook_target(sibling_results, any_header=any_header)
    if not mid:
        return {
            "target": target,
            "status": FLAG_NO_MESSAGE_ID,
            "mailbox": mailbox,
            "message_id": "",
            "mail_send": False,
        }
    if target == ENTERED_IN_AI_CATEGORY:
        status = graph.flag_matched(mailbox, mid)
    elif target == ENTERED_WITH_ISSUES_CATEGORY:
        status = graph.flag_issues(mailbox, mid)
    else:
        status = graph.flag_hold(mailbox, mid)
    return {
        "target": target,
        "status": status,
        "mailbox": mailbox,
        "message_id": mid,
        "mail_send": False,
        "flag": (
            FLAG_FLAGGED
            if target == ENTERED_IN_AI_CATEGORY
            else FLAG_ENTERED_WITH_ISSUES
            if target == ENTERED_WITH_ISSUES_CATEGORY
            else FLAG_AI_HOLD
        ),
    }
