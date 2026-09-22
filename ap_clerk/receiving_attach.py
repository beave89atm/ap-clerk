"""Attach receiving@ signed packing slips to AP headers. No new invoices. No Mail.Send.

After GET verifies the packing-slip file on the matching AP header, the
receiving@ message may be categorized `AI Completed` — only when every
identifiable slip from that email is attached. Partial success stays
uncategorized and lists leftovers.
"""

from __future__ import annotations

import logging
from typing import Any

from ap_clerk.packing_slips import (
    packing_slip_verified_on_header,
    slip_is_identifiable,
)
from ap_clerk.receiving_mail import (
    FLAG_LEFT_UNCATEGORIZED,
    ReceivingGraph,
    stamp_receiving_ai_completed,
)

LOGGER = logging.getLogger("ap_clerk.receiving")


def verify_header_packing_slip(
    kimco: Any,
    invoice_id: Any,
    *,
    attach_status: str | None,
    expected_name: str = "",
) -> dict[str, Any]:
    """GET attachments after attach. verified=True only when the slip is on the header."""
    status = (attach_status or "").strip().lower()
    attachments: list[Any] = []
    if status == "attached" and invoice_id not in (None, "") and kimco is not None:
        try:
            attachments = list(kimco.list_attachments(invoice_id) or [])
        except Exception:  # noqa: BLE001 - GET failure means not verified
            LOGGER.info("GET attachments failed for invoice %s; treat as not verified", invoice_id)
            attachments = []
    verified = status == "attached" and packing_slip_verified_on_header(
        attachments, expected_name=expected_name
    )
    if status == "attached" and not verified:
        status = "failed"
    return {
        "attach_status": attach_status,
        "status": status if status else "failed",
        "verified": verified,
        "header_attachments": attachments,
        "invoice_id": invoice_id,
        "expected_name": expected_name,
    }


def slip_result_row(
    slip: dict[str, Any],
    *,
    status: str,
    verified: bool = False,
    invoice_id: Any = None,
    invoice_number: str = "",
    vendor: str = "",
    attach_status: str = "",
) -> dict[str, Any]:
    return {
        "slip": slip,
        "identifiable": slip_is_identifiable(slip),
        "status": status,
        "verified": bool(verified),
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "vendor": vendor,
        "attach_status": attach_status,
        "pages": list(slip.get("pages") or []),
    }


def finish_receiving_email(
    graph: ReceivingGraph,
    *,
    mailbox: str,
    message_id: str,
    slip_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Stamp AI Completed only when every identifiable slip is GET-verified."""
    stamped = stamp_receiving_ai_completed(
        graph,
        mailbox=mailbox,
        message_id=message_id,
        slip_results=slip_results,
    )
    if not stamped.get("stamp"):
        LOGGER.info(
            "receiving@ %s left uncategorized: %s",
            message_id,
            stamped.get("why"),
        )
        stamped.setdefault("outlook", FLAG_LEFT_UNCATEGORIZED)
    return stamped
