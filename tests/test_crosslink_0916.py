"""Crosslink 9/16 focused-batch discovery gates.

Distinctive Crosslink only. NOTE-30 reminders stay already-entered.
Invoice dates before 2026-08-01 are reported, not entered (AQPC lesson).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.pdf_invoice import extract_crosslink_bill, parse_invoice_text  # noqa: E402
from crosslink_0916 import (  # noqa: E402
    CREATED_HEADERS,
    FIRST_FIVE,
    MIN_INVOICE_DATE,
    NEXT_FIVE,
    NOTE30_REMINDERS,
    PREFERRED_FIVE,
    VENDOR_NAME,
    is_crosslink_invoice_email,
    is_crosslink_message,
    merge_sheet_rows,
    pick_recent,
)

CROSSLINK_28100 = """
Invoice
INVOICE #
DATE
28100
09/02/2026
Crosslink Powder Coating
SUBTOTAL: $1,245.81
TAX: $0.00
Line Name Coating Color Product PO # Unit Price Qty Line Total 
1020249-1 Sandblasting
Zinc Rich Epoxy Primer -
Tiger
Time White - IFS
59022-01 205.58 6 $1233.48
Packaging/Shop Supplies
Recovery
Packaging/Shop
Supplies
Recovery
59022-01 0.01 1233 $12.33
Part Number Description Packing Slip # SO# PO # WO#
1020249-1 1020249-1 - 46.250" TALL
PEDESTAL WELDMENT
Premasking before
sandblast
5317 8421 59022-01 12296
TOTAL:
$1,245.81
"""

CROSSLINK_28102 = """
Invoice
INVOICE #
28102
09/02/2026
Crosslink Powder Coating
SUBTOTAL: $2,104.11
Line Name Coating Color Product PO # Unit Price Qty Line Total 
1020249-1 Sandblasting
Zinc Rich Epoxy Primer -
Tiger
Time White - PPG
58902 205.58 8 $1644.64
1020249-2 Sandblasting
Zinc Rich Epoxy Primer -
Tiger
Time White - PPG
58902 219.32 2 $438.64
Packaging/Shop Supplies
Recovery
Packaging/Shop
Supplies
Recovery
58902 0.01 2083 $20.83
Part Number Description Packing Slip # SO# PO # WO#
1020249-2 1020249-2 - 52.25" TALL
PEDESTAL WELDMENT
5239 8242 58902 12036
TOTAL:
$2,104.11
"""

CROSSLINK_28166 = """
Invoice
INVOICE #
28166
09/09/2026
Crosslink Powder Coating
SUBTOTAL: $200.00
Line Name Coating Color Product PO # Unit Price Qty Line Total 
Customer Touchup 59087 25.00 8 $200.00
Part Number Description Packing Slip # SO# PO # WO#
Customer Touchup Customer Touchup 5357 8497 59087 12390
TOTAL: $200.00
"""


def _msg(*, subject: str, from_name: str = "", categories: list[str] | None = None) -> dict:
    return {
        "id": "AAMk-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": "ap@example.com"}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_crosslink_distinctive_token_never_msc_rmp():
    assert is_crosslink_message(
        _msg(subject="Invoice #27943 for 58888 from Crosslink Powder Coating")
    )
    assert is_crosslink_message(
        _msg(subject="Invoice 28001", from_name="Crosslink Powder Coating of TX, LLC")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 70762501 from MSC Industrial Supply")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY")
    )
    assert not is_crosslink_message(
        _msg(subject="Invoice 10917 from AMERICAN QUALITY POWDER COATING")
    )


def test_crosslink_pick_recent_stops_before_aug_2026():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "28010",
                "date": "2026-08-12",
                "receivedDateTime": "2026-08-13T12:00:00Z",
            },
            {
                "invoice_number": "27999",
                "date": "2026-08-03",
                "receivedDateTime": "2026-08-04T12:00:00Z",
            },
            {
                "invoice_number": "27447",
                "date": "2026-07-07",
                "receivedDateTime": "2026-08-20T01:00:00Z",
            },
        ],
        already=set(NOTE30_REMINDERS),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["28010", "27999"]
    assert older == []
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"

    recent_only_old, older_only = pick_recent(
        [
            {
                "invoice_number": "27001",
                "date": "2026-07-15",
                "receivedDateTime": "2026-07-16T12:00:00Z",
            }
        ],
        already=set(),
        cap=5,
    )
    assert recent_only_old == []
    assert [b["invoice_number"] for b in older_only] == ["27001"]


def test_crosslink_note30_reminders_not_recreated():
    assert NOTE30_REMINDERS == {"27447": 9382, "27448": 9384, "27591": 9587}
    recent, older = pick_recent(
        [
            {
                "invoice_number": "27447",
                "date": "2026-08-20",
                "receivedDateTime": "2026-08-20T01:00:00Z",
            },
            {
                "invoice_number": "27591",
                "date": "2026-08-20",
                "receivedDateTime": "2026-08-20T02:00:00Z",
            },
        ],
        already=set(NOTE30_REMINDERS),
        cap=5,
    )
    assert recent == []
    assert older == []
    assert VENDOR_NAME == "Crosslink Powder Coating"
    assert FIRST_FIVE == ["28166", "28113", "28114", "28100", "28102"]
    assert CREATED_HEADERS["28166"] == 10101
    assert CREATED_HEADERS["28008"] == 10106
    assert PREFERRED_FIVE == NEXT_FIVE == ["28008"]


def test_crosslink_statement_and_past_due_are_not_invoices():
    assert not is_crosslink_invoice_email(
        _msg(subject="Statement from Crosslink Powder Coating of TX, LLC")
    )
    assert not is_crosslink_invoice_email(
        _msg(
            subject="Friendly payment reminder, as your account with us now appears as past due",
            from_name="Crosslink Powder Coating of Texas",
        )
    )
    assert is_crosslink_invoice_email(
        _msg(subject="Invoice #28166 for 59087 (#8497) from Crosslink Powder Coating")
    )


def test_crosslink_lines_and_supply_fee_not_ppv():
    """SH:27591 class: Packaging/Shop Supplies Recovery is Fees. 46.250\" is height."""
    lines, fees = extract_crosslink_bill(CROSSLINK_28100)
    assert [(ln["part"], ln["qty"], ln["unit_price"], ln["amount"]) for ln in lines] == [
        ("1020249-1", 6.0, 205.58, 1233.48)
    ]
    assert fees == [
        {"name": "Packaging/Shop Supplies Recovery", "amount": 12.33, "fee": True}
    ]
    parsed = parse_invoice_text(
        CROSSLINK_28100,
        subject="Invoice #28100 for 59022-01 from Crosslink Powder Coating",
        from_name="Crosslink Powder Coating",
    )
    assert parsed["amount"] == 1245.81
    assert parsed["po"] == "59022"
    assert len(parsed["lines"]) == 1
    assert parsed["lines"][0]["qty"] == 6.0
    assert parsed["fees"] == [
        {"name": "Packaging/Shop Supplies Recovery", "amount": 12.33, "fee": True}
    ]
    fee_amts = [f["amount"] for f in parsed["fees"]]
    assert 46.25 not in fee_amts
    assert 46.250 not in fee_amts

    two, two_fees = extract_crosslink_bill(CROSSLINK_28102)
    assert [(ln["part"], ln["qty"], ln["amount"]) for ln in two] == [
        ("1020249-1", 8.0, 1644.64),
        ("1020249-2", 2.0, 438.64),
    ]
    assert two_fees == [
        {"name": "Packaging/Shop Supplies Recovery", "amount": 20.83, "fee": True}
    ]
    assert 52.25 not in [f["amount"] for f in two_fees]

    touch, touch_fees = extract_crosslink_bill(CROSSLINK_28166)
    assert [(ln["part"], ln["qty"], ln["amount"]) for ln in touch] == [
        ("Customer Touchup", 8.0, 200.0)
    ]
    assert touch_fees == []


def test_crosslink_same_unit_leftover_cover():
    """28113 3+1 and 28114 1+2+2: unique same-unit cover, no mixed-unit guess."""
    from ap_clerk.rules import match_receipts, match_same_unit_qty_cover

    cover_28113 = match_same_unit_qty_cover(
        {"part": "1020249-1", "qty": 4.0, "unit_price": 205.58, "amount": 822.32},
        [
            {"id": 23605, "po": "59038", "part": "PO59038-01", "qty": 3.0, "unit_price": 193.94},
            {"id": 23794, "po": "59038", "part": "PO59038-01", "qty": 1.0, "unit_price": 193.94},
        ],
    )
    assert sorted(r["id"] for r in (cover_28113 or [])) == [23605, 23794]

    cover_28114 = match_same_unit_qty_cover(
        {"part": "1020249-2", "qty": 5.0, "unit_price": 232.48, "amount": 1162.40},
        [
            {"id": 23675, "po": "59052", "part": "PO59052-01", "qty": 1.0, "unit_price": 219.32},
            {"id": 23676, "po": "59052", "part": "PO59052-02", "qty": 1.0, "unit_price": 232.48},
            {"id": 23795, "po": "59052", "part": "PO59052-01", "qty": 2.0, "unit_price": 219.32},
            {"id": 23882, "po": "59052", "part": "PO59052-01", "qty": 2.0, "unit_price": 219.32},
        ],
    )
    assert sorted(r["id"] for r in (cover_28114 or [])) == [23675, 23795, 23882]

    mixed = match_same_unit_qty_cover(
        {"part": "1020249-2", "qty": 5.0, "unit_price": 232.48, "amount": 1162.40},
        [
            {"id": 1, "po": "59052", "qty": 3.0, "unit_price": 219.32},
            {"id": 2, "po": "59052", "qty": 2.0, "unit_price": 219.32},
            {"id": 3, "po": "59052", "qty": 3.0, "unit_price": 232.48},
            {"id": 4, "po": "59052", "qty": 2.0, "unit_price": 232.48},
        ],
    )
    assert sorted(r["id"] for r in (mixed or [])) == [3, 4]

    amb = match_same_unit_qty_cover(
        {"part": "X", "qty": 4.0, "unit_price": 10.0, "amount": 40.0},
        [
            {"id": 1, "po": "1", "qty": 2.0, "unit_price": 9.0},
            {"id": 2, "po": "1", "qty": 2.0, "unit_price": 9.0},
            {"id": 3, "po": "1", "qty": 2.0, "unit_price": 9.0},
        ],
    )
    assert amb is None

    match = match_receipts(
        invoice_number="28113",
        invoice_lines=[{"part": "1020249-1", "qty": 4.0, "unit_price": 205.58, "amount": 822.32}],
        receipts=[
            {"id": 23605, "po": "59038", "part": "PO59038-01", "qty": 3.0, "unit_price": 193.94, "amount": 581.82},
            {"id": 23794, "po": "59038", "part": "PO59038-01", "qty": 1.0, "unit_price": 193.94, "amount": 193.94},
        ],
        po_number="59038",
    )
    assert not match.get("hold_no_receipts")
    assert sorted((h.get("receipt") or {}).get("id") for h in (match.get("matched") or [])) == [
        23605,
        23794,
    ]
    assert all("same-unit" in str(h.get("how") or "") for h in (match.get("matched") or []))


def test_crosslink_combined_leftover_ppv_stays_in_gate():
    """28113/28114 split leftovers: PPV is the line gap, not each receipt vs full line."""
    from ap_clerk.rules import filter_matches_outside_ppv_gate

    line = {"part": "1020249-1", "qty": 4.0, "unit_price": 205.58, "amount": 822.32}
    locked = filter_matches_outside_ppv_gate(
        [
            {
                "line": line,
                "receipt": {"id": 23605, "qty": 3.0, "unit_price": 193.94, "amount": 581.82},
            },
            {
                "line": line,
                "receipt": {"id": 23794, "qty": 1.0, "unit_price": 193.94, "amount": 193.94},
            },
        ],
        invoice_total=830.54,
    )
    assert locked["select_zero"] is False
    assert sorted(h["receipt"]["id"] for h in locked["selectable"]) == [23605, 23794]
    assert locked["skipped"] == []


class _FinishKimco:
    target = "live"

    def __init__(self, receipts_by_id: dict[int, dict], invoice: dict | None = None):
        self.receipts_by_id = receipts_by_id
        self.invoice = invoice or {
            "id": 10102,
            "values": {
                "Invoice_Number": "28113",
                "Vendor": {"id": 278, "text": "1276-Crosslink Powder Coating"},
                "Purchase_Order": {"id": 1, "text": "PO59038-Crosslink Powder Coating"},
                "Invoice_Type": 3,
                "Invoice_Amount": 0.0,
                "Invoice_Verification_Amount": 830.54,
                "AP_Invoice_Batch": {"id": 715, "text": "API Agent - 9/16/26"},
            },
            "lists": {"APInvoiceLine": [], "InvoiceAdditionalCharges": []},
        }
        self.selected: list[tuple[int, list]] = []
        self.fees: list[tuple[int, list]] = []
        self.ppv: list[tuple[int, float]] = []
        self.attachments = [{"name": "invoice-28113.pdf"}]

    def get_item(self, service, item_id):
        if service == "receipts":
            return self.receipts_by_id[int(item_id)]
        if service == "ap_invoices":
            return self.invoice
        raise KeyError(service)

    def list_attachments(self, invoice_id):
        return list(self.attachments)

    def try_select_receipts(self, invoice_id, receipt_ids=None):
        refs = list(receipt_ids or [])
        self.selected.append((invoice_id, refs))
        lines = []
        for raw in refs:
            rid = raw.get("id") if isinstance(raw, dict) else raw
            rec = self.receipts_by_id[int(rid)]["values"]
            lines.append(
                {
                    "values": {
                        "Receipt": {"id": int(rid)},
                        "Quantity": rec.get("Quantity_Received"),
                        "Unit_Price": rec.get("Unit_Price"),
                    }
                }
            )
        self.invoice["lists"]["APInvoiceLine"] = lines
        merch = 0.0
        for line in lines:
            q = line["values"]["Quantity"]
            u = line["values"]["Unit_Price"]
            merch = round(merch + q * u, 2)
        self.invoice["values"]["Invoice_Amount"] = merch
        return "selected"

    def try_post_fees(self, invoice_id, fees=None):
        self.fees.append((invoice_id, list(fees or [])))
        charges = list(self.invoice["lists"].get("InvoiceAdditionalCharges") or [])
        for fee in fees or []:
            charges.append(
                {
                    "values": {
                        "Additional_Charges": {"id": 11, "text": "F-Fees & Surcharges"},
                        "Amount": fee.get("amount"),
                        "Description": fee.get("name"),
                    }
                }
            )
        self.invoice["lists"]["InvoiceAdditionalCharges"] = charges
        self.invoice["values"]["Invoice_Amount"] = 830.54
        return "posted"

    def try_post_ppv(self, invoice_id, amount):
        self.ppv.append((invoice_id, float(amount)))
        charges = list(self.invoice["lists"].get("InvoiceAdditionalCharges") or [])
        charges.append(
            {
                "values": {
                    "Additional_Charges": {"id": 13, "text": "Purchase Price Variance"},
                    "Amount": amount,
                    "Description": "Purchase Price Variance",
                }
            }
        )
        self.invoice["lists"]["InvoiceAdditionalCharges"] = charges
        self.invoice["values"]["Invoice_Amount"] = 830.54
        return "posted"


def _receipt_record(rid: int, *, qty: float, unit: float, po: str, part: str) -> dict:
    return {
        "id": rid,
        "values": {
            "Name": f"PO{po}-01",
            "Purchase_Order_Number": po,
            "Quantity_Received": qty,
            "Unit_Price": unit,
            "Amount": round(qty * unit, 2),
            "PO_Item_Number": part,
            "Invoiced": False,
        },
    }


def test_crosslink_finish_hydrates_null_qty_then_selects_fees_not_ppv():
    """List-view qty=None must GET-hydrate before Select Receipts. Supply fee ≠ PPV."""
    from crosslink_0916 import finish_hold_header, quality_crosslink_row

    hydrated = {
        23605: _receipt_record(23605, qty=3.0, unit=193.94, po="59038", part="PO59038-01"),
        23794: _receipt_record(23794, qty=1.0, unit=193.94, po="59038", part="PO59038-01"),
    }
    client = _FinishKimco(hydrated)
    list_view = [
        {"id": 23605, "po": "59038", "part": "PO59038-01", "qty": None, "unit_price": None},
        {"id": 23794, "po": "59038", "part": "PO59038-01", "qty": None, "unit_price": None},
    ]
    parsed = {
        "invoice_number": "28113",
        "po": "59038",
        "amount": 830.54,
        "lines": [
            {"part": "1020249-1", "qty": 4.0, "unit_price": 205.58, "amount": 822.32}
        ],
        "fees": [{"name": "Packaging/Shop Supplies Recovery", "amount": 8.22, "fee": True}],
        "graph_message_id": "AAMk-28113",
    }
    finish = finish_hold_header(client, parsed=parsed, kimco_id=10102, receipts=list_view)
    assert finish["select_status"] == "selected"
    assert sorted(finish["wanted"]) == [23605, 23794]
    assert finish["fee_status"] == "posted"
    assert client.fees and client.fees[0][1][0]["amount"] == 8.22
    assert finish["ppv_status"] == "posted"
    assert abs(finish["ppv_amount"] - 46.56) <= 0.02
    assert client.ppv and abs(client.ppv[0][1] - 46.56) <= 0.02
    row = quality_crosslink_row(
        None,
        parsed=parsed,
        enter_row={"Vendor": VENDOR_NAME, "Invoice #": "28113", "KIMCO id": 10102},
        proof=finish["after"],
        finish=finish,
    )
    assert row["Result"] == "Success"
    assert row["Flag status"] == "entered-in-ai"
    assert "8.22" in str(row["Fees and surcharges"])


def test_crosslink_finish_quality_does_not_invent_success():
    """Header + PDF with zero receipts stays HOLD. invent=false."""
    from crosslink_0916 import quality_crosslink_row

    proof = {
        "id": 10104,
        "invoice_type": 3,
        "vendor_id": 278,
        "invoice_amount": 0.0,
        "verification_amount": 1245.81,
        "receipt_lines": [],
        "attachments": ["invoice-28100.pdf"],
        "fee_amounts": [],
        "ppv_amounts": [],
    }
    row = quality_crosslink_row(
        None,
        parsed={
            "invoice_number": "28100",
            "po": "59022",
            "amount": 1245.81,
            "lines": [{"part": "1020249-1", "qty": 6.0, "unit_price": 205.58, "amount": 1233.48}],
            "fees": [{"name": "Packaging/Shop Supplies Recovery", "amount": 12.33}],
        },
        enter_row={"Vendor": VENDOR_NAME, "Invoice #": "28100", "KIMCO id": 10104},
        proof=proof,
        finish={"select_status": "held-unfinished", "wanted": []},
    )
    assert row["Result"] == "HOLD"
    assert row["Flag status"] == "entered-with-issues"
    assert "invent" in row["Why"].lower() or "Do not invent" in row["Why"]


def test_crosslink_over_ppv_finish_does_not_select():
    """Kyle lock: over-gate leftover must not Select Receipts."""
    from crosslink_0916 import finish_hold_header

    recs = {
        1: _receipt_record(1, qty=6.0, unit=50.0, po="59022", part="X"),
    }
    client = _FinishKimco(recs)
    parsed = {
        "invoice_number": "28100",
        "po": "59022",
        "amount": 1245.81,
        "lines": [{"part": "X", "qty": 6.0, "unit_price": 205.58, "amount": 1233.48}],
        "fees": [{"name": "Packaging/Shop Supplies Recovery", "amount": 12.33}],
    }
    finish = finish_hold_header(
        client,
        parsed=parsed,
        kimco_id=10104,
        receipts=[{"id": 1, "po": "59022", "qty": 6.0, "unit_price": 50.0, "amount": 300.0}],
    )
    assert finish["select_zero"] or finish["skipped_over_ppv"]
    assert finish["select_status"] == "ppv-lock-select-zero"
    assert client.selected == []
    assert client.fees == []
    assert client.ppv == []


def test_crosslink_plus5_starts_28008_and_skips_pre_aug():
    """Second pass: 28008 if open; July 27557 is leftover, not entered."""
    recent, older = pick_recent(
        [
            {
                "invoice_number": "28008",
                "date": "2026-08-26",
                "receivedDateTime": "2026-08-26T21:23:40Z",
            },
            {
                "invoice_number": "27557",
                "date": "2026-07-06",
                "receivedDateTime": "2026-07-20T20:03:05Z",
            },
            {
                "invoice_number": "28166",
                "date": "2026-09-09",
                "receivedDateTime": "2026-09-11T12:48:51Z",
            },
        ],
        already=set(FIRST_FIVE),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["28008"]
    assert [b["invoice_number"] for b in older] == ["27557"]


def test_crosslink_merge_keeps_first_five():
    prior = [
        {"Invoice #": "28166", "Result": "Success", "KIMCO id": 10101},
        {"Invoice #": "28113", "Result": "Success", "KIMCO id": 10102},
    ]
    new = [{"Invoice #": "28008", "Result": "Success", "KIMCO id": 10110}]
    merged = merge_sheet_rows(prior, new)
    assert [r["Invoice #"] for r in merged] == ["28166", "28113", "28008"]
    # Re-enter of a first-pass number must not duplicate.
    again = merge_sheet_rows(merged, [{"Invoice #": "28166", "Result": "HOLD"}])
    assert [r["Invoice #"] for r in again] == ["28113", "28008", "28166"]
