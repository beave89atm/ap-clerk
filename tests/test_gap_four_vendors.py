"""Pure checks for the four-vendor gap run. No live KIMCO or Graph."""

from datetime import date

from scripts.gap_four_vendors_0923 import classify_vendor, in_scope, subject_invoice_candidates


def test_classify_four_vendors():
    assert (
        classify_vendor(
            subject="O'Neal Steel Invoice For Account # 14748440",
            from_addr="vsanders@onealsteel.com",
        )
        == "oneal"
    )
    assert (
        classify_vendor(
            subject="New payment request from AMERICAN QUALITY POWDER COATING - invoice 11020",
            from_addr="intuit@notification.intuit.com",
        )
        == "aqpc"
    )
    assert (
        classify_vendor(
            subject="Legacy Wire Products - Sales Invoice PS-INV104200",
            from_addr="ar@legacywire.com",
        )
        == "legacy"
    )
    assert (
        classify_vendor(
            subject="Gas and Supply North Texas Invoice/Statement",
            from_addr="billing@gasandsupply.com",
        )
        == "gas"
    )
    assert classify_vendor(subject="McMaster-Carr invoice", from_addr="noreply@mcmaster.com") is None


def test_subject_invoice_candidates():
    assert subject_invoice_candidates(
        "New payment request from AMERICAN QUALITY POWDER COATING - invoice 11020"
    ) == ["11020"]
    assert subject_invoice_candidates("Sales Invoice PS-INV104200") == ["PS-INV104200"]
    assert "0040437952" in subject_invoice_candidates("Gas invoice 0040437952")
    assert subject_invoice_candidates("O'Neal Steel Invoice For Account # 14748440") == []


def test_in_scope_or_rule():
    assert in_scope(received="2026-08-01T00:00:00Z", invoice_date="2026-07-15") is True
    assert in_scope(received="2026-07-20T00:00:00Z", invoice_date="2026-08-02") is True
    assert in_scope(received="2026-07-20", invoice_date=date(2026, 7, 31)) is False
    assert in_scope(received=None, invoice_date=None) is False
