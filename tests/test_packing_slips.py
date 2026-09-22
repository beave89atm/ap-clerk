"""NOTE-49: receiving@ signed packing slips. No live Graph or KIMCO I/O."""

from __future__ import annotations

from ap_clerk.gates import GATE_PACKING_SLIP, RESULT_HOLD, RESULT_SUCCESS, finish_gate
from ap_clerk.graph import ALLOWED_MAILBOX
from ap_clerk.packing_slips import (
    SHARP_MFP_FROM,
    group_consecutive_slip_pages,
    header_attachment_kind,
    header_has_signed_packing_slip,
    header_has_vendor_invoice_pdf,
    is_receiving_scan,
    is_sharp_mfp_scan,
    logical_slips_from_scan,
    match_logical_slips_to_invoice,
    packing_slip_attached_for_invoice,
    pages_from_texts,
    receiving_is_not_invoice_mailbox,
    refuse_enter_from_receiving,
)
from ap_clerk.pdf_invoice import ATTACHMENT_INVOICE, ATTACHMENT_PACKING_SLIP
from ap_clerk.quality_v12 import EXCEPTION_CATEGORY_OWNERS, classify_exception
from ap_clerk.receiving_probe import RECEIVING_MAILBOX


def test_receiving_is_not_invoice_mailbox():
    assert RECEIVING_MAILBOX == "receiving@kannonmfg.com"
    assert RECEIVING_MAILBOX != ALLOWED_MAILBOX
    assert receiving_is_not_invoice_mailbox(RECEIVING_MAILBOX) is True
    assert receiving_is_not_invoice_mailbox("Receiving@KannonMfg.com") is True
    assert receiving_is_not_invoice_mailbox(ALLOWED_MAILBOX) is False
    assert "Do not enter invoices from receiving@" in refuse_enter_from_receiving()


def test_sharp_mfp_scan_detection():
    assert is_sharp_mfp_scan(from_addr=SHARP_MFP_FROM) is True
    assert is_sharp_mfp_scan(subject="Scanned image from Kannon Manufacturing") is True
    assert is_sharp_mfp_scan(filename="Kannon Manufacturing_20260922_121158.pdf") is True
    assert is_receiving_scan(mailbox=RECEIVING_MAILBOX) is True
    assert is_sharp_mfp_scan(filename="Sales Invoice PS-INV103979.pdf") is False


def test_header_filename_heuristics_invoice_vs_slip():
    assert header_attachment_kind(filename="Sales Invoice PS-INV103979.pdf") == ATTACHMENT_INVOICE
    assert header_attachment_kind(filename="Invoice-71401129.pdf") == ATTACHMENT_INVOICE
    assert header_attachment_kind(filename="TXFT4100079.pdf") == ATTACHMENT_INVOICE
    assert header_attachment_kind(filename="Receipt_114745.pdf") == ATTACHMENT_PACKING_SLIP
    assert header_attachment_kind(filename="Kannon Manufacturing_20260922_121511.pdf") == ATTACHMENT_PACKING_SLIP
    assert header_attachment_kind(
        filename="scan.pdf",
        text="PACKING SLIP\nCustomer signature\nPO 59008",
    ) == ATTACHMENT_PACKING_SLIP
    names = [
        "Sales Invoice PS-INV103979.pdf",
        "Kannon Manufacturing_20260922_121158.pdf",
    ]
    assert header_has_vendor_invoice_pdf(names) is True
    assert header_has_signed_packing_slip(names) is True
    assert header_has_signed_packing_slip(["Sales Invoice PS-INV103979.pdf"]) is False
    assert packing_slip_attached_for_invoice({"packing_slip_attached": True}) is True
    assert packing_slip_attached_for_invoice({"packing_slip_attached": False}) is False
    assert packing_slip_attached_for_invoice({"header_attachments": names}) is True
    assert packing_slip_attached_for_invoice({"header_attachments": ["Invoice-10152.pdf"]}) is False


def _three_page_same_slip():
    return [
        "PACKING SLIP No. 8801\nPO 59008\nInvoice # 71401129\nPage 1 of 3\nline items",
        "PACKING SLIP No. 8801\nPO 59008\nPage 2 of 3\nmore items",
        "PACKING SLIP No. 8801\nPO 59008\nPage 3 of 3\nCustomer signature received by Ruben",
    ]


def _two_slips_two_pages_each():
    return [
        "PACKING SLIP No. A100\nPO 59008\nPage 1 of 2\nMcMaster-Carr",
        "PACKING SLIP No. A100\nPO 59008\nPage 2 of 2\nCustomer signature",
        "PACKING SLIP No. B200\nPO 59128\nPage 1 of 2\nJPSteel",
        "PACKING SLIP No. B200\nPO 59128\nPage 2 of 2\nReceived by",
    ]


def test_multi_page_slip_stays_one_logical_slip():
    slips = logical_slips_from_scan(
        _three_page_same_slip(),
        source_filename="Kannon Manufacturing_20260922_121158.pdf",
        from_addr=SHARP_MFP_FROM,
        subject="Scanned image from Kannon Manufacturing",
    )
    assert len(slips) == 1
    assert slips[0]["pages"] == [1, 2, 3]
    assert slips[0]["po"] == "59008"
    assert slips[0]["slip_number"] == "8801"
    assert slips[0]["signed"] is True
    # Never 1 page = 1 slip
    assert [len(s["pages"]) for s in slips] != [1, 1, 1]


def test_multi_slip_pdf_keeps_each_multi_page_slip_together():
    slips = logical_slips_from_scan(_two_slips_two_pages_each())
    assert len(slips) == 2
    assert slips[0]["pages"] == [1, 2]
    assert slips[0]["po"] == "59008"
    assert slips[1]["pages"] == [3, 4]
    assert slips[1]["po"] == "59128"
    assert slips[0]["slip_number"] == "A100"
    assert slips[1]["slip_number"] == "B200"


def test_signature_continuation_stays_with_current_slip():
    texts = [
        "PACKING SLIP\nPO 58692\nInvoice # TXFT4100079\nitems page",
        "Received by warehouse\nCustomer signature\nX ________",
    ]
    slips = logical_slips_from_scan(texts)
    assert len(slips) == 1
    assert slips[0]["pages"] == [1, 2]
    assert slips[0]["signed"] is True
    assert slips[0]["po"] == "58692"


def test_do_not_treat_one_page_as_one_slip_without_new_identity():
    texts = [
        "PACKING SLIP\nPO 58807\nline 1",
        "PO 58807\nline 2 continued",
        "PO 58807\nline 3 continued",
    ]
    slips = group_consecutive_slip_pages(pages_from_texts(texts))
    assert len(slips) == 1
    assert slips[0]["pages"] == [1, 2, 3]


def test_match_unique_po_attaches_that_slip_not_the_whole_foreign_pdf():
    slips = logical_slips_from_scan(_two_slips_two_pages_each())
    plan = match_logical_slips_to_invoice(slips, po="59008", invoice_number="71401129")
    assert plan["status"] == "matched"
    assert plan["hold"] is False
    assert plan["attach_whole_pdf"] is False
    assert plan["pages"] == [1, 2]
    assert plan["matched"][0]["po"] == "59008"

    other = match_logical_slips_to_invoice(slips, po="59128")
    assert other["pages"] == [3, 4]
    assert other["attach_whole_pdf"] is False


def test_whole_pdf_when_every_slip_covers_this_bill():
    slips = logical_slips_from_scan(_three_page_same_slip())
    plan = match_logical_slips_to_invoice(
        slips, po="59008", invoice_number="71401129"
    )
    assert plan["status"] == "matched"
    assert plan["attach_whole_pdf"] is True
    assert plan["pages"] == [1, 2, 3]


def test_no_match_holds():
    slips = logical_slips_from_scan(_three_page_same_slip())
    plan = match_logical_slips_to_invoice(slips, po="99999", invoice_number="NOPE")
    assert plan["hold"] is True
    assert plan["status"] == "no_match"
    assert "No receiving@" in plan["why"]


def test_invoice_pdf_alone_is_not_success():
    result, why = finish_gate(
        header_created=True,
        attach_status="attached",
        po="59008",
        receipts_selected=True,
        kimco_id=10152,
        packing_slip_attached=False,
    )
    assert result == RESULT_HOLD
    assert result != RESULT_SUCCESS
    assert GATE_PACKING_SLIP in why
    assert "signed packing slip" in why.lower()
    assert classify_exception(result=result, why=why) == (
        "missing_packing_slip",
        EXCEPTION_CATEGORY_OWNERS["missing_packing_slip"],
    )


def test_invoice_pdf_plus_packing_slip_can_succeed():
    result, why = finish_gate(
        header_created=True,
        attach_status="attached",
        po="59008",
        receipts_selected=True,
        kimco_id=10152,
        packing_slip_attached=True,
    )
    assert result == RESULT_SUCCESS, why
    assert why == ""
