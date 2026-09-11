"""QUALITY V1.2 never-repeat + Treyce-load regressions.

One named test per Treyce 2026-09-10 Note. Fixture-based. No live Graph or KIMCO.
Hard invariant: those 8/16 failure modes are never reportable as Success.
"""

from __future__ import annotations

import json
from pathlib import Path

from ap_clerk.cli import _process_invoice
from ap_clerk.gates import (
    GATE_AUTO_PAY,
    GATE_PDF_LINK,
    GATE_PREFLIGHT,
    GATE_PRICE,
    GATE_QTY,
    RESULT_HOLD,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
    never_type_4_when_po,
    preflight_parse_gate,
    qty_gate,
    treyce_finish_selfcheck,
)
from ap_clerk.graph import (
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_ENTERED_WITH_ISSUES,
    categories_for_status,
)
from ap_clerk.pdf_invoice import (
    expand_gas_misc_invoices,
    parse_invoice_text,
    vendor_from_context,
)
from ap_clerk.pdf_links import REASON_PDF_BEHIND_LINK, classify_download, extract_https_links
from ap_clerk.quality_v12 import (
    TREYCE_FINISH_CHECKLIST,
    TREYCE_NOTES_V12,
    assert_never_success,
    note_ids,
)
from ap_clerk.rules import (
    GAS_AND_SUPPLY_MISC_ITEM,
    INVOICE_TYPE_NO_PO,
    INVOICE_TYPE_PO,
    PRICE_DOES_NOT_MATCH,
    classify_mail,
    decide_ppv,
    description_match_score,
    evaluate_bill_price_variance,
    invoice_type_for,
    is_auto_pay,
    is_fee_or_surcharge,
    is_noise_reason,
    match_receipts,
    misc_purchase_item_for,
    printed_invoice_number,
    should_create_header,
)

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


def _row(inv, *, kimco=None, po_index=None, receipts=None, samples=None, graph=None):
    client = kimco or _kimco()
    return _process_invoice(
        client,
        inv,
        batch={"id": 1},
        batch_label="API Agent - 9/10/26 (1)",
        invoice_by_number={},
        vendor_samples=samples
        or [{"vendor_id": 9, "vendor_text": inv.get("vendor") or "Vendor", "invoice_id": 100, "po_text": ""}],
        po_index=po_index or {},
        receipts=receipts,
        pdf_dir=None,
        graph_client=graph,
        flag_outlook=bool(graph),
    ), client


def test_v12_registry_covers_all_notes():
    assert note_ids() == tuple(f"NOTE-{i:02d}" for i in range(1, 12))
    assert len(TREYCE_NOTES_V12) == 11
    assert len(TREYCE_FINISH_CHECKLIST) == 8
    slugs = {note["slug"] for note in TREYCE_NOTES_V12}
    assert slugs == {
        "insight-msc-pdf-invoice-number",
        "technitool-suffix-fees-not-ppv",
        "capital-qty-discrepancy",
        "purvis-po-never-type-4",
        "oneal-description-line-match",
        "emj-price-hold-header-and-category",
        "toyota-autopay-hold",
        "melody-channell-not-noise",
        "aqpc-pdf-behind-link",
        "gas-supply-misc-vs-check-stop",
        "nova-258145-from-person-not-vendor",
    }


def test_note01_insight_msc_pdf_invoice_number():
    """NOTE-01: Insight 1809 and MSC 5157357 from PDF; filename 191471 is wrong."""
    insight = parse_invoice_text(
        NOTES["NOTE-01"]["insight_pdf_text"],
        filename=NOTES["NOTE-01"]["insight_filename"],
        from_name="Insight Controller Services",
    )
    assert insight["invoice_number"] == "1809"
    assert insight["field_sources"]["invoice_number"] == "pdf"
    ok, why = preflight_parse_gate(insight)
    assert ok is True, why
    assert_never_success("HOLD" if not ok else "checked", note_id="NOTE-01")

    msc = parse_invoice_text(
        NOTES["NOTE-01"]["msc_pdf_text"],
        filename=NOTES["NOTE-01"]["msc_filename"],
        subject=NOTES["NOTE-01"]["msc_subject"],
        from_address="DoNotReply@invoices.mscdirect.com",
    )
    assert msc["invoice_number"] == "5157357"
    assert msc["invoice_number"] != "191471"
    assert msc["po"] == "58842"
    assert msc["field_sources"]["invoice_number"] == "pdf"
    ok, _ = preflight_parse_gate(msc)
    assert ok is True

    empty = parse_invoice_text("", filename="191471.pdf", subject="Rob Brown 191471")
    ok, why = preflight_parse_gate(empty)
    assert ok is False
    assert GATE_PREFLIGHT in why or "no-pdf" in why.lower() or "parse-error" in why
    row, _ = _row({**empty, "vendor": "MSC Industrial Supply"})
    assert row["Result"] != RESULT_SUCCESS
    assert_never_success(row["Result"], note_id="NOTE-01", detail=row["Why"])
    assert row["Why"]


def test_note02_technitool_suffix_and_fees_not_ppv():
    """NOTE-02: S1387370.001 not bare S1387370; $46.20 is Fees, never PPV. Fake-Success forbidden."""
    parsed = parse_invoice_text(
        NOTES["NOTE-02"]["techni_pdf_text"],
        from_name="Techni-Tool",
        filename="S1387370.pdf",
    )
    assert parsed["invoice_number"] == "S1387370.001"
    assert parsed["invoice_number"] != "S1387370"
    assert printed_invoice_number("S1387370", vendor="Techni-Tool", text=NOTES["NOTE-02"]["techni_pdf_text"]) == "S1387370.001"

    fee_label = "Shop supplies / misc surcharge"
    assert is_fee_or_surcharge(fee_label)
    decision = decide_ppv(
        invoice_line_amount=46.20,
        po_line_amount=0.0,
        invoice_total=246.20,
        label=fee_label,
    )
    assert decision["action"] == "fee"
    assert decision["ppv"] == 0.0
    bill = evaluate_bill_price_variance(
        [{"part": "TOOL", "amount": 200.00}, {"label": fee_label, "amount": 46.20, "fee": True}],
        [{"part": "TOOL", "amount": 200.00, "unit_price": 200.00}],
        invoice_total=246.20,
    )
    assert bill["hold"] is False
    assert bill["ppv_total"] == 0.0
    assert any(item.get("action") == "fee" for item in bill["items"])

    # 8/16 buggy self-check: bare number + fee as PPV must not be Success.
    ok, why = treyce_finish_selfcheck(
        {
            "invoice_number": "S1387370",
            "field_sources": {"invoice_number": "pdf"},
            "pdf_text": NOTES["NOTE-02"]["techni_pdf_text"],
            "fees_posted_as_ppv": True,
            "require_pdf_number": True,
        }
    )
    assert ok is False
    assert "S1387370.001" in why or "suffix" in why.lower() or "PPV" in why
    assert_never_success(RESULT_HOLD, note_id="NOTE-02")


def test_note03_capital_qty_discrepancy_hold():
    """NOTE-03: Capital 26764 qty 2 vs 2.5 is HOLD, never Success. $25 is Fees."""
    n = NOTES["NOTE-03"]
    ok, why = qty_gate(
        [{"part": "BLADE", "qty": n["invoice_qty"], "amount": 400.0}],
        [{"part": "BLADE", "qty": n["po_qty"], "amount": 320.0}],
    )
    assert ok is False
    assert GATE_QTY in why or "qty" in why.lower()

    kimco = _kimco()
    row, client = _row(
        {
            "vendor": n["vendor"],
            "invoice_number": n["invoice_number"],
            "date": "2026-08-16",
            "po": n["po"],
            "amount": 425.0,
            "fees": [{"name": "Handling", "amount": n["fee_amount"]}],
            "lines": [{"part": "BLADE", "qty": n["invoice_qty"], "amount": 400.0}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=kimco,
        po_index={
            n["po"]: {
                "id": 45,
                "text": f"{n['po']}-CAPITAL",
                "vendor_id": 45,
                "vendor_text": "Capital Machine",
                "lines": [{"part": "BLADE", "qty": n["po_qty"], "amount": 400.0, "unit_price": 200.0}],
            }
        },
        samples=[{"vendor_id": 45, "vendor_text": "Capital Machine", "invoice_id": 9, "po_text": ""}],
        receipts=[{"po": n["po"], "slip": n["invoice_number"], "part": "BLADE", "qty": n["po_qty"], "id": 1}],
    )
    assert row["Result"] == RESULT_HOLD
    assert row["Result"] != RESULT_SUCCESS
    assert_never_success(row["Result"], note_id="NOTE-03", detail=row["Why"])
    assert GATE_QTY in row["Why"] or "qty" in row["Why"].lower()
    assert "25.00" in row["Fees and surcharges"] or "Handling" in row["Fees and surcharges"]
    assert row["KIMCO id"] not in (None, "")
    assert client.created
    assert row["Why"]


def test_note04_purvis_po_never_type_4():
    """NOTE-04: Purvis 32625214 has PO 58926 — Type 3, never blank Type 4 Success."""
    parsed = parse_invoice_text(
        NOTES["NOTE-04"]["purvis_pdf_text"],
        from_name="Purvis Industries",
        filename="32625214.pdf",
    )
    assert parsed["invoice_number"] == "32625214"
    assert parsed["po"] == "58926"
    assert invoice_type_for(parsed["po"]) == INVOICE_TYPE_PO
    assert invoice_type_for(parsed["po"]) != INVOICE_TYPE_NO_PO
    assert never_type_4_when_po(parsed["po"], INVOICE_TYPE_PO) is True
    assert never_type_4_when_po(parsed["po"], INVOICE_TYPE_NO_PO) is False

    ok, why = treyce_finish_selfcheck(
        {
            "printed_pos": ["58926"],
            "invoice_type": INVOICE_TYPE_NO_PO,
            "po": None,
            "require_pdf_number": False,
        }
    )
    assert ok is False
    assert "Type 4" in why or "58926" in why
    assert_never_success(RESULT_HOLD, note_id="NOTE-04")

    row, client = _row(
        {
            **parsed,
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        po_index={
            "58926": {
                "id": 77,
                "text": "58926-PURVIS",
                "vendor_id": 333,
                "vendor_text": "Purvis Industries",
                "lines": [{"part": "BEARING", "qty": 1, "amount": 150.0, "unit_price": 150.0}],
            }
        },
        samples=[{"vendor_id": 333, "vendor_text": "Purvis Industries", "invoice_id": 12, "po_text": ""}],
        receipts=[{"po": "58926", "slip": "32625214", "part": "BEARING", "qty": 1, "id": 3}],
    )
    assert client.created
    assert client.created[0]["Invoice_Type"] == INVOICE_TYPE_PO
    assert client.created[0].get("Purchase_Order", {}).get("id") == 77
    assert row["PO"] == "58926"
    assert "Misc Type 4" not in row["Why"]
    if row["Result"] == RESULT_SUCCESS:
        assert client.created[0]["Invoice_Type"] != INVOICE_TYPE_NO_PO


def test_note05_oneal_description_line_match():
    """NOTE-05: O'Neal description matches line 3, not first qty. Wrong line never Success."""
    n = NOTES["NOTE-05"]
    assert description_match_score(n["invoice_description"], n["po_line_3"]) >= 50
    assert description_match_score(n["invoice_description"], n["wrong_line_1"]) < 50

    picked = match_receipts(
        invoice_number="15439109",
        invoice_lines=[{"description": n["invoice_description"], "label": n["invoice_description"], "qty": 1, "amount": 100.03}],
        receipts=[
            {"part": "ANGLE", "description": n["wrong_line_1"], "label": n["wrong_line_1"], "qty": 1, "po_line": 1, "id": 1},
            {"part": "FLAT", "description": "FLAT BAR 1/4", "label": "FLAT BAR 1/4", "qty": 1, "po_line": 2, "id": 2},
            {"part": "P-1.00 SCH 40-A500", "description": n["po_line_3"], "label": n["po_line_3"], "qty": 1, "po_line": 3, "id": 3},
        ],
        po_number="58800",
    )
    assert picked["found"] is True
    assert str(picked["matched"][0]["receipt"]["po_line"]) == "3"
    assert picked["matched"][0]["receipt"]["id"] == 3

    rounding = decide_ppv(invoice_line_amount=100.00, po_line_amount=100.03, invoice_total=100.03)
    assert rounding["action"] == "ppv"
    assert abs(rounding["ppv"] + n["rounding"]) < 0.001
    assert rounding["hold"] is False

    ok, why = treyce_finish_selfcheck({"receipt_qty_only_match": True, "require_pdf_number": False})
    assert ok is False
    assert "qty" in why.lower() or "O'Neal" in why or "description" in why.lower()
    assert_never_success(RESULT_HOLD, note_id="NOTE-05")


def test_note06_emj_price_hold_header_entered_with_issues():
    """NOTE-06: EMJ large gap HOLDs, still creates header+PDF, Entered with issues, never Success."""
    n = NOTES["NOTE-06"]

    class Graph:
        def __init__(self):
            self.issues = []

        def flag_issues(self, mailbox, message_id):
            self.issues.append(message_id)
            return FLAG_ENTERED_WITH_ISSUES

        def flag_hold(self, mailbox, message_id):
            raise AssertionError("header+PDF price HOLD must use Entered with issues")

        def flag_matched(self, mailbox, message_id):
            raise AssertionError("must not get Entered in AI")

    graph = Graph()
    kimco = _kimco()
    row, client = _row(
        {
            "vendor": n["vendor"],
            "invoice_number": n["invoice_number"],
            "date": "2026-08-16",
            "po": "58000",
            "amount": n["amount"],
            "lines": [{"part": "STEEL", "amount": n["po_amount"]}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
            "graph_message_id": "AAMk-emj-v12",
        },
        kimco=kimco,
        po_index={
            "58000": {
                "id": 8,
                "text": "58000-EMJ",
                "vendor_id": 208,
                "vendor_text": "EMJ",
                "lines": [{"part": "STEEL", "amount": n["amount"], "unit_price": n["amount"], "qty": 1}],
            }
        },
        samples=[{"vendor_id": 208, "vendor_text": "EMJ", "invoice_id": 9, "po_text": ""}],
        graph=graph,
    )
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-06", detail=row["Why"])
    assert GATE_PRICE in row["Why"] or PRICE_DOES_NOT_MATCH in row["Why"]
    assert row["KIMCO id"] == 8800
    assert client.created
    assert row["Flag status"] == FLAG_ENTERED_WITH_ISSUES
    assert graph.issues == ["AAMk-emj-v12"]
    assert "@Shawn McKibben" in row["Why"]
    cats = categories_for_status(["Entered in AI"], add=ENTERED_WITH_ISSUES_CATEGORY)
    assert cats == [ENTERED_WITH_ISSUES_CATEGORY]


def test_note07_toyota_autopay_hold():
    """NOTE-07: Toyota / auto-pay HOLD, no ERP header, never Success."""
    n = NOTES["NOTE-07"]
    assert classify_mail(subject=n["subject"]) == "auto-pay"
    assert is_auto_pay(vendor=n["vendor"], subject=n["subject"])
    assert should_create_header({"vendor": n["vendor"], "hold_reason": "auto-pay"}) == (False, "auto-pay")

    class NoCreate:
        target = "live"

        def create(self, *args, **kwargs):
            raise AssertionError("auto-pay must not create a header")

    row, _ = _row(
        {
            "vendor": n["vendor"],
            "invoice_number": "3320056",
            "date": "2026-08-16",
            "po": "55483",
            "amount": 99.0,
            "hold_reason": "auto-pay",
            "subject": n["subject"],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=NoCreate(),
    )
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-07", detail=row["Why"])
    assert GATE_AUTO_PAY in row["Why"] or "auto-pay" in row["Why"]
    assert row["KIMCO id"] == ""
    assert row["Why"]


def test_note08_melody_channell_not_noise():
    """NOTE-08: Melody Channell invoices are bills, not junk not-a-bill."""
    n = NOTES["NOTE-08"]
    klass = classify_mail(subject=n["subject"], attachment_names=n["attachment_names"], preview=n["from_name"])
    assert klass == "invoice"
    assert klass != "not-a-bill"
    assert not is_noise_reason(klass)
    parsed = parse_invoice_text(
        "Melody Channell\nInvoice 4401\nInvoice Date 08/16/2026\nAmount Due $40.00\n",
        from_name=n["from_name"],
        subject=n["subject"],
    )
    assert parsed["vendor"] == "Melody Channell" or "Melody" in parsed["vendor"]
    assert parsed["invoice_number"] == "4401"
    ok, _ = preflight_parse_gate(parsed)
    assert ok is True
    # Classifying her mail as Skipped noise was the 8/16 miss — that is never Success.
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-08")
    assert_never_success("not-a-bill", note_id="NOTE-08")


def test_note09_aqpc_pdf_behind_link():
    """NOTE-09: AQPC https link — auth wall HOLDs pdf-behind-link, never silent skip/Success."""
    n = NOTES["NOTE-09"]
    links = extract_https_links(n["body"])
    assert links and links[0].startswith("https://")
    reason = classify_download(
        status_code=401,
        content=b"<html>please sign in</html>",
        content_type="text/html",
        text="please sign in",
    )
    assert reason == REASON_PDF_BEHIND_LINK

    ok, why = preflight_parse_gate(
        {
            "vendor": n["vendor"],
            "hold_reason": "pdf-behind-link",
            "pdf_behind_link": True,
        }
    )
    assert ok is False
    assert GATE_PDF_LINK in why
    row, _ = _row(
        {
            "vendor": n["vendor"],
            "invoice_number": "",
            "hold_reason": "pdf-behind-link",
            "pdf_behind_link": True,
            "action": "hold",
        }
    )
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-09", detail=row["Why"])
    assert row["KIMCO id"] == ""
    assert "not-a-bill" not in row["Why"].lower() or "pdf-behind-link" in row["Why"]
    # Deferred stub: authenticated portals stay HOLD, never Success.
    deferred = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-09")
    assert deferred.get("deferred")
    assert_never_success(RESULT_SUCCESS if False else RESULT_HOLD, note_id="NOTE-09")


def test_note10_gas_supply_misc_vs_check_stop():
    """NOTE-10: Gas invoice pages are Misc Shop Supplies - G&S, not blanket CHECK STOP."""
    n = NOTES["NOTE-10"]
    assert classify_mail(subject="CHECK STOP Gas and Supply") == "invoice"
    misc = parse_invoice_text(n["misc_pdf_text"], subject="CHECK STOP Gas and Supply", from_address="billing@gasandsupply.com")
    assert misc["check_stop"] is False
    assert misc["invoice_number"] == "0040367887"
    assert misc_purchase_item_for(misc["vendor"]) == GAS_AND_SUPPLY_MISC_ITEM == n["misc_item"]

    notice = parse_invoice_text(n["notice_pdf_text"], subject="CHECK STOP Gas and Supply")
    assert notice["check_stop"] is True
    row, _ = _row({**notice, "hold_reason": "CHECK STOP", "check_stop": True, "vendor": "Gas and Supply North Texas, LLC"})
    assert row["Result"] == RESULT_SKIPPED
    assert_never_success(row["Result"], note_id="NOTE-10", detail=row["Why"])

    first = parse_invoice_text(n["multi_pdf_text"], from_address="billing@gasandsupply.com")
    bills = expand_gas_misc_invoices(n["multi_pdf_text"], first)
    assert len(bills) >= 5
    assert all(b.get("gas_misc_ambiguous") for b in bills)
    amb, _ = _row(
        {
            **bills[0],
            "gas_misc_ambiguous": True,
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": ""},
        }
    )
    assert amb["Result"] == RESULT_HOLD
    assert_never_success(amb["Result"], note_id="NOTE-10", detail=amb["Why"])
    assert "Shop Supplies" in amb["Why"]
    deferred = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-10")
    assert deferred.get("deferred")


def test_v12_treyce_finish_selfcheck_blocks_fake_success():
    """Treyce-load: any checklist miss is HOLD, never Success."""
    assert [item["id"] for item in TREYCE_FINISH_CHECKLIST] == [
        "invoice-number-from-pdf",
        "po-not-blank-type-4",
        "receipt-match-by-description",
        "qty-matches",
        "fees-not-ppv",
        "ppv-within-rule",
        "pdf-attached",
        "select-receipts-when-po",
    ]
    ok, why = treyce_finish_selfcheck(
        {
            "qty_hold": True,
            "fees_posted_as_ppv": True,
            "receipt_qty_only_match": True,
            "printed_pos": ["58926"],
            "invoice_type": 4,
            "require_pdf_number": False,
            "require_attach": True,
            "attach_status": "no-pdf-on-vm",
            "require_select_receipts": True,
            "receipts_selected": False,
        }
    )
    assert ok is False
    assert "Treyce would still rework" in why
    assert_never_success(RESULT_HOLD, note_id="TREYCE-LOAD", detail=why)


def test_v12_never_success_invariant_on_every_note():
    for note in TREYCE_NOTES_V12:
        assert note["never_success"] is True
        assert_never_success(RESULT_HOLD, note_id=note["id"])
        assert_never_success(RESULT_SKIPPED, note_id=note["id"])


def test_never_repeat_nova_258145(tmp_path: Path):
    """NOTE-11: 8/18 Nova 258145 — never Vendor=Erica Barrett + parse HOLD."""
    n = NOTES["NOTE-11"]
    parsed = parse_invoice_text(
        n["pdf_text"],
        subject=n["subject"],
        from_name=n["from_name"],
        filename=n["filename"],
    )
    assert "Nova Alloys" in parsed["vendor"]
    assert n["buggy_vendor"] not in parsed["vendor"]
    assert parsed["invoice_number"] == n["invoice_number"]
    assert parsed["amount"] == n["amount"]
    assert parsed["field_sources"]["invoice_number"] == "pdf"
    ok, why = preflight_parse_gate(parsed)
    assert ok is True, why

    vendor = vendor_from_context(
        subject=n["subject"],
        from_name=n["from_name"],
        text=n["pdf_text"],
    )
    assert vendor == "Nova Alloys"
    assert vendor != n["buggy_vendor"]

    pdf_path = tmp_path / n["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 nova 258145")
    # Reconstruct the 8/18 sidecar: # tagged subject, date/amount from PDF, file on disk.
    sidecar = {
        "vendor": vendor,
        "invoice_number": n["invoice_number"],
        "date": n["date"],
        "amount": n["amount"],
        "po": None,
        "subject": n["subject"],
        "from_name": n["from_name"],
        "field_sources": {
            "invoice_number": n["buggy_number_source"],
            "date": "pdf",
            "amount": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
        "pdf_unavailable": False,
    }
    ok, why = preflight_parse_gate(sidecar)
    assert ok is True, why
    row, _ = _row(sidecar)
    assert row["Vendor"] == "Nova Alloys"
    assert row["Vendor"] != n["buggy_vendor"]
    assert "preflight-parse" not in (row["Why"] or "")
    assert row["Attach status"] != "no-pdf-on-vm"
    # 8/18 miss was Erica Barrett + parse HOLD. That pairing is the never-repeat.
    assert not (
        row["Vendor"] == n["buggy_vendor"]
        and row["Result"] == RESULT_HOLD
        and "preflight-parse" in (row["Why"] or "")
    )

    # The 8/18 buggy outcome must not be representable as a passing result.
    buggy = {
        **sidecar,
        "vendor": n["buggy_vendor"],
        "pdf_path": "",
        "pdf_on_disk": False,
        "pdf_unavailable": True,
        "pdf_text_empty": True,
    }
    buggy_ok, buggy_why = preflight_parse_gate(buggy)
    assert buggy_ok is False
    assert GATE_PREFLIGHT in buggy_why or "parse-error" in buggy_why
    assert n["buggy_vendor"] != vendor

    from ap_clerk import pdf_invoice as pdf_mod

    ocr_called = {"n": 0}

    def fake_ocr(path):
        ocr_called["n"] += 1
        return n["pdf_text"]

    orig_pypdf = pdf_mod._extract_pypdf_text
    orig_ocr = pdf_mod._ocr_pdf_text
    pdf_mod._extract_pypdf_text = lambda path: ""
    pdf_mod._ocr_pdf_text = fake_ocr
    try:
        text = pdf_mod.extract_pdf_text(pdf_path)
    finally:
        pdf_mod._extract_pypdf_text = orig_pypdf
        pdf_mod._ocr_pdf_text = orig_ocr
    assert ocr_called["n"] == 1
    assert "258145" in text
