"""Normalize / dedupe AP-inbox vendors. No live Graph or KIMCO I/O."""

from __future__ import annotations

from datetime import date

from ap_clerk.inbox_vendors import (
    VENDOR_COLUMNS,
    canonicalize_vendor,
    catalog_messages,
    classify_inbox_item,
    count_label,
    default_sheet_path,
    guess_vendor,
    vendor_sheet_rows,
    vendors_same_row,
    write_vendor_workbook,
)


def _msg(
    *,
    subject: str,
    name: str,
    address: str,
    received: str = "2026-08-15T15:00:00Z",
    preview: str = "",
) -> dict:
    return {
        "subject": subject,
        "from": {"emailAddress": {"name": name, "address": address}},
        "receivedDateTime": received,
        "bodyPreview": preview,
        "hasAttachments": True,
    }


def test_mcmaster_aliases_collapse_to_one_canonical():
    assert canonicalize_vendor("McMaster-Carr") == "McMaster-Carr Supply Company"
    assert canonicalize_vendor("McMaster Carr") == "McMaster-Carr Supply Company"
    assert canonicalize_vendor("MCMASTER-CARR SUPPLY COMPANY") == "McMaster-Carr Supply Company"
    assert vendors_same_row("McMaster-Carr", "McMaster Carr")
    assert guess_vendor(
        subject="McMaster-Carr invoice 71401129",
        from_name="DoNotReply",
        from_address="donotreply@mcmaster.com",
    ) == "McMaster-Carr Supply Company"


def test_unifirst_and_msc_rmp_stay_distinct():
    assert canonicalize_vendor("UniFirst First Aid & Safety") == "UniFirst First Aid & Safety"
    assert canonicalize_vendor("UniFirst Corporation") == "UniFirst Corporation"
    assert not vendors_same_row("UniFirst First Aid & Safety", "UniFirst Corporation")
    assert canonicalize_vendor("MSC Industrial Supply") == "MSC Industrial Supply"
    assert canonicalize_vendor("RMP Industrial Supply Inc") == "RMP Industrial Supply Inc"
    assert not vendors_same_row("MSC Industrial Supply", "RMP Industrial Supply Inc")


def test_gas_and_oneal_aliases():
    assert canonicalize_vendor("Gas & Supply") == "Gas and Supply North Texas, LLC"
    assert canonicalize_vendor("O'Neal Steel") == "O'Neal Steel - Dallas (GP)"
    assert canonicalize_vendor("ONeal") == "O'Neal Steel - Dallas (GP)"


def test_catalog_dedupes_mcmaster_and_counts_aug1():
    messages = [
        _msg(
            subject="McMaster-Carr invoice 71401129",
            name="McMaster-Carr",
            address="invoices@mcmaster.com",
            received="2026-08-12T12:00:00Z",
        ),
        _msg(
            subject="Your McMaster Carr order",
            name="McMaster Carr",
            address="donotreply@mcmaster.com",
            received="2026-09-01T12:00:00Z",
        ),
        _msg(
            subject="McMaster invoice older",
            name="McMaster-Carr Supply Company",
            address="invoices@mcmaster.com",
            received="2026-07-15T12:00:00Z",
        ),
    ]
    catalog = catalog_messages(messages, window_start=date(2026, 8, 1))
    assert catalog.unique_vendor_names() == ["McMaster-Carr Supply Company"]
    row = catalog.vendors["McMaster-Carr Supply Company"]
    assert row.count_window == 2
    assert row.count_older == 1
    assert "2" in str(count_label(row))
    assert "before 2026-08-01" in count_label(row)


def test_skip_internal_and_statement_but_keep_known_invoice():
    internal = _msg(
        subject="Lunch Friday",
        name="Kyle",
        address="kyle@kannonmfg.com",
    )
    statement = _msg(
        subject="Monthly Account Statement",
        name="Leeco Steel",
        address="statements@leecosteel.com",
    )
    leeco_invoice = _msg(
        subject="Invoice 12345 from Leeco",
        name="Leeco Steel",
        address="invoices@leecosteel.com",
    )
    assert classify_inbox_item(internal)[0] == "skip"
    assert classify_inbox_item(statement)[0] == "skip"
    kind, vendor, _ = classify_inbox_item(leeco_invoice)
    assert kind == "vendor"
    assert vendor == "Leeco Steel, LLC"

    catalog = catalog_messages([internal, statement, leeco_invoice])
    assert catalog.unique_vendor_names() == ["Leeco Steel, LLC"]
    assert "internal" in catalog.skips or "statement" in catalog.skips


def test_sheet_columns_and_owner_blank(tmp_path):
    catalog = catalog_messages(
        [
            _msg(
                subject="Fastenal invoice(s) have been generated",
                name="FastenalReporting",
                address="FastenalReporting@fastenal.com",
            )
        ]
    )
    rows = vendor_sheet_rows(catalog)
    assert list(rows[0]) == VENDOR_COLUMNS
    assert rows[0]["Vendor"] == "Fastenal Company"
    assert rows[0]["Receiving owner"] == ""
    assert rows[0]["Notes"] == ""
    path = tmp_path / "AP-vendors-from-inbox-2026-09-22.xlsx"
    write_vendor_workbook(path, catalog)
    assert path.is_file()
    assert default_sheet_path(date(2026, 9, 22)).name == "AP-vendors-from-inbox-2026-09-22.xlsx"


def test_sent_from_ap_is_skip():
    sent = _msg(
        subject="Invoice 99",
        name="Accounts Payable",
        address="accountspayable@kannonmfg.com",
    )
    assert classify_inbox_item(sent)[0] == "skip"


def test_live_inbox_domains_and_subject_company():
    assert guess_vendor(
        subject="Invoice - 16455",
        from_name="Accounting",
        from_address="accounting@tpitexas.com",
    ) == "Telecom Products Inc."
    assert guess_vendor(
        subject="Auto-email of Invoices IV-JAM-018024",
        from_name="AR mailer",
        from_address="ar@sss-steel.com",
    ) == "Beshert Steel Processing"
    assert guess_vendor(
        subject="Invoice 25641 - KANNON MANUFACTURING - AMTECH - DUE UPON RECEIPT",
        from_name="Accounts Receivable",
        from_address="ar@capitalmachine.com",
    ) == "Capital Machine Technologies, Inc"
    assert guess_vendor(
        subject="INVOICE 469115        PO 58500",
        from_name="Rosana Arteaga",
        from_address="rosana@fabcorp.com",
    ) == "Fabcorp"
    assert guess_vendor(
        subject="Kannon inv 119770",
        from_name="Michelle Dalton",
        from_address="michelle@ktgalvanizing.com",
    ) == "K-T Galvanizing"
    assert guess_vendor(
        subject="Invoice 152061 from ABY Benefits LLC",
        from_name="Danae Tally",
        from_address="danae@example.com",
    ) == "ABY Benefits LLC"
    assert canonicalize_vendor("Amazon.com") == "Amazon"
    assert canonicalize_vendor("Wasteconnections") == "Waste Connections Lone Star, Inc"
    assert guess_vendor(
        subject="INV # 141551 / CPL # 51715 / PO # 58363",
        from_name="Rachel Bailey",
        from_address="rachel@3pindustries.com",
    ) == "3P"
    assert guess_vendor(
        subject="Customer #FAS-024795 Invoice #42280000168",
        from_name="AR First Aid",
        from_address="ARFirstaidinquiry@unifirst.com",
    ) == "UniFirst First Aid & Safety"
    assert guess_vendor(
        subject="19204",
        from_name="Melody Channell",
        from_address="melody@precisionfabsvs.com",
    ) == "Precision Fabrication Services"
    assert guess_vendor(
        subject="Tejas Transportation Invoices",
        from_name="abel jasso",
        from_address="abel@example.com",
    ) == "Tejas Transportation"


def test_marketing_subject_is_not_a_vendor():
    spectrum = _msg(
        subject="A smarter way to stay connected in FORT WORTH",
        name="Spectrum Business",
        address="offers@spectrumbusiness.com",
    )
    kind, vendor, _ = classify_inbox_item(spectrum)
    assert kind == "vendor"
    assert vendor == "Spectrum Business"
    slogan = catalog_messages([spectrum])
    assert slogan.unique_vendor_names() == ["Spectrum Business"]
    dry = _msg(
        subject="AP dry run 10 — 2026-09-14 afternoon",
        name="Accounts Payable",
        address="accountspayable@kannonmfg.com",
    )
    assert classify_inbox_item(dry)[0] == "skip"
