"""Gas & Supply 9/17 dedicated-batch discovery gates.

Distinctive Gas & Supply only. Invoice dates before 2026-08-01 are
reported, not entered (NOTE-28). Never reuse Crosslink 715, JPSteel 716,
or Legacy 717. Order numbers are not invoices. After-tax including-tax
footer wins. NOTE-39 columns. NOTE-40 Transfer AP never invents 375.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.pdf_invoice import (  # noqa: E402
    expand_gas_misc_invoices,
    extract_gas_item_lines,
    gas_invoice_numbers,
    parse_invoice_text,
    prefer_after_tax_amount,
)
from ap_clerk.quality_v12 import (  # noqa: E402
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    apply_exception_category_owner,
)
from ap_clerk.rules import names_match  # noqa: E402
from gas_supply_0917 import (  # noqa: E402
    CAP,
    CREATED_HEADERS,
    PLUS5_HEADERS,
    PLUS10_HEADERS,
    PLUS10_HOLDS,
    FALLBACK_BATCH_NAME,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    KNOWN_BATCH_ID,
    KNOWN_HOLD,
    MIN_INVOICE_DATE,
    PLUS5_PREFERRED,
    PREFERRED_BATCH_NAME,
    PURCHASING_OWNER,
    TRANSFER_AP_PRIOR_ID_HINT,
    VENDOR_NAME,
    already_entered_numbers,
    apply_gas_exception_category_owner,
    blob_has_gas_supply,
    find_transfer_ap_batch,
    is_gas_invoice_email,
    is_gas_message,
    is_gas_vendor_text,
    is_over_ppv_price_hold,
    leftover_from_catalog,
    pick_plus10,
    pick_plus15,
    pick_plus5,
    pick_recent,
    quality_gas_row,
)


def _msg(
    *,
    subject: str,
    from_name: str = "",
    from_addr: str = "billing@gasandsupply.com",
    categories: list[str] | None = None,
    attachments: bool = True,
) -> dict:
    return {
        "id": "AAMk-gas-test",
        "subject": subject,
        "bodyPreview": "",
        "from": {"emailAddress": {"name": from_name, "address": from_addr}},
        "categories": categories or [],
        "flag": {},
        "hasAttachments": attachments,
    }


def test_gas_distinctive_token_never_msc_rmp_or_other_supply():
    assert is_gas_message(_msg(subject="Gas&Supply Invoice/Statement"))
    assert is_gas_message(_msg(subject="Invoice", from_name="Gas and Supply North Texas, LLC"))
    assert is_gas_message(_msg(subject="Invoice 0040434973 from Gas & Supply"))
    other = {"from_addr": "ap@example.com"}
    assert not is_gas_message(_msg(subject="Invoice 70762501 from MSC Industrial Supply", **other))
    assert not is_gas_message(_msg(subject="Invoice 1470159 from RMP INDUSTRIAL SUPPLY", **other))
    assert not is_gas_message(_msg(subject="Invoice #28166 from Crosslink Powder Coating", **other))
    assert not is_gas_message(_msg(subject="Invoice from JP Steel", **other))
    assert not is_gas_message(_msg(subject="Supply invoice 88", **other))
    assert blob_has_gas_supply("Gas and Supply")
    assert blob_has_gas_supply("gas&supply")
    assert not blob_has_gas_supply("industrial supply")
    assert not blob_has_gas_supply("tube supply")


def test_gas_names_match_and_vendor_text():
    assert names_match(VENDOR_NAME, "Gas and Supply North Texas, LLC")
    assert is_gas_vendor_text("1069-GAS AND SUPPLY")
    assert is_gas_vendor_text("71-GAS AND SUPPLY")
    assert is_gas_vendor_text("Gas and Supply North Texas, LLC")
    assert not is_gas_vendor_text("MSC Industrial Supply")
    assert not is_gas_vendor_text("RMP INDUSTRIAL SUPPLY")


def test_gas_invoice_email_not_past_due_or_check_mail():
    assert is_gas_invoice_email(_msg(subject="Gas&Supply Invoice/Statement"))
    assert not is_gas_invoice_email(_msg(subject="Past Due Notice for Account A3050"))
    assert not is_gas_invoice_email(_msg(subject="CHK#18570 iao $15,719.29", attachments=False))
    assert not is_gas_invoice_email(_msg(subject="Re: Over 100+ days--A3050", attachments=False))
    assert not is_gas_invoice_email(
        _msg(subject="Fwd: Gas&Supply Invoice/Statement", attachments=False)
    )


def test_gas_pick_recent_stops_before_aug_and_caps_bills():
    recent, older = pick_recent(
        [
            {"invoice_number": "0040435122", "date": "2026-09-15", "receivedDateTime": "2026-09-16T04:50:53Z"},
            {"invoice_number": "0040434973", "date": "2026-09-15", "receivedDateTime": "2026-09-16T04:49:24Z"},
            {"invoice_number": "0040323616", "date": "2026-07-15", "receivedDateTime": "2026-07-16T12:00:00Z"},
        ],
        already=set(),
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["0040435122", "0040434973"]
    assert [b["invoice_number"] for b in older] == ["0040323616"]
    assert MIN_INVOICE_DATE.isoformat() == "2026-08-01"
    assert CAP == 5

    many = [
        {
            "invoice_number": f"004043000{i}",
            "date": "2026-09-01",
            "receivedDateTime": f"2026-09-0{i}T12:00:00Z",
        }
        for i in range(1, 8)
    ]
    recent_cap, leftover = pick_recent(many, already=set(), cap=5)
    assert len(recent_cap) == 5
    assert len(leftover) == 2


def test_gas_already_entered_skipped():
    recent, _older = pick_recent(
        [
            {"invoice_number": "0040435122", "date": "2026-09-15"},
            {"invoice_number": "0040434973", "date": "2026-09-15"},
        ],
        already={"0040435122"},
        cap=5,
    )
    assert [b["invoice_number"] for b in recent] == ["0040434973"]


def test_gas_order_number_is_not_second_invoice():
    text = (
        "GAS AND SUPPLY NORTH TEXAS, LLC\nORIGINAL INVOICE\n"
        "INVOICE DATE ACCOUNT NUMBER INVOICE NUMBER\n"
        "AMOUNT THIS INVOICE INCLUDING TAX\n"
        "09/15/26   A3050      0040434973\n"
        "     0011118988-00      PINNACLE PROPANE\n"
        "PRO7.5C             8     0    8    8 UN1075 LIQUEFIED PETROLEUM    CYL        24.00     192.00 N\n"
        "                                                                 Subtotal                    192.00\n"
        "  TAX CD: 000000000TXDF15 TAX DESCRP: TX/Denton/\n"
        "       0.00                                                                                  192.00\n"
    )
    assert gas_invoice_numbers(text) == ["0040434973"]
    assert prefer_after_tax_amount(text, None) == 192.00
    bills = expand_gas_misc_invoices(
        text,
        {**parse_invoice_text(text, from_name=VENDOR_NAME), "vendor": VENDOR_NAME},
    )
    assert len(bills) == 1
    assert bills[0]["invoice_number"] == "0040434973"
    assert bills[0]["amount"] == 192.00
    assert not bills[0].get("gas_misc_ambiguous")


def test_gas_item_lines_skip_fuel_surcharge_fee():
    text = (
        "Gas and Supply North Texas, LLC\n"
        "AR90CD300           6     0    6    6 300 COMP.GAS N.O.S. 2.2 UN1956    CYL        28.00     168.00 N\n"
        "ARG300              2     0    2    2 300 SZ ARGON UN1006  HAZARDOUS    CYL        30.00      60.00 N\n"
        "$SUR485005          1     0           FUEL SURCHARGE                    EA         17.50      17.50 N\n"
    )
    lines = extract_gas_item_lines(text)
    parts = [ln["part"] for ln in lines]
    assert parts == ["AR90CD300", "ARG300"]
    assert all(ln["amount"] in {168.0, 60.0} for ln in lines)


def test_forbidden_batches_are_715_716_717():
    assert FORBIDDEN_BATCH_IDS == {715, 716, 717}
    assert PREFERRED_BATCH_NAME == "API Agent - 9/17/26 Gas & Supply"
    assert FALLBACK_BATCH_NAME == "API Agent - 9/17/26-G"
    assert "API Agent - 9/17/26 Legacy Wire" in FORBIDDEN_REUSE_NAMES
    assert "API Agent - 9/16/26" in FORBIDDEN_REUSE_NAMES


def test_transfer_ap_lookup_never_invents_375():
    empty = find_transfer_ap_batch([])
    assert empty["found"] is False
    assert empty["invent"] is False
    assert empty["hint_ignored"] == TRANSFER_AP_PRIOR_ID_HINT == 375
    found = find_transfer_ap_batch(
        [{"id": 401, "values": {"AP_Invoice_Batch_ID": "Transfer AP"}}]
    )
    assert found == {"found": True, "id": 401, "name": "Transfer AP", "invent": False}


def test_note39_hold_gets_category_owner_success_blank():
    hold = apply_exception_category_owner(
        {
            "Result": "HOLD",
            "Why": "HOLD (receipt): no open receipt leftover on PO 59081",
        }
    )
    assert hold[COL_EXCEPTION_CATEGORY] == "missing_receipt"
    assert hold[COL_EXCEPTION_OWNER] == "Ruben Perez"
    assert "category=missing_receipt" in hold["Why"]
    missing_po = apply_exception_category_owner(
        {
            "Result": "HOLD",
            "Why": "HOLD (po): PO 59081 is on the invoice but not findable on live.",
        }
    )
    assert missing_po[COL_EXCEPTION_CATEGORY] == "missing_po"
    assert missing_po[COL_EXCEPTION_OWNER] == "Shawn McKibben"
    assert "Misty" not in missing_po[COL_EXCEPTION_OWNER]
    success = apply_exception_category_owner({"Result": "Success", "Why": "Finished bill"})
    assert success[COL_EXCEPTION_CATEGORY] == ""
    assert success[COL_EXCEPTION_OWNER] == ""


def test_is_over_ppv_not_missing_receipt():
    assert is_over_ppv_price_hold(
        {"Result": "HOLD", "Exception category": "price_variance", "Why": "over"},
        {},
    )
    assert not is_over_ppv_price_hold(
        {"Result": "HOLD", "Exception category": "missing_receipt", "Why": "no open receipt"},
        {},
    )


def test_quality_gas_misc_header_only_never_success():
    """NOTE-42: Type 4 header-only (empty Lines-K) cannot be Success."""
    row = quality_gas_row(
        None,
        parsed={
            "invoice_number": "0040435122",
            "amount": 120.0,
            "po": None,
            "fees": [],
            "lines": [],
            "graph_message_id": "",
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "0040435122",
            "PO": "",
            "Amount": 120.0,
            "Result": "HOLD",
            "KIMCO id": 10128,
        },
        proof={
            "id": 10128,
            "invoice_number": "0040435122",
            "invoice_amount": 0.0,
            "verification_amount": 120.0,
            "invoice_type": 4,
            "vendor_id": 71,
            "attachments": ["g1378.pdf"],
            "receipt_lines": [],
            "misc_lines": [],
            "fee_amounts": [],
            "ppv_amounts": [],
        },
        finish={"select_status": "no-po-misc", "select_zero": False, "skipped_over_ppv": False},
        vendor_id=71,
    )
    assert row["Result"] == "Incomplete"
    assert row["Result"] != "Success"
    assert "NOTE-42" in row["Why"]
    assert row[COL_EXCEPTION_CATEGORY] == "other"


def test_quality_gas_misc_success_no_receipts():
    row = quality_gas_row(
        None,
        parsed={
            "invoice_number": "0040434973",
            "amount": 192.0,
            "po": None,
            "fees": [],
            "lines": [],
            "graph_message_id": "",
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "0040434973",
            "PO": "",
            "Amount": 192.0,
            "Result": "HOLD",
            "Why": "",
            "KIMCO id": 10199,
        },
        proof={
            "id": 10199,
            "invoice_number": "0040434973",
            "invoice_amount": 192.0,
            "verification_amount": 192.0,
            "invoice_type": 4,
            "vendor_id": 71,
            "attachments": ["billing01_A3050_c_0040434973.pdf"],
            "receipt_lines": [],
            "misc_lines": [
                {
                    "desc": "AR90CD300 300 COMP.GAS",
                    "qty": 4.0,
                    "unit": 48.0,
                    "misc": {"id": 31, "text": "Shop Supplies - G&S-."},
                }
            ],
            "fee_amounts": [],
            "ppv_amounts": [],
        },
        finish={"select_status": "no-po-misc", "select_zero": False, "skipped_over_ppv": False},
        vendor_id=71,
    )
    assert row["Result"] == "Success"
    assert row[COL_EXCEPTION_CATEGORY] == ""
    assert "Shop Supplies" in row["Why"]
    assert "Lines-K" in row["Why"]


def test_quality_gas_missing_receipt_tags_ruben():
    row = quality_gas_row(
        None,
        parsed={
            "invoice_number": "0040430010",
            "amount": 14.0,
            "po": "59081",
            "fees": [],
            "lines": [{"part": "MLW49-56-7240", "qty": 1, "unit_price": 14.0, "amount": 14.0}],
            "graph_message_id": "mid",
        },
        enter_row={
            "Vendor": VENDOR_NAME,
            "Invoice #": "0040430010",
            "PO": "59081",
            "Amount": 14.0,
            "Result": "HOLD",
            "KIMCO id": 10200,
        },
        proof={
            "id": 10200,
            "invoice_number": "0040430010",
            "invoice_amount": 14.0,
            "verification_amount": 14.0,
            "invoice_type": 3,
            "vendor_id": 71,
            "attachments": ["x.pdf"],
            "receipt_lines": [],
            "fee_amounts": [],
            "ppv_amounts": [],
        },
        finish={"select_status": "held-unfinished", "select_zero": False},
        vendor_id=71,
    )
    assert row["Result"] == "HOLD"
    assert row[COL_EXCEPTION_CATEGORY] == "missing_receipt"
    assert "@Ruben Perez" in row["Why"]


def test_plus5_skips_first_pass_and_prefers_leftovers():
    assert KNOWN_BATCH_ID == 720
    assert CREATED_HEADERS == {
        "0040435122": 10128,
        "0040434973": 10129,
        "0040431060": 10130,
        "0040425657": 10131,
    }
    assert KNOWN_HOLD == {"0040430010", "0040424839"}
    assert PLUS5_PREFERRED == (
        "0040425612",
        "0040424839",
        "0040424382",
        "0040423658",
        "0040421569",
    )
    assert PLUS5_HEADERS == {
        "0040425612": 10132,
        "0040424382": 10133,
        "0040423658": 10134,
        "0040421569": 10135,
    }
    assert PLUS10_HEADERS == {"0040438494": 10136}
    assert PLUS10_HOLDS == frozenset({"0040438057", "0040438056", "0040438055", "0040438053"})
    first_pass_only = set(CREATED_HEADERS) | {"0040430010", "0040424839"}
    bills = [
        {"invoice_number": "0040435122", "date": "2026-09-15"},
        {"invoice_number": "0040430010", "date": "2026-09-11", "po": "59081"},
        {"invoice_number": "0040429000", "date": "2026-09-10", "amount": 99.0},
        {"invoice_number": "0040425612", "date": "2026-09-09", "amount": 240.0},
        {"invoice_number": "0040424839", "date": "2026-09-09", "po": "59081", "amount": 224.94},
        {"invoice_number": "0040424382", "date": "2026-09-09", "amount": 335.0},
        {"invoice_number": "0040423658", "date": "2026-09-09", "po": "58948", "amount": 173.5},
        {"invoice_number": "0040421569", "date": "2026-09-08", "amount": 331.5},
    ]
    chosen, leftover = pick_plus5(bills, already=first_pass_only, cap=5)
    assert [b["invoice_number"] for b in chosen] == [
        "0040425612",
        "0040424382",
        "0040423658",
        "0040421569",
        "0040429000",
    ]
    assert "0040435122" not in [b["invoice_number"] for b in leftover]
    assert "0040430010" not in [b["invoice_number"] for b in leftover]
    assert "0040424839" not in [b["invoice_number"] for b in leftover]


def test_plus10_discovers_next_window_after_plus5():
    bills = [
        {"invoice_number": "0040435122", "date": "2026-09-15"},
        {"invoice_number": "0040425612", "date": "2026-09-09"},
        {"invoice_number": "0040424839", "date": "2026-09-09", "po": "59081"},
        {"invoice_number": "0040423658", "date": "2026-09-09", "po": "58948"},
        {"invoice_number": "0040420801", "date": "2026-09-07", "amount": 100.0},
        {"invoice_number": "0040420800", "date": "2026-09-07", "amount": 110.0},
        {"invoice_number": "0040419999", "date": "2026-09-05", "amount": 120.0},
        {"invoice_number": "0040418888", "date": "2026-09-04", "amount": 130.0},
        {"invoice_number": "0040417777", "date": "2026-09-03", "amount": 140.0},
        {"invoice_number": "0040320000", "date": "2026-07-15", "amount": 9.0},
    ]
    chosen, leftover = pick_plus10(bills, already=already_entered_numbers(), cap=5)
    assert [b["invoice_number"] for b in chosen] == [
        "0040420801",
        "0040420800",
        "0040419999",
        "0040418888",
        "0040417777",
    ]
    assert [b["invoice_number"] for b in leftover] == ["0040320000"]
    catalog = leftover_from_catalog(leftover)
    assert catalog == []


def test_missing_po_owner_is_shawn_not_misty():
    row = apply_gas_exception_category_owner(
        {
            "Result": "HOLD",
            "Why": (
                "HOLD (po): PO 59081 is on the invoice but not findable on live "
                "by vendor + part/WO. Will not enter as Misc Type 4."
            ),
        }
    )
    assert row[COL_EXCEPTION_CATEGORY] == "missing_po"
    assert row[COL_EXCEPTION_OWNER] == PURCHASING_OWNER == "Shawn McKibben"
    assert "Misty" not in row["Why"]
    assert "category=missing_po; owner=Shawn McKibben" in row["Why"]


def test_plus15_prefers_no_po_over_po_cited():
    bills = [
        {"invoice_number": "0040438052", "date": "2026-09-17", "po": "59081", "amount": 5178.55},
        {"invoice_number": "0040437952", "date": "2026-09-17", "po": "59006", "amount": 241.46},
        {"invoice_number": "0040438494", "date": "2026-09-17", "amount": 331.5},
        {"invoice_number": "0040438057", "date": "2026-09-17", "po": "59081"},
        {"invoice_number": "0040412279", "date": "2026-08-31", "amount": None, "lines": []},
        {"invoice_number": "0040401083", "date": "2026-08-31", "hold_reason": "CHECK STOP"},
        {"invoice_number": "0040420801", "date": "2026-09-07", "amount": 88.0},
        {"invoice_number": "0040420800", "date": "2026-09-07", "amount": 99.0},
        {"invoice_number": "0040419001", "date": "2026-09-04", "amount": 111.0},
        {"invoice_number": "0040419000", "date": "2026-09-04", "po": "59081", "amount": 50.0},
        {"invoice_number": "0040418000", "date": "2026-09-03", "amount": 70.0},
        {"invoice_number": "0040320000", "date": "2026-07-15", "amount": 9.0},
    ]
    chosen, leftover = pick_plus15(bills, already=already_entered_numbers(), cap=5)
    assert [b["invoice_number"] for b in chosen] == [
        "0040420801",
        "0040420800",
        "0040419001",
        "0040418000",
        "0040438052",
    ]
    assert all(not b.get("po") for b in chosen[:4])
    assert chosen[4]["po"] == "59081"
    leftover_invs = [b["invoice_number"] for b in leftover]
    assert "0040438494" not in leftover_invs
    assert "0040438057" not in leftover_invs
    assert "0040435122" not in leftover_invs
    assert "0040412279" not in leftover_invs
    assert "0040401083" not in leftover_invs


def test_merchandise_lines_skip_fuel_surcharge():
    from gas_supply_0917 import merchandise_lines_from_parsed

    lines = merchandise_lines_from_parsed(
        {
            "lines": [
                {"part": "PRO7.5C", "description": "UN1075", "qty": 5, "unit_price": 24},
                {"part": "$SUR485005", "description": "FUEL SURCHARGE", "qty": 1, "unit_price": 17.5},
            ]
        }
    )
    assert [ln["part"] for ln in lines] == ["PRO7.5C"]


def test_leftover_from_catalog_dedupes():
    leftover = leftover_from_catalog(
        [
            {"invoice_number": "0040424382", "date": "2026-09-09", "amount": 335.0},
            {"invoice_number": "0040424382", "date": "2026-09-09", "amount": 335.0},
            {"invoice_number": "0040423658", "date": "2026-09-09", "po": "58948"},
            {"invoice_number": "0040320000", "date": "2026-07-15", "amount": 9.0},
        ]
    )
    assert [r["invoice_number"] for r in leftover] == ["0040424382", "0040423658"]
