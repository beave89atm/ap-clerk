"""Legacy Wire 9/17 dedicated-batch discovery gates.

Distinctive Legacy Wire only. Invoice dates before 2026-08-01 are
reported, not entered (AQPC NOTE-28). Never reuse Crosslink 715 or
JPSteel 716. Invoice # is exactly PS-INV######. Packing slips ignored.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.pdf_invoice import extract_legacy_wire_bill, parse_invoice_text  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    extract_subject_invoice_number,
    match_receipts,
    names_match,
    rounding_ppv_to_hit_pdf_total,
)
from legacy_wire_0917 import (  # noqa: E402
    CAP,
    DO_NOT_MUTATE_IDS,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    KNOWN_ENTERED,
    MIN_INVOICE_DATE,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    _qty_hold,
    already_set,
    blob_has_legacy_wire,
    exact_invoice_number,
    invoice_aliases,
    is_legacy_wire_invoice_email,
    is_legacy_wire_message,
    is_legacy_wire_vendor_text,
    is_packing_slip_attachment,
    pick_recent,
    quality_legacy_row,
)


def _msg(
    *,
    subject: str,
    from_name: str = "",
    from_addr: str = "ap@example.com",
    categories: list[str] | None = None,
) -> dict:
    return {
        "id": "AAMk-legacy-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": from_addr}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_legacy_wire_distinctive_token_never_other_vendors():
    assert is_legacy_wire_message(_msg(subject="Legacy Wire Products - Sales Invoice PS-INV104100"))
    assert is_legacy_wire_message(_msg(subject="Sales Invoice PS-INV104101", from_name="Legacy Wire"))
    assert is_legacy_wire_message(_msg(subject="Invoice", from_name="LegacyWire Products"))
    assert is_legacy_wire_message(
        _msg(subject="Sales Invoice PS-INV104102", from_addr="ar@legacywire.com")
    )
    assert not is_legacy_wire_message(_msg(subject="Invoice 70762501 from MSC Industrial Supply"))
    assert not is_legacy_wire_message(_msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY"))
    assert not is_legacy_wire_message(_msg(subject="Invoice 10917 from AMERICAN QUALITY POWDER COATING"))
    assert not is_legacy_wire_message(_msg(subject="Invoice #28166 from Crosslink Powder Coating"))
    assert not is_legacy_wire_message(_msg(subject="Invoice 125315 from JP Steel"))
    assert not is_legacy_wire_message(_msg(subject="Wire invoice 88"))
    assert blob_has_legacy_wire("Legacy Wire Products")
    assert blob_has_legacy_wire("legacywire")
    assert not blob_has_legacy_wire("steel wire supply")
    assert not blob_has_legacy_wire("industrial wire")


def test_legacy_wire_names_match_and_vendor_text():
    assert names_match("Legacy Wire Products", "Legacy Wire Products")
    assert is_legacy_wire_vendor_text("Legacy Wire Products")
    assert is_legacy_wire_vendor_text("292-LEGACY WIRE PRODUCTS")
    assert is_legacy_wire_vendor_text("Legacy Wire")
    assert not is_legacy_wire_vendor_text("MSC Industrial Supply")
    assert not is_legacy_wire_vendor_text("JP Steel")
    assert VENDOR_NAME == "Legacy Wire Products"


def test_legacy_wire_invoice_number_is_exact_ps_inv():
    assert extract_subject_invoice_number("Sales Invoice PS-INV103979") == "PS-INV103979"
    assert extract_subject_invoice_number("Legacy Wire Products - Sales Invoice PS-INV103980") == "PS-INV103980"
    assert exact_invoice_number("PS-INV103979") == "PS-INV103979"
    assert exact_invoice_number("ps-inv103979") == "PS-INV103979"
    assert exact_invoice_number("PS-INV103979") != "103979"
    assert "103979" in invoice_aliases("PS-INV103979")
    assert "PS-INV103979" in invoice_aliases("103979")
    already = already_set({"PS-INV103979": 9995})
    assert "PS-INV103979" in already
    assert "103979" in already


def test_legacy_wire_packing_slip_is_not_an_invoice():
    assert is_packing_slip_attachment(filename="Receipt_2026-08-19_114745.pdf")
    assert is_packing_slip_attachment(
        filename="Receipt_2026-08-19_114744_dragged_.pdf",
        text="PACKING SLIP\nCustomer Signature\nReceived by: Kyle Cleaver",
    )
    assert not is_packing_slip_attachment(filename="Sales Invoice PS-INV103979.pdf")
    assert not is_legacy_wire_invoice_email(_msg(subject="Statement from Legacy Wire"))
    assert not is_legacy_wire_invoice_email(_msg(subject="Past due invoices — Legacy Wire"))
    assert is_legacy_wire_invoice_email(
        _msg(subject="Legacy Wire Products - Sales Invoice PS-INV104100")
    )
    reply = _msg(subject="Re: Legacy Wire Products - Sales Invoice PS-INV104100")
    reply["hasAttachments"] = False
    assert not is_legacy_wire_invoice_email(reply)


def test_legacy_wire_pick_recent_stops_before_aug_2026():
    recent, older, slips = pick_recent(
        [
            {
                "invoice_number": "PS-INV104200",
                "date": "2026-09-10",
                "receivedDateTime": "2026-09-11T12:00:00Z",
            },
            {
                "invoice_number": "PS-INV104010",
                "date": "2026-08-03",
                "receivedDateTime": "2026-08-04T12:00:00Z",
            },
            {
                "invoice_number": "PS-INV103800",
                "date": "2026-07-15",
                "receivedDateTime": "2026-08-14T12:00:00Z",
            },
            {
                "invoice_number": "",
                "hold_reason": "packing_slip",
                "skip_reason": "packing_slip",
                "ignored_packing_slips": ["Receipt_114745.pdf"],
            },
        ],
        already=set(),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["PS-INV104200", "PS-INV104010"]
    assert [b["invoice_number"] for b in older] == ["PS-INV103800"]
    assert len(slips) == 1
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"
    assert CAP == 5

    recent_only_old, older_only, slips_only = pick_recent(
        [
            {
                "invoice_number": "PS-INV103700",
                "date": "2026-07-20",
                "receivedDateTime": "2026-07-21T12:00:00Z",
            }
        ],
        already=set(),
        cap=5,
    )
    assert recent_only_old == []
    assert [b["invoice_number"] for b in older_only] == ["PS-INV103700"]
    assert slips_only == []


def test_legacy_wire_already_entered_and_cap():
    recent, older, slips = pick_recent(
        [
            {
                "invoice_number": "PS-INV104201",
                "date": "2026-09-12",
                "receivedDateTime": "2026-09-12T12:00:00Z",
            },
            {
                "invoice_number": "PS-INV103979",
                "date": "2026-08-19",
                "receivedDateTime": "2026-08-19T16:10:00Z",
            },
            {
                "invoice_number": "103980",
                "date": "2026-08-19",
                "receivedDateTime": "2026-08-19T16:20:00Z",
            },
        ],
        already=already_set(dict(KNOWN_ENTERED)),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["PS-INV104201"]
    assert older == []
    assert slips == []

    many = [
        {
            "invoice_number": f"PS-INV10430{i}",
            "date": "2026-09-01",
            "receivedDateTime": f"2026-09-01T0{i}:00:00Z",
        }
        for i in range(7)
    ]
    recent_capped, _, _ = pick_recent(many, already=set(), cap=5)
    assert len(recent_capped) == 5


def test_legacy_wire_batch_is_dedicated_not_715_or_716():
    assert PREFERRED_BATCH_NAME == "API Agent - 9/17/26 Legacy Wire"
    assert FALLBACK_BATCH_NAME == "API Agent - 9/17/26"
    assert FORBIDDEN_BATCH_IDS == {715, 716}
    assert "API Agent - 9/16/26" in FORBIDDEN_REUSE_NAMES
    assert "API Agent - 9/16/26 JPSteel" in FORBIDDEN_REUSE_NAMES
    assert PREFERRED_BATCH_NAME not in FORBIDDEN_REUSE_NAMES
    assert FALLBACK_BATCH_NAME not in FORBIDDEN_REUSE_NAMES
    assert 715 in FORBIDDEN_BATCH_IDS
    assert 716 in FORBIDDEN_BATCH_IDS
    cell = "API Agent - 9/17/26 Legacy Wire (717)"
    assert not re.search(r"\((715|716)\)", cell)
    assert KNOWN_ENTERED == {"PS-INV103979": 9995, "PS-INV103980": 9996}
    assert DO_NOT_MUTATE_IDS == {9995, 9996}


def test_legacy_wire_inches_are_not_rolled_qty():
    """77\" in a part description is not invoice qty (PS-INV103979 class)."""
    parsed = {
        "lines": [
            {"part": "KANNON-A-05480-002", "qty": 0.0, "description": 'BIFOLD GATE-77"(2"TUBE)'}
        ]
    }
    recs = [{"qty": 77.0, "unit": 0.0}]
    # Zero merch qty + leftover 77 is a hold (description inches leaked).
    parsed_real = {
        "lines": [
            {"part": "KANNON-A-12345-001", "qty": 24.0, "description": "GATE HARDWARE"},
            {"part": "KANNON-A-12345-002", "qty": 1.0, "description": "LATCH"},
        ]
    }
    assert _qty_hold(parsed_real, [{"qty": 77.0}]) is True
    assert _qty_hold(parsed_real, [{"qty": 24.0}, {"qty": 1.0}]) is False
    parsed_len = {"lines": [{"part": "BAR", "qty": 1.0, "length_inches": 32.0}]}
    assert _qty_hold(parsed_len, [{"qty": 32.0}]) is False
    assert parsed["lines"][0]["qty"] != 77


LEGACY_103979 = """
Legacy Wire Products
SALES INVOICE
Invoice No.
PS-INV103979
Invoice Date
08/19/2026
External Document No.
58807
KANNON-A-12345-001
GATE HARDWARE
24 Each 41.00 984.00
KANNON-A-12345-002
LATCH
1 Each 41.00 41.00
KANNON-A-05480-002
BIFOLD GATE-77"(2"TUBE)
0 Each 0.00 0.00
Freight Charge - Wholesale - In
State
1
246.75
246.75
Total $ Incl. Tax
1,271.75
"""


def test_legacy_wire_pdf_truth_and_freight_fees():
    parsed = parse_invoice_text(
        LEGACY_103979,
        filename="Sales Invoice PS-INV103979.pdf",
        subject="Sales Invoice PS-INV103979",
        from_name="Legacy Wire Products",
    )
    assert parsed["invoice_number"] == "PS-INV103979"
    assert parsed["invoice_number"] != "103979"
    assert parsed["po"] == "58807"
    assert parsed["amount"] == 1271.75
    qtys = [line.get("qty") for line in parsed["lines"]]
    assert 77 not in qtys
    assert 24 in qtys
    assert 1 in qtys
    assert any(
        float(fee.get("amount") or 0) == 246.75 for fee in (parsed.get("fees") or [])
    )
    lines, fees = extract_legacy_wire_bill(LEGACY_103979)
    assert {round(float(line["qty"]), 2) for line in lines} == {24.0, 1.0}
    assert any(float(fee.get("amount") or 0) == 246.75 for fee in fees)


def test_legacy_wire_combine_same_item_receipts():
    """NOTE-37: two leftovers of the same item/unit cover one invoice line."""
    match = match_receipts(
        invoice_number="PS-INV104300",
        invoice_lines=[{"part": "GATE", "qty": 21.0, "unit_price": 33.0, "amount": 693.0}],
        receipts=[
            {"id": 25001, "po": "59200", "part": "GATE", "qty": 8.0, "unit_price": 33.0, "amount": 264.0},
            {"id": 25002, "po": "59200", "part": "GATE", "qty": 13.0, "unit_price": 33.0, "amount": 429.0},
        ],
        po_number="59200",
        invoice_amount=693.0,
    )
    assert sorted((h.get("receipt") or {}).get("id") for h in (match.get("matched") or [])) == [
        25001,
        25002,
    ]
    assert not match.get("hold_no_receipts")


def test_legacy_wire_rounding_ppv_hits_pdf():
    """NOTE-38: in-gate rounding PPV, never HOLD. Do not mutate 9995/9996."""
    decision = rounding_ppv_to_hit_pdf_total(1580.73, 1580.83)
    assert decision["ppv"] == -0.10
    decision_pos = rounding_ppv_to_hit_pdf_total(1130.40, 1130.34)
    assert decision_pos["ppv"] == 0.06
    assert 9995 in DO_NOT_MUTATE_IDS
    assert 9996 in DO_NOT_MUTATE_IDS

    parsed = parse_invoice_text(
        LEGACY_103979,
        filename="Sales Invoice PS-INV103979.pdf",
        subject="Sales Invoice PS-INV103979",
        from_name="Legacy Wire Products",
    )
    row = quality_legacy_row(
        None,
        parsed={
            **parsed,
            "invoice_number": "PS-INV104316",
            "amount": 1580.73,
            "po": "59210",
            "lines": [{"qty": 10.0, "unit_price": 158.073, "amount": 1580.73}],
            "fees": [],
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV104316",
            "PO": "59210",
            "Amount": 1580.73,
            "Result": "HOLD",
            "KIMCO id": 10120,
            "Batch": "API Agent - 9/17/26 Legacy Wire (717)",
        },
        proof={
            "id": 10120,
            "invoice_number": "PS-INV104316",
            "invoice_amount": 1580.73,
            "verification": 1580.73,
            "invoice_type": 3,
            "vendor_id": 292,
            "batch_id": 717,
            "attachments": ["2026-09-17_Sales_Invoice_PS-INV104316.pdf"],
            "receipt_lines": [{"qty": 10.0, "unit": 158.083, "receipt": 25010}],
            "fee_amounts": [],
            "ppv_amounts": [-0.10],
        },
        finish={"select_status": "already-selected", "ppv_amount": -0.10, "ppv_status": "posted"},
        vendor_id=292,
    )
    assert row["Result"] == "Success"
    assert row["Invoice #"] == "PS-INV104316"
    assert row["PPV"] == "-0.10"


def test_legacy_wire_freight_as_fees_not_ppv_is_success():
    parsed = parse_invoice_text(
        LEGACY_103979,
        filename="Sales Invoice PS-INV103979.pdf",
        subject="Sales Invoice PS-INV103979",
        from_name="Legacy Wire Products",
    )
    row = quality_legacy_row(
        None,
        parsed=parsed,
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV103979",
            "PO": "58807",
            "Amount": 1271.75,
            "Result": "HOLD",
            "KIMCO id": 10121,
            "Batch": "API Agent - 9/17/26 Legacy Wire (717)",
        },
        proof={
            "id": 10121,
            "invoice_number": "PS-INV103979",
            "invoice_amount": 1271.75,
            "verification": 1271.75,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV103979.pdf"],
            "receipt_lines": [
                {"qty": 24.0, "unit": 41.0, "receipt": 23746},
                {"qty": 1.0, "unit": 41.0, "receipt": 23747},
            ],
            "fee_amounts": [246.75],
            "ppv_amounts": [],
        },
        finish={"select_status": "already-selected", "fee_status": "already-posted"},
        vendor_id=292,
    )
    assert row["Result"] == "Success"
    assert "246.75" in str(row.get("Fees and surcharges") or "")
    assert row["Invoice #"] == "PS-INV103979"

    hold_fees = quality_legacy_row(
        None,
        parsed=parsed,
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV103979",
            "PO": "58807",
            "Amount": 1271.75,
            "KIMCO id": 10122,
        },
        proof={
            "id": 10122,
            "invoice_number": "PS-INV103979",
            "invoice_amount": 1271.75,
            "verification": 1271.75,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV103979.pdf"],
            "receipt_lines": [
                {"qty": 24.0, "unit": 41.0, "receipt": 23746},
                {"qty": 1.0, "unit": 41.0, "receipt": 23747},
            ],
            "fee_amounts": [],
            "ppv_amounts": [246.75],
        },
        finish={"select_status": "already-selected"},
        vendor_id=292,
    )
    assert hold_fees["Result"] == "HOLD"
    assert "fees" in hold_fees["Why"].lower()
