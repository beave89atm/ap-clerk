"""Inbox selection and PDF parse. No live Graph or KIMCO I/O."""

from __future__ import annotations

from pathlib import Path

from ap_clerk.inbox import pull_recent_bills
from ap_clerk.pdf_invoice import parse_invoice_text, vendor_from_context
from ap_clerk.cli import _process_invoice
from ap_clerk.rules import PRICE_DOES_NOT_MATCH, is_fee_or_surcharge


def test_vendor_from_email_domain_and_filename():
    assert vendor_from_context(
        subject="MSC invoice",
        from_name="DoNotReply",
        from_address="DoNotReply@invoices.mscdirect.com",
        text="MSC INDUSTRIAL SUPPLY CO.\nInvoice Number 64564711",
    ) == "MSC Industrial Supply"
    assert vendor_from_context(
        subject="Priority1Invoice17786949",
        from_name="Treyce Wegmann",
        from_address="ap-clerk-test@example.com",
        text="Remit To: Priority1\nInvoice 17786949",
    ) == "Priority 1"
    assert vendor_from_context(
        subject="Sales Invoice SI1090494",
        from_name="Metal Supermarkets",
        from_address="fortworth@metalsupermarkets.com",
        text="",
    ) == "Metal Supermarkets"
    assert vendor_from_context(
        subject="Invoice",
        from_name="mkinv37",
        from_address="mkinv37@marmonkeystone.com",
        text="SOLD BY: MARMON/KEYSTONE",
    ) == "Marmon/Keystone"


def test_parse_invoice_text_generic_bill():
    text = """
    Fastenal Company
    Invoice Date Invoice No.
    07/30/2026 TXFT499739
    Invoice Total
    2620.08 USD
    Cust. No.
    Cust. P.O.
    TXFT40601
    58749
    Shipping & Handling
    124.83
    """
    parsed = parse_invoice_text(
        text,
        subject="Fastenal invoice(s) have been generated for you.",
        from_name="FastenalReporting",
        from_address="FastenalReporting@fastenal.com",
        filename="TXFT499739.pdf",
    )
    assert parsed["invoice_number"] == "TXFT499739"
    assert parsed["po"] == "58749"
    assert parsed["amount"] == 2620.08
    assert parsed["vendor"] == "Fastenal Company"
    assert parsed["check_stop"] is False


def test_parse_air_products_and_emj():
    air = parse_invoice_text(
        "Invoice No.: 436251638\nDate: 08/26/2026\nTotal to be paid USD 2,017.20\nPurchase Order Number: NONE\nDelivery Charge 135.00\nHazmat Charge 120.00",
        subject="Air Products Invoice 0436251638",
        from_address="apdirect@airproducts.com",
        filename="Air_Products_Invoice_0436251638.PDF",
    )
    assert air["invoice_number"] == "436251638"
    assert air["amount"] == 2017.20
    assert air["po"] is None
    assert air["vendor"] == "Air Products and Chemicals, Inc"

    emj = parse_invoice_text(
        "INVOICE NUMBER S814379432\nINVOICE DATE 26-AUG-2026\nCUSTOMER PO 58984\nINVOICE TOTAL $ 752.10",
        subject="EARLE M. JORGENSEN COMPANY - Invoices for 08/26/26",
        from_address="EMJCreditSouth@emjmetals.com",
    )
    assert emj["invoice_number"] == "S814379432"
    assert emj["po"] == "58984"
    assert emj["amount"] == 752.10
    assert emj["vendor"] == "Earle M. Jorgensen Co"


def test_parse_modern_heat_printed_invoice_number_and_parts():
    text = """
    Modern Heat Treat Inc
    Invoice Number: 8-220804
    Invoice Date: 08/26/2026
    PO 58800, line 6
    625-5200-002    1    80.00
    400-5200-001    1    90.00
    Shipping & Handling 26.25
    Invoice Total $196.25
    """
    parsed = parse_invoice_text(
        text,
        subject="Modern Heat Treat Inc., Invoice Number: 220804  Dated: 8/27/2026",
        from_name="Modern Heat Treat Inc",
        filename="2026-08-27_220804.pdf",
    )
    assert parsed["invoice_number"] == "8-220804"
    assert parsed["date"] == "2026-08-26"
    assert parsed["vendor"] == "Modern Heat Treat Inc"
    parts = {line["part"] for line in parsed["lines"]}
    assert "625-5200-002" in parts
    assert "400-5200-001" in parts
    assert any(is_fee_or_surcharge(f["name"]) for f in parsed["fees"])


def test_parse_telecom_uses_printed_date_not_email_received():
    text = """
    Telecom Products Inc.
    Invoice #: 17602
    Invoice Date: 08/26/2026
    PO 58351
    Amount Due $412.00
    """
    parsed = parse_invoice_text(
        text,
        subject="Invoice - 17602",
        from_address="ar@telecomproducts.com",
        filename="2026-08-27_Invoice-17602.pdf",
    )
    assert parsed["invoice_number"] == "17602"
    assert parsed["date"] == "2026-08-26"
    assert parsed["date"] != "2026-08-27"


def test_does_not_invent_invoice_prefix_for_other_vendors():
    parsed = parse_invoice_text(
        "Fastenal Company\nInvoice No. TXFT499356\nInvoice Date 08/26/2026\nInvoice Total 10.00",
        subject="Fastenal invoice(s) have been generated for you.",
        from_address="FastenalReporting@fastenal.com",
        filename="TXFT499356.pdf",
    )
    assert parsed["invoice_number"] == "TXFT499356"


def test_parse_invoice_text_check_stop():
    text = "Gas and Supply\nInvoice #: 0040325801\nPO: CHECK STOP\nAmount Due $418.93"
    parsed = parse_invoice_text(text, subject="CHECK STOP invoice")
    assert parsed["check_stop"] is True


def test_parse_multi_po_leaves_header_po_blank():
    text = "Invoice Number 55555\nPO 58111\nPO 58222\nInvoice Total $100.00\nInvoice Date 08/01/2026"
    parsed = parse_invoice_text(text)
    assert parsed["multi_po"] is True
    assert parsed["po"] is None
    assert parsed["pos"] == ["58111", "58222"]


class _FakeGraph:
    def __init__(self, messages, pdfs_by_id):
        self.messages = messages
        self.pdfs_by_id = pdfs_by_id

    def list_messages(self, mailbox, **kwargs):
        assert mailbox == "accountspayable@kannonmfg.com"
        return self.messages

    def list_attachment_names(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("names") or [])

    def download_pdf_attachments(self, mailbox, message_id):
        return list(self.pdfs_by_id.get(message_id, {}).get("pdfs") or [])


def test_pull_recent_bills_skips_and_returns_oldest_first(tmp_path: Path):
    from pypdf import PdfWriter

    def _pdf_bytes(label: str) -> bytes:
        path = tmp_path / f"{label}.src.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(path)
        return path.read_bytes()

    # Newest-first feed: statement, then three bills. Limit 2 => most recent two bills, oldest-first.
    messages = [
        {
            "id": "m-statement",
            "subject": "Monthly Account Statement",
            "receivedDateTime": "2026-08-27T18:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "Vendor", "address": "ap@vendor.com"}},
        },
        {
            "id": "m-new",
            "subject": "Invoice NEW1",
            "receivedDateTime": "2026-08-26T18:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "Fastenal Company", "address": "billing@fastenal.com"}},
        },
        {
            "id": "m-mid",
            "subject": "Invoice MID2",
            "receivedDateTime": "2026-08-25T18:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "McMaster-Carr", "address": "ar@mcmaster.com"}},
        },
        {
            "id": "m-old",
            "subject": "Invoice OLD3",
            "receivedDateTime": "2026-08-24T18:00:00Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "Telecom Products Inc.", "address": "ar@telecom.com"}},
        },
    ]
    # parse_invoice_pdf will get little text from blank pages; seed via filename + subject
    graph = _FakeGraph(
        messages,
        {
            "m-statement": {"names": ["statement.pdf"], "pdfs": [("statement.pdf", _pdf_bytes("st"))]},
            "m-new": {"names": ["Invoice-NEW1.pdf"], "pdfs": [("Invoice-NEW1.pdf", _pdf_bytes("n"))]},
            "m-mid": {"names": ["Invoice-MID2.pdf"], "pdfs": [("Invoice-MID2.pdf", _pdf_bytes("m"))]},
            "m-old": {"names": ["Invoice-OLD3.pdf"], "pdfs": [("Invoice-OLD3.pdf", _pdf_bytes("o"))]},
        },
    )
    # Blank PDFs won't parse numbers; inject by writing real text PDFs is hard without reportlab.
    # Instead, patch parse_invoice_pdf.
    from ap_clerk import inbox as inbox_mod

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        number = "NEW1"
        if "MID2" in path.name or "MID2" in subject:
            number = "MID2"
        elif "OLD3" in path.name or "OLD3" in subject:
            number = "OLD3"
        elif "NEW1" in path.name or "NEW1" in subject:
            number = "NEW1"
        return {
            "vendor": from_name or "Vendor",
            "invoice_number": number,
            "date": "2026-08-20",
            "po": None,
            "pos": [],
            "amount": 10.0,
            "fees": [],
            "check_stop": False,
            "pdf_text_empty": False,
        }

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, skipped = pull_recent_bills(graph, limit=2, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig
    assert [s["class"] for s in skipped if s.get("class") == "statement"]
    assert [inv["invoice_number"] for inv in selected] == ["MID2", "NEW1"]
    assert str(selected[0]["receivedDateTime"]) < str(selected[1]["receivedDateTime"])


def test_inbox_does_not_copy_email_received_date_onto_invoice(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        {
            "id": "m-telecom",
            "subject": "Invoice - 17602",
            "receivedDateTime": "2026-08-27T19:52:53Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "Telecom Products Inc.", "address": "ar@telecom.com"}},
        }
    ]
    graph = _FakeGraph(
        messages,
        {"m-telecom": {"names": ["Invoice-17602.pdf"], "pdfs": [("Invoice-17602.pdf", b"%PDF-1.4")]}},
    )

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        return {
            "vendor": "Telecom Products Inc.",
            "invoice_number": "17602",
            "date": "2026-08-26",
            "po": "58351",
            "pos": ["58351"],
            "amount": 412.0,
            "fees": [],
            "check_stop": False,
            "pdf_text_empty": False,
        }

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, _skipped = pull_recent_bills(graph, limit=1, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig
    assert selected[0]["date"] == "2026-08-26"
    assert str(selected[0]["receivedDateTime"]).startswith("2026-08-27")
    assert selected[0]["date"] != str(selected[0]["receivedDateTime"])[:10]


def test_inbox_leaves_invoice_date_blank_when_pdf_has_none(tmp_path: Path):
    from ap_clerk import inbox as inbox_mod

    messages = [
        {
            "id": "m-nodate",
            "subject": "Invoice - 17602",
            "receivedDateTime": "2026-08-27T19:52:53Z",
            "hasAttachments": True,
            "from": {"emailAddress": {"name": "Telecom Products Inc.", "address": "ar@telecom.com"}},
        }
    ]
    graph = _FakeGraph(
        messages,
        {"m-nodate": {"names": ["Invoice-17602.pdf"], "pdfs": [("Invoice-17602.pdf", b"%PDF-1.4")]}},
    )

    def fake_parse(path, *, subject="", from_name="", from_address=""):
        return {
            "vendor": "Telecom Products Inc.",
            "invoice_number": "17602",
            "date": None,
            "po": None,
            "pos": [],
            "amount": 412.0,
            "fees": [],
            "check_stop": False,
            "pdf_text_empty": False,
        }

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, _skipped = pull_recent_bills(graph, limit=1, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig
    assert selected[0].get("date") in (None, "")
    assert str(selected[0]["receivedDateTime"]).startswith("2026-08-27")


def test_live_missing_vendor_is_fail_and_no_outlook_flag():
    class FakeKimco:
        target = "live"

        def create(self, service, values):
            raise AssertionError("must not create without a vendor")

        def get_item(self, service, item_id):
            raise AssertionError("must not GET")

    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Unknown Vendor LLC",
            "invoice_number": "ZZ-1",
            "date": "2026-08-27",
            "po": None,
            "amount": 12.0,
            "graph_message_id": "AAMk-should-not-flag",
        },
        batch={"id": 1},
        batch_label="API Agent - 8/27/26 (1)",
        invoice_by_number={},
        vendor_samples=[],
        po_index={},
        pdf_dir=None,
        graph_client=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Fail"
    assert "vendor missing" in row["Why"]
    assert row["Flag in Outlook"] == "Yes"
    assert row["Flag status"] == "skipped-not-success"


def test_po_not_on_target_still_creates_header():
    created = {"id": 77, "values": {"Invoice_Number": "ZZ-2"}}

    class FakeKimco:
        target = "live"

        def create(self, service, values):
            assert "Purchase_Order" not in values
            return 77, created, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Currency": {"id": 3, "text": "USD"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "no-pdf-on-vm"

    row = _process_invoice(
        FakeKimco(),
        {
            "vendor": "Fastenal Company",
            "invoice_number": "ZZ-2",
            "date": "2026-08-27",
            "po": "59999",
            "amount": 20.0,
        },
        batch={"id": 1},
        batch_label="API Agent - 8/27/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}],
        po_index={},
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert row["KIMCO id"] == 77
    assert "PO 59999" in row["Why"]
    assert row["Flag in Outlook"] == "Yes"


def _sample_kimco():
    created = {"id": 80, "values": {"Invoice_Number": "X"}}

    class FakeKimco:
        target = "live"

        def create(self, service, values):
            return 80, created, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Currency": {"id": 3, "text": "USD"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "no-pdf-on-vm"

    return FakeKimco()


def test_name_mismatch_uses_vendor_on_the_po():
    row = _process_invoice(
        _sample_kimco(),
        {
            "vendor": "Weird Printed Name LLC",
            "invoice_number": "453743",
            "date": "2026-08-26",
            "po": "59000",
            "amount": 50.0,
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[
            {
                "vendor_id": 1386,
                "vendor_text": "1386-NATIONAL SPECIALTY ALLOYS",
                "invoice_id": 44,
                "po_text": "59000-NATIONAL SPECIALTY ALLOYS",
            }
        ],
        po_index={
            "59000": {
                "id": 12,
                "text": "59000-NATIONAL SPECIALTY ALLOYS",
                "vendor_id": 1386,
                "vendor_text": "National Specialty Alloys",
                "lines": [],
            }
        },
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert "vendor missing" not in row["Why"]


def test_vendor_from_live_po_is_not_vendor_missing():
    row = _process_invoice(
        _sample_kimco(),
        {
            "vendor": "National Specialty Alloys, Inc",
            "invoice_number": "453743",
            "date": "2026-08-26",
            "po": "59000",
            "amount": 50.0,
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[
            {
                "vendor_id": 1386,
                "vendor_text": "1386-NATIONAL SPECIALTY ALLOYS",
                "invoice_id": 44,
                "po_text": "59000-NATIONAL SPECIALTY ALLOYS",
            }
        ],
        po_index={
            "59000": {
                "id": 12,
                "text": "59000-NATIONAL SPECIALTY ALLOYS",
                "vendor_id": 1386,
                "vendor_text": "National Specialty Alloys",
                "lines": [],
            }
        },
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert "vendor missing" not in row["Why"]
    assert row["KIMCO id"] == 80


def test_coherent_alias_1410_is_not_vendor_missing():
    row = _process_invoice(
        _sample_kimco(),
        {
            "vendor": "Coherent Corp.",
            "invoice_number": "120953",
            "date": "2026-08-26",
            "po": "59001",
            "amount": 25.0,
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 1410, "vendor_text": "Coherent Corp.", "invoice_id": 55, "po_text": ""}],
        po_index={"59001": {"id": 13, "text": "59001-COHERENT", "vendor_id": 1410, "vendor_text": "Coherent", "lines": []}},
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert "vendor missing" not in row["Why"]


def test_price_mismatch_holds_and_does_not_create():
    class NoCreate:
        target = "live"

        def create(self, *args, **kwargs):
            raise AssertionError("must not create a header when price does not match")

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                },
            }

    row = _process_invoice(
        NoCreate(),
        {
            "vendor": "Earle M. Jorgensen Co",
            "invoice_number": "S-BIG",
            "date": "2026-08-26",
            "po": "58984",
            "amount": 2000.0,
            "lines": [{"part": "STEEL", "amount": 1880.0}],
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 1, "vendor_text": "EMJ", "invoice_id": 9, "po_text": ""}],
        po_index={
            "58984": {
                "id": 2,
                "text": "58984-EMJ",
                "vendor_id": 1,
                "lines": [{"part": "STEEL", "amount": 2000.0, "unit_price": 2000.0, "po_line": 1}],
            }
        },
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "HOLD"
    assert PRICE_DOES_NOT_MATCH in row["Why"]
    assert row["PPV"] == "none"
    assert row["KIMCO id"] == ""
    assert "@Shawn McKibben" in row["Why"]


def test_emj_small_ppv_is_recorded_signed():
    row = _process_invoice(
        _sample_kimco(),
        {
            "vendor": "Earle M. Jorgensen Co",
            "invoice_number": "S814379432",
            "date": "2026-08-26",
            "po": "58984",
            "amount": 752.10,
            "lines": [{"part": "STEEL", "amount": 752.10}],
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 1, "vendor_text": "EMJ Earl M. Jorgensen", "invoice_id": 9, "po_text": ""}],
        po_index={
            "58984": {
                "id": 2,
                "text": "58984-EMJ",
                "vendor_id": 1,
                "lines": [{"part": "STEEL", "amount": 770.16, "unit_price": 770.16, "po_line": 1}],
            }
        },
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert row["PPV"] == "-18.06"
    assert "Purchase Price Variance" in row["Why"]


def test_fastenal_receipt_slip_avoids_hold_no_receipts():
    row = _process_invoice(
        _sample_kimco(),
        {
            "vendor": "Fastenal Company",
            "invoice_number": "TXFT499356",
            "date": "2026-08-26",
            "po": "58700",
            "amount": 40.0,
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}],
        po_index={"58700": {"id": 3, "text": "58700-FASTENAL", "vendor_id": 9, "lines": []}},
        receipts=[{"slip": "TXFT499356", "qty": 6, "part": "FAST-1", "po_line": 2, "po": "58700"}],
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "Success"
    assert "no receipts" not in row["Why"].lower()


def test_hold_no_receipts_after_thorough_search():
    class NoCreate:
        target = "live"

        def create(self, *args, **kwargs):
            raise AssertionError("must not create when no receipts match")

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                },
            }

    row = _process_invoice(
        NoCreate(),
        {
            "vendor": "Fastenal Company",
            "invoice_number": "TXFT000000",
            "date": "2026-08-26",
            "po": "58700",
            "amount": 40.0,
            "lines": [{"part": "NEED-THIS", "qty": 6}],
        },
        batch={"id": 1},
        batch_label="API Agent - 8/28/26 (1)",
        invoice_by_number={},
        vendor_samples=[{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}],
        po_index={"58700": {"id": 3, "text": "58700-FASTENAL", "vendor_id": 9, "lines": []}},
        receipts=[{"slip": "OTHER", "qty": 6, "part": "DIFFERENT", "po_line": 1}],
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == "HOLD"
    assert "no receipts" in row["Why"].lower()
    assert row["KIMCO id"] == ""


def test_parse_capital_machine_po_and_trailing_total():
    text = """
    Invoice
    Date
    7/30/2026
    Invoice #
    26167
    Capital Machine Technologies, Inc
    P.O. Number
    58634
    FREIGHT UPS TRKG # 1Z0ER88103504318501 34.00 34.00
    $403.00
    """
    parsed = parse_invoice_text(text, filename="Inv_26167_from_Capital_Machine_Technologies_Inc.pdf")
    assert parsed["vendor"] == "Capital Machine Technologies, Inc"
    assert parsed["invoice_number"] == "26167"
    assert parsed["po"] == "58634"
    assert parsed["amount"] == 403.00
    assert parsed["date"] == "2026-07-30"


def test_parse_willbanks_total_due_usd_and_your_po():
    text = """
    Invoice 209661 (USD)
    Date 7/30/2026
    Issued from:
    Willbanks Metals
    Your PO  58504  (6/24/2026)
    Totals 858.03 LBS Amount 1,828.08
    Total Due   (USD) 1,828.08
    """
    parsed = parse_invoice_text(text, from_name="Estevan Uribe")
    assert parsed["vendor"] == "Willbanks Metals"
    assert parsed["invoice_number"] == "209661"
    assert parsed["po"] == "58504"
    assert parsed["amount"] == 1828.08


def test_parse_telecom_jul_26_date():
    text = """
    Number: 16960 Date:  27-Jul-26
    Invoice
    TELECOM PRODUCTS INC.
    PO: 58351 Ln: 001
    $10,431.36 Invoice Total:
    """
    parsed = parse_invoice_text(text, subject="Invoice - 16960")
    assert parsed["invoice_number"] == "16960"
    assert parsed["date"] == "2026-07-27"
    assert parsed["po"] == "58351"
    assert parsed["amount"] == 10431.36
    assert parsed["vendor"] == "Telecom Products Inc."


def test_parse_shoppas_psi_is_not_customer_po():
    text = """
    Invoice Date
    PSI-001216291 07/30/26
    Shoppa’s Material Handling, Ltd
    Bill To:
    C109050
    KANNON MANUFACTURING
    Dallas, TX 75261-2027 Total Due 814.73
    """
    parsed = parse_invoice_text(
        text,
        filename="C109050__PSI-001216291__.pdf",
        from_name="Shoppas Material Handling",
    )
    assert parsed["invoice_number"] == "PSI-001216291"
    assert parsed["amount"] == 814.73
    assert parsed["po"] is None
    assert "109050" not in parsed["pos"]
    assert parsed["vendor"] == "Shoppa's Material Handling"


def test_parse_unifirst_first_aid_from_sender():
    parsed = parse_invoice_text(
        "Invoice Number: 42203000308\nInvoice Date: 07/29/2026\nInvoice Total:\n 711.16\n 744.50\n 33.34\n 744.50",
        from_name="ARFirstaidinquiry@unifirst.com",
        from_address="ARFirstaidinquiry@unifirst.com",
    )
    assert parsed["vendor"] == "UniFirst First Aid & Safety"
    assert parsed["invoice_number"] == "42203000308"
    assert parsed["amount"] == 744.50


def test_parse_0902_ntex_msc_grm_altparts_tube():
    ntex = parse_invoice_text(
        "NTEX Electric Inc.\nINVOICE # DATE TOTAL DUE\n17-2557 08/01/2026 $1,694.48\nBALANCE DUE $1,694.48",
        from_address="matt@ntexelectric.com",
        filename="INVOICE_17-2557_from_NTEX_Electric_Inc_.pdf",
    )
    assert ntex["vendor"] == "NTEX Electric Inc."
    assert ntex["invoice_number"] == "17-2557"
    assert ntex["amount"] == 1694.48

    msc = parse_invoice_text(
        "MSC INDUSTRIAL SUPPLY CO.\nCustomer Number Invoice Number\n02627782 66623661\nAmount Due $422.87\nInvoice Number Purchase Order No.\n66623661 VENDING/1565",
        from_address="DoNotReply@invoices.mscdirect.com",
        filename="66623661_02627782.PDF",
    )
    assert msc["invoice_number"] == "66623661"
    assert msc["amount"] == 422.87

    grm = parse_invoice_text(
        "GRM INFORMATION MANAGEMENT SERVICES.\nInvoice   00012913\nDate      07/31/2026\nAccount   13010065\nTotal amount due: $189.78",
        from_address="billingdal@grmdocument.com",
        filename="INVOICE_13010065_202607.pdf",
    )
    assert grm["vendor"] == "GRM Information Management Services"
    assert grm["invoice_number"] == "00012913"
    assert grm["amount"] == 189.78

    alt = parse_invoice_text(
        "www.altparts.com\nInvoice Number:\n0097416-IN\nP.O. Number\n58832\n1,765.44 Invoice Total:",
        from_address="DoNotReply@altparts.com",
        filename="000002434_SO_0097416IN_20260803_000.PDF",
    )
    assert alt["vendor"] == "Alternative Parts Inc"
    assert alt["invoice_number"] == "0097416-IN"
    assert alt["po"] == "58832"
    assert alt["amount"] == 1765.44

    tube = parse_invoice_text(
        "https://www.tubesupply.com/terms-and-conditions\n58809\n01175961\n$2140.74",
        from_address="accounting2@sss-steel.com",
        filename="FMGLASER853499000006.pdf",
    )
    assert tube["vendor"] == "Tube Supply"
    assert tube["invoice_number"] == "01175961"
    assert tube["po"] == "58809"
    assert tube["amount"] == 2140.74

    lav = parse_invoice_text(
        "Remit address: Lavanture Products\nSV1426672\n58843\nInvoice amount\n38.04",
        from_name="Sandi Ehlers",
        filename="SalesInvoice_SV1426672_20260804_58843_1786258.PDF",
    )
    assert lav["vendor"] == "Lavanture Products"
    assert lav["invoice_number"] == "SV1426672"
    assert lav["po"] == "58843"

    kloeckner = parse_invoice_text(
        "Kloeckner Metals Corporation\nPLEASE PAY THIS AMOUNT\n$7,167.00\n15206040",
        from_address="autotask@kloecknermetals.com",
        filename="invoice_15206040_260803225345.pdf",
    )
    assert kloeckner["vendor"] == "Kloeckner Metals Corporation"
    assert kloeckner["invoice_number"] == "15206040"
    assert 25576511 not in (kloeckner.get("pos") or [])
    assert kloeckner["amount"] == 7167.00


def test_parse_0904_leeco_austin_a1_legacy_maynard():
    leeco = parse_invoice_text(
        "Leeco Steel, LLC * 1011 Warrenville Rd.\nInvoice:\n619920\nPO Number\n57891\nTotal:\n900.00",
        from_name="NoreplyMV",
        filename="619920.pdf",
    )
    assert leeco["vendor"] == "Leeco Steel, LLC"
    assert leeco["invoice_number"] == "619920"
    assert leeco["po"] == "57891"
    assert leeco["amount"] == 900.00

    austin = parse_invoice_text(
        "PLEASE REMIT TO:\nAustin Hardware & Supply Inc.\nInvoice#\n2488089\nPO Number\n58838\n"
        "TOTAL\nTracking Number\n Due Date\nDuty\n293.70",
        from_address="autoinvoices@austinhardware.com",
        filename="2488089.pdf",
    )
    assert austin["vendor"] == "Austin Hardware & Supply Inc."
    assert austin["invoice_number"] == "2488089"
    assert austin["po"] == "58838"
    assert austin["amount"] == 293.70

    a1 = parse_invoice_text(
        "Invoice:\nINVOICE DATE\n8/6/2026\n66609\nA1 Image, Inc.\nTOTAL\n$753.46",
        from_address="a1imageinc@gmail.com",
        filename="Inv_66609_from_A1_IMAGE_INC._22868.pdf",
    )
    assert a1["vendor"] == "A1 Image Office Systems"
    assert a1["invoice_number"] == "66609"
    assert a1["amount"] == 753.46

    legacy = parse_invoice_text(
        "Legacy Wire Products\nInvoice No.\nPS-INV103946\nExternal Document No.\n58819\nTotal $ Incl. Tax\n657.25",
        from_name="Giovanny",
        filename="Sales Invoice PS-INV103946.pdf",
    )
    assert legacy["vendor"] == "Legacy Wire Products"
    assert legacy["invoice_number"] == "PS-INV103946"
    assert legacy["po"] == "58819"
    assert legacy["amount"] == 657.25

    maynard = parse_invoice_text(
        "Invoice No. 536280291\nInvoice Date April 8, 2026\nTotal This Invoice $678.00\n"
        "Maynard Nexsen PC USPS Mail: Dept 6575",
        from_name="Kyle Cleaver",
        filename="536280291_BL_1194107_824266.pdf",
    )
    assert maynard["vendor"] == "Maynard Nexsen PC"
    assert maynard["invoice_number"] == "536280291"
    assert maynard["amount"] == 678.00
    assert maynard["po"] is None
