"""Why-must-be-true + PDF-is-truth. No live Graph or KIMCO I/O.

Kyle 2026-09-11: Crosslink/Nova/Telecom/Insight HOLDs must not reuse an
MSC/McQueary slogan. The vendor PDF is the version of the truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from ap_clerk.cli import _process_invoice
from ap_clerk.gates import (
    GATE_PREFLIGHT,
    RESULT_HOLD,
    attach_presence_status,
    pdf_file_present,
    preflight_parse_gate,
    why_has_foreign_history,
    why_preflight_this_invoice,
)
from ap_clerk.pdf_invoice import parse_invoice_text, vendor_from_context
from ap_clerk.quality_v12 import assert_never_success

FIXTURE = json.loads(Path("fixtures/treyce-2026-09-10-never-repeat.json").read_text())
NOTES = FIXTURE["notes"]


def _kimco(*, attach="attached", select="selected", created_id=8800):
    class K:
        target = "live"

        def __init__(self):
            self.created = []

        def create(self, service, values):
            self.created.append(values)
            return created_id, {"id": created_id, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return attach

        def try_select_receipts(self, *args, **kwargs):
            return select

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    return K()


def _row(inv, *, kimco=None, po_index=None, samples=None):
    client = kimco or _kimco()
    return _process_invoice(
        client,
        inv,
        batch={"id": 1},
        batch_label="API Agent - 9/11/26 (1)",
        invoice_by_number={},
        vendor_samples=samples
        or [{"vendor_id": 9, "vendor_text": inv.get("vendor") or "Vendor", "invoice_id": 100, "po_text": ""}],
        po_index=po_index or {},
        receipts=None,
        pdf_dir=None,
        graph_client=None,
        flag_outlook=False,
    ), client


def _assert_why_this_invoice(why: str, inv: dict) -> None:
    assert why
    assert GATE_PREFLIGHT in why or "parse-error" in why
    assert why_has_foreign_history(why, inv) == []
    facts_ok = (
        (inv.get("vendor") or "")[:8] in why
        or str(inv.get("invoice_number") or "") in why
        or str(inv.get("filename") or "") in why
    )
    assert facts_ok, why
    source = str((inv.get("field_sources") or {}).get("invoice_number") or "")
    if source in {"filename", "subject", "pdf"}:
        assert source in why.lower()
    if pdf_file_present(inv):
        assert "no-pdf-on-vm" not in why
        assert "PDF on disk" in why or "on disk" in why.lower()
    else:
        assert "missing" in why.lower() or "not" in why.lower()
    assert "Next:" in why


def test_crosslink_27943_why_not_msc_mcqueary(tmp_path: Path):
    """Crosslink 27943 Why must not contain McQueary or MSC; must cite this bill."""
    n = NOTES["NOTE-13"]
    bill = n["bills"][0]
    pdf_path = tmp_path / bill["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 crosslink 27943")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": bill["invoice_number"],
        "date": n["date"],
        "amount": 412.0,
        "po": bill["po"],
        "subject": bill["subject"],
        "filename": bill["filename"],
        "field_sources": {
            "invoice_number": "filename",
            "date": "pdf",
            "amount": "pdf",
            "po": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    ok, why = preflight_parse_gate(sidecar)
    assert ok is True, why
    assert "McQueary" not in why
    assert "MSC" not in why
    assert "preflight-parse" not in why

    missing = {**sidecar, "pdf_path": "", "pdf_on_disk": False, "pdf_unavailable": True, "pdf_text_empty": True}
    miss_ok, miss_why = preflight_parse_gate(missing)
    assert miss_ok is False
    _assert_why_this_invoice(miss_why, missing)
    assert "McQueary" not in miss_why
    assert "MSC" not in miss_why
    assert "Crosslink" in miss_why
    assert "27943" in miss_why
    assert "filename" in miss_why
    assert "invoice-27943" in miss_why
    assert attach_presence_status(missing) == "no-pdf-on-vm"
    assert attach_presence_status(sidecar) == "pdf-on-vm"


def test_nova_258145_why_not_msc_mcqueary(tmp_path: Path):
    n = NOTES["NOTE-11"]
    pdf_path = tmp_path / n["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 nova 258145")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": n["invoice_number"],
        "date": n["date"],
        "amount": n["amount"],
        "subject": n["subject"],
        "from_name": n["from_name"],
        "filename": n["filename"],
        "field_sources": {
            "invoice_number": n["buggy_number_source"],
            "date": "pdf",
            "amount": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    ok, why = preflight_parse_gate(sidecar)
    assert ok is True, why
    assert "McQueary" not in why
    assert "MSC" not in why

    missing = {**sidecar, "pdf_path": "", "pdf_on_disk": False, "pdf_unavailable": True, "pdf_text_empty": True}
    miss_ok, miss_why = preflight_parse_gate(missing)
    assert miss_ok is False
    _assert_why_this_invoice(miss_why, missing)
    assert "McQueary" not in miss_why
    assert "MSC" not in miss_why
    assert "Nova" in miss_why
    assert "258145" in miss_why
    assert "subject" in miss_why


def test_telecom_style_why_not_msc_mcqueary():
    inv = {
        "vendor": "Telecom Products Inc.",
        "invoice_number": "88221",
        "date": "2026-08-18",
        "amount": 40.0,
        "subject": "Telecom invoice 88221",
        "filename": "telecom-88221.pdf",
        "field_sources": {"invoice_number": "filename", "date": "pdf", "amount": "pdf"},
        "pdf_path": "",
        "pdf_on_disk": False,
        "pdf_unavailable": True,
        "pdf_text_empty": True,
    }
    ok, why = preflight_parse_gate(inv)
    assert ok is False
    _assert_why_this_invoice(why, inv)
    assert "Telecom" in why
    assert "88221" in why
    assert "McQueary" not in why
    assert "MSC" not in why


def test_missing_pdf_hold_may_say_missing_still_no_foreign_slogan():
    inv = {
        "vendor": "Insight Controller Services",
        "invoice_number": "1809",
        "filename": "insight-unknown.pdf",
        "field_sources": {"invoice_number": "filename"},
        "pdf_unavailable": True,
        "pdf_text_empty": True,
        "pdf_on_disk": False,
        "pdf_path": "",
    }
    ok, why = preflight_parse_gate(inv)
    assert ok is False
    _assert_why_this_invoice(why, inv)
    assert "missing" in why.lower() or "PDF path missing" in why
    assert "Insight" in why
    assert "McQueary" not in why
    assert "MSC" not in why
    # A true MSC missing-PDF HOLD may name MSC — that is THIS vendor.
    msc = {
        "vendor": "MSC Industrial Supply",
        "invoice_number": "191471",
        "subject": "Rob Brown invoice 191471",
        "filename": "191471.pdf",
        "field_sources": {"invoice_number": "filename"},
        "pdf_unavailable": True,
        "pdf_on_disk": False,
        "pdf_path": "",
    }
    msc_ok, msc_why = preflight_parse_gate(msc)
    assert msc_ok is False
    assert "MSC" in msc_why
    assert "McQueary" not in msc_why
    assert "191471" in msc_why
    assert why_has_foreign_history(msc_why, msc) == []


def test_pdf_is_truth_crosslink_creates_header_and_attaches(tmp_path: Path):
    n = NOTES["NOTE-13"]
    bill = n["bills"][0]
    pdf_path = tmp_path / bill["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 crosslink 27943")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": bill["invoice_number"],
        "date": n["date"],
        "amount": 412.0,
        "po": bill["po"],
        "pos": [bill["po"]],
        "subject": bill["subject"],
        "filename": bill["filename"],
        "field_sources": {
            "invoice_number": "filename",
            "date": "pdf",
            "amount": "pdf",
            "po": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    ok, why = preflight_parse_gate(sidecar)
    assert ok is True, why
    row, client = _row(
        sidecar,
        po_index={
            bill["po"]: {
                "id": 22,
                "text": f"{bill['po']}-CROSSLINK",
                "vendor_id": 278,
                "vendor_text": n["vendor"],
                "lines": [{"part": "COAT", "po_line": 1, "qty": 1, "amount": 412.0, "unit_price": 412.0}],
            }
        },
        samples=[{"vendor_id": 278, "vendor_text": n["vendor"], "invoice_id": 100, "po_text": bill["po"]}],
    )
    assert "preflight-parse" not in (row["Why"] or "")
    assert row["Result"] != RESULT_HOLD or "preflight-parse" not in (row["Why"] or "")
    assert row["KIMCO id"] not in (None, "")
    assert row["Attach status"] == "attached"
    assert row["Attach status"] != "no-pdf-on-vm"
    assert client.created
    assert client.created[0]["Invoice_Number"] == "27943"


def test_pdf_is_truth_nova_creates_header_and_attaches(tmp_path: Path):
    n = NOTES["NOTE-11"]
    pdf_path = tmp_path / n["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 nova 258145")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": n["invoice_number"],
        "date": n["date"],
        "amount": n["amount"],
        "po": None,
        "subject": n["subject"],
        "from_name": n["from_name"],
        "filename": n["filename"],
        "field_sources": {
            "invoice_number": "subject",
            "date": "pdf",
            "amount": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    ok, why = preflight_parse_gate(sidecar)
    assert ok is True, why
    row, client = _row(sidecar)
    assert "preflight-parse" not in (row["Why"] or "")
    assert row["KIMCO id"] not in (None, "")
    assert row["Attach status"] == "attached"
    assert client.created
    assert client.created[0]["Invoice_Number"] == "258145"


def test_ocr_failed_hold_describes_this_invoice(tmp_path: Path):
    pdf_path = tmp_path / "invoice-27943.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 unreadable")
    inv = {
        "vendor": "Crosslink Powder Coating",
        "invoice_number": "27943",
        "filename": "invoice-27943.pdf",
        "field_sources": {"invoice_number": "filename"},
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
        "pdf_text_empty": True,
        "ocr_failed": True,
    }
    ok, why = preflight_parse_gate(inv)
    assert ok is False
    _assert_why_this_invoice(why, inv)
    assert "OCR" in why or "ocr" in why.lower() or "Extract" in why
    assert "no-pdf-on-vm" not in why
    assert "Crosslink" in why
    assert "McQueary" not in why
    assert "MSC" not in why
    assert attach_presence_status(inv) == "pdf-on-vm"


def test_pdf_wins_vendor_over_from_person():
    vendor = vendor_from_context(
        subject="Invoice 258145",
        from_name="Erica Barrett",
        text="NOVA ALLOYS, INC.\nInvoice 258145\nAmount Due $25.00\n",
    )
    assert vendor == "Nova Alloys"
    assert vendor != "Erica Barrett"


def test_why_preflight_builder_never_appends_slogan():
    inv = {
        "vendor": "Crosslink Powder Coating",
        "invoice_number": "27943",
        "filename": "invoice-27943.pdf",
        "field_sources": {"invoice_number": "filename"},
        "pdf_on_disk": False,
    }
    detail = why_preflight_this_invoice(inv, "pdf_missing")
    assert "MSC/McQueary" not in detail
    assert "McQueary" not in detail
    assert "MSC" not in detail
    assert "Crosslink" in detail
    assert "filename" in detail
    assert_never_success(RESULT_HOLD, note_id="WHY-THIS-INVOICE", detail=detail)


def test_gas_unverified_total_why_is_this_invoice():
    parsed = parse_invoice_text(
        "Gas and Supply\nInvoice Number 0040323616\nInvoice Date 08/01/2026\n",
        subject="Gas 0040323616 Amount 418.93",
        filename="0040323616.pdf",
    )
    ok, why = preflight_parse_gate(parsed)
    assert ok is False
    assert "0040323616" in why
    assert "McQueary" not in why
    assert "(MSC/McQueary)" not in why
    assert "Next:" in why
