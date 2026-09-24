"""Pure checks for the four-vendor gap run. No live KIMCO or Graph."""

from datetime import date

from scripts.gap_four_vendors_0923 import (
    classify_vendor,
    credit_signed_amount,
    in_scope,
    invoice_portal_url,
    subject_invoice_candidates,
)


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


def test_invoice_portal_url_ignores_images_and_social():
    assert invoice_portal_url("https://www.gasandsupply.com/images/emailFooter.gif") is False
    assert invoice_portal_url("https://us.content.exclaimer.net/?url=https%3A%2F%2Fwww.instagram.com%2Foneal_steel") is False
    assert (
        invoice_portal_url("https://links.notification.intuit.com/ls/click?upn=abc")
        is True
    )


def test_credit_memo_amount_is_negative():
    text = "CREDIT MEMO\nTOTAL CREDIT AMOUNT\n64.50-\n"
    assert credit_signed_amount(64.50, text) == -64.50
    assert credit_signed_amount(638.88, "TOTAL ORDER AMOUNT\n638.88\n") == 638.88


def test_in_scope_or_rule():
    assert in_scope(received="2026-08-01T00:00:00Z", invoice_date="2026-07-15") is True
    assert in_scope(received="2026-07-20T00:00:00Z", invoice_date="2026-08-02") is True
    assert in_scope(received="2026-07-20", invoice_date=date(2026, 7, 31)) is False
    assert in_scope(received=None, invoice_date=None) is False
