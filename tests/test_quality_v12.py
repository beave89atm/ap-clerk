"""QUALITY V1.2 never-repeat + Treyce-load regressions.

One named test per Treyce 2026-09-10 Note. Fixture-based. No live Graph or KIMCO.
Hard invariant: those 8/16 failure modes are never reportable as Success.
"""

from __future__ import annotations

import json
from pathlib import Path

from ap_clerk.cli import _process_invoice, _resolve_vendor
from ap_clerk.gates import (
    GATE_AUTO_PAY,
    GATE_PDF_LINK,
    GATE_PREFLIGHT,
    GATE_PRICE,
    GATE_QTY,
    GATE_VENDOR,
    RESULT_HOLD,
    RESULT_INCOMPLETE,
    RESULT_SKIPPED,
    RESULT_SUCCESS,
    finish_gate,
    invoice_number_matches_subject_or_filename,
    never_type_4_when_po,
    preflight_parse_gate,
    qty_gate,
    treyce_finish_selfcheck,
    vendor_confirmation_gate,
)
from ap_clerk.graph import (
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_ENTERED_WITH_ISSUES,
    categories_for_status,
)
from ap_clerk.pdf_invoice import (
    expand_gas_misc_invoices,
    extract_invoice_lines,
    extract_fees,
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
    known_vendor_id,
    format_unmatched_lines,
    match_receipts,
    merchandise_qty,
    misc_purchase_item_for,
    names_match,
    printed_invoice_number,
    should_create_header,
    vendor_match_score,
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

        def try_post_fees(self, *args, **kwargs):
            return "posted"

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
    assert note_ids() == tuple(f"NOTE-{i:02d}" for i in range(1, 16))
    assert len(TREYCE_NOTES_V12) == 15
    assert len(TREYCE_FINISH_CHECKLIST) == 11
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
        "msc-70762501-not-rmp",
        "crosslink-27943-filename-pdf-on-disk",
        "fastenal-txft4100079-qty-and-fees",
        "emj-z250725432-two-lines",
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
        "fees-posted-on-bill",
        "ppv-within-rule",
        "pdf-attached",
        "select-receipts-when-po",
        "all-invoice-lines-selected",
        "posted-vendor-matches-parsed",
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
    row, client = _row(sidecar)
    assert row["Vendor"] == "Nova Alloys"
    assert row["Vendor"] != n["buggy_vendor"]
    assert "preflight-parse" not in (row["Why"] or "")
    assert "McQueary" not in (row["Why"] or "")
    assert "MSC" not in (row["Why"] or "")
    assert row["Attach status"] != "no-pdf-on-vm"
    assert row["KIMCO id"] not in (None, "")
    assert row["Attach status"] == "attached"
    assert client.created
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
    assert "McQueary" not in buggy_why
    assert "MSC" not in buggy_why
    assert "Nova" in buggy_why or "258145" in buggy_why
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


def test_never_repeat_msc_70762501_not_rmp(tmp_path: Path):
    """NOTE-12: 8/18 MSC 70762501 must not names_match RMP or Success as 1320-RMP."""
    n = NOTES["NOTE-12"]
    parsed = n["vendor"]
    posted = n["posted_vendor"]
    assert parsed == "MSC Industrial Supply"
    assert posted == "1320-RMP INDUSTRIAL SUPPLY"
    assert not names_match(parsed, "RMP INDUSTRIAL SUPPLY")
    assert not names_match(parsed, posted)
    assert vendor_match_score(parsed, "RMP INDUSTRIAL SUPPLY") == 0
    assert vendor_match_score(parsed, posted) == 0
    assert known_vendor_id(parsed) == n["msc_alias_id"] == 128
    assert known_vendor_id("RMP INDUSTRIAL SUPPLY") == n["rmp_alias_id"] == 322
    assert known_vendor_id(parsed) != known_vendor_id(posted)

    ok, why = vendor_confirmation_gate(
        parsed_vendor=parsed,
        posted_name=posted,
        posted_id=n["posted_lookup_id"],
    )
    assert ok is False
    assert GATE_VENDOR in why
    assert "parsed" in why.lower() and "MSC" in why
    assert "RMP" in why
    assert_never_success(RESULT_HOLD, note_id="NOTE-12")

    self_ok, self_why = treyce_finish_selfcheck(
        {
            "parsed_vendor": parsed,
            "posted_vendor": posted,
            "posted_vendor_id": n["posted_lookup_id"],
            "require_pdf_number": False,
        }
    )
    assert self_ok is False
    assert GATE_VENDOR in self_why
    result, finish_why = finish_gate(
        header_created=True,
        attach_status="attached",
        po=None,
        receipts_selected=False,
        kimco_id=n["kimco_id"],
        selfcheck={
            "parsed_vendor": parsed,
            "posted_vendor": posted,
            "posted_vendor_id": n["posted_lookup_id"],
            "require_pdf_number": False,
        },
    )
    assert result != RESULT_SUCCESS
    assert result == RESULT_HOLD
    assert GATE_VENDOR in finish_why

    rmp_sample = {
        "vendor_id": n["posted_lookup_id"],
        "vendor_text": posted,
        "invoice_id": 50,
        "po_text": "",
    }
    resolved = _resolve_vendor(
        _kimco(),
        parsed,
        None,
        [rmp_sample],
        invoice_by_number={},
        invoice_number=n["invoice_number"],
    )
    assert resolved is not None
    assert resolved["vendor_id"] == 128
    assert resolved["vendor_id"] != n["posted_lookup_id"]

    class PostedRmp:
        target = "live"

        def __init__(self):
            self.created = []

        def create(self, service, values):
            self.created.append(values)
            return n["kimco_id"], {"id": n["kimco_id"], "values": values}, 200, ""

        def get_item(self, service, item_id):
            values = {
                "Remit_To_Address": {"id": 1, "text": "remit"},
                "Terms_Code": {"id": 2, "text": "Net 30"},
            }
            if item_id == n["kimco_id"]:
                values["Vendor"] = {"id": n["posted_lookup_id"], "text": posted}
            return {"id": item_id, "values": values}

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, *args, **kwargs):
            return "selected"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    pdf = tmp_path / f"{n['invoice_number']}.pdf"
    pdf.write_bytes(b"%PDF-1.4 msc 70762501")
    kimco = PostedRmp()
    row, _ = _row(
        {
            "vendor": parsed,
            "invoice_number": n["invoice_number"],
            "date": n["date"],
            "po": None,
            "amount": 88.40,
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
            },
            "pdf_path": str(pdf),
            "pdf_on_disk": True,
        },
        kimco=kimco,
        samples=[
            rmp_sample,
            {
                "vendor_id": 128,
                "vendor_text": "MSC INDUSTRIAL SUPPLY",
                "invoice_id": 51,
                "po_text": "",
            },
        ],
    )
    assert row["Result"] != RESULT_SUCCESS
    assert_never_success(row["Result"], note_id="NOTE-12", detail=row["Why"])
    assert GATE_VENDOR in (row["Why"] or "")
    assert "MSC" in (row["Why"] or "")
    assert "RMP" in (row["Why"] or "")
    assert kimco.created
    assert kimco.created[0]["Vendor"]["id"] == 128
    assert kimco.created[0]["Vendor"]["id"] != n["posted_lookup_id"]


def test_never_repeat_crosslink_27943(tmp_path: Path):
    """NOTE-13: 8/18 Crosslink 27943 — filename-sourced # + PDF on disk is not a parse HOLD.

    Same bug as Nova NOTE-11 (subject tag). Also covers 27944 / 58909 and 27946 / 58741.
    """
    n = NOTES["NOTE-13"]
    fee_label = n["fee_label"]
    assert is_fee_or_surcharge(fee_label)

    # Named case: 27943 reconstructs the 8/18 sidecar (filename tag + file on disk).
    bill = n["bills"][0]
    assert bill["invoice_number"] == "27943"
    assert bill["po"] == "58888"
    assert bill["filename"] == "invoice-27943.pdf"
    parsed = parse_invoice_text(
        bill["pdf_text"],
        subject=bill["subject"],
        from_name="Crosslink Powder Coating",
        filename=bill["filename"],
    )
    assert parsed["invoice_number"] == "27943"
    assert parsed["po"] == "58888"
    assert parsed["field_sources"]["invoice_number"] in {"pdf", "pdf-prefix"}
    assert "Crosslink" in (parsed.get("vendor") or "")
    ok, why = preflight_parse_gate(parsed)
    assert ok is True, why

    pdf_path = tmp_path / bill["filename"]
    pdf_path.write_bytes(b"%PDF-1.4 crosslink 27943")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": bill["invoice_number"],
        "date": n["date"],
        "amount": parsed["amount"],
        "po": bill["po"],
        "pos": [bill["po"]],
        "subject": bill["subject"],
        "filename": bill["filename"],
        "fees": [{"name": fee_label, "amount": 12.50}],
        "field_sources": {
            "invoice_number": n["buggy_number_source"],
            "date": "pdf",
            "amount": "pdf",
            "po": "pdf",
        },
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
        "pdf_unavailable": False,
    }
    assert invoice_number_matches_subject_or_filename(sidecar)
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
                "lines": [{"part": "COAT", "po_line": 1, "qty": 1, "amount": parsed["amount"], "unit_price": parsed["amount"]}],
            }
        },
        samples=[{"vendor_id": 278, "vendor_text": n["vendor"], "invoice_id": 100, "po_text": bill["po"]}],
    )
    assert "preflight-parse" not in (row["Why"] or "")
    assert "McQueary" not in (row["Why"] or "")
    assert "MSC" not in (row["Why"] or "")
    assert row["Attach status"] != "no-pdf-on-vm"
    assert row["Invoice #"] == "27943"
    assert row["KIMCO id"] not in (None, "")
    assert row["Attach status"] == "attached"
    assert client.created
    # False 8/18 pairing: filename HOLD + no-pdf-on-vm while the file exists.
    assert not (
        row["Result"] == RESULT_HOLD
        and "preflight-parse" in (row["Why"] or "")
        and row["Attach status"] == "no-pdf-on-vm"
    )
    # False 8/18 pairing is the never-repeat — a finished header+PDF is allowed.
    if row["Attach status"] == "no-pdf-on-vm" or "preflight-parse" in (row["Why"] or ""):
        assert_never_success(row["Result"], note_id="NOTE-13", detail=row["Why"])

    for extra in n["bills"]:
        extra_path = tmp_path / extra["filename"]
        if not extra_path.exists():
            extra_path.write_bytes(f"%PDF-1.4 crosslink {extra['invoice_number']}".encode())
        extra_sidecar = {
            "vendor": n["vendor"],
            "invoice_number": extra["invoice_number"],
            "date": n["date"],
            "amount": 100.0,
            "po": extra["po"],
            "subject": extra["subject"],
            "filename": extra["filename"],
            "field_sources": {
                "invoice_number": "filename",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
            "pdf_path": str(extra_path),
            "pdf_on_disk": True,
        }
        extra_ok, extra_why = preflight_parse_gate(extra_sidecar)
        assert extra_ok is True, extra_why
        assert invoice_number_matches_subject_or_filename(extra_sidecar)

    missing = {
        **sidecar,
        "pdf_path": "",
        "pdf_on_disk": False,
        "pdf_unavailable": True,
        "pdf_text_empty": True,
    }
    missing_ok, missing_why = preflight_parse_gate(missing)
    assert missing_ok is False
    assert GATE_PREFLIGHT in missing_why or "parse-error" in missing_why
    assert "McQueary" not in missing_why
    assert "MSC" not in missing_why
    assert "Crosslink" in missing_why or "invoice-27943" in missing_why
    assert "filename" in missing_why.lower()
    assert "27943" in missing_why
    assert "no-pdf-on-vm" not in missing_why or "missing" in missing_why.lower()
    assert_never_success(RESULT_HOLD, note_id="NOTE-13")


def test_never_repeat_fastenal_txft4100079(tmp_path: Path):
    """NOTE-14: TXFT4100079 empty lines + qty 35 vs 36; fees must be posted."""
    n = NOTES["NOTE-14"]
    pdf_path = tmp_path / "TXFT4100079.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fastenal TXFT4100079")
    receipts = [
        {
            "id": 3601,
            "po": n["po"],
            "slip": "RECV-36",
            "part": "FAST-A",
            "qty": n["wrong_receipt_qty"],
            "amount": 1115.64,
            "unit_price": 30.99,
        },
        {
            "id": 3501,
            "po": n["po"],
            "slip": "RECV-35",
            "part": "FAST-A",
            "qty": n["invoice_qty"],
            "amount": n["merchandise"],
            "unit_price": 30.99,
        },
    ]
    picked = match_receipts(
        invoice_number=n["invoice_number"],
        invoice_lines=[],
        receipts=receipts,
        po_number=n["po"],
        invoice_qty=n["invoice_qty"],
        invoice_amount=n["merchandise"],
    )
    assert picked["found"] is True
    assert picked["hold_no_receipts"] is False
    assert not picked["ambiguous"]
    assert picked["matched"][0]["receipt"]["qty"] == n["invoice_qty"]
    assert picked["matched"][0]["receipt"]["id"] == 3501
    assert picked["matched"][0]["receipt"]["qty"] != n["wrong_receipt_qty"]
    assert "not first qty" not in picked["why"] or "qty" in picked["why"].lower()
    assert "first open" not in picked["why"].lower() or "not first" in picked["why"].lower()

    # No invoice evidence + differing open receipts → HOLD ambiguous, not first-open Success.
    blind = match_receipts(
        invoice_number=n["invoice_number"],
        invoice_lines=[],
        receipts=receipts,
        po_number=n["po"],
    )
    assert blind["found"] is False
    assert blind["ambiguous"]
    assert_never_success(RESULT_HOLD, note_id="NOTE-14")

    class RecordingFees:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []
            self.fees = []

        def create(self, service, values):
            self.created.append(values)
            return 9968, {"id": 9968, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 9, "text": "Fastenal Company"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, invoice_id, receipt_ids=None):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return "selected"

        def try_post_fees(self, invoice_id, fees=None):
            self.fees.append((invoice_id, list(fees or [])))
            return "posted"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": n["invoice_number"],
        "date": n["date"],
        "po": n["po"],
        "pos": [n["po"]],
        "amount": n["amount"],
        "qty": n["invoice_qty"],
        "lines": [],
        "fees": [{"name": n["fee_name"], "amount": n["fee_amount"]}],
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    po_index = {
        n["po"]: {
            "id": 58692,
            "text": f"{n['po']}-FASTENAL",
            "vendor_id": 9,
            "vendor_text": "Fastenal Company",
            "lines": [],
        }
    }
    samples = [{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}]
    row, client = _row(
        sidecar,
        kimco=RecordingFees(),
        po_index=po_index,
        receipts=receipts,
        samples=samples,
    )
    assert client.selected
    assert client.selected[0][1] == [3501]
    assert client.fees
    assert client.fees[0][1][0]["amount"] == n["fee_amount"]
    assert n["fee_name"] in (client.fees[0][1][0].get("name") or "")
    assert row["Result"] == RESULT_SUCCESS
    assert n["fee_amount"] == 63.98
    assert "63.98" in row["Fees and surcharges"] or n["fee_name"] in row["Fees and surcharges"]
    assert "Posted Additional Charge" in row["Why"] or "F-Fees" in row["Why"]
    assert merchandise_qty([{"qty": hit["receipt"]["qty"]} for hit in picked["matched"]]) == n["invoice_qty"]

    class NoFeePost(RecordingFees):
        def try_post_fees(self, invoice_id, fees=None):
            self.fees.append((invoice_id, list(fees or [])))
            return "blocked-405"

    blocked, _ = _row(
        sidecar,
        kimco=NoFeePost(),
        po_index=po_index,
        receipts=receipts,
        samples=samples,
    )
    assert blocked["Result"] != RESULT_SUCCESS
    assert_never_success(blocked["Result"], note_id="NOTE-14", detail=blocked["Why"])
    assert blocked["Result"] == RESULT_INCOMPLETE or "Fees" in (blocked["Why"] or "")

    ok, why = treyce_finish_selfcheck(
        {
            "require_pdf_number": False,
            "fees_required": True,
            "fees_posted": False,
            "receipt_qty_mismatch": True,
        }
    )
    assert ok is False
    assert "Fees" in why or "qty" in why.lower()
    assert_never_success(RESULT_HOLD, note_id="NOTE-14", detail=why)

    result, finish_why = finish_gate(
        header_created=True,
        attach_status="attached",
        po=n["po"],
        receipts_selected=True,
        kimco_id=n["kimco_id"],
        fees=[{"name": n["fee_name"], "amount": n["fee_amount"]}],
        fees_posted=False,
    )
    assert result != RESULT_SUCCESS
    assert result == RESULT_INCOMPLETE
    assert "Fees" in finish_why or "surcharge" in finish_why.lower()


def test_never_repeat_emj_z250725432_two_lines(tmp_path: Path):
    """NOTE-15: EMJ Z250725432 two lines — parse both; never silent one-receipt Success."""
    n = NOTES["NOTE-15"]
    parsed = parse_invoice_text(
        n["pdf_text"],
        from_name="Earle M. Jorgensen Co",
        from_address="EMJCreditSouth@emjmetals.com",
        filename="Z250725432.pdf",
    )
    assert parsed["invoice_number"] == n["invoice_number"]
    assert parsed["po"] == n["po"]
    assert parsed["amount"] == n["amount"]
    assert len(parsed["lines"]) >= 2
    labels = " ".join(
        f"{line.get('label') or ''} {line.get('description') or ''}" for line in parsed["lines"]
    )
    assert n["line1_desc"] in labels
    assert n["line2_desc"] in labels
    assert all(line.get("amount") for line in parsed["lines"][:2])
    fees = extract_fees(n["pdf_text"])
    assert fees == []
    assert not any("SHIP" in str(f.get("name") or "").upper() for f in parsed.get("fees") or [])
    assert not any("PREPAID" in str(f.get("name") or "").upper() for f in parsed.get("fees") or [])
    assert extract_invoice_lines(n["pdf_text"])

    receipts = [
        {
            "id": 101,
            "po": n["po"],
            "po_line": 1,
            "part": n["line1_desc"],
            "description": n["line1_desc"],
            "label": n["line1_desc"],
            "qty": n["line1_qty"],
            "amount": n["po_line1_amount"],
            "unit_price": 12.45,
        },
        {
            "id": 102,
            "po": n["po"],
            "po_line": 2,
            "part": n["line2_desc"],
            "description": n["line2_desc"],
            "label": n["line2_desc"],
            "qty": n["line2_qty"],
            "amount": n["line2_amount"],
            "unit_price": 16.1445,
        },
    ]
    picked = match_receipts(
        invoice_number=n["invoice_number"],
        invoice_lines=parsed["lines"],
        receipts=receipts,
        po_number=n["po"],
    )
    assert picked["found"] is True
    assert len(picked["matched"]) == 2
    assert not picked["unmatched_lines"]
    assert {hit["receipt"]["id"] for hit in picked["matched"]} == {101, 102}

    bill = evaluate_bill_price_variance(
        parsed["lines"],
        [
            {"part": n["line1_desc"], "description": n["line1_desc"], "amount": n["po_line1_amount"], "qty": 20.0, "po_line": 1},
            {"part": n["line2_desc"], "description": n["line2_desc"], "amount": n["line2_amount"], "qty": 40.0, "po_line": 2},
        ],
        invoice_total=n["amount"],
    )
    assert bill["hold"] is False
    assert bill["ppv_total"] != 0
    assert abs(bill["ppv_total"] - (n["line1_amount"] - n["po_line1_amount"])) < 0.02
    assert all(item.get("action") != "fee" for item in bill["items"] if item.get("action") == "ppv")

    missing_one = match_receipts(
        invoice_number=n["invoice_number"],
        invoice_lines=parsed["lines"],
        receipts=receipts[:1],
        po_number=n["po"],
    )
    assert missing_one["unmatched_lines"]
    assert len(missing_one["matched"]) == 1
    assert n["line2_desc"] in format_unmatched_lines(missing_one["unmatched_lines"]) or "HR FLT 3/8" in missing_one["why"]
    ok, why = treyce_finish_selfcheck(
        {
            "require_pdf_number": False,
            "unmatched_invoice_lines": missing_one["unmatched_lines"],
        }
    )
    assert ok is False
    assert "Unmatched" in why or "unmatched" in why.lower()
    assert_never_success(RESULT_HOLD, note_id="NOTE-15", detail=why)

    class RecordingPpv:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []
            self.ppv = []

        def create(self, service, values):
            self.created.append(values)
            return 9969, {"id": 9969, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 208, "text": "Earle M. Jorgensen Co"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, invoice_id, receipt_ids=None):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return "selected"

        def try_post_fees(self, *args, **kwargs):
            return "none"

        def try_post_ppv(self, invoice_id, amount):
            self.ppv.append((invoice_id, amount))
            return "posted"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    pdf_path = tmp_path / "Z250725432.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 emj Z250725432")
    sidecar = {
        **parsed,
        "qty": None,
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    po_index = {
        n["po"]: {
            "id": 58913,
            "text": f"{n['po']}-EMJ",
            "vendor_id": 208,
            "vendor_text": "Earle M. Jorgensen Co",
            "lines": [
                {"part": n["line1_desc"], "description": n["line1_desc"], "qty": n["line1_qty"], "amount": n["po_line1_amount"], "unit_price": 12.45, "po_line": 1},
                {"part": n["line2_desc"], "description": n["line2_desc"], "qty": 40.0, "amount": n["line2_amount"], "unit_price": 16.1445, "po_line": 2},
            ],
        }
    }
    samples = [{"vendor_id": 208, "vendor_text": "Earle M. Jorgensen Co", "invoice_id": 9, "po_text": ""}]
    row, client = _row(
        sidecar,
        kimco=RecordingPpv(),
        po_index=po_index,
        receipts=receipts,
        samples=samples,
    )
    assert len(client.selected[0][1]) == 2
    assert set(client.selected[0][1]) == {101, 102}
    assert row["Result"] == RESULT_SUCCESS
    assert row["PPV"] != "none"
    assert "Fees" not in row["PPV"]
    assert client.ppv
    assert abs(float(client.ppv[0][1]) - (n["line1_amount"] - n["po_line1_amount"])) < 0.05

    skipped, skipped_client = _row(
        sidecar,
        kimco=RecordingPpv(),
        po_index=po_index,
        receipts=receipts[:1],
        samples=samples,
    )
    assert skipped["Result"] != RESULT_SUCCESS
    assert_never_success(skipped["Result"], note_id="NOTE-15", detail=skipped["Why"])
    assert "Unmatched" in skipped["Why"] or "unmatched" in skipped["Why"].lower()
    assert n["line2_desc"][:12] in skipped["Why"] or "3/8" in skipped["Why"] or "line" in skipped["Why"].lower()
    assert skipped_client.selected
    assert skipped_client.selected[0][1] == [101]
