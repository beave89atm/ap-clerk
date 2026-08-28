"""Inbox selection and PDF parse. No live Graph or KIMCO I/O."""

from __future__ import annotations

from pathlib import Path

from ap_clerk.inbox import pull_recent_bills
from ap_clerk.pdf_invoice import parse_invoice_text
from ap_clerk.cli import _process_invoice


def test_parse_invoice_text_generic_bill():
    text = """
    Fastenal Company
    Invoice Number: TXFT499739
    Invoice Date: 07/30/2026
    Customer PO: 58749
    Shipping & Handling $124.83
    Invoice Total $2,620.08
    """
    parsed = parse_invoice_text(text, subject="Invoice TXFT499739", from_name="Fastenal Company")
    assert parsed["invoice_number"] == "TXFT499739"
    assert parsed["po"] == "58749"
    assert parsed["amount"] == 2620.08
    assert parsed["date"] == "2026-07-30"
    assert parsed["check_stop"] is False
    assert any("Shipping" in f["name"] or "shipping" in f["name"].lower() for f in parsed["fees"])


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

    def fake_parse(path, *, subject="", from_name=""):
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
    assert row["Flag in Outlook"] == "No"
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
