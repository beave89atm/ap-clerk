"""3P 9/22 first-5 enter gates. No live Graph or KIMCO I/O."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

from ap_clerk.receiving_owners import (
    PEOPLE,
    lookup_receiving_owner,
    mention_span,
    missing_receipt_comments_1_html,
    missing_receipt_exception_owner,
)
from ap_clerk.rules import known_vendor_id, never_skip_vendor_invoice

_SPEC = importlib.util.spec_from_file_location(
    "three_p_0922",
    Path(__file__).resolve().parents[1] / "scripts" / "three_p_0922.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)

CAP = _MOD.CAP
PREFERRED_BATCH_NAME = _MOD.PREFERRED_BATCH_NAME
VENDOR_ID_HINT = _MOD.VENDOR_ID_HINT
is_3p_email = _MOD.is_3p_email
is_3p_vendor_text = _MOD.is_3p_vendor_text
is_other_vendor_email = _MOD.is_other_vendor_email
pick_oldest_open = _MOD.pick_oldest_open
skip_reason_for_message = _MOD.skip_reason_for_message


def _msg(
    *,
    sender: str = "Rachel@3pindustries.com",
    name: str = "Rachel Bailey",
    subject: str = "INV # 142100 / CPL # 77001 / PO # 59100",
    preview: str = "3P INDUSTRIES L.L.C. invoice attached",
) -> dict:
    return {
        "from": {"emailAddress": {"address": sender, "name": name}},
        "sender": {"emailAddress": {"address": sender, "name": name}},
        "subject": subject,
        "bodyPreview": preview,
    }


def test_batch_name_and_vendor_id():
    assert PREFERRED_BATCH_NAME == "API Agent - 9/22/26 3P"
    assert CAP == 5
    assert VENDOR_ID_HINT == 1
    assert known_vendor_id("3P") == 1
    assert known_vendor_id("3P Industries") == 1
    assert is_3p_vendor_text("3P INDUSTRIES L.L.C.")
    assert is_3p_vendor_text("999-3P INDUSTRIES")


def test_is_3p_email_accepts_rachel_dennis():
    assert is_3p_email(_msg())
    assert is_3p_email(
        _msg(sender="Dennis@3pindustries.com", name="Dennis", subject="INV # 142050")
    )
    assert is_3p_email(
        _msg(
            sender="ap@other.com",
            name="3P Industries",
            subject="Invoice 142060 PO 59001",
        )
    )
    assert is_3p_email(
        _msg(
            sender="billing@vendor.com",
            name="Rachel Bailey",
            subject="INV # 142041 / CPL # 76659 / PO # 58766",
            preview="3P INDUSTRIES Invoice",
        )
    )


def test_is_3p_email_rejects_other_vendors():
    assert is_other_vendor_email(
        _msg(sender="noreply@mcmaster.com", name="McMaster-Carr", subject="Invoice 71401129")
    )
    assert not is_3p_email(
        _msg(sender="noreply@mcmaster.com", name="McMaster-Carr", subject="Invoice 71401129")
    )
    assert not is_3p_email(
        _msg(sender="ar@onealsteel.com", name="O'Neal Steel", subject="Invoice 15469453")
    )
    assert not is_3p_email(
        _msg(
            sender="billing@gasandsupply.com",
            name="Gas & Supply",
            subject="Gas&Supply Invoice/Statement",
        )
    )
    assert not is_3p_email(
        _msg(sender="ap@legacywire.com", name="Legacy Wire", subject="Invoice 123")
    )


def test_skip_rfq_freight_statement_without_invoice():
    assert skip_reason_for_message(_msg(subject="RFQ 3P laser parts")) == "rfq"
    assert skip_reason_for_message(_msg(subject="Freight FYI 3P shipment")) == "freight-fyi"
    assert skip_reason_for_message(_msg(subject="Account Statement August")) == "statement"
    assert skip_reason_for_message(_msg(subject="INV # 142041 / CPL # 76659")) is None
    assert skip_reason_for_message(_msg(subject="RFQ follow-up INV # 142200")) is None
    assert skip_reason_for_message(_msg(subject="Corrected invoice INV # 142041")) is None


def test_never_skip_3p_142041_style():
    subject = "INV # 142041 / CPL # 76659 / PO # 58766, 58770"
    assert never_skip_vendor_invoice(subject=subject, from_name="Rachel Bailey")
    assert skip_reason_for_message(_msg(subject=subject)) is None
    assert is_3p_email(_msg(subject=subject))


def test_pick_oldest_open_cap_five_skips_entered_and_pre_aug1():
    bills = [
        {"invoice_number": "141900", "date": "2026-07-31", "receivedDateTime": "2026-08-01T10:00:00Z"},
        {"invoice_number": "142010", "date": "2026-08-02", "receivedDateTime": "2026-08-03T10:00:00Z"},
        {"invoice_number": "142020", "date": "2026-08-03", "receivedDateTime": "2026-08-04T10:00:00Z"},
        {"invoice_number": "142030", "date": "2026-08-04", "receivedDateTime": "2026-08-05T10:00:00Z"},
        {"invoice_number": "142040", "date": "2026-08-05", "receivedDateTime": "2026-08-06T10:00:00Z"},
        {"invoice_number": "142041", "date": "2026-08-06", "receivedDateTime": "2026-08-07T10:00:00Z"},
        {"invoice_number": "142050", "date": "2026-08-07", "receivedDateTime": "2026-08-08T10:00:00Z"},
        {"invoice_number": "142060", "date": "2026-08-08", "receivedDateTime": "2026-08-09T10:00:00Z"},
    ]
    entered = {"142020": 9980}
    picked = pick_oldest_open(bills, entered, cap=5, min_date=date(2026, 8, 1))
    assert [b["invoice_number"] for b in picked] == [
        "142010",
        "142030",
        "142040",
        "142041",
        "142050",
    ]
    assert "141900" not in [b["invoice_number"] for b in picked]
    assert "142020" not in [b["invoice_number"] for b in picked]
    assert "142060" not in [b["invoice_number"] for b in picked]


def test_pick_oldest_open_accepts_slash_dates():
    bills = [
        {"invoice_number": "142100", "date": "8/2/2026", "receivedDateTime": "2026-08-03T10:00:00Z"},
        {"invoice_number": "142101", "date": "", "receivedDateTime": "2026-08-04T10:00:00Z"},
        {"invoice_number": "142102", "date": "08/01/2026", "receivedDateTime": "2026-08-02T10:00:00Z"},
    ]
    picked = pick_oldest_open(bills, {}, cap=5)
    assert [b["invoice_number"] for b in picked] == ["142102", "142100", "142101"]


def test_3p_receiving_owner_is_ruben_plain_no_mention_id():
    entry = lookup_receiving_owner("3P")
    assert entry and entry["receiving_owner_raw"] == "Ruben"
    assert entry["needs_dock_receive"] is True
    assert lookup_receiving_owner("3P Industries")["vendor"] == "3P"
    assert missing_receipt_exception_owner("3P") == "Ruben Perez"
    assert PEOPLE["ruben"]["mention_id"] is None
    assert mention_span(PEOPLE["ruben"]) == "@Ruben Perez"
    html = missing_receipt_comments_1_html("3P")
    assert "@Ruben Perez" in html
    assert "data-mention-id" not in html
    assert "104" not in html
