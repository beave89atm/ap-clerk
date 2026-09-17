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
    decide_ppv,
    extract_subject_invoice_number,
    filter_matches_outside_ppv_gate,
    match_receipts,
    names_match,
    rounding_ppv_to_hit_pdf_total,
)
from legacy_wire_0917 import (  # noqa: E402
    CAP,
    CREATED_HEADERS,
    DO_NOT_MUTATE_IDS,
    FALLBACK_BATCH_NAME,
    FINISH_ORDER,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    KNOWN_ENTERED,
    LEAVE_ALONE_HOLD_IDS,
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
    assert CREATED_HEADERS == {
        "PS-INV104020": 10112,
        "PS-INV104019": 10113,
        "PS-INV104018": 10114,
        "PS-INV104015": 10115,
        "PS-INV104017": 10116,
    }
    assert LEAVE_ALONE_HOLD_IDS == {10116}
    assert FINISH_ORDER.index("PS-INV104018") < FINISH_ORDER.index("PS-INV104019")
    assert FINISH_ORDER[-1] == "PS-INV104017"


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


def test_legacy_wire_10116_one_at_41_vs_one_at_100_is_over_ppv_gate():
    """Kyle lock: leftover 1@$41 vs invoice 1@$100 on $114.28 is 51.6% — select zero."""
    from kyle_10116_leftover import one_at_41_vs_one_at_100

    gate = one_at_41_vs_one_at_100()
    assert gate["variance"] == 59.0
    assert gate["pct_of_invoice"] == 51.6
    assert gate["pct_limit"] == 11.43
    assert gate["decision"]["hold"] is True
    assert gate["select_zero"] is True
    assert gate["bill_over_ppv"] is True
    assert gate["kyle_lock_rule"] is True
    decision = decide_ppv(
        invoice_line_amount=100.0,
        po_line_amount=41.0,
        invoice_total=114.28,
        invoice_unit_price=100.0,
        po_unit_price=41.0,
        qty=1.0,
        label="A-05480-001",
    )
    assert decision["hold"] is True
    assert "51.6%" in decision["reason"]
    locked = filter_matches_outside_ppv_gate(
        [
            {
                "line": {"part": "A-05480-001", "qty": 1.0, "unit_price": 100.0, "amount": 100.0},
                "receipt": {"id": 23747, "qty": 1.0, "unit_price": 41.0, "amount": 41.0},
            }
        ],
        invoice_total=114.28,
    )
    assert locked["select_zero"] is True
    assert not locked["selectable"]


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
