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
    CREATED_HEADERS,
    DO_NOT_MUTATE_IDS,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    KNOWN_BATCH_ID,
    LEAVE_ALONE_HOLD_IDS,
    MIN_INVOICE_DATE,
    EXISTING_HEADER_IDS,
    PLUS10_HEADERS,
    PLUS5_HEADERS,
    PREFERRED_PLUS10,
    RETRY_HEADERS,
    PREFERRED_BATCH_NAME,
    PREFERRED_NEXT,
    VENDOR_NAME,
    blob_has_mcmaster,
    exact_invoice_number,
    is_credit_bill,
    is_mcmaster_invoice_email,
    is_mcmaster_message,
    is_mcmaster_vendor_text,
    apply_over_ppv_transfer_ap,
    is_missing_receipt_hold,
    is_over_ppv_price_hold,
    missing_receipt_hold_comment,
    over_ppv_hold_comment,
    pick_recent,
    quality_mcmaster_row,
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


def test_mcmaster_plus5_prefers_leftover_window_and_skips_first_five():
    bills = [
        {
            "invoice_number": inv,
            "date": "2026-09-17",
            "receivedDateTime": "2026-09-18T06:00:00Z",
            "po": "58221",
            "amount": 10.0,
            "subject": f"Invoice for Your Order {inv}",
        }
        for inv in (*PREFERRED_NEXT, "71668723")
    ]
    recent, leftover, credits = pick_recent(
        bills + [
            {
                "invoice_number": "71647463",
                "date": "2026-09-10",
                "po": "59161",
                "amount": 111.31,
                "subject": "Invoice for Your Order 59161",
            }
        ],
        already=set(CREATED_HEADERS),
        cap=5,
        receipts=None,
        preferred=PREFERRED_NEXT,
    )
    assert [b["invoice_number"] for b in recent] == list(PREFERRED_NEXT)
    assert "71647463" not in [b["invoice_number"] for b in recent]
    assert leftover[0]["invoice_number"] == "71668723"
    assert credits == []
    assert KNOWN_BATCH_ID == 721
    assert CREATED_HEADERS["72094446"] == 10142
    assert PLUS5_HEADERS["72068812"] == 10143
    assert PLUS5_HEADERS["71839575"] == 10147
    assert LEAVE_ALONE_HOLD_IDS == {10139, 10140, 10142, 10143, 10146, 10148, 10152}
    assert RETRY_HEADERS == {
        "71080498": 10139,
        "72094446": 10142,
        "72087570": 10144,
        "72012111": 10145,
        "72013304": 10146,
        "71839575": 10147,
    }
    assert DO_NOT_MUTATE_IDS == {10138, 10141, 10144, 10145, 10147, 10149, 10150, 10151}
    assert 720 in FORBIDDEN_BATCH_IDS


def test_never_repeat_fees_are_not_missing_receipt():
    """NOTE-44: 10147-class Fees are Success; 10142 merch leftovers stay HOLD."""
    parsed_10147 = {
        "invoice_number": "71839575",
        "po": "59191",
        "amount": 217.39,
        "lines": [
            {"part": "4082T15", "qty": 40.0, "unit_price": 4.67, "amount": 186.80, "label": "4082T15"},
            {"part": "SHIP", "qty": 1.0, "label": "Shipping", "amount": 30.59},
        ],
        "fees": [{"name": "Shipping", "amount": 30.59}],
    }
    proof_10147 = {
        "id": 10147,
        "invoice_number": "71839575",
        "invoice_amount": 217.39,
        "verification_amount": 217.39,
        "invoice_type": 3,
        "vendor_id": 117,
        "attachments": ["Invoice_71839575.PDF"],
        "receipt_lines": [{"receipt": 24245, "qty": 40.0, "unit": 4.67}],
        "fee_amounts": [30.59],
        "ppv_amounts": [],
    }
    enter = {
        "Vendor": VENDOR_NAME,
        "Invoice #": "71839575",
        "PO": "59191",
        "KIMCO id": 10147,
        "Result": "HOLD",
        "Exception category": "missing_receipt",
    }
    row = quality_mcmaster_row(
        None,
        parsed=parsed_10147,
        enter_row=enter,
        proof=proof_10147,
        finish={"select_status": "already-selected", "wanted": [{"id": 24245, "qty": 40.0}]},
        vendor_id=117,
    )
    assert row["Result"] == "Success"
    assert "missing_receipt" not in str(row.get("Exception category") or "")
    assert "24245" in row["Why"]
    assert "not missing merch" in row["Why"]
    assert "[{'id'" not in row["Why"]

    parsed_10142 = {
        "invoice_number": "72094446",
        "po": "59224",
        "amount": 131.21,
        "lines": [
            {"part": "3616N12", "qty": 6.0, "unit_price": 7.0, "amount": 42.0, "label": "3616N12"},
            {"part": "2151A27", "qty": 1.0, "unit_price": 23.18, "amount": 23.18, "label": "2151A27"},
            {"part": "5513T12", "qty": 1.0, "unit_price": 6.32, "amount": 6.32, "label": "5513T12"},
            {"part": "93320A385", "qty": 2.0, "unit_price": 17.56, "amount": 35.12, "label": "93320A385"},
        ],
        "fees": [{"name": "Shipping", "amount": 24.59}],
    }
    proof_10142 = {
        "id": 10142,
        "invoice_number": "72094446",
        "invoice_amount": 42.15,
        "verification_amount": 131.21,
        "invoice_type": 3,
        "vendor_id": 117,
        "attachments": ["Invoice_72094446.PDF"],
        "receipt_lines": [{"receipt": 24248, "qty": 1.0, "unit": 17.56}],
        "fee_amounts": [24.59],
        "ppv_amounts": [],
    }
    hold = quality_mcmaster_row(
        None,
        parsed=parsed_10142,
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "72094446",
            "PO": "59224",
            "KIMCO id": 10142,
            "Result": "HOLD",
        },
        proof=proof_10142,
        finish={"select_status": "already-selected", "wanted": []},
        vendor_id=117,
    )
    assert hold["Result"] == "HOLD"
    assert hold.get("Exception category") == "missing_receipt"
    assert hold.get("Exception owner") == "Shawn McKibben"
    assert "@Shawn McKibben" in hold["Why"]
    assert "Do not Transfer AP" in hold["Why"]


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


def test_mcmaster_transfer_ap_writes_comments_tab_not_header_string():
    class _Fake:
        def __init__(self):
            self.payloads = []
            self.tab = []

        def list_items(self, _name):
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]

        def update(self, _svc, _kid, payload):
            self.payloads.append(payload)
            rows = (payload.get("lists") or {}).get("Comments_1") or []
            if rows:
                self.tab = [{"id": 886, "values": rows[0]["values"]}]
            return {}, 200, ""

        def get_item(self, _svc, kid):
            return {
                "id": kid,
                "values": {
                    "Comments": "@Shawn McKibben HEADER MUST NOT COUNT",
                    "AP_Invoice_Batch": {"id": 375, "text": "TRANSFER AP"},
                },
                "lists": {"Comments_1": list(self.tab)},
            }

        def _record_url(self, _svc, _kid, suffix=""):
            return f"https://live.example/{_kid}/{suffix}"

        def request(self, _method, _url):
            class _Resp:
                status_code = 404

            return _Resp()

    fake = _Fake()
    comment = over_ppv_hold_comment(
        invoice_number="72068812", po="58221", pdf_amount=336.49
    )
    out = apply_over_ppv_transfer_ap(fake, kimco_id=10999, comment=comment)
    assert out["status"] == "moved"
    assert "Comments" not in fake.payloads[0].get("values", {})
    assert "Comments_1" in fake.payloads[1]["lists"]
    assert out["mention_notify"]["tab_persisted"] is True
    assert "Comments persisted" not in out["mention_notify"]["report"]
    assert apply_over_ppv_transfer_ap(fake, kimco_id=10140, comment=comment)["status"] == "leave-alone"
    assert apply_over_ppv_transfer_ap(fake, kimco_id=10999, comment=comment).get("mail_send") is False


def test_never_repeat_missing_receipt_never_transfer_ap():
    """NOTE-45: McMaster missing_receipt Comments_1 @Shawn; never Transfer AP."""
    from ap_clerk.comments_tab import (
        EXCEPTION_MAIL_SEND,
        SHAWN_MENTION_ID,
        apply_missing_receipt_comment_tab,
        comment_tab_add_payload,
    )
    from ap_clerk.quality_v12 import exception_owner_for
    from ap_clerk.rules import SHAWN_MCKIBBEN

    assert EXCEPTION_MAIL_SEND is False
    assert exception_owner_for("missing_receipt", vendor="McMaster-Carr Supply Company") == "Shawn McKibben"
    assert exception_owner_for("missing_receipt", vendor="Legacy Wire") == "Ruben Perez"
    assert exception_owner_for("price_variance", vendor="McMaster-Carr") == "Shawn McKibben"
    text = missing_receipt_hold_comment(invoice_number="71743140", po="59159", pdf_amount=35.85)
    assert SHAWN_MCKIBBEN in text
    assert "do not Transfer AP" in text
    assert "No email" in text
    assert is_missing_receipt_hold(
        {"Result": "HOLD", "Exception category": "missing_receipt", "Why": "HOLD (receipt)"}
    )
    assert not is_missing_receipt_hold(
        {"Result": "HOLD", "Exception category": "price_variance", "Why": "HOLD (price-does-not-match)"}
    )

    html_payload = comment_tab_add_payload("<p>x</p>", invoice_id=10950)
    assert "AP_Invoice_Batch" not in (html_payload.get("values") or {})

    class _Fake:
        def __init__(self):
            self.payloads = []

        def get_item(self, _svc, kid):
            comments = []
            if self.payloads:
                comments = [{"id": 900, "values": self.payloads[-1]["lists"]["Comments_1"][0]["values"]}]
            return {
                "id": kid,
                "values": {"AP_Invoice_Batch": {"id": 721, "text": "API Agent - 9/18/26 McMaster"}},
                "lists": {"Comments_1": comments},
            }

        def update(self, _svc, _kid, payload):
            self.payloads.append(payload)
            return {}, 200, ""

    fake = _Fake()
    out = apply_missing_receipt_comment_tab(
        fake,
        invoice_id=10950,
        body=text,
    )
    assert out["transfer_ap"] is False
    assert out["mail_send"] is False
    assert out["tab_persisted"] is True
    assert "AP_Invoice_Batch" not in (fake.payloads[0].get("values") or {})
    assert f'data-mention-id="{SHAWN_MENTION_ID}"' in fake.payloads[0]["lists"]["Comments_1"][0]["values"]["HtmlValue"]
    assert EXISTING_HEADER_IDS == set(range(10138, 10148))
    assert 10142 in EXISTING_HEADER_IDS
    assert PREFERRED_PLUS10[0] == "71743140"
    assert PLUS10_HEADERS == {
        "71743140": 10148,
        "71740547": 10149,
        "71642803": 10150,
        "71668723": 10151,
        "71401129": 10152,
    }

    bills = [
        {
            "invoice_number": inv,
            "date": "2026-09-11",
            "receivedDateTime": "2026-09-12T06:00:00Z",
            "po": "59159",
            "amount": 10.0,
            "subject": f"Invoice for Your Order {inv}",
        }
        for inv in PREFERRED_PLUS10
    ]
    recent, leftover, credits = pick_recent(
        bills
        + [
            {
                "invoice_number": "72068812",
                "date": "2026-09-17",
                "po": "58221",
                "amount": 336.49,
                "subject": "Invoice for Your Order 58221",
            }
        ],
        already=set(CREATED_HEADERS) | set(PLUS5_HEADERS),
        cap=5,
        receipts=None,
        preferred=PREFERRED_PLUS10,
    )
    assert [b["invoice_number"] for b in recent] == list(PREFERRED_PLUS10)
    assert "72068812" not in [b["invoice_number"] for b in recent]
    assert credits == []
