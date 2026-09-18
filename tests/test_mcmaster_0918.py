"""McMaster-Carr 9/18 dedicated-batch discovery gates.

McMaster only. Invoice dates before 2026-08-01 are reported, not entered
(NOTE-28). Credits and pay-reminders are not invoices. Prefer newest
clean PO+receipt matches. Never reuse leftover weekday batches.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.pdf_invoice import (  # noqa: E402
    extract_mcmaster_lines,
    is_purchase_order_document,
    parse_invoice_text,
)
from ap_clerk.rules import names_match  # noqa: E402
from mcmaster_0918 import (  # noqa: E402
    CAP,
    CENSUS_POS,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    MIN_INVOICE_DATE,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    blob_has_mcmaster,
    exact_invoice_number,
    is_credit_bill,
    is_mcmaster_invoice_email,
    is_mcmaster_message,
    is_mcmaster_vendor_text,
    is_over_ppv_price_hold,
    over_ppv_hold_comment,
    pick_recent,
    subject_po,
)


def _msg(*, subject: str, from_name: str = "McMaster-Carr", categories: list[str] | None = None) -> dict:
    return {
        "id": "AAMk-mcmaster-test",
        "subject": subject,
        "bodyPreview": "Purchase Order  59224\r\nInvoice 72094446",
        "from": {"emailAddress": {"name": from_name, "address": "ar@mcmaster.com"}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_mcmaster_distinctive_never_other_vendors():
    assert is_mcmaster_message(_msg(subject="Invoice for Your Order 59224"))
    assert is_mcmaster_message(_msg(subject="Invoice 72094446", from_name="McMaster-Carr"))
    assert not is_mcmaster_message(
        {
            **_msg(subject="Invoice 125315 from JP Steel"),
            "from": {"emailAddress": {"name": "JP Steel", "address": "ap@jpsteel.com"}},
            "bodyPreview": "",
        }
    )
    assert not is_mcmaster_message(
        {
            **_msg(subject="Invoice from Gas and Supply"),
            "from": {"emailAddress": {"name": "Gas and Supply", "address": "ar@gasandsupply.com"}},
            "bodyPreview": "",
        }
    )
    assert blob_has_mcmaster("McMaster-Carr Supply Company")
    assert blob_has_mcmaster("117-MCMASTER CARR")
    assert not blob_has_mcmaster("MSC Industrial Supply")


def test_mcmaster_names_match_and_vendor_text():
    assert names_match("McMaster-Carr Supply Company", "McMaster-Carr Supply Company")
    assert is_mcmaster_vendor_text("McMaster-Carr Supply Company")
    assert is_mcmaster_vendor_text("117-MCMASTER CARR")
    assert is_mcmaster_vendor_text("McMaster")
    assert not is_mcmaster_vendor_text("MSC Industrial Supply")
    assert not is_mcmaster_vendor_text("RMP INDUSTRIAL SUPPLY")


def test_mcmaster_skip_credits_and_flagged_and_pay_reminders():
    assert is_mcmaster_invoice_email(_msg(subject="Invoice for Your Order 59224"))
    assert not is_mcmaster_invoice_email(_msg(subject="Credit from Your Order 58221"))
    assert not is_mcmaster_invoice_email(_msg(subject="Please Deduct Credit on PO 58139"))
    assert not is_mcmaster_invoice_email(_msg(subject="Please Pay for Your PO 58492"))
    flagged = _msg(subject="Invoice for Your Order 59224", categories=["Entered in AI"])
    assert flagged["categories"] == ["Entered in AI"]
    assert is_mcmaster_invoice_email(flagged)


def test_mcmaster_invoice_number_never_subject_order_po():
    assert exact_invoice_number("72094446") == "72094446"
    assert exact_invoice_number("Invoice 72094446 for PO 59224.PDF") == "72094446"
    assert exact_invoice_number("59224") == ""
    assert exact_invoice_number("Invoice for Your Order 59224") == ""
    assert subject_po("Invoice for Your Order 59224") == "59224"
    assert subject_po("Please Pay for Your PO 58492") == "58492"


def test_mcmaster_pick_prefers_newest_clean_then_skips_old_and_credit():
    receipts = [
        {
            "id": 1,
            "po": "59224",
            "part": "1234K11",
            "qty": 2,
            "unit_price": 10.0,
            "amount": 20.0,
        }
    ]
    clean = {
        "invoice_number": "72094446",
        "date": "2026-09-17",
        "receivedDateTime": "2026-09-18T06:43:57Z",
        "po": "59224",
        "amount": 20.0,
        "lines": [{"part": "1234K11", "qty": 2, "unit_price": 10.0, "amount": 20.0}],
        "subject": "Invoice for Your Order 59224",
    }
    newer_no_receipt = {
        "invoice_number": "72087570",
        "date": "2026-09-18",
        "receivedDateTime": "2026-09-18T06:43:52Z",
        "po": "59235",
        "amount": 30.32,
        "lines": [{"part": "9999K99", "qty": 1, "unit_price": 30.32, "amount": 30.32}],
        "subject": "Invoice for Your Order 59235",
    }
    older = {
        "invoice_number": "71000000",
        "date": "2026-07-15",
        "receivedDateTime": "2026-07-16T12:00:00Z",
        "po": "58139",
        "amount": 10.0,
        "subject": "Invoice for Your Order 58139",
    }
    credit = {
        "invoice_number": "72022981",
        "date": "2026-09-18",
        "receivedDateTime": "2026-09-18T06:43:43Z",
        "po": "58221",
        "amount": -336.49,
        "subject": "Credit from Your Order 58221",
        "skip_reason": "credit",
    }
    already_on_kimco = {
        "invoice_number": "69440053",
        "date": "2026-09-01",
        "po": "58840",
        "amount": 12.0,
        "subject": "Invoice for Your Order 58840",
    }
    recent, leftover, credits = pick_recent(
        [newer_no_receipt, clean, older, credit, already_on_kimco],
        already={"69440053"},
        cap=5,
        receipts=receipts,
    )
    assert [b["invoice_number"] for b in recent] == ["72094446", "72087570"]
    assert recent[0].get("_clean") is True
    assert recent[1].get("_clean") is False
    assert [b["invoice_number"] for b in leftover] == ["71000000"]
    assert [b["invoice_number"] for b in credits] == ["72022981"]
    assert is_credit_bill(credit)
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"
    assert CAP == 5
    assert "59224" in CENSUS_POS


def test_mcmaster_pick_cap_and_forbidden_batches():
    many = [
        {
            "invoice_number": str(72000000 + i),
            "date": "2026-09-10",
            "receivedDateTime": f"2026-09-10T12:00:0{i}Z",
            "po": "59224",
            "amount": 1.0,
            "subject": f"Invoice for Your Order 59224 {i}",
        }
        for i in range(8)
    ]
    recent, leftover, credits = pick_recent(many, already=set(), cap=5, receipts=None)
    assert len(recent) == 5
    assert len(leftover) == 3
    assert credits == []
    assert PREFERRED_BATCH_NAME == "API Agent - 9/18/26 McMaster"
    assert FALLBACK_BATCH_NAME == "API Agent - 9/18/26"
    assert FORBIDDEN_BATCH_IDS == {715, 716, 717, 720}
    assert "API Agent - 9/17/26 Gas & Supply" in FORBIDDEN_REUSE_NAMES
    assert VENDOR_NAME == "McMaster-Carr Supply Company"


_MCMASTER_FACE = """ Invoice
McMaster-Carr Supply Company
Billed to
KANNON MANUFACTURING INC
Purchase Order 59224
Total $131.21
Invoice 72094446
Invoice Date 9/17/26
Line Product Ordered Shipped Balance Price Total
1 3616N12 Horizontal-Surface-Mount Level, 2-1/2" Long x 1"
Wide, Black, Packs of 2
6
Packs
6 0 7.00
Per Pack
42.00
2 2151A27 Magnetic Level for Tight Spaces, 10" Long 1
Each
1 0 23.18
Each
23.18
Merchandise  106.62
Shipping  24.59
Total  $131.21
"""


def test_mcmaster_invoice_is_not_a_purchase_order_doc():
    assert is_purchase_order_document(text=_MCMASTER_FACE, filename="Invoice_72094446_for_PO_59224.PDF") is False
    assert is_purchase_order_document(filename="Invoice 72094446 for PO 59224.PDF") is False
    parsed = parse_invoice_text(
        _MCMASTER_FACE,
        filename="Invoice_72094446_for_PO_59224.PDF",
        subject="Invoice for Your Order 59224",
        from_name="McMaster-Carr",
    )
    assert parsed["is_purchase_order_doc"] is False
    assert parsed["invoice_number"] == "72094446"
    assert parsed["po"] == "59224"
    assert parsed["amount"] == 131.21
    parts = [str(ln.get("part") or "") for ln in parsed.get("lines") or []]
    assert "3616N12" in parts
    assert "2151A27" in parts
    assert any(abs(float(ln.get("amount") or 0) - 42.00) < 0.01 for ln in parsed["lines"])
    assert extract_mcmaster_lines(_MCMASTER_FACE)


def test_mcmaster_over_ppv_comment_tags_shawn():
    text = over_ppv_hold_comment(invoice_number="72094446", po="59224", pdf_amount=131.21)
    assert "@Shawn McKibben" in text
    assert "72094446" in text
    assert "59224" in text
    assert "NOT selected" in text
    row = {"Result": "HOLD", "Why": "HOLD (price-does-not-match): over the PPV gate", "Exception category": "price_variance"}
    assert is_over_ppv_price_hold(row, {"select_zero": True})
    assert not is_over_ppv_price_hold(
        {"Result": "HOLD", "Why": "HOLD (receipt): no open receipt leftover", "Exception category": "missing_receipt"},
        {},
    )
