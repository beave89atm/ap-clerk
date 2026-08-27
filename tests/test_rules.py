from datetime import date

from ap_clerk.rules import (
    batch_name_for,
    due_date_from_terms,
    extract_po_number,
    format_fees,
    is_fee_or_surcharge,
    names_match,
)


def test_batch_name_no_leading_zeros():
    assert batch_name_for(date(2026, 8, 27)) == "API Agent - 8/27/26"
    assert batch_name_for(date(2026, 8, 4)) == "API Agent - 8/4/26"


def test_half_percent_terms_are_net_30():
    assert due_date_from_terms(date(2026, 8, 3), "F-0.5/10,N30-0.5% 10, Net 30") == date(2026, 9, 2)
    assert due_date_from_terms(date(2026, 7, 13), "1/2% 10 - Net 30") == date(2026, 8, 12)
    assert due_date_from_terms(date(2026, 7, 10), "F-N60-Net 60") == date(2026, 9, 8)


def test_vendor_name_matching():
    assert names_match("O'Neal Steel - Dallas (GP)", "1135-ONEAL STEEL, LLC.")
    assert names_match("Earle M. Jorgensen Co", "EMJ Earl M. Jorgensen Company")
    assert names_match("Fastenal Company", "FASTENAL INDUSTRIAL & CONSTRUCTION  Acct# TXFT40601")
    assert names_match("Crosslink Powder Coating of TX, LLC", "1276-Crosslink Powder Coating")
    assert names_match("Capital Machine Technologies, Inc", "CAPITAL MACHINE TECHNOLOGIES")


def test_fees_are_not_ppv():
    assert is_fee_or_surcharge("SUPPLY FEE / Shop supplies")
    assert is_fee_or_surcharge("Packaging/Shop Supplies Recovery")
    assert is_fee_or_surcharge("Shipping & Handling")
    assert is_fee_or_surcharge("FUEL SURCHARGE")
    assert format_fees([{"name": "Shipping", "amount": 42.17}]) == "Shipping 42.17"
    assert format_fees([]) == "none"


def test_extract_po_number():
    assert extract_po_number("PO58351-TELECOM PRODUCTS") == "58351"
    assert extract_po_number("PO58634-CAPITAL MACHINE TECHNOLOGIES") == "58634"
