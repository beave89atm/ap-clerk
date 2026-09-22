"""NOTE-49 live attach matching. No Graph or KIMCO I/O."""

from __future__ import annotations

from ap_clerk.receiving_attach import (
    index_live_ap_invoice,
    unique_invoice_for_slip,
    pdf_bytes_for_pages,
    attach_name_for_slip,
)
from ap_clerk.receiving_attach import slip_result_row
from ap_clerk.packing_slips import decide_receiving_ai_completed, logical_slips_from_scan


def _inv(*, kid: int, number: str, vendor: str, po: str = "") -> dict:
    return index_live_ap_invoice(
        {
            "id": kid,
            "values": {
                "Invoice_Number": number,
                "Vendor": {"id": 1, "text": vendor},
                "Purchase_Order": {"id": 2, "text": f"{po}-{vendor}" if po else ""},
                "Void": False,
            },
        }
    )


def test_unique_invoice_number_match():
    invoices = [
        _inv(kid=10152, number="71401129", vendor="McMaster-Carr Supply Company", po="59008"),
        _inv(kid=10160, number="15460544", vendor="O'Neal Steel - Dallas (GP)", po="58920"),
    ]
    slip = {"pages": [1], "invoice_number": "71401129", "po": None, "slip_number": None}
    hit = unique_invoice_for_slip(slip, invoices)
    assert hit["status"] == "matched"
    assert hit["invoice"]["id"] == 10152


def test_unique_po_match_and_ambiguous_same_po():
    invoices = [
        _inv(kid=10160, number="15460544", vendor="O'Neal Steel - Dallas (GP)", po="58920"),
        _inv(kid=10161, number="15460995", vendor="O'Neal Steel - Dallas (GP)", po="59085"),
    ]
    one = unique_invoice_for_slip({"pages": [1], "po": "58920", "invoice_number": None}, invoices)
    assert one["status"] == "matched"
    assert one["invoice"]["id"] == 10160

    same_po = [
        _inv(kid=1, number="A", vendor="O'Neal Steel - Dallas (GP)", po="58920"),
        _inv(kid=2, number="B", vendor="O'Neal Steel - Dallas (GP)", po="58920"),
    ]
    amb = unique_invoice_for_slip({"pages": [1], "po": "58920", "invoice_number": None}, same_po)
    assert amb["status"] == "ambiguous"
    assert amb["invoice"] is None
    assert len(amb["candidates"]) == 2


def test_no_invent_when_unidentifiable_or_missing():
    invoices = [_inv(kid=9, number="X", vendor="Fastenal Company", po="58700")]
    none = unique_invoice_for_slip({"pages": [1], "po": None, "invoice_number": None}, invoices)
    assert none["status"] == "unidentifiable"
    miss = unique_invoice_for_slip({"pages": [1], "po": "59999", "invoice_number": "NOPE"}, invoices)
    assert miss["status"] == "no_match"


def test_vendor_alone_is_not_a_match():
    invoices = [_inv(kid=9, number="X", vendor="Fastenal Company", po="58700")]
    hit = unique_invoice_for_slip(
        {"pages": [1], "vendor": "Fastenal Company", "po": None, "invoice_number": None},
        invoices,
    )
    assert hit["status"] == "unidentifiable"


def test_unread_extra_pages_block_ai_completed():
    attached = slip_result_row(
        {"pages": [1], "po": "59235", "invoice_number": None},
        status="attached",
        verified=True,
        invoice_id=10144,
    )
    unread = slip_result_row(
        {"pages": [2], "po": None, "invoice_number": None},
        status="unidentifiable",
        verified=False,
    )
    decision = decide_receiving_ai_completed([attached, unread])
    assert decision["stamp"] is False
    assert decision["leftover"]


def test_partial_multi_slip_does_not_stamp_ai_completed():
    slips = logical_slips_from_scan(
        [
            "PACKING SLIP No. 8801\nPO 59008\nPage 1 of 1",
            "PACKING SLIP No. 9902\nPO 59128\nPage 1 of 1",
        ],
        source_filename="Kannon Manufacturing_20260922_073207.pdf",
        from_addr="scans@sharp-mfp.com",
    )
    assert len(slips) == 2
    rows = [
        slip_result_row(slips[0], status="attached", verified=True, invoice_id=1),
        slip_result_row(slips[1], status="no_match", verified=False),
    ]
    decision = decide_receiving_ai_completed(rows)
    assert decision["stamp"] is False
    assert decision["leftover"]


def test_mcmaster_zip_30135_is_not_a_po():
    from ap_clerk.packing_slips import extract_page_keys, extract_slip_po

    zip_only = (
        "McMASTER-CARR Packing List\n"
        "1901 Riverside Pkwy Douglasville GA 30135-3150\n"
        "Kannon Manufacturing Inc 5129 Vesta Farley Rd Fort Worth TX 76119\n"
    )
    assert extract_slip_po(zip_only) is None
    assert extract_page_keys(zip_only, page_index=1)["po"] is None

    real = zip_only + "\nThe purchase order number was changed from PO59235 to 59235.\n"
    assert extract_slip_po(real) == "59235"
    labeled = zip_only + "\nPurchase Order 59224\n"
    assert extract_slip_po(labeled) == "59224"


def test_attach_name_keeps_sharp_filename_when_whole_pdf():
    name = attach_name_for_slip(
        {"pages": [1, 2], "po": "59008"},
        source_filename="Kannon Manufacturing_20260922_073207.pdf",
        split=False,
    )
    assert name == "Kannon Manufacturing_20260922_073207.pdf"
