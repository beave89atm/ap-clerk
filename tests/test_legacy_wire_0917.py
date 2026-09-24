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
from ap_clerk.quality_v12 import (  # noqa: E402
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    EXCEPTION_CATEGORY_OWNERS,
    apply_exception_category_owner,
)
from legacy_wire_0917 import (  # noqa: E402
    CAP,
    CREATED_HEADERS,
    DO_NOT_MUTATE_IDS,
    FALLBACK_BATCH_NAME,
    FINISH_10126,
    FINISH_10126_ID,
    FINISH_ORDER,
    FIRST_FIVE,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    KNOWN_BATCH_ID,
    KNOWN_ENTERED,
    LEAVE_ALONE_HOLD_IDS,
    PLUS5_HEADERS,
    PLUS10_HEADERS,
    PLUS10_SEARCH,
    MIN_INVOICE_DATE,
    NEXT_FIVE,
    PREFERRED_BATCH_NAME,
    PREFERRED_NEXT,
    RUBEN_PEREZ,
    TRANSFER_AP_PRIOR_ID_HINT,
    VENDOR_NAME,
    _qty_hold,
    already_set,
    apply_over_ppv_transfer_ap,
    blob_has_legacy_wire,
    exact_invoice_number,
    find_transfer_ap_batch,
    invoice_aliases,
    is_legacy_wire_invoice_email,
    is_legacy_wire_message,
    is_legacy_wire_vendor_text,
    is_over_ppv_price_hold,
    is_packing_slip_attachment,
    leftover_from_catalog,
    merge_sheet_rows,
    over_ppv_hold_comment,
    pick_recent,
    prefer_candidate_messages,
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
    assert CREATED_HEADERS == {
        "PS-INV104020": 10112,
        "PS-INV104019": 10113,
        "PS-INV104018": 10114,
        "PS-INV104015": 10115,
        "PS-INV104017": 10116,
    }
    assert LEAVE_ALONE_HOLD_IDS == {10116, 10123, 10125, 10127}
    assert FINISH_ORDER.index("PS-INV104018") < FINISH_ORDER.index("PS-INV104019")
    assert FINISH_ORDER[-1] == "PS-INV104017"
    assert KNOWN_BATCH_ID == 717
    assert FIRST_FIVE == (
        "PS-INV104020",
        "PS-INV104019",
        "PS-INV104018",
        "PS-INV104015",
        "PS-INV104017",
    )
    assert NEXT_FIVE == (
        "PS-INV104013",
        "PS-INV104012",
        "PS-INV104011",
        "PS-INV104010",
        "PS-INV104009",
    )
    assert PREFERRED_NEXT == NEXT_FIVE
    already = already_set({})
    for inv in CREATED_HEADERS:
        assert inv in already
        assert CREATED_HEADERS[inv] in {10112, 10113, 10114, 10115, 10116}
    assert PLUS5_HEADERS == {
        "PS-INV104013": 10123,
        "PS-INV104012": 10124,
        "PS-INV104011": 10125,
        "PS-INV104010": 10126,
        "PS-INV104009": 10127,
    }
    for inv in PLUS5_HEADERS:
        assert inv in already
    assert PLUS10_HEADERS == {}
    assert "PS-INV104014" in PLUS10_SEARCH
    assert "PS-INV104021" in PLUS10_SEARCH
    assert TRANSFER_AP_PRIOR_ID_HINT == 375


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


def test_legacy_wire_10113_does_not_steal_10114_qty17():
    """17@$36 takes 18@$36 qty 17. Do not steal leftover 17@$41 (10114)."""
    receipts = [
        {"id": 24188, "po": "59030", "part": "PO59030-01", "qty": 18.0, "unit_price": 36.0, "amount": 648.0},
        {"id": 24189, "po": "59030", "part": "PO59030-02", "qty": 9.0, "unit_price": 41.0, "amount": 369.0},
        {"id": 24190, "po": "59030", "part": "PO59030-03", "qty": 17.0, "unit_price": 41.0, "amount": 697.0},
        {"id": 24191, "po": "59030", "part": "PO59030-04", "qty": 27.0, "unit_price": 44.0, "amount": 1188.0},
    ]
    match_019 = match_receipts(
        invoice_number="PS-INV104019",
        invoice_lines=[
            {"part": "A-02390-000", "qty": 17.0, "unit_price": 36.0, "amount": 612.0},
            {"part": "A-06809-000", "qty": 27.0, "unit_price": 44.0, "amount": 1188.0},
        ],
        receipts=receipts,
        po_number="59030",
        invoice_amount=1800.0,
    )
    picked_019 = sorted(
        ((h.get("receipt") or {}).get("id"), h.get("select_qty"))
        for h in (match_019.get("matched") or [])
    )
    assert picked_019 == [(24188, 17.0), (24191, None)]
    assert not match_019.get("hold_no_receipts")

    match_018 = match_receipts(
        invoice_number="PS-INV104018",
        invoice_lines=[
            {"part": "75-10-201007", "qty": 9.0, "unit_price": 41.0, "amount": 369.0},
            {"part": "75-10-201007", "qty": 17.0, "unit_price": 41.0, "amount": 697.0},
        ],
        receipts=receipts,
        po_number="59030",
        invoice_amount=1066.0,
    )
    picked_018 = sorted((h.get("receipt") or {}).get("id") for h in (match_018.get("matched") or []))
    assert picked_018 == [24189, 24190]


def test_legacy_wire_10113_success_after_invented_ppv_removed():
    """Qty-17 of leftover 18@$36 + 27@$44. No −$85 PPV. Amount = PDF."""
    row = quality_legacy_row(
        None,
        parsed={
            "invoice_number": "PS-INV104019",
            "amount": 2600.0,
            "po": "59030",
            "lines": [
                {"part": "A-02390-000", "qty": 17.0, "unit_price": 36.0, "amount": 612.0},
                {"part": "A-06809-000", "qty": 27.0, "unit_price": 44.0, "amount": 1188.0},
            ],
            "fees": [{"name": "Freight Charge - Wholesale - In", "amount": 800.0, "fee": True}],
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV104019",
            "PO": "59030",
            "Amount": 2600.0,
            "KIMCO id": 10113,
            "Batch": "API Agent - 9/17/26 Legacy Wire (717)",
        },
        proof={
            "id": 10113,
            "invoice_number": "PS-INV104019",
            "invoice_amount": 2600.0,
            "verification": 2600.0,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV104019.pdf"],
            "receipt_lines": [
                {"qty": 17.0, "unit": 36.0, "receipt": 24188},
                {"qty": 27.0, "unit": 44.0, "receipt": 24191},
            ],
            "fee_amounts": [800.0],
            "ppv_amounts": [],
        },
        finish={"select_status": "already-selected", "fee_status": "already-posted", "ppv_amount": 0.0},
        vendor_id=292,
    )
    assert row["Result"] == "Success"
    assert row["PPV"] == "none"
    assert "24188" in str(row.get("Receipts") or "")
    assert "-85" not in str(row.get("Why") or "")

    still_invented = quality_legacy_row(
        None,
        parsed={
            "invoice_number": "PS-INV104019",
            "amount": 2600.0,
            "po": "59030",
            "lines": [
                {"part": "A-02390-000", "qty": 17.0, "unit_price": 36.0, "amount": 612.0},
                {"part": "A-06809-000", "qty": 27.0, "unit_price": 44.0, "amount": 1188.0},
            ],
            "fees": [{"name": "Freight Charge - Wholesale - In", "amount": 800.0, "fee": True}],
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV104019",
            "PO": "59030",
            "Amount": 2600.0,
            "KIMCO id": 10113,
        },
        proof={
            "id": 10113,
            "invoice_number": "PS-INV104019",
            "invoice_amount": 2685.0,
            "verification": 2600.0,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV104019.pdf"],
            "receipt_lines": [
                {"qty": 17.0, "unit": 36.0, "receipt": 24188},
                {"qty": 27.0, "unit": 44.0, "receipt": 24191},
            ],
            "fee_amounts": [800.0],
            "ppv_amounts": [-85.0, 170.0],
        },
        finish={"select_status": "already-selected"},
        vendor_id=292,
    )
    assert still_invented["Result"] != "Success"


def test_legacy_wire_10116_leave_alone_stays_hold():
    row = quality_legacy_row(
        None,
        parsed={
            "invoice_number": "PS-INV104017",
            "amount": 114.28,
            "po": "58807",
            "lines": [{"part": "A-05480-001", "qty": 1.0, "unit_price": 100.0, "amount": 100.0}],
            "fees": [{"name": "Freight Charge", "amount": 14.28, "fee": True}],
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV104017",
            "PO": "58807",
            "Amount": 114.28,
            "KIMCO id": 10116,
        },
        proof={
            "id": 10116,
            "invoice_number": "PS-INV104017",
            "invoice_amount": 14.28,
            "verification": 114.28,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV104017.pdf"],
            "receipt_lines": [],
            "fee_amounts": [14.28],
            "ppv_amounts": [],
        },
        finish={
            "select_status": "leave-alone",
            "select_zero": True,
            "skipped_over_ppv": True,
            "do_not_stamp_outlook": True,
        },
        vendor_id=292,
    )
    assert row["Result"] == "HOLD"
    assert "price-does-not-match" in row["Why"]
    assert "Shawn" in row["Why"]
    assert row["Receipts"] == "none"
    assert row[COL_EXCEPTION_CATEGORY] == "price_variance"
    assert row[COL_EXCEPTION_OWNER] == EXCEPTION_CATEGORY_OWNERS["price_variance"]
    assert "category=price_variance; owner=Shawn McKibben" in row["Why"]


def test_legacy_wire_no_receipt_why_names_this_invoice():
    """Why-must-be-true: empty leftovers are not the 77\" TUBE slogan."""
    row = quality_legacy_row(
        None,
        parsed={
            "invoice_number": "PS-INV104020",
            "amount": 1029.0,
            "po": "59034",
            "lines": [{"part": "75-10-201007", "qty": 19.0, "unit_price": 41.0, "amount": 779.0}],
            "fees": [{"name": "Freight Charge", "amount": 250.0, "fee": True}],
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "PS-INV104020",
            "PO": "59034",
            "Amount": 1029.0,
            "KIMCO id": 10112,
        },
        proof={
            "id": 10112,
            "invoice_number": "PS-INV104020",
            "invoice_amount": 250.0,
            "verification": 1029.0,
            "invoice_type": 3,
            "vendor_id": 292,
            "attachments": ["Sales Invoice PS-INV104020.pdf"],
            "receipt_lines": [],
            "fee_amounts": [250.0],
            "ppv_amounts": [],
        },
        finish={"select_status": "held-unfinished"},
        vendor_id=292,
    )
    assert row["Result"] == "HOLD"
    assert "PS-INV104020" in row["Why"]
    assert "59034" in row["Why"]
    assert "77" not in row["Why"]
    assert "TUBE" not in row["Why"]
    assert "no open receipt" in row["Why"]
    assert RUBEN_PEREZ in row["Why"]
    assert row[COL_EXCEPTION_CATEGORY] == "missing_receipt"
    assert row[COL_EXCEPTION_OWNER] == "Ruben Perez"


def test_legacy_wire_plus5_prefers_pending_list():
    """Plus-5 prefers 104013–104009 even when a newer unentered bill exists."""
    bills = [
        {
            "invoice_number": "PS-INV104050",
            "date": "2026-09-12",
            "receivedDateTime": "2026-09-12T12:00:00Z",
        },
        {
            "invoice_number": "PS-INV104013",
            "date": "2026-09-03",
            "receivedDateTime": "2026-09-03T17:54:28Z",
        },
        {
            "invoice_number": "PS-INV104012",
            "date": "2026-09-03",
            "receivedDateTime": "2026-09-03T17:46:56Z",
        },
        {
            "invoice_number": "PS-INV104011",
            "date": "2026-09-02",
            "receivedDateTime": "2026-09-02T20:37:00Z",
        },
        {
            "invoice_number": "PS-INV104010",
            "date": "2026-09-02",
            "receivedDateTime": "2026-09-02T19:54:16Z",
        },
        {
            "invoice_number": "PS-INV104009",
            "date": "2026-09-02",
            "receivedDateTime": "2026-09-02T19:07:54Z",
        },
        {
            "invoice_number": "PS-INV104020",
            "date": "2026-09-04",
            "receivedDateTime": "2026-09-09T19:28:55Z",
        },
        {
            "invoice_number": "PS-INV103800",
            "date": "2026-07-15",
            "receivedDateTime": "2026-07-16T12:00:00Z",
        },
    ]
    first_pass_already: set[str] = set()
    for number in list(KNOWN_ENTERED) + list(CREATED_HEADERS):
        first_pass_already |= invoice_aliases(number)
    recent, older, slips = pick_recent(
        bills,
        already=first_pass_already,
        cap=5,
        preferred=NEXT_FIVE,
    )
    assert [b["invoice_number"] for b in recent] == list(NEXT_FIVE)
    assert [b["invoice_number"] for b in older] == ["PS-INV103800"]
    assert slips == []

    by_inv = {
        bill["invoice_number"]: {
            "subject": f"Legacy Wire Products - Sales Invoice {bill['invoice_number']}",
            "receivedDateTime": bill["receivedDateTime"],
            "_wanted_invoice": bill["invoice_number"],
        }
        for bill in bills
        if bill["invoice_number"].startswith("PS-INV104")
    }
    preferred_msgs = prefer_candidate_messages(by_inv, preferred=NEXT_FIVE, cap=5)
    assert [m["_wanted_invoice"] for m in preferred_msgs[:5]] == list(NEXT_FIVE)


def test_legacy_wire_plus5_merges_prior_rows_and_leaves_10116():
    prior = [
        {"Invoice #": "PS-INV104020", "Result": "Success", "KIMCO id": 10112},
        {"Invoice #": "PS-INV104019", "Result": "Success", "KIMCO id": 10113},
        {"Invoice #": "PS-INV104018", "Result": "Success", "KIMCO id": 10114},
        {"Invoice #": "PS-INV104015", "Result": "Success", "KIMCO id": 10115},
        {
            "Invoice #": "PS-INV104017",
            "Result": "HOLD",
            "KIMCO id": 10116,
            "Why": "HOLD (price-does-not-match): leftover vs invoice line is over the PPV gate. @Shawn McKibben",
        },
    ]
    new = [
        {"Invoice #": "PS-INV104013", "Result": "Success", "KIMCO id": 10117},
    ]
    merged = merge_sheet_rows(prior, new)
    assert [r["Invoice #"] for r in merged] == [
        "PS-INV104020",
        "PS-INV104019",
        "PS-INV104018",
        "PS-INV104015",
        "PS-INV104017",
        "PS-INV104013",
    ]
    hold = next(r for r in merged if r["KIMCO id"] == 10116)
    assert hold["Result"] == "HOLD"
    tagged = apply_exception_category_owner(dict(hold))
    assert tagged[COL_EXCEPTION_CATEGORY] == "price_variance"
    assert tagged[COL_EXCEPTION_OWNER] == "Shawn McKibben"
    success = apply_exception_category_owner(dict(merged[0]))
    assert success[COL_EXCEPTION_CATEGORY] == ""
    assert success[COL_EXCEPTION_OWNER] == ""


def test_finish_10126_constants_and_sheet_replace():
    """Kyle 2026-09-17: finish 10126 on 717 after Shawn reprice. No Transfer AP."""
    assert FINISH_10126 == "PS-INV104010"
    assert FINISH_10126_ID == 10126
    assert PLUS5_HEADERS[FINISH_10126] == 10126
    assert 10126 not in LEAVE_ALONE_HOLD_IDS
    prior = [
        {
            "Invoice #": "PS-INV104010",
            "Result": "HOLD",
            "KIMCO id": 10126,
            "Batch": "API Agent - 9/17/26 Legacy Wire (717)",
        },
        {"Invoice #": "PS-INV104009", "Result": "HOLD", "KIMCO id": 10127},
    ]
    new = [
        {
            "Invoice #": "PS-INV104010",
            "Result": "Success",
            "KIMCO id": 10126,
            "Batch": "API Agent - 9/17/26 Legacy Wire (717)",
        }
    ]
    merged = merge_sheet_rows(prior, new)
    row = next(r for r in merged if r["KIMCO id"] == 10126)
    assert row["Result"] == "Success"
    assert "717" in row["Batch"]
    assert "Transfer AP" not in row["Batch"]
    assert next(r for r in merged if r["KIMCO id"] == 10127)["Result"] == "HOLD"


def test_over_ppv_hold_comment_tags_shawn_and_says_receipts_not_selected():
    text = over_ppv_hold_comment(
        invoice_number="PS-INV104100",
        po="59100",
        pdf_amount=250.0,
    )
    assert "@Shawn McKibben" in text
    assert "price-does-not-match" in text
    assert "over the PPV gate" in text
    assert "NOT selected" in text
    assert "PS-INV104100" in text
    assert "59100" in text
    assert "250.00" in text


def test_find_transfer_ap_batch_looks_up_name_never_invents_375():
    found = find_transfer_ap_batch(
        [
            {
                "id": 375,
                "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"},
            }
        ]
    )
    assert found == {"found": True, "id": 375, "name": "TRANSFER AP", "invent": False}

    titled = find_transfer_ap_batch(
        [{"id": 401, "values": {"AP_Invoice_Batch_ID": "Transfer AP"}}]
    )
    assert titled["found"] is True
    assert titled["id"] == 401
    assert titled["invent"] is False

    missing = find_transfer_ap_batch(
        [{"id": 717, "values": {"AP_Invoice_Batch_ID": "API Agent - 9/17/26 Legacy Wire"}}]
    )
    assert missing["found"] is False
    assert missing["id"] is None
    assert missing["invent"] is False
    assert missing["hint_ignored"] == 375


def test_is_over_ppv_price_hold_not_missing_receipt():
    assert is_over_ppv_price_hold(
        {"Result": "HOLD", "Exception category": "price_variance"},
        {"select_zero": True, "skipped_over_ppv": True},
    )
    assert is_over_ppv_price_hold(
        {"Result": "HOLD", "Why": "HOLD (price-does-not-match): leftover vs invoice line is over the PPV gate."},
        {"select_zero": True},
    )
    assert not is_over_ppv_price_hold(
        {"Result": "HOLD", "Exception category": "missing_receipt", "Why": "HOLD (receipt): no open receipt"},
        {"select_zero": False, "skipped_over_ppv": False},
    )
    assert not is_over_ppv_price_hold(
        {"Result": "Success", "Exception category": ""},
        {"select_zero": False},
    )


def test_apply_over_ppv_transfer_ap_skips_leave_alone_and_moves_new():
    assert (
        apply_over_ppv_transfer_ap(
            object(), kimco_id=10116, comment="@Shawn McKibben test"
        )["status"]
        == "leave-alone"
    )
    assert (
        apply_over_ppv_transfer_ap(
            object(), kimco_id=10123, comment="@Shawn McKibben test"
        )["status"]
        == "leave-alone"
    )

    class _Fake:
        def list_items(self, _name):
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]

        def update(self, _svc, _kid, payload):
            self.payload = payload
            return {}, 200, ""

        def get_item(self, _svc, kid):
            return {
                "id": kid,
                "values": {
                    "Comments": self.payload["values"]["Comments"],
                    "AP_Invoice_Batch": {"id": 375, "text": "TRANSFER AP"},
                },
                "lists": {},
            }

        def _record_url(self, _svc, _kid, suffix=""):
            return f"https://live.example/{_kid}/{suffix}"

        def request(self, _method, _url):
            class _Resp:
                status_code = 404

            return _Resp()

    fake = _Fake()
    comment = over_ppv_hold_comment(
        invoice_number="PS-INV104100", po="59100", pdf_amount=99
    )
    out = apply_over_ppv_transfer_ap(fake, kimco_id=10140, comment=comment)
    assert out["status"] == "moved"
    assert out["batch_id"] == 375
    assert out["batch_name"] == "TRANSFER AP"
    assert out["invent"] is False
    assert "@Shawn McKibben" in out["comment"]
    assert out["mention_notify"]["comments_persisted"] is True
    assert out["mention_notify"]["worked"] is False
    assert "not confirmed" in out["mention_notify"]["report"]


def test_leftover_from_catalog_skips_entered_and_pre_aug():
    catalog = [
        {
            "invoice": "PS-INV104020",
            "flagged": False,
            "received": "2026-09-09T19:28:55Z",
            "subject": "Legacy Wire Products - Sales Invoice PS-INV104020",
        },
        {
            "invoice": "PS-INV104013",
            "flagged": False,
            "received": "2026-09-03T17:54:28Z",
            "subject": "Legacy Wire Products - Sales Invoice PS-INV104013",
        },
        {
            "invoice": "PS-INV104100",
            "flagged": False,
            "received": "2026-09-16T12:00:00Z",
            "subject": "Legacy Wire Products - Sales Invoice PS-INV104100",
        },
        {
            "invoice": "PS-INV103800",
            "flagged": False,
            "received": "2026-07-15T12:00:00Z",
            "subject": "Legacy Wire Products - Sales Invoice PS-INV103800",
        },
        {
            "invoice": "PS-INV104101",
            "flagged": True,
            "received": "2026-09-16T13:00:00Z",
            "subject": "Legacy Wire Products - Sales Invoice PS-INV104101",
        },
    ]
    pending = leftover_from_catalog(catalog, entered={}, chosen=set())
    assert [p["invoice_number"] for p in pending] == ["PS-INV104100"]
