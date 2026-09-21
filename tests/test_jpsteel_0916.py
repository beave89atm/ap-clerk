"""JPSteel 9/16 dedicated-batch discovery gates.

Distinctive JPSteel / JP Steel only. Invoice dates before 2026-08-01
are reported, not entered (AQPC NOTE-28). Never reuse Crosslink batch 715.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.pdf_invoice import extract_jpsteel_bill, parse_invoice_text  # noqa: E402
from ap_clerk.rules import names_match  # noqa: E402
from jpsteel_0916 import (  # noqa: E402
    CAP,
    CREATED_HEADERS,
    CROSSLINK_TODAY_NAME,
    DO_NOT_MUTATE_IDS,
    DO_NOT_WALK,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    KNOWN_BATCH_ID,
    LEAVE_ALONE_HOLD_IDS,
    MIN_INVOICE_DATE,
    PREFERRED_BATCH_NAME,
    VENDOR_NAME,
    blob_has_jpsteel,
    is_jpsteel_invoice_email,
    is_jpsteel_message,
    is_jpsteel_vendor_text,
    match_jpsteel_inch_partial,
    merge_sheet_rows,
    pick_recent,
    _qty_hold,
)


def _msg(*, subject: str, from_name: str = "", categories: list[str] | None = None) -> dict:
    return {
        "id": "AAMk-jpsteel-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": "ap@example.com"}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": True,
    }


def test_jpsteel_distinctive_token_never_other_steel_or_msc():
    assert is_jpsteel_message(_msg(subject="Invoice 125100 from JP Steel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125101 from JPSteel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125102", from_name="JP Steel"))
    assert is_jpsteel_message(_msg(subject="Invoice 125103", from_name="JPSteel Inc"))
    assert not is_jpsteel_message(_msg(subject="Invoice 70762501 from MSC Industrial Supply"))
    assert not is_jpsteel_message(_msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY"))
    assert not is_jpsteel_message(_msg(subject="Invoice 10917 from AMERICAN QUALITY POWDER COATING"))
    assert not is_jpsteel_message(_msg(subject="Invoice #28166 from Crosslink Powder Coating"))
    assert not is_jpsteel_message(_msg(subject="Invoice 15439109 from Morgan Steel"))
    assert not is_jpsteel_message(_msg(subject="Invoice 12345 from Leeco Steel"))
    assert not is_jpsteel_message(_msg(subject="Invoice 999 from Beshert Steel Processing"))
    assert not is_jpsteel_message(_msg(subject="Steel invoice 88"))
    assert blob_has_jpsteel("JPSteel")
    assert blob_has_jpsteel("jp steel")
    assert not blob_has_jpsteel("morgan steel")
    assert not blob_has_jpsteel("industrial steel supply")


def test_jpsteel_names_match_compact_and_spaced():
    assert names_match("JP Steel", "JP Steel")
    assert is_jpsteel_vendor_text("JP Steel")
    assert is_jpsteel_vendor_text("100-JP STEEL")
    assert is_jpsteel_vendor_text("1098-JP STEEL")
    assert is_jpsteel_vendor_text("JPSteel")
    assert not is_jpsteel_vendor_text("304-MORGAN STEEL")
    assert not is_jpsteel_vendor_text("Leeco Steel, LLC")
    assert not is_jpsteel_vendor_text("MSC Industrial Supply")


def test_jpsteel_pick_recent_stops_before_aug_2026():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "125200",
                "date": "2026-09-10",
                "receivedDateTime": "2026-09-11T12:00:00Z",
            },
            {
                "invoice_number": "125010",
                "date": "2026-08-03",
                "receivedDateTime": "2026-08-04T12:00:00Z",
            },
            {
                "invoice_number": "124747",
                "date": "2026-07-15",
                "receivedDateTime": "2026-08-14T12:00:00Z",
            },
        ],
        already=set(),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["125200", "125010"]
    assert [b["invoice_number"] for b in older] == ["124747"]
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"
    assert CAP == 5

    recent_only_old, older_only = pick_recent(
        [
            {
                "invoice_number": "124100",
                "date": "2026-07-20",
                "receivedDateTime": "2026-07-21T12:00:00Z",
            }
        ],
        already=set(),
        cap=5,
    )
    assert recent_only_old == []
    assert [b["invoice_number"] for b in older_only] == ["124100"]


def test_jpsteel_already_entered_and_cap():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "125201",
                "date": "2026-09-12",
                "receivedDateTime": "2026-09-12T12:00:00Z",
            },
            {
                "invoice_number": "125202",
                "date": "2026-09-11",
                "receivedDateTime": "2026-09-11T12:00:00Z",
            },
        ],
        already={"125201"},
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["125202"]
    assert older == []

    many = [
        {
            "invoice_number": str(125300 + i),
            "date": "2026-09-01",
            "receivedDateTime": f"2026-09-01T0{i}:00:00Z",
        }
        for i in range(7)
    ]
    recent_capped, _ = pick_recent(many, already=set(), cap=5)
    assert len(recent_capped) == 5


def test_jpsteel_statement_is_not_an_invoice():
    assert not is_jpsteel_invoice_email(
        _msg(subject="Statement from JP Steel")
    )
    assert not is_jpsteel_invoice_email(
        _msg(subject="Past due invoices — JPSteel")
    )
    assert is_jpsteel_invoice_email(
        _msg(subject="Invoice 125200 from JP Steel")
    )
    assert is_jpsteel_invoice_email(
        _msg(subject="JP Steel Invoice#  (125316) Transmission for KANNON MFG")
    )
    reply = _msg(subject="Re: JP Steel Invoice#  (124506) Transmission for KANNON MFG")
    reply["hasAttachments"] = False
    assert not is_jpsteel_invoice_email(reply)
    assert not is_jpsteel_invoice_email(
        _msg(subject="JP Steel Invoice#  (CM123738) Transmission for KANNON MFG")
    )


def test_jpsteel_batch_is_dedicated_not_crosslink_715():
    assert PREFERRED_BATCH_NAME == "API Agent - 9/16/26 JPSteel"
    assert FALLBACK_BATCH_NAME == "API Agent - 9/16/26-2"
    assert CROSSLINK_TODAY_NAME == "API Agent - 9/16/26"
    assert 715 in FORBIDDEN_BATCH_IDS
    assert PREFERRED_BATCH_NAME != CROSSLINK_TODAY_NAME
    assert FALLBACK_BATCH_NAME != CROSSLINK_TODAY_NAME
    assert VENDOR_NAME == "JP Steel"
    jpsteel_716 = "API Agent - 9/16/26 JPSteel (716)"
    assert not re.search(r"\(715\)", jpsteel_716)
    assert CROSSLINK_TODAY_NAME in jpsteel_716  # prefix only — must not abort 716
    assert jpsteel_716 != CROSSLINK_TODAY_NAME
    assert KNOWN_BATCH_ID == 716
    assert CREATED_HEADERS == {
        "125315": 10107,
        "125316": 10108,
        "125314": 10109,
        "125122": 10110,
        "125051": 10111,
    }
    assert LEAVE_ALONE_HOLD_IDS == set()
    assert DO_NOT_MUTATE_IDS == {10107}
    assert DO_NOT_WALK == {"124506", "123248"}


def test_jpsteel_plus5_stops_before_pre_aug_and_keeps_holds():
    recent, older = pick_recent(
        [
            {
                "invoice_number": "124506",
                "date": "2026-07-28",
                "receivedDateTime": "2026-07-29T13:44:29Z",
            },
            {
                "invoice_number": "123248",
                "date": "2026-05-06",
                "receivedDateTime": "2026-05-06T22:46:30Z",
            },
        ],
        already=set(CREATED_HEADERS) | set(DO_NOT_WALK),
        cap=5,
    )
    assert recent == []
    assert older == []

    prior = [
        {"Invoice #": "125315", "Result": "HOLD", "KIMCO id": 10107},
        {"Invoice #": "125314", "Result": "Success", "KIMCO id": 10109},
    ]
    merged = merge_sheet_rows(prior, [{"Invoice #": "125400", "Result": "Success", "KIMCO id": 10112}])
    assert [r["Invoice #"] for r in merged] == ["125315", "125314", "125400"]
    assert merged[0]["KIMCO id"] == 10107


def test_jpsteel_inches_are_not_rolled_qty():
    """Description inches must not equal a leftover rolled qty."""
    parsed = {
        "lines": [
            {"part": "PLATE", "qty": 2.0, "description": '48.000" x 96.000" PLATE'}
        ]
    }
    recs = [{"qty": 48.0, "unit": 1.25}]
    assert _qty_hold(parsed, recs) is True
    recs_ok = [{"qty": 2.0, "unit": 1.25}]
    assert _qty_hold(parsed, recs_ok) is False
    # Explicit length_inches 32 vs receipt qty 32 is allowed (Metal Supermarkets class).
    parsed_len = {"lines": [{"part": "BAR", "qty": 1.0, "length_inches": 32.0}]}
    assert _qty_hold(parsed_len, [{"qty": 32.0}]) is False
    # NOTE-44: shipping/surcharge rows are Fees, not merch qty.
    parsed_fees = {
        "lines": [
            {"part": "4082T15", "qty": 40.0, "label": "4082T15"},
            {"part": "SHIP", "qty": 1.0, "label": "Shipping"},
        ]
    }
    assert _qty_hold(parsed_fees, [{"qty": 40.0}]) is False


JPSTEEL_125315 = """
JP Steel
https://jpsteel.us/
Invoice No: 125315
Customer P.O.#: 59128
Invoice Date: 9/15/26
--------------- BOL No: 17343 ---------------
21684-1--4.000 X 0.375
1026 DOM -- 21684-1
 250.89 $33.00 $693.00 E9.875" E 21  1 P
Invoice Totals  250.89  21
Subtotal Non Taxable $693.00
Total $693.00
"""

JPSTEEL_125316 = """
JP Steel
https://jpsteel.us/
Invoice No: 125316
Customer P.O.#: 59154
Invoice Date: 9/15/26
--------------- BOL No: 17344 ---------------
3 X 2 X 0.188 A500 B/C
Square/Rec
 673.13 $9.25 $1,113.85 E289" F 5  1 P 24.08'120.42'
1.313 X 0.120 1020 DOM  158.49 $4.50 $466.88 E249" F 5  2 P 20.75'103.75'
Invoice Totals  831.62  10
Subtotal Non Taxable $1,580.73
Total $1,580.73
"""

JPSTEEL_125314 = """
JP Steel
https://jpsteel.us/
Invoice No: 125314
Customer P.O.#: 59104
Invoice Date: 9/15/26
--------------- BOL No: 17344 ---------------
15878-3--5.500 X 0.500
1026 DOM
 1,122.80 $115.00 $2,530.00 E22.9375" E 22  1 P
Invoice Totals  1,122.80  22
Subtotal Non Taxable $2,530.00
Total $2,530.00
"""

JPSTEEL_125122 = """
JP Steel
https://jpsteel.us/
Invoice No: 125122
Customer P.O.#: 59018
Invoice Date: 9/1/26
--------------- BOL No: 17168 ---------------
1.000 X 0.083 1026 DOM  35.22 $3.25 $140.83 E260" F 2  1 P 21.67'43.33'
1.250 X 1.250 X 14 GA
A513 Square/Rec
 316.08 $2.88 $691.20 E288" F 10  2 P 24'240.00'
Invoice Totals  351.30  12
Subtotal Non Taxable $832.03
Total $832.03
"""

JPSTEEL_125051 = """
JP Steel
https://jpsteel.us/
Invoice No: 125051
Customer P.O.#: 58937
Invoice Date: 8/27/26
--------------- BOL No: 17101 ---------------
0.250 X 1.500 6061-T6 BAR
- FLAT
 96.00 $7.92 $380.16 E144" F 4  1 P 12'48.00'
1.250 X 0.083 6061 ROUND
TUBE
 25.92 $10.42 $750.24 E144" F 6  2 P 12'72.00'
Invoice Totals  121.92  10
Subtotal Non Taxable $1,130.40
Total $1,130.40
"""


def test_jpsteel_pdf_piece_and_foot_lines():
    """Piece bills use pcs. Foot bills use rolled inches. Cut length is not qty."""
    piece = extract_jpsteel_bill(JPSTEEL_125315)
    assert len(piece) == 1
    assert piece[0]["qty"] == 21.0
    assert piece[0]["unit_price"] == 33.0
    assert piece[0]["amount"] == 693.0
    assert piece[0]["qty_uom"] == "pcs"
    assert piece[0]["length_inches"] in {9.875, 9.88}
    assert piece[0]["qty"] != piece[0]["length_inches"]

    two = extract_jpsteel_bill(JPSTEEL_125316)
    assert [(round(ln["qty"], 2), ln["amount"], ln["qty_uom"]) for ln in two] == [
        (1445.0, 1113.85, "in"),
        (1245.0, 466.88, "in"),
    ]
    assert two[0]["length_inches"] == 289.0
    assert two[0]["qty"] != 289.0
    assert two[0]["qty"] != 5.0

    split = extract_jpsteel_bill(JPSTEEL_125314)
    assert split[0]["qty"] == 22.0
    assert split[0]["amount"] == 2530.0
    assert split[0]["qty_uom"] == "pcs"

    feet = extract_jpsteel_bill(JPSTEEL_125122)
    assert [(ln["qty"], ln["amount"]) for ln in feet] == [
        (520.0, 140.83),
        (2880.0, 691.20),
    ]
    assert feet[1]["length_inches"] == 288.0
    assert feet[1]["qty"] != 288.0

    swap = extract_jpsteel_bill(JPSTEEL_125051)
    assert [(ln["qty"], ln["amount"]) for ln in swap] == [
        (576.0, 380.16),
        (864.0, 750.24),
    ]


def test_jpsteel_inch_partial_takes_unique_unit_only():
    lines = [
        {"qty": 1445.0, "unit_price": 0.770833, "amount": 1113.85, "qty_uom": "in"},
        {"qty": 1245.0, "unit_price": 0.375, "amount": 466.88, "qty_uom": "in"},
    ]
    pool = [
        {"id": 24128, "qty": 14400.0, "unit_price": 0.77},
        {"id": 24129, "qty": 14940.0, "unit_price": 0.38},
    ]
    hits = match_jpsteel_inch_partial(lines, pool, set())
    assert [(h["id"], h["select_qty"]) for h in hits] == [(24128, 1445.0), (24129, 1245.0)]
    # Two leftovers at the same unit — do not guess.
    tied = match_jpsteel_inch_partial(
        [{"qty": 100.0, "unit_price": 0.50, "qty_uom": "in"}],
        [
            {"id": 1, "qty": 500.0, "unit_price": 0.50},
            {"id": 2, "qty": 600.0, "unit_price": 0.50},
        ],
        set(),
    )
    assert tied == []


def test_jpsteel_parse_invoice_text_uses_pdf_truth():
    parsed = parse_invoice_text(
        JPSTEEL_125315,
        subject="JP Steel Invoice#  (125315) Transmission for KANNON MFG",
        from_name="JP Steel",
    )
    assert parsed["invoice_number"] == "125315"
    assert parsed["po"] == "59128"
    assert parsed["amount"] == 693.0
    assert parsed["date"] == "2026-09-15"
    assert parsed["vendor"]
    assert "jp" in parsed["vendor"].lower()
    assert parsed["lines"][0]["qty"] == 21.0


def test_jpsteel_125315_combine_same_item_receipts_is_not_blocked_400_hold():
    """NOTE-37: matcher combines 8+13@$33; quality Success from Kyle GET; no Outlook stamp."""
    from ap_clerk.rules import match_receipts
    from jpsteel_0916 import quality_jpsteel_row

    parsed = parse_invoice_text(
        JPSTEEL_125315,
        subject="JP Steel Invoice#  (125315) Transmission for KANNON MFG",
        from_name="JP Steel",
    )
    match = match_receipts(
        invoice_number="125315",
        invoice_lines=list(parsed.get("lines") or []),
        receipts=[
            {"id": 24126, "po": "59128", "part": "PO59128-01", "qty": 8.0, "unit_price": 33.0, "amount": 264.0},
            {"id": 24127, "po": "59128", "part": "PO59128-01", "qty": 13.0, "unit_price": 33.0, "amount": 429.0},
        ],
        po_number="59128",
        invoice_amount=693.0,
    )
    assert sorted((h.get("receipt") or {}).get("id") for h in (match.get("matched") or [])) == [
        24126,
        24127,
    ]
    assert not match.get("hold_no_receipts")

    class _NoGraph:
        def flag_matched(self, *_a, **_k):
            raise AssertionError("do not fight Outlook on Kyle-finished 10107")

        def flag_issues(self, *_a, **_k):
            raise AssertionError("do not fight Outlook on Kyle-finished 10107")

    row = quality_jpsteel_row(
        _NoGraph(),
        parsed=parsed,
        enter_row={
            "Vendor": "JP Steel",
            "Invoice #": "125315",
            "date": "2026-09-15",
            "PO": "59128",
            "Amount": 693.0,
            "Result": "HOLD",
            "KIMCO id": 10107,
            "Batch": "API Agent - 9/16/26 JPSteel (716)",
            "Flag in Outlook": "Yes",
            "outlook": "entered-with-issues",
        },
        proof={
            "id": 10107,
            "invoice_amount": 693.0,
            "verification": 693.0,
            "invoice_type": 3,
            "vendor_id": 100,
            "batch_id": 716,
            "attachments": ["2026-09-16_Invoice_125315.pdf"],
            "receipt_lines": [
                {"qty": 8.0, "unit": 33.0, "receipt": 24126},
                {"qty": 13.0, "unit": 33.0, "receipt": 24127},
            ],
        },
        finish=None,
        vendor_id=100,
    )
    assert row["Result"] == "Success"
    assert row["KIMCO id"] == 10107
    assert "Kyle finished" in row["Why"]
    assert "NOTE-37" in row["Why"]
    assert "24126" in row["Why"] or "24126" in row["Receipts"]
    assert row["outlook"] == "left-as-kyle" or row["outlook"] == "entered-with-issues"
    assert "not re-stamped" in row["Why"] or row["outlook"] == "left-as-kyle"


def test_jpsteel_125316_rounding_ppv_hits_pdf_and_skips_10107():
    """NOTE-38: −$0.10 PPV for 10108; finish_rounding_ppv_only never writes 10107."""
    from ap_clerk.rules import rounding_ppv_to_hit_pdf_total
    from jpsteel_0916 import DO_NOT_MUTATE_IDS, finish_rounding_ppv_only, quality_jpsteel_row

    decision = rounding_ppv_to_hit_pdf_total(1580.73, 1580.83)
    assert decision["ppv"] == -0.10

    class _Client:
        def __init__(self):
            self.posted = []

        def try_post_ppv(self, invoice_id, amount):
            self.posted.append((invoice_id, amount))
            return "posted"

    # finish_rounding_ppv_only GETs via jpsteel_proof — stub by patching that name.
    import jpsteel_0916 as mod

    proof_10107 = {
        "id": 10107,
        "invoice_amount": 693.0,
        "verification": 693.0,
        "receipt_lines": [{"qty": 8.0, "unit": 33.0, "receipt": 24126}],
        "ppv_amounts": [],
    }
    orig = mod.jpsteel_proof
    mod.jpsteel_proof = lambda _c, kid: proof_10107
    try:
        out = finish_rounding_ppv_only(_Client(), kimco_id=10107, pdf_amount=693.0)
    finally:
        mod.jpsteel_proof = orig
    assert out["ppv_status"] == "do-not-mutate"
    assert out["mutated"] is False
    assert 10107 in DO_NOT_MUTATE_IDS

    parsed = parse_invoice_text(
        JPSTEEL_125316,
        subject="JP Steel Invoice#  (125316) Transmission for KANNON MFG",
        from_name="JP Steel",
    )
    row = quality_jpsteel_row(
        None,
        parsed=parsed,
        enter_row={
            "Vendor": "JP Steel",
            "Invoice #": "125316",
            "PO": "59154",
            "Amount": 1580.73,
            "Result": "HOLD",
            "KIMCO id": 10108,
            "Batch": "API Agent - 9/16/26 JPSteel (716)",
        },
        proof={
            "id": 10108,
            "invoice_amount": 1580.73,
            "verification": 1580.73,
            "invoice_type": 3,
            "vendor_id": 100,
            "batch_id": 716,
            "attachments": ["2026-09-16_Invoice_125316.pdf"],
            "receipt_lines": [
                {"qty": 1445.0, "unit": 0.7709, "receipt": 24128},
                {"qty": 1245.0, "unit": 0.375, "receipt": 24129},
            ],
            "ppv_amounts": [-0.10],
        },
        finish={"select_status": "already-selected", "ppv_amount": -0.10, "ppv_status": "posted"},
        vendor_id=100,
    )
    assert row["Result"] == "Success"
    assert row["KIMCO id"] == 10108
    assert row["PPV"] == "-0.10"
