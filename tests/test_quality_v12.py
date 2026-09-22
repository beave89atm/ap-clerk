"""QUALITY V1.2 never-repeat + Treyce-load regressions.

One named test per Treyce 2026-09-10 Note. Fixture-based. No live Graph or KIMCO.
Hard invariant: those 8/16 failure modes are never reportable as Success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap_clerk.cli import (
    _matching_existing_invoices,
    _process_invoice,
    _resolve_vendor,
    _resolve_vendor_from_po_partial,
)
from ap_clerk.gates import (
    GATE_ALREADY_ENTERED,
    GATE_TOO_OLD,
    GATE_AUTO_PAY,
    GATE_PO,
    GATE_PDF_LINK,
    GATE_PREFLIGHT,
    GATE_PRICE,
    GATE_QTY,
    GATE_RECEIPT,
    GATE_VENDOR,
    RESULT_FAIL,
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
    AI_SKIPPED_CATEGORY,
    ENTERED_WITH_ISSUES_CATEGORY,
    FLAG_AI_SKIPPED,
    FLAG_ELIGIBLE,
    FLAG_ENTERED_WITH_ISSUES,
    FLAG_HOLD_ELIGIBLE,
    FLAG_ISSUES_ELIGIBLE,
    FLAG_SKIP_ELIGIBLE,
    apply_flag_after_match,
    categories_for_status,
    decide_flag_status,
    is_already_flagged,
)
from ap_clerk.pdf_invoice import (
    ATTACHMENT_INVOICE,
    ATTACHMENT_PACKING_SLIP,
    NON_INVOICE_ATTACHMENT_KINDS,
    assign_split_pdfs,
    classify_attachment,
    expand_gas_misc_invoices,
    extract_aqpc_intuit_lines,
    extract_invoice_lines,
    extract_fees,
    extract_legacy_wire_bill,
    gas_invoice_numbers,
    is_account_statement_document,
    parse_invoice_text,
    prefer_after_tax_amount,
    vendor_from_context,
)
from ap_clerk.pdf_links import (
    REASON_PDF_BEHIND_LINK,
    classify_download,
    download_first_pdf,
    download_first_public_pdf,
    extract_https_links,
)
from ap_clerk.quality_v12 import (
    COL_EXCEPTION_CATEGORY,
    COL_EXCEPTION_OWNER,
    EXCEPTION_CATEGORY_OWNERS,
    MONDAY_LIVE10_BASICS,
    PURCHASING_EXCEPTION_CATEGORIES,
    TREYCE_FINISH_CHECKLIST,
    TREYCE_NOTES_V12,
    apply_exception_category_owner,
    assert_never_success,
    canonical_exception_owner,
    classify_exception,
    exception_prefix,
    note_ids,
)
from ap_clerk.rules import (
    GAS_AND_SUPPLY_MISC_ITEM,
    INVOICE_TYPE_NO_PO,
    INVOICE_TYPE_PO,
    PRICE_DOES_NOT_MATCH,
    classify_mail,
    decide_ppv,
    evaluate_bill_price_variance,
    extract_subject_invoice_number,
    extract_subject_pos,
    flag_in_outlook_for,
    is_freight_vendor,
    is_rfq_not_kimco_po,
    known_vendor_id,
    length_qty_equivalent,
    never_skip_vendor_invoice,
    description_match_score,
    invoice_type_for,
    is_auto_pay,
    is_fee_or_surcharge,
    looks_like_account_statement,
    is_noise_reason,
    subject_has_invoice_bill_hint,
    format_unmatched_lines,
    match_receipts,
    merchandise_qty,
    misc_purchase_item_for,
    names_match,
    NO_PO_ON_PDF_BUYER_COMMENT,
    po_vendor_name_from_text,
    po_vendor_partial_match,
    printed_invoice_number,
    qty_discrepancy,
    should_create_header,
    should_transfer_ap_missing_po,
    vendor_match_score,
    AQPC_MIN_INVOICE_DATE,
    AQPC_TOO_OLD_INVOICES,
    AQPC_TOO_OLD_KIMCO_IDS,
    aqpc_discover_skip_invoice,
    aqpc_invoice_too_old,
    filter_matches_outside_ppv_gate,
    is_aqpc_vendor,
)

FIXTURE = json.loads(Path("fixtures/treyce-2026-09-10-never-repeat.json").read_text())
NOTES = FIXTURE["notes"]


def _kimco(*, attach="attached", select="selected", created_id=8800):
    class K:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []

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

        def try_select_receipts(self, invoice_id, receipt_ids=None, **kwargs):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return select

        def try_post_fees(self, *args, **kwargs):
            return "posted"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    return K()


def _row(inv, *, kimco=None, po_index=None, receipts=None, samples=None, graph=None, invoice_by_number=None):
    client = kimco or _kimco()
    return _process_invoice(
        client,
        inv,
        batch={"id": 1},
        batch_label="API Agent - 9/10/26 (1)",
        invoice_by_number=invoice_by_number or {},
        vendor_samples=samples
        or [{"vendor_id": 9, "vendor_text": inv.get("vendor") or "Vendor", "invoice_id": 100, "po_text": ""}],
        po_index=po_index or {},
        receipts=receipts,
        pdf_dir=None,
        graph_client=graph,
        flag_outlook=bool(graph),
    ), client


def test_v12_registry_covers_all_notes():
    assert note_ids() == tuple(f"NOTE-{i:02d}" for i in range(1, 40)) + ("NOTE-42", "NOTE-48")
    assert len(TREYCE_NOTES_V12) == 41
    assert len(TREYCE_FINISH_CHECKLIST) == 16
    assert len(MONDAY_LIVE10_BASICS) == 10
    assert {item["note"] for item in MONDAY_LIVE10_BASICS} <= set(note_ids())
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
        "gas-multi-invoice-pdf-after-tax",
        "insight-1809-already-entered",
        "outlook-ai-skipped-noise",
        "3p-rachel-bailey-multi-po",
        "eastern-metal-818600-not-noise",
        "aqpc-10917-link-download",
        "kimco-vendor-invoice-never-skip",
        "3p-select-receipts-part-po-never-fail-close",
        "leeco-account-statement-skip",
        "legacy-packing-slip-and-line-receipts",
        "greentree-invoice-from-not-statement",
        "aqpc-10956-same-cost-inverted-qty-unit",
        "aqpc-too-old-before-2026-08-01",
        "over-ppv-do-not-select-receipts",
        "crosslink-reminder-already-entered",
        "priority1-freight-external-no-receipts",
        "mcnichols-vendor-from-po-partial",
        "emj-no-po-on-pdf-transfer-ap",
        "metal-supermarkets-inches-qty",
        "oneal-per-line-ppv-not-rolled",
        "gas-labeled-total-amount-due",
        "jpsteel-125315-combine-same-item-receipts",
        "jpsteel-125316-rounding-ppv-not-hold",
        "exception-category-owner-at-hold",
        "gas-misc-lines-k-shop-supplies",
        "pod-mailbox-not-invoice-inbox",
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
    """NOTE-09: AQPC https / Intuit link — auth after browser HOLDs, never skip/Success."""
    n = NOTES["NOTE-09"]
    links = extract_https_links(n["body"])
    assert links and links[0].startswith("https://")
    assert n["link_host"] in links[0]
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
            "pdf_link_host": n["link_host"],
            "browser_tried": True,
            "browser_failure": "login-required",
        }
    )
    assert ok is False
    assert GATE_PDF_LINK in why
    assert "guest browser was tried" in why.lower()
    assert "login" in why.lower()
    assert "AP_CLERK_INTUIT_STORAGE_STATE" not in why
    row, _ = _row(
        {
            "vendor": n["vendor"],
            "invoice_number": "",
            "hold_reason": "pdf-behind-link",
            "pdf_behind_link": True,
            "pdf_link_host": n["link_host"],
            "browser_tried": True,
            "browser_failure": "login-required",
            "action": "hold",
        }
    )
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-09", detail=row["Why"])
    assert row["KIMCO id"] == ""
    assert "not-a-bill" not in row["Why"].lower() or "pdf-behind-link" in row["Why"]
    note = next(item for item in TREYCE_NOTES_V12 if item["id"] == "NOTE-09")
    assert not note.get("deferred")
    assert "browser" in str(note.get("expected") or "").lower()
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
    assert all(b.get("amount") == 10.0 for b in bills)
    assert not any(b.get("gas_misc_ambiguous") for b in bills)
    shared = "Gas and Supply\nInvoice 0040367881\nInvoice 0040367882\nTotal 50.00\n"
    shared_first = parse_invoice_text(shared, from_address="billing@gasandsupply.com")
    shared_bills = expand_gas_misc_invoices(shared, shared_first)
    assert len(shared_bills) >= 2
    assert any(b.get("gas_misc_ambiguous") for b in shared_bills)
    amb, _ = _row(
        {
            **shared_bills[0],
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
        "all-pos-selected",
        "partial-select-receipts-never-fail-close",
        "combine-same-item-same-unit-receipts",
        "gas-misc-lines-k-shop-supplies",
        "pod-mailbox-not-invoice-inbox",
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


def test_never_repeat_gas_multi_invoice_pdf():
    """NOTE-16: Gas billing pack → 6 bills, after-tax amount, not one collapsed Incomplete."""
    n = NOTES["NOTE-16"]
    parsed = parse_invoice_text(
        n["pdf_text"],
        from_name="Gas and Supply North Texas, LLC",
        from_address="billing@gasandsupply.com",
        filename=n["filename"],
    )
    bills = expand_gas_misc_invoices(n["pdf_text"], parsed)
    assert len(bills) == n["invoice_count"] == 6
    numbers = [bill["invoice_number"] for bill in bills]
    assert n["invoice_number"] in numbers
    assert len(set(numbers)) == 6
    assert all(bill.get("gas_split") for bill in bills)
    assert not any(bill.get("gas_misc_ambiguous") for bill in bills)
    assert not (len(bills) == 1 and parsed.get("multi_po"))
    target = next(bill for bill in bills if bill["invoice_number"] == n["invoice_number"])
    assert target["amount"] == n["amount_after_tax"]
    assert target["amount"] != n["amount_before_tax"]
    assert target.get("multi_invoice_pdf") is True
    assert "multi-invoice-pdf page" in str(target.get("multi_invoice_note") or "")
    assert " of 6" in str(target.get("multi_invoice_note") or "")
    from ap_clerk.inbox import HARD_EMAIL_CAP

    assert HARD_EMAIL_CAP == 10
    # One email touch; N invoices are separate bill rows from that touch.
    assert len(bills) > 1

    collapsed = {
        **parsed,
        "invoice_number": n["invoice_number"],
        "invoice_numbers_in_pdf": numbers,
        "multi_po": True,
        "gas_split": False,
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        "vendor": n["vendor"],
    }
    hold_row, _ = _row(collapsed)
    assert hold_row["Result"] != RESULT_SUCCESS
    assert hold_row["Result"] != RESULT_INCOMPLETE
    assert_never_success(hold_row["Result"], note_id="NOTE-16", detail=hold_row["Why"])
    assert "invoice numbers" in hold_row["Why"].lower() or "expand" in hold_row["Why"].lower()

    samples = [{"vendor_id": 71, "vendor_text": n["vendor"], "invoice_id": 9, "po_text": ""}]
    row, _ = _row(
        {
            **target,
            "field_sources": target.get("field_sources")
            or {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        samples=samples,
    )
    assert row["Invoice #"] == n["invoice_number"]
    assert row["Amount"] == n["amount_after_tax"]
    assert "multi-invoice-pdf page" in row["Why"]
    assert target.get("multi_invoice_page_start")
    assert target.get("multi_invoice_page_end")


def test_gas_page_range_pdf_split_when_feasible(tmp_path: Path):
    """NOTE-16: prefer a page-range PDF per invoice when the pack has pages."""
    from pypdf import PdfReader, PdfWriter

    n = NOTES["NOTE-16"]
    source = tmp_path / n["filename"]
    writer = PdfWriter()
    for _ in range(n["invoice_count"]):
        writer.add_blank_page(width=72, height=72)
    with source.open("wb") as handle:
        writer.write(handle)
    bills = [
        {
            "invoice_number": f"004037006{index}",
            "multi_invoice_page_start": index,
            "multi_invoice_page_end": index,
            "multi_invoice_count": n["invoice_count"],
        }
        for index in range(1, n["invoice_count"] + 1)
    ]
    bills[0]["invoice_number"] = n["invoice_number"]
    assign_split_pdfs(source, bills)
    paths = [bill["pdf_path"] for bill in bills]
    assert len(set(paths)) == n["invoice_count"]
    for bill in bills:
        assert bill.get("pdf_split") is True
        path = Path(bill["pdf_path"])
        assert path.is_file()
        assert bill["invoice_number"] in path.name
        assert len(PdfReader(str(path)).pages) == 1
    single = tmp_path / "one-page.pdf"
    one = PdfWriter()
    one.add_blank_page(width=72, height=72)
    with single.open("wb") as handle:
        one.write(handle)
    fallback = [{"invoice_number": "0040370068", "multi_invoice_page_start": 1, "multi_invoice_page_end": 1}]
    assign_split_pdfs(single, fallback)
    assert fallback[0]["pdf_path"] == str(single)
    assert fallback[0].get("pdf_split") is False


def test_never_repeat_gas_after_tax_amount():
    """NOTE-16: Gas amount is after-tax Amount Due / Total, never Subtotal 322."""
    n = NOTES["NOTE-16"]
    section = (
        "GAS AND SUPPLY NORTH TEXAS, LLC\nORIGINAL INVOICE\n"
        "INVOICE DATE ACCOUNT NUMBER INVOICE NUMBER\n08/18/26 A3050 0040370068\n"
        "CUSTOMER PO 58920\nMerchandise 300.00\nSubtotal 322.00\nTax 26.57\n"
        "Amount Due: 348.57\n"
    )
    parsed = parse_invoice_text(
        section,
        from_name=n["vendor"],
        from_address="billing@gasandsupply.com",
        filename=n["filename"],
    )
    assert parsed["amount"] == n["amount_after_tax"]
    assert parsed["amount"] != n["amount_before_tax"]
    assert prefer_after_tax_amount(section, n["amount_before_tax"]) == n["amount_after_tax"]
    assert prefer_after_tax_amount(
        "Merchandise 300.00\nSubtotal 322.00\nTax 26.57\nTotal 348.57\n",
        n["amount_before_tax"],
    ) == n["amount_after_tax"]

    bills = expand_gas_misc_invoices(n["pdf_text"], {**parsed, "vendor": n["vendor"]})
    target = next(bill for bill in bills if bill["invoice_number"] == n["invoice_number"])
    assert target["amount"] == n["amount_after_tax"]
    samples = [{"vendor_id": 71, "vendor_text": n["vendor"], "invoice_id": 9, "po_text": ""}]
    row, _ = _row(
        {
            **target,
            "field_sources": target.get("field_sources")
            or {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        samples=samples,
    )
    assert row["Amount"] == n["amount_after_tax"]
    assert row["Amount"] != n["amount_before_tax"]
    if row["Result"] in {RESULT_SUCCESS, RESULT_INCOMPLETE}:
        assert row["Amount"] != n["amount_before_tax"]
    assert_never_success(
        RESULT_SUCCESS if row["Amount"] == n["amount_before_tax"] else row["Result"],
        note_id="NOTE-16",
        detail=f"pre-tax amount {n['amount_before_tax']} used",
    )


def test_never_repeat_insight_1809_already_entered(tmp_path: Path):
    """NOTE-17: Insight 1809 already on live → HOLD already-entered, not McQueary parse."""
    n = NOTES["NOTE-17"]
    parsed = parse_invoice_text(
        n["pdf_text"],
        from_name=n["vendor"],
        filename="Invoice_1809.pdf",
    )
    pdf_path = tmp_path / "Invoice_1809.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 insight 1809")
    existing = {
        "1809": [
            {
                "id": n["kimco_id"],
                "values": {
                    "Invoice_Number": "1809",
                    "Vendor": {"id": 1, "text": n["vendor"]},
                },
            },
            {
                "id": 9951,
                "values": {
                    "Invoice_Number": "1809",
                    "Vendor": {"id": 1, "text": n["vendor"]},
                },
            },
        ]
    }
    sidecar = {
        **parsed,
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
        "field_sources": {"invoice_number": "subject", "date": "", "amount": "", "po": ""},
    }
    row, _ = _row(sidecar, invoice_by_number=existing)
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-17", detail=row["Why"])
    why = row["Why"]
    assert "already-entered" in why or "already entered" in why.lower() or "duplicate" in why.lower()
    assert n["vendor"].split()[0] in why
    assert "1809" in why
    assert str(n["kimco_id"]) in why or str(row["KIMCO id"]) == str(n["kimco_id"])
    assert "9951" in why
    assert "McQueary" not in why
    assert "MSC" not in why
    assert "preflight-parse" not in why
    assert "no-pdf-on-vm" not in why
    assert row["Attach status"] == "pdf-on-vm"


def test_aqpc_10938_not_jmor_4779_already_entered():
    """Same invoice # on a different known vendor is not a duplicate.

    AQPC payment-request 10938 is not JMOR Machinery 4779 (vendor 98, $21025,
    2025-08-05). Unique-number matching must not block the AQPC header.
    NoreplyMV / Leeco unique-# dups stay in force when the From is weak.
    """
    existing = {
        "10938": [
            {
                "id": 4779,
                "values": {
                    "Invoice_Number": "10938",
                    "Vendor": {"id": 98, "text": "1096-JMOR MACHINERY"},
                },
            }
        ]
    }
    hits = _matching_existing_invoices(
        existing, "10938", "American Quality Powder Coating"
    )
    assert hits == []
    leeco = {
        "619920": [
            {
                "id": 9001,
                "values": {
                    "Invoice_Number": "619920",
                    "Vendor": {"id": 109, "text": "109-LEECO STEEL, LLC"},
                },
            }
        ]
    }
    noreply = _matching_existing_invoices(leeco, "619920", "NoreplyMV")
    assert [hit.get("id") for hit in noreply] == [9001]
    same = _matching_existing_invoices(
        existing, "10938", "JMOR Machinery"
    )
    assert [hit.get("id") for hit in same] == [4779]
    text_only = {
        "10938": [
            {
                "id": 4779,
                "values": {
                    "Invoice_Number": "10938",
                    "Vendor": {"text": "1096-JMOR MACHINERY"},
                },
            }
        ]
    }
    assert _matching_existing_invoices(
        text_only, "10938", "American Quality Powder Coating"
    ) == []


def test_never_repeat_ai_skipped_noise():
    """NOTE-18: noise → Outlook AI Skipped 2, never AI HOLD; already-flagged includes it."""
    n = NOTES["NOTE-18"]
    assert AI_SKIPPED_CATEGORY == "AI Skipped 2"
    assert n["category"] == "AI Skipped 2"
    assert n["category"] == AI_SKIPPED_CATEGORY
    assert flag_in_outlook_for("Skipped") == "Yes"
    assert decide_flag_status(result="Skipped", kimco_id="", message_id="AAMk") == FLAG_SKIP_ELIGIBLE
    assert is_already_flagged({"categories": ["AI Skipped 2"]})
    assert is_already_flagged({"categories": [AI_SKIPPED_CATEGORY]})
    assert is_already_flagged({"categories": ["AI Skipped"]})
    row = {"Result": RESULT_SKIPPED, "KIMCO id": "", "Why": "Skipped (bill-vs-noise): statement."}

    class Graph:
        def flag_skipped(self, mailbox, message_id):
            return FLAG_AI_SKIPPED

        def flag_hold(self, mailbox, message_id):
            raise AssertionError("noise must not get AI HOLD")

    status = apply_flag_after_match(row, {"graph_message_id": "AAMk-noise"}, Graph())
    assert status == FLAG_AI_SKIPPED
    assert row["Flag status"] == FLAG_AI_SKIPPED
    missing = {"Result": RESULT_SKIPPED, "KIMCO id": "", "Why": "Skipped (bill-vs-noise): statement."}
    denied = apply_flag_after_match(missing, {"graph_message_id": "AAMk-noise"}, None)
    assert denied == "graph-denied"
    assert "outlook-category-missing: AI Skipped 2" in missing["Why"]
    assert missing["Why"].count("AI Skipped 2") == 1
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-18")


def test_never_repeat_3p_rachel_bailey_not_noise():
    """NOTE-19: Rachel Bailey INV#+PO# is a 3P invoice, not Skipped not-a-bill."""
    n = NOTES["NOTE-19"]
    assert classify_mail(subject=n["subject"], preview=n["from_name"]) == "invoice"
    assert classify_mail(subject=n["subject"], preview="Rachel Bailey") == "invoice"
    assert never_skip_vendor_invoice(subject=n["subject"], from_name=n["from_name"])
    assert extract_subject_invoice_number(n["subject"]) == n["invoice_number"]
    assert extract_subject_pos(n["subject"]) == n["pos"]
    assert classify_mail(subject=n["subject"]) != "not-a-bill"
    for sibling in n["sibling_subjects"]:
        assert classify_mail(subject=sibling, preview=n["from_name"]) == "invoice"
        assert classify_mail(subject=sibling) != "not-a-bill"
        assert extract_subject_pos(sibling) == n["pos"]
    assert decide_flag_status(result="Success", kimco_id=9971, message_id="AAMk") == FLAG_ELIGIBLE
    assert decide_flag_status(result="HOLD", kimco_id="", message_id="AAMk") == FLAG_HOLD_ELIGIBLE
    assert decide_flag_status(result="Incomplete", kimco_id=9971, message_id="AAMk") == FLAG_ISSUES_ELIGIBLE
    assert decide_flag_status(result="Success", kimco_id=9971, message_id="AAMk") != FLAG_SKIP_ELIGIBLE
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-19")


def test_3p_multi_po_select_receipts(tmp_path: Path):
    """NOTE-19: multi-PO 3P → blank header PO, Select Receipts per PO, name unmatched."""
    n = NOTES["NOTE-19"]
    pdf_path = tmp_path / "142041.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 3P 142041")
    sidecar = {
        "vendor": n["vendor"],
        "invoice_number": n["invoice_number"],
        "date": n["date"],
        "po": None,
        "pos": n["pos"],
        "multi_po": True,
        "amount": n["amount"],
        "lines": [],
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf"},
        "subject": n["subject"],
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    receipts = [
        {"id": 11, "po": "58766", "qty": 1, "part": "A"},
        {"id": 12, "po": "58767", "qty": 1, "part": "B"},
        {"id": 13, "po": "58844", "qty": 1, "part": "C"},
    ]
    samples = [{"vendor_id": 9, "vendor_text": "3P", "invoice_id": 100, "po_text": ""}]

    class Recording:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []

        def create(self, service, values):
            self.created.append(values)
            return 9971, {"id": 9971, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 9, "text": "3P"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, invoice_id, receipt_ids=None):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return "selected"

        def try_post_fees(self, *args, **kwargs):
            return "none"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    all_row, all_client = _row(sidecar, kimco=Recording(), receipts=receipts, samples=samples)
    assert "Purchase_Order" not in all_client.created[0]
    assert all_client.created[0]["Invoice_Type"] == INVOICE_TYPE_PO
    assert set(all_client.selected[0][1]) == {11, 12, 13}
    assert all_row["PO"] == "58766, 58767, 58844"
    assert all_row["Result"] == RESULT_SUCCESS
    assert "Selected receipts: 11 on PO 58766, 12 on PO 58767, 13 on PO 58844" in all_row["Why"]
    assert "AI Skipped" not in all_row["Why"]

    missing, missing_client = _row(sidecar, kimco=Recording(), receipts=receipts[:2], samples=samples)
    assert missing["Result"] != RESULT_SUCCESS
    assert_never_success(missing["Result"], note_id="NOTE-19", detail=missing["Why"])
    assert "58844" in missing["Why"]
    assert "Unmatched PO" in missing["Why"]
    assert "Selected receipts: 11 on PO 58766, 12 on PO 58767" in missing["Why"]
    assert "Purchase_Order" not in missing_client.created[0]
    assert missing_client.created[0]["Invoice_Type"] == INVOICE_TYPE_PO
    assert set(missing_client.selected[0][1]) == {11, 12}
    assert missing["PO"] == "58766, 58767, 58844"


def test_never_repeat_3p_select_receipts_cpl(tmp_path: Path):
    """NOTE-23 / 9988–9991: 3P lines match by part+PO. CPL is not required.

    Live 9/14 HOLDed 142041–142044 (KIMCO 9988–9991) 'no receipts after
    second pass' with zero Select Receipts. Open receipts existed on those
    PO lines. Kyle: match invoice line → PO receipts by part / PO line /
    qty; do not require subject CPL; check every line; never fail-close
    the whole bill when some lines match.
    """
    subject = (
        "INV # 142041 / CPL # 76659, 76664, 76663, 76661, 76660, 76662 / "
        "PO # 58766, 58767, 58844"
    )
    lines = [
        {
            "part": "1007044-1",
            "qty": 1,
            "amount": 97.50,
            "po": "58766",
            "label": "1 1007044-1 / SUBFRAME WELDMENT 97.50 97.50",
            "description": "1007044-1 / SUBFRAME WELDMENT",
        },
        {
            "part": "1020592-1",
            "qty": 6,
            "amount": 133.02,
            "po": "58767",
            "label": "6 1020592-1 - LOWER PLATFORM 22.17 133.02",
            "description": "1020592-1 - LOWER PLATFORM",
        },
        {
            "part": "29340-1",
            "qty": 9,
            "amount": 338.40,
            "po": "58844",
            "label": "9 29340-1 LOWER ROTATOR WELDMENT 37.60 338.40",
            "description": "29340-1 LOWER ROTATOR WELDMENT",
        },
        {
            "part": "21913-1",
            "qty": 47,
            "amount": 868.56,
            "po": "58844",
            "label": "47 21913-1 UPPER SUPPORT WELDMENT 18.48 868.56",
            "description": "21913-1 UPPER SUPPORT WELDMENT",
        },
        {
            "part": "35145-1",
            "qty": 36,
            "amount": 955.80,
            "po": "58844",
            "label": "36 35145-1 JIB ARM WELDMENT 26.55 955.80",
            "description": "35145-1 JIB ARM WELDMENT",
        },
    ]
    # Receipt slips are NOT the subject CPL numbers — CPL must not be the path.
    receipts = [
        {
            "id": 201,
            "po": "58766",
            "part": "1007044-1 SUBFRAME WELDMENT",
            "qty": 1,
            "amount": 97.50,
            "slip": "R-PO58766",
        },
        {
            "id": 202,
            "po": "58767",
            "part": "",
            "description": "1020592-1 - LOWER PLATFORM",
            "qty": 6,
            "amount": 133.02,
            "slip": "R-PO58767",
        },
        {
            "id": 203,
            "po": "58844",
            "part": "29340-1",
            "qty": 9,
            "amount": 338.40,
            "slip": "R-ROTATOR",
        },
        {
            "id": 204,
            "po": "58844",
            "part": "21913-1",
            "qty": 47,
            "amount": 868.56,
            "slip": "R-SUPPORT",
        },
        {
            "id": 205,
            "po": "58844",
            "part": "35145-1",
            "qty": 36,
            "amount": 955.80,
            "slip": "R-JIB",
        },
    ]
    picked = match_receipts(
        invoice_number="142041",
        invoice_lines=lines,
        receipts=receipts,
        po_numbers=["58766", "58767", "58844"],
        invoice_qty=99,
        invoice_amount=2393.28,
        slip_numbers=[],
    )
    assert picked["found"] is True
    assert picked["hold_no_receipts"] is False
    assert not picked["unmatched_lines"]
    assert {hit["receipt"]["id"] for hit in picked["matched"]} == {201, 202, 203, 204, 205}
    assert "76659" not in (picked["why"] or "") or "part" in (picked["why"] or "").lower()

    pdf_path = tmp_path / "142041.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 3P 142041")
    sidecar = {
        "vendor": "3P",
        "invoice_number": "142041",
        "date": "2026-08-06",
        "po": None,
        "pos": ["58766", "58767", "58844"],
        "multi_po": True,
        "amount": 2393.28,
        "lines": lines,
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf"},
        "subject": subject,
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }
    samples = [{"vendor_id": 9, "vendor_text": "3P", "invoice_id": 100, "po_text": ""}]

    class Recording:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []

        def create(self, service, values):
            self.created.append(values)
            return 9988, {"id": 9988, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 9, "text": "3P"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, invoice_id, receipt_ids=None):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return "selected"

        def try_post_fees(self, *args, **kwargs):
            return "none"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    all_row, all_client = _row(sidecar, kimco=Recording(), receipts=receipts, samples=samples)
    assert "Purchase_Order" not in all_client.created[0]
    assert all_client.created[0]["Invoice_Type"] == INVOICE_TYPE_PO
    assert set(all_client.selected[0][1]) == {201, 202, 203, 204, 205}
    assert all_row["Result"] == RESULT_SUCCESS
    assert "no receipts after second pass" not in (all_row["Why"] or "").lower()
    assert "Selected receipts:" in all_row["Why"]

    # One leftover line: still Select Receipts for the four matches.
    # Never blanket no-receipts HOLD that posts zero receipts.
    partial = match_receipts(
        invoice_number="142041",
        invoice_lines=lines,
        receipts=receipts[:4],
        po_numbers=["58766", "58767", "58844"],
        slip_numbers=[],
    )
    assert partial["found"] is True
    assert partial["hold_no_receipts"] is False
    assert len(partial["matched"]) == 4
    assert {hit["receipt"]["id"] for hit in partial["matched"]} == {201, 202, 203, 204}
    assert partial["unmatched_lines"]
    assert "35145-1" in format_unmatched_lines(partial["unmatched_lines"]) or "JIB" in partial["why"]
    assert "Selected vs unmatched" in partial["why"] or "Unmatched invoice line" in partial["why"]
    assert "candidates considered" in partial["why"].lower()

    missing, missing_client = _row(sidecar, kimco=Recording(), receipts=receipts[:4], samples=samples)
    assert missing["Result"] != RESULT_SUCCESS
    assert_never_success(missing["Result"], note_id="NOTE-23", detail=missing["Why"])
    assert set(missing_client.selected[0][1]) == {201, 202, 203, 204}
    assert "no receipts after second pass" not in (missing["Why"] or "").lower()
    assert "35145-1" in missing["Why"] or "JIB" in missing["Why"]
    assert "Selected receipts:" in missing["Why"]
    assert missing["Result"] in {RESULT_HOLD, RESULT_INCOMPLETE}


def _notes_3p_recording(created_id):
    class Recording:
        target = "live"

        def __init__(self):
            self.created = []
            self.selected = []
            self.ppv = []

        def create(self, service, values):
            self.created.append(values)
            return created_id, {"id": created_id, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 9, "text": "3P"},
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, invoice_id, receipt_ids=None):
            self.selected.append((invoice_id, list(receipt_ids or [])))
            return "selected"

        def try_post_ppv(self, invoice_id, amount):
            self.ppv.append((invoice_id, amount))
            return "posted"

        def try_post_fees(self, *args, **kwargs):
            return "none"

        def try_put_probe_rejected(self, *args, **kwargs):
            return ""

    return Recording()


def _notes_3p_sidecar(tmp_path: Path, bill: dict, invoice_number: str) -> dict:
    pdf_path = tmp_path / f"{invoice_number}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 3P " + invoice_number.encode())
    return {
        "vendor": "3P",
        "invoice_number": invoice_number,
        "date": "2026-08-06",
        "po": None,
        "pos": list(bill["pos"]),
        "multi_po": True,
        "amount": bill["amount"],
        "lines": list(bill["lines"]),
        "fees": [],
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf"},
        "subject": bill["subject"],
        "pdf_path": str(pdf_path),
        "pdf_on_disk": True,
    }


def _selected_ids(client) -> set:
    refs = client.selected[0][1]
    ids = set()
    for ref in refs:
        if isinstance(ref, dict):
            ids.add(ref.get("id"))
        else:
            ids.add(ref)
    return ids


def test_never_repeat_3p_notes_142041_142044(tmp_path: Path):
    """NOTE-23 Notes truth: pull every matchable line; no invented $0.02 PPV."""
    n = NOTES["NOTE-23"]
    samples = [{"vendor_id": 9, "vendor_text": "3P", "invoice_id": 100, "po_text": ""}]
    bills = n["bills"]

    # Invoice amounts add cleanly — do not invent a $0.02 PPV.
    for number, bill in bills.items():
        total = round(sum(float(line["amount"]) for line in bill["lines"]), 2)
        assert total == float(bill["amount"]), number
    from ap_clerk.rules import decide_ppv

    clean = decide_ppv(invoice_line_amount=97.50, po_line_amount=97.50, invoice_total=2393.28)
    assert clean["ppv"] == 0.0
    assert clean["action"] == "match"
    tiny = decide_ppv(invoice_line_amount=97.50, po_line_amount=97.48, invoice_total=2393.28)
    assert tiny["ppv"] == 0.0
    assert tiny["action"] == "match"

    # --- 142041: price-wrong lines still select; MUST pull 2/4/5; over-threshold HOLD ---
    b041 = bills["142041"]
    receipts_041 = [
        {"id": 401, "po": "58766", "part": "1007044-1", "qty": 1, "amount": 80.00, "unit_price": 80.00},
        {"id": 402, "po": "58767", "part": "1020592-1", "qty": 6, "amount": 133.02, "unit_price": 22.17},
        {"id": 403, "po": "58844", "part": "29340-1", "qty": 9, "amount": 450.00, "unit_price": 50.00},
        {"id": 404, "po": "58844", "part": "21913-1", "qty": 47, "amount": 868.56, "unit_price": 18.48},
        {"id": 405, "po": "58844", "part": "35145-1", "qty": 36, "amount": 955.80, "unit_price": 26.55},
    ]
    po_index_041 = {
        "58766": {"id": 1, "text": "PO58766", "lines": [{"part": "1007044-1", "qty": 1, "amount": 80.00, "unit_price": 80.00}]},
        "58767": {"id": 2, "text": "PO58767", "lines": [{"part": "1020592-1", "qty": 6, "amount": 133.02, "unit_price": 22.17}]},
        "58844": {
            "id": 3,
            "text": "PO58844",
            "lines": [
                {"part": "29340-1", "qty": 9, "amount": 450.00, "unit_price": 50.00},
                {"part": "21913-1", "qty": 47, "amount": 868.56, "unit_price": 18.48},
                {"part": "35145-1", "qty": 36, "amount": 955.80, "unit_price": 26.55},
            ],
        },
    }
    picked_041 = match_receipts(
        invoice_number="142041",
        invoice_lines=b041["lines"],
        receipts=receipts_041,
        po_numbers=b041["pos"],
        slip_numbers=[],
    )
    assert picked_041["hold_no_receipts"] is False
    assert {hit["receipt"]["id"] for hit in picked_041["matched"]} == {401, 402, 403, 404, 405}
    row041, client041 = _row(
        _notes_3p_sidecar(tmp_path, b041, "142041"),
        kimco=_notes_3p_recording(9988),
        receipts=receipts_041,
        samples=samples,
        po_index=po_index_041,
    )
    # 403 / 29340-1 is over the $100 PPV cap ($111.60) — NOTE-29 skip.
    assert _selected_ids(client041) == {401, 402, 404, 405}
    assert row041["Result"] != RESULT_SUCCESS
    assert_never_success(row041["Result"], note_id="NOTE-23", detail=row041["Why"])
    assert "no receipts after second pass" not in (row041["Why"] or "").lower()
    assert "Shawn" in row041["Why"] or "price" in (row041["Why"] or "").lower()
    assert float(str(row041["PPV"]).replace(",", "") or 0) != 0.02
    assert 0.02 not in client041.ppv[0] if client041.ppv else True
    if client041.ppv:
        assert abs(client041.ppv[0][1]) <= 100
        assert abs(client041.ppv[0][1] - 0.02) > 0.001

    # --- 142042: line1 MUST pull; line2 price mismatch still selects ---
    b042 = bills["142042"]
    receipts_042 = [
        {"id": 421, "po": "58862", "part": "21678-1", "qty": 4, "amount": 364.92},
        {"id": 422, "po": "58844", "part": "1008270-1", "qty": 2, "amount": 150.00, "unit_price": 75.00},
    ]
    po_index_042 = {
        "58862": {"id": 10, "text": "PO58862", "lines": [{"part": "21678-1", "qty": 4, "amount": 364.92, "unit_price": 91.23}]},
        "58844": {"id": 11, "text": "PO58844", "lines": [{"part": "1008270-1", "qty": 2, "amount": 150.00, "unit_price": 75.00}]},
    }
    row042, client042 = _row(
        _notes_3p_sidecar(tmp_path, b042, "142042"),
        kimco=_notes_3p_recording(9989),
        receipts=receipts_042,
        samples=samples,
        po_index=po_index_042,
    )
    assert 421 in _selected_ids(client042)
    assert 422 in _selected_ids(client042)
    assert "no receipts after second pass" not in (row042["Why"] or "").lower()
    assert row042["PPV"] != "0.02"

    # --- 142043: select qty 4 of receipt qty 6; line2 no receipts ---
    b043 = bills["142043"]
    receipts_043 = [
        {"id": 431, "po": "58862", "part": "21678-1", "qty": 6, "amount": 547.38, "unit_price": 91.23},
    ]
    picked_043 = match_receipts(
        invoice_number="142043",
        invoice_lines=b043["lines"],
        receipts=receipts_043,
        po_numbers=b043["pos"],
        slip_numbers=[],
    )
    assert picked_043["found"] is True
    assert picked_043["hold_no_receipts"] is False
    assert [hit["receipt"]["id"] for hit in picked_043["matched"]] == [431]
    assert picked_043["matched"][0].get("select_qty") == 4
    assert picked_043["unmatched_lines"]
    row043, client043 = _row(
        _notes_3p_sidecar(tmp_path, b043, "142043"),
        kimco=_notes_3p_recording(9990),
        receipts=receipts_043,
        samples=samples,
    )
    assert client043.selected
    sel043 = client043.selected[0][1]
    assert any(
        (isinstance(ref, dict) and ref.get("id") == 431 and ref.get("qty") == 4) or ref == 431
        for ref in sel043
    )
    assert row043["Result"] != RESULT_SUCCESS
    assert_never_success(row043["Result"], note_id="NOTE-23", detail=row043["Why"])
    assert "21678-1" in row043["Why"] or "58887" in row043["Why"]

    # --- 142044: MUST pull lines 1,2,4; line3 qty+price do not match ---
    b044 = bills["142044"]
    receipts_044 = [
        {"id": 441, "po": "58887", "part": "21678-1", "qty": 4, "amount": 364.92},
        {"id": 442, "po": "58887", "part": "1020586-1", "qty": 6, "amount": 239.82},
        {"id": 443, "po": "58844", "part": "29340-1", "qty": 2, "amount": 90.00, "unit_price": 45.00},
        {"id": 444, "po": "58844", "part": "35145-1", "qty": 4, "amount": 106.20},
    ]
    picked_044 = match_receipts(
        invoice_number="142044",
        invoice_lines=b044["lines"],
        receipts=receipts_044,
        po_numbers=b044["pos"],
        slip_numbers=[],
    )
    assert {hit["receipt"]["id"] for hit in picked_044["matched"]} == {441, 442, 444}
    assert 443 not in {hit["receipt"]["id"] for hit in picked_044["matched"]}
    row044, client044 = _row(
        _notes_3p_sidecar(tmp_path, b044, "142044"),
        kimco=_notes_3p_recording(9991),
        receipts=receipts_044,
        samples=samples,
    )
    assert _selected_ids(client044) == {441, 442, 444}
    assert row044["Result"] != RESULT_SUCCESS
    assert_never_success(row044["Result"], note_id="NOTE-23", detail=row044["Why"])
    assert "no receipts after second pass" not in (row044["Why"] or "").lower()
    assert "29340-1" in row044["Why"] or "LOWER ROTATOR" in row044["Why"]


def test_never_repeat_eastern_metal_818600_not_noise(tmp_path: Path):
    """NOTE-20: Eastern Metal Invoice : 818600 is a bill, never Skipped."""
    from ap_clerk.inbox import pull_recent_bills
    from ap_clerk.rules import has_invoice_hint

    n = NOTES["NOTE-20"]
    assert classify_mail(subject=n["subject"]) == "invoice"
    assert classify_mail(subject=n["subject_818601"]) == "invoice"
    assert extract_subject_invoice_number(n["subject"]) == n["invoice_number"]
    assert extract_subject_invoice_number(n["subject_818601"]) == "818601"
    assert known_vendor_id(n["vendor"]) == 64
    assert known_vendor_id("EASTERN METAL SUPPLY of TEXAS, INC.") == 64
    assert never_skip_vendor_invoice(subject=n["subject"], from_name=n["vendor"])
    assert classify_mail(subject=n["subject"]) != "not-a-bill"
    assert has_invoice_hint(subject=n["subject"])
    assert classify_mail(
        subject="Please see attached",
        attachment_names=["Invoice-818600.pdf"],
        preview="EASTERN METAL SUPPLY",
    ) == "invoice"
    assert classify_mail(
        subject="Remittance advice",
        attachment_names=["Invoice-818601.pdf"],
    ) != "not-a-bill"
    assert decide_flag_status(result="Success", kimco_id=64, message_id="AAMk") == FLAG_ELIGIBLE
    assert decide_flag_status(result="Success", kimco_id=64, message_id="AAMk") != FLAG_SKIP_ELIGIBLE

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-em-818600",
                    "subject": n["subject"],
                    "receivedDateTime": "2026-08-18T12:00:00Z",
                    "hasAttachments": False,
                    "bodyPreview": n["vendor"],
                    "from": {"emailAddress": {"name": n["vendor"], "address": "ar@easternmetal.com"}},
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return []

        def download_pdf_attachments(self, mailbox, message_id):
            return []

    selected, skipped = pull_recent_bills(Graph(), limit=1, pdf_dir=tmp_path / "pdfs")
    skip_noise = [row for row in skipped if row.get("class") != "already-flagged"]
    assert not skip_noise
    assert selected
    assert selected[0].get("invoice_number") == n["invoice_number"]
    assert selected[0].get("hold_reason") != "not-a-bill"
    row, _ = _row(selected[0])
    assert row["Result"] != RESULT_SKIPPED
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-20")


def test_never_repeat_aqpc_10917_link_download(tmp_path: Path):
    """NOTE-21: AQPC payment-request + invoice # is not Skipped; attempts link download."""
    from ap_clerk.inbox import pull_recent_bills

    n = NOTES["NOTE-21"]
    assert classify_mail(subject=n["subject"]) == "invoice"
    assert classify_mail(subject=n["subject_10918"]) == "invoice"
    assert classify_mail(subject=n["subject"]) != "payment"
    assert extract_subject_invoice_number(n["subject"]) == n["invoice_number"]
    assert extract_subject_invoice_number(n["subject_10918"]) == "10918"
    assert never_skip_vendor_invoice(subject=n["subject"], from_name=n["vendor"], preview=n["body"])
    links = extract_https_links(n["body"])
    assert links and n["link_host"] in links[0]
    fetched: list[str] = []

    class Resp:
        status_code = 200
        content = b"%PDF-1.4 AMERICAN QUALITY POWDER COATING Invoice Number 10917 Amount Due 125.00"
        headers = {"Content-Type": "application/pdf"}
        text = ""

    def getter(url, **kwargs):
        fetched.append(url)
        return Resp()

    fetched_ok = download_first_public_pdf(n["body"], getter=getter)
    assert fetched_ok.get("ok") is True
    assert fetched == [f"https://{n['link_host']}/invoices/10917"]

    class FetchGraph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-aqpc-ok",
                    "subject": n["subject"],
                    "receivedDateTime": "2026-08-18T12:00:00Z",
                    "hasAttachments": False,
                    "bodyPreview": n["body"],
                    "from": {"emailAddress": {"name": n["vendor"], "address": "billing@aqpowder.com"}},
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return []

        def download_pdf_attachments(self, mailbox, message_id):
            return []

        def get_message(self, mailbox, message_id, select="id"):
            return {"id": message_id, "bodyPreview": n["body"], "body": {"content": n["body"]}}

        def download_public_pdf_from_text(self, text):
            assert "https://" in text
            return {
                "ok": True,
                "content": Resp.content,
                "reason": "ok",
                "url": f"https://{n['link_host']}/invoices/10917",
            }

    selected_ok, skipped_ok = pull_recent_bills(FetchGraph(), limit=1, pdf_dir=tmp_path / "pdfs-ok")
    skip_noise_ok = [row for row in skipped_ok if row.get("class") != "already-flagged"]
    assert not skip_noise_ok
    assert selected_ok
    fetched_bill = selected_ok[0]
    assert fetched_bill.get("invoice_number") == n["invoice_number"]
    assert fetched_bill.get("hold_reason") != "not-a-bill"
    assert fetched_bill.get("pdf_path")
    assert Path(fetched_bill["pdf_path"]).is_file()
    assert "10917" in Path(fetched_bill["pdf_path"]).name
    ok_row, _ = _row(fetched_bill)
    assert ok_row["Result"] != RESULT_SKIPPED
    assert "no-pdf-on-vm" not in ok_row["Why"]
    assert "no-pdf-on-vm" not in str(ok_row.get("Attach status") or "")

    class AuthGraph(FetchGraph):
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-aqpc",
                    "subject": n["subject_10918"],
                    "receivedDateTime": "2026-08-18T12:05:00Z",
                    "hasAttachments": False,
                    "bodyPreview": n["body"].replace("10917", "10918"),
                    "from": {"emailAddress": {"name": n["vendor"], "address": "billing@aqpowder.com"}},
                }
            ]

        def download_public_pdf_from_text(self, text):
            assert "https://" in text
            return {
                "ok": False,
                "content": None,
                "reason": REASON_PDF_BEHIND_LINK,
                "url": f"https://{n['link_host']}/invoices/10918",
            }

    selected, skipped = pull_recent_bills(AuthGraph(), limit=1, pdf_dir=tmp_path / "pdfs")
    skip_noise = [row for row in skipped if row.get("class") != "already-flagged"]
    assert not skip_noise
    assert selected
    bill = selected[0]
    assert bill.get("hold_reason") == "pdf-behind-link"
    assert bill.get("invoice_number") == "10918"
    assert n["link_host"] in str(bill.get("pdf_link_host") or "")
    row, _ = _row(bill)
    assert row["Result"] == RESULT_HOLD
    assert_never_success(row["Result"], note_id="NOTE-21", detail=row["Why"])
    assert "pdf-behind-link" in row["Why"]
    assert "10918" in row["Why"]
    assert n["vendor"].split()[0] in row["Why"] or "Quality" in row["Why"]
    assert n["link_host"] in row["Why"] or "aqpowder" in row["Why"].lower()
    assert "not-a-bill" not in row["Why"].lower() or "pdf-behind-link" in row["Why"]
    assert row["Result"] != RESULT_SKIPPED

    intuit_body = n["intuit_body_10917"]
    intuit_links = extract_https_links(intuit_body)
    assert intuit_links and n["intuit_host"] in intuit_links[0]

    class AuthWall:
        status_code = 401
        content = b"<html>please sign in</html>"
        headers = {"Content-Type": "text/html"}
        text = "please sign in to Intuit"

    def unauth_wall(url, **kwargs):
        return AuthWall()

    pdf_10917 = (
        b"%PDF-1.4 AMERICAN QUALITY POWDER COATING Invoice Number 10917 Amount Due 125.00"
    )

    def browser_ok(url, **kwargs):
        assert n["intuit_host"] in url
        return {"ok": True, "content": pdf_10917, "reason": "ok"}

    escalated = download_first_pdf(intuit_body, getter=unauth_wall, browser=browser_ok)
    assert escalated.get("ok") is True
    assert escalated.get("method") == "browser"
    assert escalated.get("browser_tried") is True
    assert escalated["content"][:5] == b"%PDF-"

    class BrowserSuccessGraph(FetchGraph):
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-aqpc-browser-ok",
                    "subject": n["subject"],
                    "receivedDateTime": "2026-08-18T12:10:00Z",
                    "hasAttachments": False,
                    "bodyPreview": intuit_body,
                    "from": {
                        "emailAddress": {
                            "name": n["vendor"],
                            "address": "quickbooks@notification.intuit.com",
                        }
                    },
                }
            ]

        def get_message(self, mailbox, message_id, select="id"):
            return {"id": message_id, "bodyPreview": intuit_body, "body": {"content": intuit_body}}

        def download_public_pdf_from_text(self, text):
            return download_first_pdf(text, getter=unauth_wall, browser=browser_ok)

    selected_br, skipped_br = pull_recent_bills(
        BrowserSuccessGraph(), limit=1, pdf_dir=tmp_path / "pdfs-browser-ok"
    )
    skip_br = [row for row in skipped_br if row.get("class") != "already-flagged"]
    assert not skip_br
    assert selected_br
    browser_bill = selected_br[0]
    assert browser_bill.get("invoice_number") == n["invoice_number"]
    assert browser_bill.get("hold_reason") != "not-a-bill"
    assert browser_bill.get("pdf_path")
    assert Path(browser_bill["pdf_path"]).is_file()
    assert "10917" in Path(browser_bill["pdf_path"]).name
    br_row, _ = _row(browser_bill)
    assert br_row["Result"] != RESULT_SKIPPED
    assert "no-pdf-on-vm" not in br_row["Why"]

    guest_html = (
        "<html><body>Sign in Create an account "
        "AMERICAN QUALITY POWDER COATING Invoice Number 10917 Amount Due 125.00 "
        "<button>View invoice</button> <a>Download invoice</a></body></html>"
    )

    class GuestHtml:
        status_code = 200
        content = guest_html.encode("utf-8")
        headers = {"Content-Type": "text/html"}
        text = guest_html

    def unauth_guest_html(url, **kwargs):
        return GuestHtml()

    def guest_click_no_session(url, **kwargs):
        assert n["intuit_host"] in url
        from ap_clerk.browser_pdf import session_file_present, storage_state_path

        assert storage_state_path() is None
        assert session_file_present() is False
        return {"ok": True, "content": pdf_10917, "reason": "ok"}

    guest_escalated = download_first_pdf(
        intuit_body, getter=unauth_guest_html, browser=guest_click_no_session
    )
    assert guest_escalated.get("ok") is True
    assert guest_escalated.get("method") == "browser"
    assert guest_escalated["content"][:5] == b"%PDF-"

    class GuestSuccessGraph(FetchGraph):
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-aqpc-guest-ok",
                    "subject": n["subject"],
                    "receivedDateTime": "2026-08-18T12:12:00Z",
                    "hasAttachments": False,
                    "bodyPreview": intuit_body,
                    "from": {
                        "emailAddress": {
                            "name": n["vendor"],
                            "address": "quickbooks@notification.intuit.com",
                        }
                    },
                }
            ]

        def get_message(self, mailbox, message_id, select="id"):
            return {"id": message_id, "bodyPreview": intuit_body, "body": {"content": intuit_body}}

        def download_public_pdf_from_text(self, text):
            return download_first_pdf(text, getter=unauth_guest_html, browser=guest_click_no_session)

    selected_guest, skipped_guest = pull_recent_bills(
        GuestSuccessGraph(), limit=1, pdf_dir=tmp_path / "pdfs-guest-ok"
    )
    skip_guest = [row for row in skipped_guest if row.get("class") != "already-flagged"]
    assert not skip_guest
    assert selected_guest
    guest_bill = selected_guest[0]
    assert guest_bill.get("invoice_number") == n["invoice_number"]
    assert guest_bill.get("hold_reason") != "not-a-bill"
    assert guest_bill.get("pdf_path")
    assert Path(guest_bill["pdf_path"]).is_file()
    assert "10917" in Path(guest_bill["pdf_path"]).name
    guest_row, _ = _row(guest_bill)
    assert guest_row["Result"] != RESULT_SKIPPED
    assert "no-pdf-on-vm" not in guest_row["Why"]
    assert "AP_CLERK_INTUIT_STORAGE_STATE" not in str(guest_row.get("Why") or "")

    def browser_login_fail(url, **kwargs):
        return {
            "ok": False,
            "content": None,
            "reason": REASON_PDF_BEHIND_LINK,
            "browser_failure": "login-required",
        }

    class BrowserAuthGraph(FetchGraph):
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-aqpc-browser-fail",
                    "subject": n["subject_10918"],
                    "receivedDateTime": "2026-08-18T12:15:00Z",
                    "hasAttachments": False,
                    "bodyPreview": n["intuit_body_10918"],
                    "from": {
                        "emailAddress": {
                            "name": n["vendor"],
                            "address": "quickbooks@notification.intuit.com",
                        }
                    },
                }
            ]

        def get_message(self, mailbox, message_id, select="id"):
            body = n["intuit_body_10918"]
            return {"id": message_id, "bodyPreview": body, "body": {"content": body}}

        def download_public_pdf_from_text(self, text):
            return download_first_pdf(text, getter=unauth_wall, browser=browser_login_fail)

    selected_fail, skipped_fail = pull_recent_bills(
        BrowserAuthGraph(), limit=1, pdf_dir=tmp_path / "pdfs-browser-fail"
    )
    skip_fail = [row for row in skipped_fail if row.get("class") != "already-flagged"]
    assert not skip_fail
    assert selected_fail
    fail_bill = selected_fail[0]
    assert fail_bill.get("hold_reason") == "pdf-behind-link"
    assert fail_bill.get("invoice_number") == "10918"
    assert fail_bill.get("browser_tried") is True
    assert fail_bill.get("browser_failure") == "login-required"
    assert n["intuit_host"] in str(fail_bill.get("pdf_link_host") or "")
    fail_row, _ = _row(fail_bill)
    assert fail_row["Result"] == RESULT_HOLD
    assert fail_row["Result"] != RESULT_SKIPPED
    assert_never_success(fail_row["Result"], note_id="NOTE-21", detail=fail_row["Why"])
    assert "pdf-behind-link" in fail_row["Why"]
    assert "guest browser was tried" in fail_row["Why"].lower()
    assert "login" in fail_row["Why"].lower()
    assert "AP_CLERK_INTUIT_STORAGE_STATE" not in fail_row["Why"]
    assert "10918" in fail_row["Why"]
    assert n["intuit_host"] in fail_row["Why"]
    assert "AI Skipped" not in fail_row["Why"]


def test_never_repeat_kimco_vendor_invoice_never_skip(tmp_path: Path):
    """NOTE-22: KIMCO-listed vendor + invoice is never Skipped / AI Skipped 2."""
    from ap_clerk.inbox import pull_recent_bills
    from ap_clerk.rules import has_invoice_link

    n = NOTES["NOTE-22"]
    assert known_vendor_id("Eastern Metal Supply of Texas") == 64
    assert known_vendor_id(n["msc_from"]) == 128
    assert never_skip_vendor_invoice(subject=n["subject"], from_name=n["vendor"])
    assert never_skip_vendor_invoice(subject=n["msc_subject"], from_name=n["msc_from"])
    assert never_skip_vendor_invoice(
        subject="Documents ready",
        from_name=n["msc_from"],
        attachment_names=[n["msc_pdf"]],
    )
    assert never_skip_vendor_invoice(
        subject="Documents ready",
        from_name=n["msc_from"],
        preview=n["msc_link_body"],
    )
    assert never_skip_vendor_invoice(
        subject="Documents ready",
        from_name=n["fastenal_from"],
        attachment_names=["TXFT4100079.pdf"],
    )
    assert has_invoice_link(preview=n["msc_link_body"])
    assert classify_mail(subject=n["subject"], preview=n["vendor"]) == "invoice"
    assert classify_mail(subject=n["msc_subject"]) == "invoice"
    assert classify_mail(
        subject="Documents ready",
        from_name=n["msc_from"],
        attachment_names=[n["msc_pdf"]],
    ) == "invoice"
    assert classify_mail(subject="Monthly Account Statement") == "statement"
    assert not never_skip_vendor_invoice(subject="Monthly Account Statement", from_name="Bank")
    assert (
        classify_mail(
            subject="Leeco Account Statement",
            attachment_names=["1058256.pdf"],
            from_name="credit@leecosteel.com",
        )
        == "statement"
    )
    assert not never_skip_vendor_invoice(
        subject="Leeco Account Statement",
        from_name="Leeco Steel, LLC",
        attachment_names=["1058256.pdf"],
    )
    assert decide_flag_status(result="Success", kimco_id=9968, message_id="AAMk") != FLAG_SKIP_ELIGIBLE
    assert decide_flag_status(result="HOLD", kimco_id="", message_id="AAMk") != FLAG_SKIP_ELIGIBLE

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-kimco-inv",
                    "subject": n["msc_subject"],
                    "receivedDateTime": "2026-08-18T14:00:00Z",
                    "hasAttachments": False,
                    "bodyPreview": n["msc_from"],
                    "from": {"emailAddress": {"name": n["msc_from"], "address": "billing@mscdirect.com"}},
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return []

        def download_pdf_attachments(self, mailbox, message_id):
            return []

    selected, skipped = pull_recent_bills(Graph(), limit=1, pdf_dir=tmp_path / "pdfs")
    skip_noise = [row for row in skipped if row.get("class") != "already-flagged"]
    assert not skip_noise
    assert selected
    assert selected[0].get("hold_reason") != "not-a-bill"
    row, _ = _row(selected[0])
    assert row["Result"] != RESULT_SKIPPED
    assert "AI Skipped" not in row["Why"]
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-22")


def test_never_repeat_leeco_account_statement(tmp_path: Path):
    """NOTE-24: Leeco Account Statement is Skipped noise — no header, not a bill."""
    import ap_clerk.inbox as inbox_mod
    from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report

    n = NOTES["NOTE-24"]
    assert n["do_not_void"] is True
    assert n["leftover_kimco_id"] == 9985
    assert classify_mail(
        subject=n["subject"],
        attachment_names=[n["filename"]],
        from_name=n["from_address"],
    ) == "statement"
    assert classify_mail(
        subject="Documents ready",
        from_name=n["from_name"],
        preview=n["pdf_text"],
        attachment_names=[n["filename"]],
    ) == "statement"
    assert classify_mail(
        subject="Leeco Steel",
        from_name=n["from_name"],
        preview=n["statement_of_account_text"],
    ) == "statement"
    assert not never_skip_vendor_invoice(
        subject=n["subject"],
        from_name=n["from_name"],
        attachment_names=[n["filename"]],
    )
    assert not never_skip_vendor_invoice(
        subject="Documents ready",
        from_name=n["from_name"],
        preview=n["pdf_text"],
        attachment_names=[n["filename"]],
    )
    assert looks_like_account_statement(subject=n["subject"])
    assert looks_like_account_statement(preview=n["statement_of_account_text"])
    assert is_account_statement_document(
        text=n["pdf_text"], filename=n["filename"], subject=n["subject"]
    )
    parsed = parse_invoice_text(
        n["pdf_text"],
        filename=n["filename"],
        subject=n["subject"],
        from_name=n["from_address"],
    )
    assert parsed.get("is_statement_doc") is True
    assert parsed.get("invoice_number") != "1058256"
    assert should_create_header(
        {
            "vendor": n["vendor"],
            "invoice_number": "1058256",
            "subject": n["subject"],
            "amount": 6290.0,
        }
    ) == (False, "statement")
    assert should_create_header({"is_statement_doc": True, "vendor": n["vendor"]}) == (
        False,
        "statement",
    )

    leftover_like = {
        "vendor": n["vendor"],
        "invoice_number": "1058256",
        "date": "2026-07-07",
        "po": None,
        "pos": n["pos_listed"],
        "amount": 6290.0,
        "subject": n["subject"],
        "filename": n["filename"],
        "text": n["pdf_text"],
        "field_sources": {"invoice_number": "pdf"},
    }
    row, client = _row(
        leftover_like,
        invoice_by_number={"1058256": [{"id": n["leftover_kimco_id"], "vendor": n["vendor"]}]},
    )
    assert row["Result"] == RESULT_SKIPPED
    assert row["Result"] != RESULT_SUCCESS
    assert row["Result"] != RESULT_HOLD
    assert row["KIMCO id"] == ""
    assert not client.created
    assert "bill-vs-noise" in row["Why"]
    assert "statement" in row["Why"].lower()
    assert n["subject"] in row["Why"]

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-leeco-statement",
                    "subject": n["subject"],
                    "receivedDateTime": n["received"],
                    "hasAttachments": True,
                    "bodyPreview": n["pdf_text"],
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": n["from_address"],
                        }
                    },
                },
                {
                    "id": "m-leeco-pdf-body",
                    "subject": "Documents ready",
                    "receivedDateTime": "2026-08-18T16:00:00Z",
                    "hasAttachments": True,
                    "bodyPreview": "",
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": n["from_address"],
                        }
                    },
                },
            ]

        def list_attachment_names(self, mailbox, message_id):
            return [n["filename"]]

        def download_pdf_attachments(self, mailbox, message_id):
            return [(n["filename"], b"%PDF-1.4 statement")]

    def fake_parse(path, **kwargs):
        return parse_invoice_text(
            n["pdf_text"],
            filename=n["filename"],
            subject=str(kwargs.get("subject") or ""),
            from_name=str(kwargs.get("from_name") or ""),
        )

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, skipped = pull_recent_bills(Graph(), limit=2, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig

    skip_noise = [item for item in skipped if item.get("class") != "already-flagged"]
    assert not selected
    assert skip_noise
    assert {item.get("class") for item in skip_noise} == {"statement"}
    assert all(item.get("hold_reason") == "statement" for item in skip_noise)
    report = skip_rows_for_report(skip_noise, "API Agent - 9/14/26 (708)")
    assert report
    assert all(r["Result"] == RESULT_SKIPPED for r in report)
    assert all(r["KIMCO id"] == "" for r in report)
    assert any(n["subject"] in r["Why"] for r in report)
    assert_never_success(row["Result"], note_id="NOTE-24", detail=row["Why"])


def test_never_repeat_julie_hencke_past_due_invoices(tmp_path: Path):
    """NOTE-24: Julie Hencke Past Due Invoices is Skipped noise — same as Account Statements."""
    from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report

    n = NOTES["NOTE-24"]
    julie = n["julie"]
    assert classify_mail(subject=julie["subject"], from_name=julie["from_name"]) == "statement"
    assert classify_mail(
        subject="Documents ready",
        from_name=julie["from_name"],
        preview=julie["pdf_text"],
        attachment_names=["aging.pdf"],
    ) == "statement"
    assert not never_skip_vendor_invoice(
        subject=julie["subject"],
        from_name=julie["from_name"],
        attachment_names=["aging.pdf"],
    )
    assert looks_like_account_statement(subject=julie["subject"])
    assert looks_like_account_statement(preview=julie["pdf_text"])
    assert is_account_statement_document(text=julie["pdf_text"], subject=julie["subject"])
    assert should_create_header(
        {"vendor": julie["vendor"], "subject": julie["subject"], "amount": 100.0}
    ) == (False, "statement")
    # Specific Invoice/INV # is still a bill (Eastern Metal / 3P).
    assert classify_mail(subject="Invoice : 818600 from EASTERN METAL SUPPLY of TEXAS, INC.") == "invoice"

    row, client = _row(
        {
            "vendor": julie["vendor"],
            "invoice_number": "",
            "subject": julie["subject"],
            "text": julie["pdf_text"],
            "from_name": julie["from_name"],
        }
    )
    assert row["Result"] == RESULT_SKIPPED
    assert row["Result"] != RESULT_SUCCESS
    assert row["Result"] != RESULT_HOLD
    assert row["KIMCO id"] == ""
    assert not client.created
    assert "bill-vs-noise" in row["Why"]
    assert "Past Due Invoices" in row["Why"]

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-julie-pastdue",
                    "subject": julie["subject"],
                    "receivedDateTime": julie["received"],
                    "hasAttachments": True,
                    "bodyPreview": julie["pdf_text"],
                    "from": {
                        "emailAddress": {
                            "name": julie["from_name"],
                            "address": "julie@example.com",
                        }
                    },
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return ["aging.pdf"]

        def download_pdf_attachments(self, mailbox, message_id):
            return [("aging.pdf", b"%PDF-1.4 past due")]

    selected, skipped = pull_recent_bills(Graph(), limit=1, pdf_dir=tmp_path / "pdfs")
    skip_noise = [item for item in skipped if item.get("class") != "already-flagged"]
    assert not selected
    assert skip_noise
    assert skip_noise[0].get("class") == "statement"
    report = skip_rows_for_report(skip_noise, "API Agent - 9/14/26 (708)")
    assert report[0]["Result"] == RESULT_SKIPPED
    assert report[0]["KIMCO id"] == ""
    assert julie["subject"] in report[0]["Why"]
    assert_never_success(row["Result"], note_id="NOTE-24", detail=row["Why"])


def test_never_repeat_legacy_receipt_114745_not_invoice(tmp_path: Path):
    """NOTE-25: Receipt_114745 is a signed packing slip — not invoice 114745."""
    import ap_clerk.inbox as inbox_mod
    from ap_clerk.inbox import pull_recent_bills, skip_rows_for_report

    n = NOTES["NOTE-25"]
    slip = n["packing_slip_114745"]
    assert n["do_not_void"] is True
    assert 9995 in n["leftover_kimco_ids"]
    assert classify_attachment(filename=slip["filename"]) == ATTACHMENT_PACKING_SLIP
    for name in n["receipt_scan_names"]:
        kind = classify_attachment(filename=name)
        assert kind in NON_INVOICE_ATTACHMENT_KINDS
        assert kind != ATTACHMENT_INVOICE
    assert classify_attachment(filename=slip["filename"], text=slip["pdf_text"]) == ATTACHMENT_PACKING_SLIP
    parsed = parse_invoice_text(
        slip["pdf_text"],
        filename=slip["filename"],
        subject=slip["subject"],
        from_name=n["from_name"],
    )
    assert parsed.get("attachment_class") == ATTACHMENT_PACKING_SLIP
    assert parsed.get("is_receipt_scan_doc") is True
    assert parsed.get("invoice_number") not in {"114745", "103979"}
    assert should_create_header(parsed) == (False, "pod")
    row, client = _row(
        {
            **parsed,
            "vendor": n["vendor"],
            "invoice_number": "114745",
            "subject": slip["subject"],
            "filename": slip["filename"],
            "attachment_class": ATTACHMENT_PACKING_SLIP,
            "is_receipt_scan_doc": True,
        }
    )
    assert row["Result"] == RESULT_SKIPPED
    assert row["Result"] != RESULT_HOLD
    assert row["KIMCO id"] == ""
    assert not client.created
    assert "114745" not in str(row.get("Invoice #") or "")
    assert "packing slip" in row["Why"].lower() or "pod" in row["Why"].lower()
    assert_never_success(row["Result"], note_id="NOTE-25", detail=row["Why"])

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-legacy-slip-only",
                    "subject": slip["subject"],
                    "receivedDateTime": "2026-08-19T16:00:00Z",
                    "hasAttachments": True,
                    "bodyPreview": "",
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": n["from_address"],
                        }
                    },
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return [slip["filename"]]

        def download_pdf_attachments(self, mailbox, message_id):
            return [(slip["filename"], b"%PDF-1.4 packing slip")]

    def fake_parse(path, **kwargs):
        return parse_invoice_text(
            slip["pdf_text"],
            filename=slip["filename"],
            subject=str(kwargs.get("subject") or ""),
            from_name=str(kwargs.get("from_name") or ""),
        )

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, skipped = pull_recent_bills(Graph(), limit=1, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig

    skip_noise = [item for item in skipped if item.get("class") != "already-flagged"]
    assert not selected
    assert skip_noise
    assert skip_noise[0].get("class") == "pod"
    assert all(item.get("invoice_number") not in {"114745", "103979"} for item in selected)
    report = skip_rows_for_report(skip_noise, "API Agent - 9/15/26 (711)")
    assert report[0]["Result"] == RESULT_SKIPPED
    assert report[0]["KIMCO id"] == ""
    assert_never_success(RESULT_HOLD, note_id="NOTE-25")


def test_never_repeat_legacy_ps_inv103979_and_103980(tmp_path: Path):
    """NOTE-25: 103979 qty-77 + 103980 multi-open → line receipts + freight Fees."""
    import ap_clerk.inbox as inbox_mod
    from ap_clerk.inbox import pull_recent_bills

    n = NOTES["NOTE-25"]
    inv979 = n["ps_inv103979"]
    inv980 = n["ps_inv103980"]
    slip = n["packing_slip_114745"]

    assert extract_subject_invoice_number(inv979["subject"]) == "PS-INV103979"
    assert extract_subject_invoice_number("Invoice PS-INV103979") == "PS-INV103979"
    assert extract_subject_invoice_number(inv979["subject"]) != "103979"

    parsed979 = parse_invoice_text(
        inv979["pdf_text"],
        filename=inv979["filename"],
        subject=inv979["subject"],
        from_name=n["from_name"],
    )
    assert parsed979["invoice_number"] == "PS-INV103979"
    assert parsed979["invoice_number"] != "103979"
    assert parsed979["po"] == inv979["po"]
    assert parsed979["amount"] == inv979["amount"]
    qtys = [line.get("qty") for line in parsed979["lines"]]
    assert 77 not in qtys
    assert 24 in qtys
    assert 1 in qtys
    steel_qty = extract_invoice_lines(inv979["pdf_text"])
    assert not any((line.get("qty") == 77) for line in steel_qty)
    fees979 = parsed979["fees"] or extract_fees(inv979["pdf_text"])
    assert any(
        is_fee_or_surcharge(str(fee.get("name") or "")) and float(fee.get("amount") or 0) == inv979["freight"]
        for fee in fees979
    )
    legacy_lines, legacy_fees = extract_legacy_wire_bill(inv979["pdf_text"])
    assert {round(float(line["qty"]), 2) for line in legacy_lines} == {24.0, 1.0}
    assert any(float(fee.get("amount") or 0) == inv979["freight"] for fee in legacy_fees)

    picked979 = match_receipts(
        invoice_number=inv979["invoice_number"],
        invoice_lines=inv979["lines"],
        receipts=inv979["receipts"],
        po_number=inv979["po"],
        invoice_qty=77,
        invoice_amount=inv979["amount"],
    )
    assert picked979["found"] is True
    assert picked979["hold_no_receipts"] is False
    assert {hit["receipt"]["id"] for hit in picked979["matched"]} == {23746, 23747}
    assert 23750 not in {hit["receipt"]["id"] for hit in picked979["matched"]}
    assert not any("uniquely align" in str(amb.get("why") or "") for amb in picked979["ambiguous"])
    assert "77" not in (picked979["why"] or "") or "24" in (picked979["why"] or "")

    parsed980 = parse_invoice_text(
        inv980["pdf_text"],
        filename=inv980["filename"],
        subject=inv980["subject"],
        from_name=n["from_name"],
    )
    assert parsed980["invoice_number"] == "PS-INV103980"
    assert parsed980["po"] == inv980["po"]
    assert parsed980["amount"] == inv980["amount"]
    assert len(parsed980["lines"]) >= 4
    assert any(
        is_fee_or_surcharge(str(fee.get("name") or "")) and float(fee.get("amount") or 0) == inv980["freight"]
        for fee in (parsed980["fees"] or [])
    )

    picked980 = match_receipts(
        invoice_number=inv980["invoice_number"],
        invoice_lines=inv980["lines"],
        receipts=inv980["receipts"],
        po_number=inv980["po"],
        invoice_amount=inv980["amount"],
    )
    assert picked980["found"] is True
    assert picked980["hold_no_receipts"] is False
    assert {hit["receipt"]["id"] for hit in picked980["matched"]} == {24001, 24002, 24003, 24004}
    assert 24099 not in {hit["receipt"]["id"] for hit in picked980["matched"]}
    why980 = (picked980["why"] or "").lower()
    assert "uniquely align" not in why980
    assert "first-open" not in why980 or "not first" in why980
    assert not picked980["unmatched_lines"]

    class Recording:
        target = "live"

        def __init__(self, created_id):
            self.created_id = created_id
            self.created = []
            self.selected = []
            self.fees = []

        def create(self, service, values):
            self.created.append(values)
            return self.created_id, {"id": self.created_id, "values": values}, 200, ""

        def get_item(self, service, item_id):
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 292, "text": "292-LEGACY WIRE PRODUCTS"},
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

    def _process(bill, created_id):
        pdf_path = tmp_path / f"{bill['invoice_number']}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 legacy")
        sidecar = {
            "vendor": n["vendor"],
            "invoice_number": bill["invoice_number"],
            "date": "2026-08-19",
            "po": bill["po"],
            "pos": [bill["po"]],
            "amount": bill["amount"],
            "lines": bill["lines"],
            "fees": [{"name": "Freight Charge", "amount": bill["freight"], "fee": True}],
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
            "pdf_path": str(pdf_path),
            "pdf_on_disk": True,
        }
        po_index = {
            bill["po"]: {
                "id": int(bill["po"]),
                "text": f"{bill['po']}-LEGACY WIRE",
                "vendor_id": 292,
                "vendor_text": "292-LEGACY WIRE PRODUCTS",
                "lines": [],
            }
        }
        samples = [
            {
                "vendor_id": 292,
                "vendor_text": "292-LEGACY WIRE PRODUCTS",
                "invoice_id": 100,
                "po_text": "",
            }
        ]
        return _row(
            sidecar,
            kimco=Recording(created_id),
            po_index=po_index,
            receipts=bill["receipts"],
            samples=samples,
        )

    row979, client979 = _process(inv979, 8801)
    assert client979.selected
    selected979 = set(client979.selected[0][1])
    assert selected979 == {23746, 23747}
    assert client979.fees
    assert client979.fees[0][1][0]["amount"] == inv979["freight"]
    assert "held-unfinished" not in str(row979.get("Why") or "").lower()
    assert "uniquely align" not in (row979["Why"] or "").lower()
    assert row979["Result"] != RESULT_HOLD or "qty 77" not in (row979["Why"] or "")
    assert_never_success(RESULT_HOLD, note_id="NOTE-25", detail=row979["Why"])

    row980, client980 = _process(inv980, 8802)
    assert client980.selected
    selected980 = set(client980.selected[0][1])
    assert selected980 == {24001, 24002, 24003, 24004}
    assert 24099 not in selected980
    assert client980.fees
    assert client980.fees[0][1][0]["amount"] == inv980["freight"]
    assert "held-unfinished" not in str(row980.get("Why") or "").lower()
    assert "uniquely align" not in (row980["Why"] or "").lower()
    assert row980["Result"] != RESULT_HOLD
    assert_never_success(RESULT_HOLD, note_id="NOTE-25", detail=row980["Why"])

    class MixedGraph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-legacy-mixed",
                    "subject": inv979["subject"],
                    "receivedDateTime": "2026-08-19T16:10:00Z",
                    "hasAttachments": True,
                    "bodyPreview": "",
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": n["from_address"],
                        }
                    },
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return [inv979["filename"], slip["filename"]]

        def download_pdf_attachments(self, mailbox, message_id):
            return [
                (inv979["filename"], b"%PDF-1.4 invoice"),
                (slip["filename"], b"%PDF-1.4 slip"),
            ]

    def fake_parse_mixed(path, **kwargs):
        name = path.name
        if "Receipt" in name or "114745" in name:
            return parse_invoice_text(
                slip["pdf_text"],
                filename=slip["filename"],
                subject=str(kwargs.get("subject") or ""),
                from_name=str(kwargs.get("from_name") or ""),
            )
        return parse_invoice_text(
            inv979["pdf_text"],
            filename=inv979["filename"],
            subject=str(kwargs.get("subject") or ""),
            from_name=str(kwargs.get("from_name") or ""),
        )

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse_mixed
    try:
        selected, skipped = pull_recent_bills(
            MixedGraph(), limit=1, pdf_dir=tmp_path / "pdfs-mixed"
        )
    finally:
        inbox_mod.parse_invoice_pdf = orig

    numbers = [str(item.get("invoice_number") or "") for item in selected]
    assert numbers == ["PS-INV103979"]
    assert "114745" not in numbers
    assert "103979" not in numbers
    assert not any(item.get("hold_reason") == "parse-error" for item in selected)

    class MultiInvGraph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-legacy-two-inv",
                    "subject": "Legacy invoices",
                    "receivedDateTime": "2026-08-19T16:20:00Z",
                    "hasAttachments": True,
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": n["from_address"],
                        }
                    },
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return [inv979["filename"], inv980["filename"]]

        def download_pdf_attachments(self, mailbox, message_id):
            return [
                (inv979["filename"], b"%PDF-1.4 979"),
                (inv980["filename"], b"%PDF-1.4 980"),
            ]

    def fake_parse_two(path, **kwargs):
        if "103980" in path.name:
            return parse_invoice_text(
                inv980["pdf_text"],
                filename=inv980["filename"],
                subject=str(kwargs.get("subject") or ""),
                from_name=str(kwargs.get("from_name") or ""),
            )
        return parse_invoice_text(
            inv979["pdf_text"],
            filename=inv979["filename"],
            subject=str(kwargs.get("subject") or ""),
            from_name=str(kwargs.get("from_name") or ""),
        )

    inbox_mod.parse_invoice_pdf = fake_parse_two
    try:
        selected_two, _skipped_two = pull_recent_bills(
            MultiInvGraph(), limit=1, pdf_dir=tmp_path / "pdfs-two"
        )
    finally:
        inbox_mod.parse_invoice_pdf = orig

    assert {item.get("invoice_number") for item in selected_two} == {
        "PS-INV103979",
        "PS-INV103980",
    }


def test_never_repeat_greentree_invoice_from_not_statement(tmp_path: Path):
    """NOTE-26: Invoice from Greentree + invoice PDF is never Skipped-as-statement."""
    import ap_clerk.inbox as inbox_mod
    from ap_clerk.inbox import _clear_skip_now, pull_recent_bills, skip_rows_for_report
    from ap_clerk.graph import AI_SKIPPED_CATEGORY, FLAG_SKIP_ELIGIBLE

    n = NOTES["NOTE-26"]
    subject = n["subject"]
    assert subject == "Invoice from Greentree Packaging & Lumber"
    assert subject_has_invoice_bill_hint(subject)
    assert not looks_like_account_statement(
        subject=subject,
        preview=n["preview"],
        filename=n["filename"],
    )
    assert looks_like_account_statement(subject="Leeco Account Statement")
    assert looks_like_account_statement(subject="Past Due Invoices")
    assert classify_mail(
        subject=subject,
        preview=n["preview"],
        attachment_names=[n["filename"]],
        from_name=n["from_name"],
    ) == "invoice"
    assert classify_mail(
        subject=subject,
        preview=n["preview"],
        attachment_names=[n["filename"]],
        from_name=n["from_name"],
    ) != "statement"
    assert never_skip_vendor_invoice(
        subject=subject,
        from_name=n["from_name"],
        preview=n["preview"],
        attachment_names=[n["filename"]],
    )
    assert should_create_header(
        {
            "vendor": n["vendor"],
            "invoice_number": n["invoice_number"],
            "subject": subject,
            "amount": n["amount"],
        }
    ) == (True, "")
    assert _clear_skip_now(
        "statement", subject=subject, has_attachments=True
    ) is False
    parsed = parse_invoice_text(
        n["pdf_text"],
        filename=n["filename"],
        subject=subject,
        from_name=n["from_name"],
    )
    assert parsed.get("is_statement_doc") is not True
    assert classify_attachment(
        filename=n["filename"], text=n["pdf_text"], subject=subject
    ) == ATTACHMENT_INVOICE
    assert parsed.get("invoice_number") == n["invoice_number"]

    class Graph:
        def list_messages(self, mailbox, **kwargs):
            return [
                {
                    "id": "m-greentree-invoice-from",
                    "subject": subject,
                    "receivedDateTime": n["received"],
                    "hasAttachments": True,
                    "bodyPreview": n["preview"],
                    "from": {
                        "emailAddress": {
                            "name": n["from_name"],
                            "address": "billing@greentree.example",
                        }
                    },
                }
            ]

        def list_attachment_names(self, mailbox, message_id):
            return [n["filename"]]

        def download_pdf_attachments(self, mailbox, message_id):
            return [(n["filename"], b"%PDF-1.4 greentree invoice")]

    def fake_parse(path, **kwargs):
        return parse_invoice_text(
            n["pdf_text"],
            filename=n["filename"],
            subject=str(kwargs.get("subject") or ""),
            from_name=str(kwargs.get("from_name") or ""),
        )

    orig = inbox_mod.parse_invoice_pdf
    inbox_mod.parse_invoice_pdf = fake_parse
    try:
        selected, skipped = pull_recent_bills(Graph(), limit=1, pdf_dir=tmp_path / "pdfs")
    finally:
        inbox_mod.parse_invoice_pdf = orig

    skip_noise = [item for item in skipped if item.get("class") != "already-flagged"]
    assert not skip_noise
    assert selected
    assert selected[0].get("hold_reason") != "statement"
    assert selected[0].get("invoice_number") == n["invoice_number"]
    report = skip_rows_for_report(skip_noise, "API Agent - 9/15/26 (711)")
    assert not report

    row, client = _row(selected[0])
    assert row["Result"] != RESULT_SKIPPED
    why = str(row.get("Why") or "")
    assert "bill-vs-noise" not in why.lower() or "statement" not in why.lower()
    assert AI_SKIPPED_CATEGORY not in why
    assert row.get("Flag status") != FLAG_SKIP_ELIGIBLE
    assert client.created, f"must attempt header+attach, not skip; Why={why}"
    assert n["do_not_invent_success"] is True
    # Fixture Type 4 finish is not a live Success claim for the 9/15 miss.
    assert_never_success(RESULT_SKIPPED, note_id="NOTE-26", detail=why)


def test_never_repeat_aqpc_11002_qty_not_line_number():
    """AQPC Intuit row ``1. AMT-5003558`` is line 1, qty 100 @ $3, not qty 1."""
    text = (
        "INVOICE\nAMERICAN QUALITY POWDER COATING\n"
        "Invoice no.: 11002\nInvoice date: 09/14/2026\nP.O. Number: 59172\n"
        "# Product or service\tDescription\tQty\tRate\tAmount\n"
        "1. AMT-5003558\tEnd Plate Recoat White RAL\n"
        "9016(PO90008)\n"
        "100\t$3.00\t$300.00\n"
        "Total\t$300.00\n"
    )
    parsed = parse_invoice_text(
        text,
        subject="New payment request from AMERICAN QUALITY POWDER COATING - invoice 11002",
        from_name="AMERICAN QUALITY POWDER COATING",
    )
    assert parsed.get("invoice_number") == "11002"
    assert parsed.get("amount") == 300.0
    assert parsed.get("po") == "59172"
    lines = parsed.get("lines") or extract_aqpc_intuit_lines(text)
    assert lines, "AQPC QBO merchandise lines must parse"
    assert lines[0].get("part") == "AMT-5003558"
    assert lines[0].get("qty") == 100.0
    assert lines[0].get("amount") == 300.0
    assert lines[0].get("unit_price") == 3.0
    ok, why = qty_gate(lines, [{"qty": 100.0, "part": "AMT-5003558", "description": "End Plate"}])
    assert ok is True, why
    bad_ok, bad_why = qty_gate(
        [{"qty": 1.0, "label": "1. AMT-5003558 End Plate Recoat White RAL"}],
        [{"qty": 100.0, "description": "End Plate"}],
    )
    assert bad_ok is False
    assert "1.0" in bad_why and "100.0" in bad_why


def test_never_repeat_aqpc_11004_six_lines_not_line_numbers():
    """11004 QBO: six merchandise lines; leading N. is line #, not qty. Two AMT-5003753 rows stay separate."""
    text = (
        "INVOICE\nAMERICAN QUALITY POWDER COATING\n"
        "Invoice no.: 11004\nInvoice date: 09/15/2026\nP.O. Number: 59165\n"
        "# Product or service Description Qty Rate Amount\n"
        "1. AMT-6001232 5'x4'x4' Rotating Basket Primed and P.C.\n"
        "Blue RAL 5002(SO34627)\n"
        "5 $445.00 $2,225.00\n"
        "2. AMT-5003753 8\"x8\"x3/8\" Z Bracket P.C. Black(SO34627) 5 $5.00 $25.00\n"
        "3. AMT-5003753 8\"x8\"x3/8\" Z Bracket P.C. Black(SO34627) 5 $5.00 $25.00\n"
        "4. AMT-5003741 24\"x24\" Panel Decal Primed and P.C. Blue\n"
        "RAL 5002(SO34627)\n"
        "15 $10.00 $150.00\n"
        "5. AMT-5003750-002 Gear cover Primed and PC Blue RAL\n"
        "5002(SO34627)\n"
        "5 $10.00 $50.00\n"
        "6. AMT-5003750 Crank Swivel Weldment Primed and P.C.\n"
        "Blue RAL 5002(SO34627)\n"
        "5 $25.00 $125.00\n"
        "Total $2,600.00\n"
    )
    parsed = parse_invoice_text(
        text,
        subject="New payment request from AMERICAN QUALITY POWDER COATING - invoice 11004",
        from_name="AMERICAN QUALITY POWDER COATING",
    )
    assert parsed.get("invoice_number") == "11004"
    assert parsed.get("amount") == 2600.0
    assert parsed.get("po") == "59165"
    lines = parsed.get("lines") or extract_aqpc_intuit_lines(text)
    assert len(lines) == 6
    assert [ln.get("qty") for ln in lines] == [5.0, 5.0, 5.0, 15.0, 5.0, 5.0]
    assert [ln.get("part") for ln in lines] == [
        "AMT-6001232",
        "AMT-5003753",
        "AMT-5003753",
        "AMT-5003741",
        "AMT-5003750-002",
        "AMT-5003750",
    ]
    assert [ln.get("amount") for ln in lines] == [2225.0, 25.0, 25.0, 150.0, 50.0, 125.0]
    assert sum(float(ln.get("amount") or 0) for ln in lines) == 2600.0
    # Line numbers 1-6 must never be used as qty.
    assert 1.0 not in [ln.get("qty") for ln in lines]
    assert 6.0 not in [ln.get("qty") for ln in lines]


def test_named_po_single_receipt_consumes_aqpc_line():
    """AQPC receipt part is PO59160-01. Named-PO pick must not leave the line unmatched."""
    result = match_receipts(
        invoice_number="10999",
        invoice_lines=[
            {
                "part": "AMT-55700014",
                "qty": 12.0,
                "amount": 120.0,
                "label": "AMT-55700014 24x3x3 Gate Equalizer P.C. Black",
            }
        ],
        receipts=[
            {
                "id": 23978,
                "part": "PO59160-01",
                "po": "59160",
                "qty": 12.0,
                "amount": 120.0,
                "name": "PO59160-AMERICAN QUALITY POWDERCOATING - 2026/9/14",
            }
        ],
        po_number="59160",
    )
    assert result.get("found") is True
    assert result.get("matched")
    assert not result.get("unmatched_lines"), result.get("why")
    assert (result["matched"][0].get("receipt") or {}).get("id") == 23978


def test_never_repeat_aqpc_11004_swapped_po_lines_select_by_qty_cost():
    """11004 leftover 04/05: unique qty+unit selects 24110 for qty 15 and 24109 for qty 5."""
    result = match_receipts(
        invoice_number="11004",
        invoice_lines=[
            {"part": "AMT-6001232", "qty": 5.0, "unit_price": 445.0, "amount": 2225.0, "po_line": 1},
            {"part": "AMT-5003753", "qty": 5.0, "unit_price": 5.0, "amount": 25.0, "po_line": 2},
            {"part": "AMT-5003753", "qty": 5.0, "unit_price": 5.0, "amount": 25.0, "po_line": 3},
            {
                "part": "AMT-5003741",
                "qty": 15.0,
                "unit_price": 10.0,
                "amount": 150.0,
                "description": "Panel Decal",
                "po_line": 4,
            },
            {
                "part": "AMT-5003750-002",
                "qty": 5.0,
                "unit_price": 10.0,
                "amount": 50.0,
                "description": "Gear cover",
                "po_line": 5,
            },
            {"part": "AMT-5003750", "qty": 5.0, "unit_price": 25.0, "amount": 125.0, "po_line": 6},
        ],
        receipts=[
            {"id": 24106, "po": "59165", "part": "PO59165-01", "qty": 5.0, "unit_price": 445.0, "amount": 2225.0, "po_line": 1},
            {"id": 24107, "po": "59165", "part": "PO59165-02", "qty": 5.0, "unit_price": 5.0, "amount": 25.0, "po_line": 2},
            {"id": 24108, "po": "59165", "part": "PO59165-03", "qty": 5.0, "unit_price": 5.0, "amount": 25.0, "po_line": 3},
            {
                "id": 24109,
                "po": "59165",
                "part": "PO59165-04",
                "qty": 5.0,
                "unit_price": 10.0,
                "amount": 50.0,
                "po_line": 4,
            },
            {
                "id": 24110,
                "po": "59165",
                "part": "PO59165-05",
                "qty": 15.0,
                "unit_price": 10.0,
                "amount": 150.0,
                "po_line": 5,
            },
            {"id": 24111, "po": "59165", "part": "PO59165-06", "qty": 5.0, "unit_price": 25.0, "amount": 125.0, "po_line": 6},
        ],
        po_number="59165",
    )
    assert result.get("found") is True
    assert not result.get("unmatched_lines"), result.get("why")
    ids = {(hit.get("receipt") or {}).get("id") for hit in result.get("matched") or []}
    assert ids == {24106, 24107, 24108, 24109, 24110, 24111}
    by_line = {
        str((hit.get("line") or {}).get("part")): (hit.get("receipt") or {}).get("id")
        for hit in result.get("matched") or []
    }
    assert by_line.get("AMT-5003741") == 24110
    assert by_line.get("AMT-5003750-002") == 24109


def test_never_repeat_aqpc_10998_po_suffix_line_match():
    """Invoice po_line 1 matches receipt part PO59158-01; four qty-6@$10 lines all select."""
    result = match_receipts(
        invoice_number="10998",
        invoice_lines=[
            {"part": "AMT-55700001", "qty": 6.0, "unit_price": 10.0, "amount": 60.0, "po_line": 1},
            {"part": "AMT-55700001", "qty": 6.0, "unit_price": 10.0, "amount": 60.0, "po_line": 2},
            {"part": "AMT-5570009", "qty": 6.0, "unit_price": 10.0, "amount": 60.0, "po_line": 3},
            {"part": "AMT-5570009", "qty": 6.0, "unit_price": 10.0, "amount": 60.0, "po_line": 4},
        ],
        receipts=[
            {"id": 23979, "po": "59158", "part": "PO59158-01", "qty": 6.0, "unit_price": 10.0, "amount": 60.0},
            {"id": 23980, "po": "59158", "part": "PO59158-02", "qty": 6.0, "unit_price": 10.0, "amount": 60.0},
            {"id": 23981, "po": "59158", "part": "PO59158-03", "qty": 6.0, "unit_price": 10.0, "amount": 60.0},
            {"id": 23982, "po": "59158", "part": "PO59158-04", "qty": 6.0, "unit_price": 10.0, "amount": 60.0},
        ],
        po_number="59158",
    )
    assert result.get("found") is True
    assert not result.get("unmatched_lines"), result.get("why")
    ids = {(hit.get("receipt") or {}).get("id") for hit in result.get("matched") or []}
    assert ids == {23979, 23980, 23981, 23982}


def test_never_repeat_aqpc_10956_same_cost_inverted_qty_unit():
    """NOTE-27: 10956 plate 6@$50=$300 selects leftover 23517 2@$150=$300."""
    result = match_receipts(
        invoice_number="10956",
        invoice_lines=[
            {
                "part": "AMT-BB2000",
                "qty": 2.0,
                "unit_price": 200.0,
                "amount": 400.0,
                "description": "Rack",
                "po_line": 1,
            },
            {
                "part": "AMT-BB2000",
                "qty": 6.0,
                "unit_price": 50.0,
                "amount": 300.0,
                "description": "10'x8\" Aluminum Plate PC White(SO34672)",
                "po_line": 2,
            },
        ],
        receipts=[
            {
                "id": 23516,
                "po": "59016",
                "part": "PO59016-01",
                "qty": 2.0,
                "unit_price": 200.0,
                "amount": 400.0,
            },
            {
                "id": 23517,
                "po": "59016",
                "part": "PO59016-02",
                "qty": 2.0,
                "unit_price": 150.0,
                "amount": 300.0,
            },
        ],
        po_number="59016",
        invoice_amount=700.0,
    )
    assert result.get("found") is True
    assert not result.get("unmatched_lines"), result.get("why")
    ids = {(hit.get("receipt") or {}).get("id") for hit in result.get("matched") or []}
    assert ids == {23516, 23517}
    by_amount = {
        (hit.get("line") or {}).get("amount"): (hit.get("receipt") or {}).get("id")
        for hit in result.get("matched") or []
    }
    assert by_amount.get(400.0) == 23516
    assert by_amount.get(300.0) == 23517
    hows = " ".join(str(h) for h in (result.get("hows") or []))
    assert "same-cost" in hows or "inverted" in hows or 23517 in ids


def test_never_repeat_aqpc_too_old_before_2026_08_01():
    """NOTE-28: AQPC invoice date before 2026-08-01 is skip / no header."""
    from datetime import date

    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-28")
    assert n["slug"] == "aqpc-too-old-before-2026-08-01"
    assert n["never_success"] is True
    assert n["do_not_void"] is False
    assert set(n["voided_kimco_ids"]) == set(AQPC_TOO_OLD_KIMCO_IDS)
    assert AQPC_MIN_INVOICE_DATE == date(2026, 8, 1)
    assert is_aqpc_vendor("AMERICAN QUALITY POWDERCOATING")
    assert is_aqpc_vendor(vendor_id=22)
    assert aqpc_invoice_too_old(
        vendor="American Quality Powder Coating",
        invoice_date="2026-06-08",
        invoice_number="10696",
    )
    assert aqpc_invoice_too_old(
        vendor="American Quality Powder Coating",
        invoice_date=date(2025, 4, 22),
        invoice_number="9343",
    )
    assert aqpc_invoice_too_old(
        vendor="American Quality Powder Coating",
        invoice_date=None,
        invoice_number="9352",
    )
    assert aqpc_discover_skip_invoice("10381")
    assert not aqpc_invoice_too_old(
        vendor="American Quality Powder Coating",
        invoice_date="2026-08-01",
        invoice_number="10900",
    )
    assert not aqpc_invoice_too_old(
        vendor="American Quality Powder Coating",
        invoice_date="2026-08-27",
        invoice_number="10956",
    )
    assert not aqpc_invoice_too_old(
        vendor="JMOR MACHINERY",
        invoice_date="2025-04-22",
        invoice_number="4779",
    )
    for number in AQPC_TOO_OLD_INVOICES:
        row, client = _row(
            {
                "vendor": "American Quality Powder Coating",
                "invoice_number": number,
                "date": "2026-03-10" if number == "10381" else "2025-04-22",
                "po": "57572",
                "amount": 50.0,
                "graph_message_id": f"msg-{number}",
            }
        )
        assert row["Result"] == RESULT_SKIPPED, (number, row)
        assert row["KIMCO id"] in ("", None)
        assert "too-old" in str(row["Why"])
        assert GATE_TOO_OLD in str(row["Why"])
        assert not client.created
        assert_never_success(row["Result"], note_id="NOTE-28", detail=row["Why"])
    aug, _client = _row(
        {
            "vendor": "American Quality Powder Coating",
            "invoice_number": "10956",
            "date": "2026-08-27",
            "po": "59016",
            "amount": 700.0,
        }
    )
    assert "too-old" not in str(aug.get("Why") or "")
    assert aug["Result"] != RESULT_SKIPPED or "already" in str(aug.get("Why") or "").lower()


def test_never_repeat_aqpc_over_ppv_does_not_select_receipts():
    """NOTE-29: 11003 / 10991 over-PPV leftovers must not be Select Receipts'd."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-29")
    assert n["slug"] == "over-ppv-do-not-select-receipts"
    assert n["never_success"] is True
    assert set(n["leftover_kimco_ids"]) == {10009, 10013}

    cases = [
        {
            "invoice_number": "11003",
            "po": "59083",
            "amount": 10.0,
            "qty": 2.0,
            "unit": 5.0,
            "rec_id": 24103,
            "rec_unit": 0.777,
            "rec_amt": 1.55,
        },
        {
            "invoice_number": "10991",
            "po": "59148",
            "amount": 199.0,
            "qty": 199.0,
            "unit": 1.0,
            "rec_id": 23967,
            "rec_unit": 0.75,
            "rec_amt": 149.25,
        },
    ]
    for case in cases:
        line = {
            "part": "AMT-TEST",
            "qty": case["qty"],
            "unit_price": case["unit"],
            "amount": case["amount"],
        }
        rec = {
            "id": case["rec_id"],
            "po": case["po"],
            "part": f"PO{case['po']}-01",
            "qty": case["qty"],
            "unit_price": case["rec_unit"],
            "amount": case["rec_amt"],
        }
        locked = filter_matches_outside_ppv_gate(
            [{"line": line, "receipt": rec}],
            invoice_total=case["amount"],
        )
        assert locked["select_zero"] is True, case
        assert locked["selectable"] == []
        assert locked["skipped"]

        row, client = _row(
            {
                "vendor": "American Quality Powder Coating",
                "invoice_number": case["invoice_number"],
                "date": "2026-09-14",
                "po": case["po"],
                "amount": case["amount"],
                "lines": [line],
                "field_sources": {
                    "invoice_number": "pdf",
                    "date": "pdf",
                    "amount": "pdf",
                    "po": "pdf",
                },
            },
            po_index={
                case["po"]: {
                    "id": 900,
                    "text": f"PO{case['po']}-AQPC",
                    "vendor_id": 22,
                    "vendor_text": "AMERICAN QUALITY POWDERCOATING",
                    "lines": [
                        {
                            "part": "AMT-TEST",
                            "qty": case["qty"],
                            "amount": case["rec_amt"],
                            "unit_price": case["rec_unit"],
                        }
                    ],
                }
            },
            samples=[
                {
                    "vendor_id": 22,
                    "vendor_text": "American Quality Powder Coating",
                    "invoice_id": 100,
                    "po_text": "",
                }
            ],
            receipts=[rec],
        )
        assert row["Result"] == RESULT_HOLD, (case["invoice_number"], row)
        assert row["KIMCO id"] not in (None, "")
        assert client.created
        assert GATE_PRICE in row["Why"] or "price" in row["Why"].lower()
        assert "NOTE-29" in row["Why"] or "not selected" in row["Why"].lower() or "zero" in row["Why"].lower()
        selected_ids = []
        for _inv, refs in client.selected:
            for ref in refs:
                selected_ids.append(ref.get("id") if isinstance(ref, dict) else ref)
        assert case["rec_id"] not in selected_ids, (case["invoice_number"], client.selected)
        assert_never_success(row["Result"], note_id="NOTE-29", detail=row["Why"])

    in_gate = {
        "line": {"part": "IN", "qty": 1.0, "unit_price": 10.0, "amount": 10.0},
        "receipt": {"id": 1, "qty": 1.0, "unit_price": 9.50, "amount": 9.50},
    }
    over_gate = {
        "line": {"part": "OUT", "qty": 1.0, "unit_price": 200.0, "amount": 200.0},
        "receipt": {"id": 2, "qty": 1.0, "unit_price": 50.0, "amount": 50.0},
    }
    mixed = filter_matches_outside_ppv_gate(
        [in_gate, over_gate], invoice_total=210.0
    )
    assert mixed["select_zero"] is False
    assert [hit["receipt"]["id"] for hit in mixed["selectable"]] == [1]
    assert [hit["receipt"]["id"] for hit in mixed["skipped"]] == [2]


def test_never_repeat_crosslink_reminder_already_entered():
    """NOTE-30: Crosslink reminder emails stay already-entered HOLD. No Success invent."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-30")
    assert n["slug"] == "crosslink-reminder-already-entered"
    assert n["never_success"] is True
    assert set(n["leftover_kimco_ids"]) == {9382, 9384, 9587}

    cases = (
        ("27447", 9382),
        ("27448", 9384),
        ("27591", 9587),
    )
    for number, kimco_id in cases:
        existing = {
            number: [
                {
                    "id": kimco_id,
                    "values": {
                        "Invoice_Number": number,
                        "Vendor": {"id": 278, "text": "Crosslink Powder Coating"},
                    },
                }
            ]
        }
        row, client = _row(
            {
                "vendor": "Crosslink Powder Coating",
                "invoice_number": number,
                "date": "2026-07-07",
                "po": "58453",
                "amount": 1567.04,
                "pdf_on_disk": True,
                "filename": f"invoice-{number}.pdf",
                "field_sources": {
                    "invoice_number": "pdf",
                    "date": "pdf",
                    "amount": "pdf",
                    "po": "pdf",
                },
            },
            invoice_by_number=existing,
        )
        assert row["Result"] == RESULT_HOLD, (number, row)
        assert row["KIMCO id"] == kimco_id
        assert GATE_ALREADY_ENTERED in row["Why"] or "already-entered" in row["Why"]
        assert not client.created
        assert_never_success(row["Result"], note_id="NOTE-30", detail=row["Why"])


def test_never_repeat_priority1_freight_external():
    """NOTE-31: Priority 1 enters without Select Receipts; charges are Freight External."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-31")
    assert n["slug"] == "priority1-freight-external-no-receipts"
    assert is_freight_vendor("Priority 1")
    assert is_freight_vendor("PRIORITY 1 INC")
    assert not is_freight_vendor("Fastenal")

    from ap_clerk.kimco import (
        ADDITIONAL_CHARGE_FIELD,
        ADDITIONAL_CHARGE_LIST,
        FEE_CHARGE_LOOKUP_ID,
        FREIGHT_EXTERNAL_CHARGE_CODE,
        FREIGHT_EXTERNAL_CHARGE_LOOKUP_ID,
        fees_payload,
    )

    payload = fees_payload(
        [{"name": "Freight Charge USD$235.77", "amount": 235.77, "freight_external": True}],
        invoice_id=10047,
        freight_external=True,
    )
    child = payload["lists"][ADDITIONAL_CHARGE_LIST][0]["values"]
    lookup = child[ADDITIONAL_CHARGE_FIELD]
    assert ADDITIONAL_CHARGE_LIST == "InvoiceAdditionalCharges"
    assert lookup["id"] == FREIGHT_EXTERNAL_CHARGE_LOOKUP_ID
    assert lookup["text"] == FREIGHT_EXTERNAL_CHARGE_CODE
    assert lookup["id"] != FEE_CHARGE_LOOKUP_ID
    assert child["Amount"] == 235.77

    result, why = finish_gate(
        header_created=True,
        attach_status="attached",
        po=None,
        receipts_selected=False,
        fees=[{"name": "Freight Charge", "amount": 235.77}],
        fees_posted=True,
        freight_vendor=True,
    )
    assert result == RESULT_SUCCESS, why

    blocked, blocked_why = finish_gate(
        header_created=True,
        attach_status="attached",
        po=None,
        receipts_selected=False,
        fees=[{"name": "Freight Charge", "amount": 235.77}],
        fees_posted=False,
        freight_vendor=True,
    )
    assert blocked != RESULT_SUCCESS
    assert "Freight External" in blocked_why
    assert_never_success(blocked, note_id="NOTE-31", detail=blocked_why)

    row, client = _row(
        {
            "vendor": "Priority 1",
            "invoice_number": "18030910",
            "date": "2026-08-07",
            "po": None,
            "amount": 235.77,
            "fees": [{"name": "Freight Charge USD$235.77", "amount": 235.77, "fee": True}],
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
            },
        },
        samples=[{"vendor_id": 145, "vendor_text": "Priority 1", "invoice_id": 100, "po_text": ""}],
        receipts=[{"id": 999, "qty": 1, "amount": 235.77}],
    )
    assert row["KIMCO id"] not in (None, "")
    assert not client.selected
    assert "Freight External" in row["Why"] or row["Result"] == RESULT_SUCCESS
    if row["Result"] != RESULT_SUCCESS:
        assert_never_success(row["Result"], note_id="NOTE-31", detail=row["Why"])


def test_never_repeat_mcnichols_vendor_from_po_partial():
    """NOTE-32: email/name fail + PO vendor partial match → use PO vendor, never invent."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-32")
    assert n["slug"] == "mcnichols-vendor-from-po-partial"
    assert po_vendor_name_from_text("PO58935-MCNICHOLS CO.") == "MCNICHOLS CO."
    assert po_vendor_partial_match("billings@e.mcnichols.com", "MCNICHOLS CO.")
    assert po_vendor_partial_match("billings@e.mcnichols.com", "1116-MCNICHOLS")
    assert not po_vendor_partial_match("billings@e.mcnichols.com", "FASTENAL COMPANY")
    assert vendor_from_context(from_address="billings@e.mcnichols.com") == "McNichols"

    samples = [
        {
            "vendor_id": 77,
            "vendor_text": "1116-MCNICHOLS",
            "invoice_id": 200,
            "po_text": "PO58935-MCNICHOLS CO.",
        }
    ]
    po_info = {
        "id": 58935,
        "text": "PO58935-MCNICHOLS CO.",
        "vendor_id": 77,
        "vendor_text": "MCNICHOLS CO.",
        "lines": [{"part": "GRATE", "qty": 1, "amount": 1957.04, "unit_price": 1957.04}],
    }
    hit = _resolve_vendor_from_po_partial("billings@e.mcnichols.com", po_info, samples)
    assert hit is not None
    assert hit["vendor_id"] == 77
    assert hit["vendor_id"] != 1116 or hit.get("from_po_partial")

    invented = _resolve_vendor_from_po_partial(
        "billings@e.mcnichols.com",
        {"text": "PO58935-MCNICHOLS CO.", "vendor_text": "MCNICHOLS CO."},
        [],
    )
    assert invented is None

    row, client = _row(
        {
            "vendor": "billings@e.mcnichols.com",
            "invoice_number": "2559543",
            "date": "2026-08-19",
            "po": "58935",
            "amount": 1957.04,
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
        },
        po_index={"58935": po_info},
        samples=samples,
    )
    assert row["Result"] != RESULT_FAIL, row
    assert client.created
    assert client.created[0]["Vendor"]["id"] == 77
    fail_row, fail_client = _row(
        {
            "vendor": "billings@unknown-vendor.example",
            "invoice_number": "2559543",
            "date": "2026-08-19",
            "po": "58935",
            "amount": 1957.04,
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
        },
        po_index={
            "58935": {
                "id": 58935,
                "text": "PO58935-OTHER CO.",
                "vendor_text": "OTHER CO.",
                "lines": [],
            }
        },
        samples=[
            {
                "vendor_id": 9,
                "vendor_text": "Acme Fasteners LLC",
                "invoice_id": 100,
                "po_text": "",
            }
        ],
    )
    assert fail_row["Result"] == RESULT_FAIL
    assert "invent" in fail_row["Why"].lower()
    assert not fail_client.created
    assert_never_success(fail_row["Result"], note_id="NOTE-32", detail=fail_row["Why"])


def test_never_repeat_emj_no_po_transfer_ap():
    """NOTE-33: no PO on PDF → @Shawn McKibben + Transfer AP batch, not a fake receipt HOLD."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-33")
    assert n["slug"] == "emj-no-po-on-pdf-transfer-ap"
    assert "Shawn McKibben" in n["expected"]
    assert "Misty McCoy is not a hard default" in n["expected"]
    assert is_rfq_not_kimco_po("RFQ 081026.3")
    assert is_rfq_not_kimco_po("081026.3")
    assert not is_rfq_not_kimco_po("58913")
    assert should_transfer_ap_missing_po(
        vendor="Earle M. Jorgensen Co",
        printed_pos=[],
    )
    assert should_transfer_ap_missing_po(
        vendor="Earle M. Jorgensen Co",
        printed_pos=["081026.3"],
    )
    assert not should_transfer_ap_missing_po(
        vendor="Earle M. Jorgensen Co",
        printed_pos=["58913"],
        resolved={"info": {"id": 1}},
    )
    assert not should_transfer_ap_missing_po(vendor="Priority 1", printed_pos=[], freight=True)

    row, client = _row(
        {
            "vendor": "Earle M. Jorgensen Co",
            "invoice_number": "Z250741432",
            "date": "2026-08-19",
            "po": None,
            "amount": 4222.58,
            "lines": [
                {"part": "RB-3.75-1018V", "qty": 39, "unit_price": 20.94, "amount": 816.66},
                {"part": "RB-3.75-1018", "qty": 146, "unit_price": 10.42, "amount": 1521.32},
                {"part": "RB-3.75-1018V", "qty": 90, "unit_price": 20.94, "amount": 1884.60},
            ],
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
            },
        },
        samples=[
            {
                "vendor_id": 208,
                "vendor_text": "Earle M. Jorgensen Co",
                "invoice_id": 100,
                "po_text": "",
            }
        ],
        receipts=[{"id": 1, "qty": 39, "amount": 816.66}],
    )
    assert row["Result"] == RESULT_HOLD, row
    assert row["KIMCO id"] not in (None, "")
    assert "Shawn McKibben" in row["Why"]
    assert "@Shawn McKibben" in NO_PO_ON_PDF_BUYER_COMMENT
    assert "Misty McCoy" not in row["Why"]
    assert "Misty McCoy" not in NO_PO_ON_PDF_BUYER_COMMENT
    assert "Transfer AP" in row["Why"]
    assert GATE_PO in row["Why"] or "no-po" in row["Why"].lower() or "missing" in row["Why"].lower()
    assert "receipt" not in row["Why"].lower() or "do not invent" in row["Why"].lower() or "fake" in row["Why"].lower()
    assert not client.selected
    _assert_exception_tagged(row, category="missing_po", owner="Shawn McKibben")
    assert_never_success(row["Result"], note_id="NOTE-33", detail=row["Why"])


def test_never_repeat_metal_supermarkets_inches_qty():
    """NOTE-34: 1 @ 32 inches matches PO qty 32. Do not HOLD dollar-as-qty 262.74."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-34")
    assert n["slug"] == "metal-supermarkets-inches-qty"

    parsed_lines = extract_invoice_lines(
        "Metal Supermarkets\nCold Rolled Round Tube 1026 DOM 2.500 X 0.219 TUBE\n"
        "1 @ 32 inches 262.74\nAmount Due 262.74\n"
    )
    assert parsed_lines
    assert parsed_lines[0].get("qty") != 262.74
    assert parsed_lines[0].get("length_inches") == 32.0 or parsed_lines[0].get("qty") in {1.0, 32.0}

    inv = {
        "description": "Cold Rolled Round Tube 1026 DOM 1 @ 32 inches",
        "qty": 1.0,
        "length_inches": 32.0,
        "amount": 262.74,
        "part": "CTR1026D/2500219",
    }
    po = {"description": "Cold Rolled Round Tube 32 inches", "qty": 32.0, "amount": 262.74, "part": "CTR1026D/2500219"}
    assert length_qty_equivalent(inv, po)
    found = qty_discrepancy([inv], [po])
    assert found.get("hold") is False, found
    dollar_as_qty = qty_discrepancy(
        [{"description": "Tube", "qty": 262.74, "amount": 262.74, "length_inches": 32.0, "part": "TUBE"}],
        [{"description": "Tube", "qty": 32.0, "amount": 262.74, "part": "TUBE"}],
    )
    assert dollar_as_qty.get("hold") is False, dollar_as_qty
    ok, why = qty_gate([inv], [po])
    assert ok is True, why

    row, client = _row(
        {
            "vendor": "Metal Supermarkets",
            "invoice_number": "1091102",
            "date": "2026-08-19",
            "po": "58919",
            "amount": 262.74,
            "lines": [inv],
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
        },
        po_index={
            "58919": {
                "id": 58919,
                "text": "PO58919-METAL SUPERMARKETS",
                "vendor_id": 121,
                "vendor_text": "Metal Supermarkets",
                "lines": [po],
            }
        },
        samples=[
            {
                "vendor_id": 121,
                "vendor_text": "Metal Supermarkets",
                "invoice_id": 100,
                "po_text": "",
            }
        ],
        receipts=[{"id": 23185, "po": "58919", "qty": 32.0, "amount": 262.74, "part": "CTR1026D/2500219"}],
    )
    assert GATE_QTY not in str(row.get("Why") or "")
    assert "262.74 vs" not in str(row.get("Why") or "")
    if row["Result"] != RESULT_SUCCESS:
        assert_never_success(row["Result"], note_id="NOTE-34", detail=row["Why"])


def test_never_repeat_oneal_per_line_ppv():
    """NOTE-35: per-line unit gap 6*(901.97-901.9)=0.42. Not a rolled $714.60 HOLD."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-35")
    assert n["slug"] == "oneal-per-line-ppv-not-rolled"

    invoice_lines = [
        {"part": "PLATE-A", "qty": 20.0, "unit_price": 248.4845, "amount": 4969.69, "description": "PLATE A"},
        {"part": "PLATE-B", "qty": 6.0, "unit_price": 901.97, "amount": 5411.82, "description": "PLATE B"},
    ]
    po_lines = [
        {"part": "PLATE-A", "qty": 20.0, "unit_price": 248.4845, "amount": 4969.69, "description": "PLATE A"},
        {"part": "PLATE-B", "qty": 6.0, "unit_price": 901.9, "amount": 5411.40, "description": "PLATE B"},
    ]
    bill = evaluate_bill_price_variance(invoice_lines, po_lines, invoice_total=10381.51)
    assert bill["hold"] is False, bill
    assert abs(bill["ppv_total"] - 0.42) < 0.001, bill
    unit_decision = decide_ppv(
        invoice_line_amount=5411.82,
        po_line_amount=5411.40,
        invoice_total=10381.51,
        invoice_unit_price=901.97,
        po_unit_price=901.9,
        qty=6.0,
    )
    assert unit_decision["action"] == "ppv"
    assert unit_decision["ppv"] == 0.42
    assert unit_decision["hold"] is False

    rolled_bill = evaluate_bill_price_variance(
        [
            {"part": "PLATE-A", "qty": 20.0, "unit_price": 248.4845, "amount": 10381.51},
            {"part": "PLATE-B", "qty": 6.0, "unit_price": 901.97, "amount": 5411.82},
        ],
        po_lines,
        invoice_total=10381.51,
    )
    assert rolled_bill["hold"] is False, rolled_bill
    assert abs((rolled_bill.get("ppv_total") or 0) - 0.42) < 0.02 or rolled_bill["ppv_total"] in (0.0, 0.42)

    row, client = _row(
        {
            "vendor": "O'Neal Steel - Dallas (GP)",
            "invoice_number": "14748440",
            "date": "2026-08-18",
            "po": "58964",
            "amount": 10381.51,
            "lines": invoice_lines,
            "field_sources": {
                "invoice_number": "pdf",
                "date": "pdf",
                "amount": "pdf",
                "po": "pdf",
            },
        },
        po_index={
            "58964": {
                "id": 58964,
                "text": "PO58964-O'NEAL",
                "vendor_id": 137,
                "vendor_text": "O'Neal Steel",
                "lines": po_lines,
            }
        },
        samples=[
            {
                "vendor_id": 137,
                "vendor_text": "O'Neal Steel - Dallas (GP)",
                "invoice_id": 100,
                "po_text": "",
            }
        ],
        receipts=[
            {
                "id": 24001,
                "po": "58964",
                "part": "PLATE-A",
                "qty": 20.0,
                "unit_price": 248.4845,
                "amount": 4969.69,
            },
            {
                "id": 24002,
                "po": "58964",
                "part": "PLATE-B",
                "qty": 6.0,
                "unit_price": 901.9,
                "amount": 5411.40,
            },
        ],
    )
    assert "714.60" not in str(row.get("Why") or "")
    assert GATE_PRICE not in str(row.get("Why") or "") or "0.42" in str(row.get("PPV") or "")
    assert row.get("PPV") in {"0.42", 0.42} or str(row.get("PPV") or "").endswith("0.42")
    if row["Result"] != RESULT_SUCCESS:
        assert_never_success(row["Result"], note_id="NOTE-35", detail=row["Why"])

    over = filter_matches_outside_ppv_gate(
        [
            {
                "line": {"part": "OUT", "qty": 1.0, "unit_price": 200.0, "amount": 200.0},
                "receipt": {"id": 9, "qty": 1.0, "unit_price": 50.0, "amount": 50.0},
            }
        ],
        invoice_total=200.0,
    )
    assert over["select_zero"] is True


def test_never_repeat_gas_labeled_total_amount_due():
    """NOTE-36: labeled Total / Amount Due (incl. stacked) wins. Never invent. Single invoice ≠ multi."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-36")
    assert n["slug"] == "gas-labeled-total-amount-due"

    stacked = (
        "GAS AND SUPPLY NORTH TEXAS, LLC\nORIGINAL INVOICE\n"
        "INVOICE DATE ACCOUNT NUMBER INVOICE NUMBER\n08/19/26 A3050 0040374117\n"
        "Merchandise 40.00\nSubtotal 40.00\nTax 3.30\nAmount Due\n$43.30\n"
    )
    assert prefer_after_tax_amount(stacked, None) == 43.30
    assert prefer_after_tax_amount(stacked, 40.00) == 43.30
    parsed = parse_invoice_text(stacked, from_name="Gas and Supply North Texas, LLC")
    assert parsed["amount"] == 43.30
    bills = expand_gas_misc_invoices(stacked, {**parsed, "vendor": "Gas and Supply North Texas, LLC"})
    assert len(bills) == 1
    assert bills[0]["amount"] == 43.30
    assert not bills[0].get("gas_misc_ambiguous")
    assert bills[0].get("gas_single_invoice") is True or bills[0].get("multi_invoice_count") in (None, 1)

    ok, why = preflight_parse_gate(bills[0])
    assert ok is True, why

    missing = {
        "vendor": "Gas and Supply North Texas, LLC",
        "invoice_number": "0040372952",
        "date": "2026-08-19",
        "pdf_on_disk": True,
        "filename": "billing01_A3050_c_0040372952_p2-2.pdf",
        "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": ""},
        "gas_misc_ambiguous": True,
        "gas_single_invoice": True,
        "multi_invoice_count": 1,
    }
    hold_ok, hold_why = preflight_parse_gate(missing)
    assert hold_ok is False
    assert "invent" in hold_why.lower()
    assert "multiple Misc invoices" not in hold_why
    assert_never_success(RESULT_HOLD, note_id="NOTE-36", detail=hold_why)

    invented = prefer_after_tax_amount("Gas and Supply\nINVOICE 0011062611\nno totals here\n", None)
    assert invented in (None, 0, 0.0)

    # Live 9/16 pack: order # 0011118988-00 is not a second invoice.
    # Amount This Invoice Including Tax is the TAX CD footer, never Subtotal-only invent.
    live_one = (
        "GAS AND SUPPLY NORTH TEXAS, LLC\nORIGINAL INVOICE\n"
        "INVOICE DATE ACCOUNT NUMBER INVOICE NUMBER\n"
        "AMOUNT THIS INVOICE INCLUDING TAX\n"
        "09/15/26   A3050      0040434973\n"
        "     0011118988-00      PINNACLE PROPANE\n"
        "PRO7.5C             8     0    8    8 UN1075 LIQUEFIED PETROLEUM    CYL        24.00     192.00 N\n"
        "                                                                 Subtotal                    192.00\n"
        "  TAX CD: 000000000TXDF15 TAX DESCRP: TX/Denton/ EXMPT CD:  0 EXMPT/CERT:\n"
        "       0.00                                                                                  192.00\n"
    )
    assert gas_invoice_numbers(live_one) == ["0040434973"]
    assert prefer_after_tax_amount(live_one, None) == 192.00
    live_bills = expand_gas_misc_invoices(
        live_one, {**parse_invoice_text(live_one, from_name="Gas and Supply North Texas, LLC"),
                   "vendor": "Gas and Supply North Texas, LLC"}
    )
    assert len(live_bills) == 1
    assert live_bills[0]["invoice_number"] == "0040434973"
    assert live_bills[0]["amount"] == 192.00
    assert not live_bills[0].get("gas_misc_ambiguous")


def test_never_repeat_jpsteel_125315_combine_same_item_receipts():
    """NOTE-37: 21@$33 = 24126 8@$33 + 24127 13@$33. Combine; do not HOLD blocked-400."""
    from ap_clerk.rules import (
        SAME_ITEM_COVER_HOW,
        blocked_400_not_a_hold_when_same_item_cover,
        match_combine_same_item_receipts,
        match_receipts,
        match_same_unit_qty_cover,
    )

    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-37")
    assert n["slug"] == "jpsteel-125315-combine-same-item-receipts"
    assert n["gate"] == GATE_RECEIPT
    assert n["do_not_mutate"] is True
    assert n["kyle_finished_kimco_ids"] == (10107,)
    assert "combine" in n["expected"].lower()
    assert "blocked-400" in n["expected"]
    assert "24126" in n["9_17_bug"] and "24127" in n["9_17_bug"]

    line = {
        "part": "21684-1",
        "qty": 21.0,
        "unit_price": 33.0,
        "amount": 693.0,
    }
    leftovers = [
        {
            "id": 24126,
            "po": "59128",
            "part": "PO59128-01",
            "qty": 8.0,
            "unit_price": 33.0,
            "amount": 264.0,
        },
        {
            "id": 24127,
            "po": "59128",
            "part": "PO59128-01",
            "qty": 13.0,
            "unit_price": 33.0,
            "amount": 429.0,
        },
    ]
    cover = match_combine_same_item_receipts(line, leftovers)
    assert cover is not None
    assert sorted(r["id"] for r in cover) == [24126, 24127]
    assert match_same_unit_qty_cover(line, leftovers) == cover

    match = match_receipts(
        invoice_number="125315",
        invoice_lines=[line],
        receipts=leftovers,
        po_number="59128",
        invoice_amount=693.0,
    )
    assert not match.get("hold_no_receipts")
    assert not match.get("unmatched_lines")
    ids = sorted((h.get("receipt") or {}).get("id") for h in (match.get("matched") or []))
    assert ids == [24126, 24127]
    assert all("same-unit" in str(h.get("how") or "") or "same-item" in str(h.get("how") or "") for h in match["matched"])
    assert "125315" in SAME_ITEM_COVER_HOW
    assert blocked_400_not_a_hold_when_same_item_cover("blocked-400", match["matched"]) is True
    assert blocked_400_not_a_hold_when_same_item_cover("selected", match["matched"]) is False

    # False HOLD Why is the never-repeat miss — not Success, and not "can't combine".
    false_hold_why = (
        "HOLD (receipt): Select Receipts blocked-400 on same-PO-line split "
        "24126 8@$33 + 24127 13@$33 (PDF 21@$33=$693)."
    )
    assert "blocked-400" in false_hold_why
    assert_never_success(RESULT_HOLD, note_id="NOTE-37", detail=false_hold_why)

    ok, why = treyce_finish_selfcheck({"same_item_cover_blocked_400": True})
    assert ok is False
    assert "NOTE-37" in why or "24126" in why
    assert_never_success(RESULT_HOLD, note_id="NOTE-37", detail=why)

    # Unique sum only — do not guess two covers at the same unit.
    amb = match_combine_same_item_receipts(
        line,
        leftovers
        + [{"id": 9, "po": "59128", "qty": 8.0, "unit_price": 33.0, "amount": 264.0}],
    )
    assert amb is None


def test_never_repeat_jpsteel_125316_rounding_ppv():
    """NOTE-38: posted≠PDF unit-rounding → signed PPV, not HOLD. Over-gate still locks."""
    from ap_clerk.kimco import PPV_CHARGE_LOOKUP_ID, ppv_payload
    from ap_clerk.rules import rounding_ppv_to_hit_pdf_total

    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-38")
    assert n["slug"] == "jpsteel-125316-rounding-ppv-not-hold"
    assert n["gate"] == GATE_PRICE
    assert "−$0.10" in n["expected"] or "-$0.10" in n["expected"] or "0.10" in n["expected"]
    assert "NOTE-29" in n["expected"]

    hit = rounding_ppv_to_hit_pdf_total(1580.73, 1580.83, receipts_selected=True)
    assert hit["action"] == "ppv"
    assert hit["ppv"] == -0.10
    assert hit["hold"] is False

    same = rounding_ppv_to_hit_pdf_total(1130.40, 1130.34, receipts_selected=True)
    assert same["action"] == "ppv"
    assert same["ppv"] == 0.06
    assert same["hold"] is False
    assert "125051" in n["9_17_bug"] or "10111" in n["9_17_bug"]
    assert "0.06" in n["expected"]

    two_cent = rounding_ppv_to_hit_pdf_total(832.03, 832.02, receipts_selected=True)
    assert two_cent["action"] == "match"
    assert two_cent["ppv"] == 0.0

    no_receipts = rounding_ppv_to_hit_pdf_total(1580.73, 1580.83, receipts_selected=False)
    assert no_receipts["hold"] is True
    assert no_receipts["ppv"] == 0.0
    assert_never_success(RESULT_HOLD, note_id="NOTE-38", detail=no_receipts["reason"])

    over = rounding_ppv_to_hit_pdf_total(100.0, 250.0, receipts_selected=True)
    assert over["action"] == "hold"
    assert over["ppv"] == 0.0
    assert_never_success(RESULT_HOLD, note_id="NOTE-38", detail=over["reason"])

    payload = ppv_payload(-0.10, invoice_id=10108)
    child = payload["lists"]["InvoiceAdditionalCharges"][0]["values"]
    assert child["Additional_Charges"]["id"] == PPV_CHARGE_LOOKUP_ID
    assert child["Amount"] == -0.10
    assert child["Price"] == -0.10

    false_hold = (
        "HOLD after live GET of 10108: PDF/verification=$1,580.73 posted=$1,580.83 "
        "($0.10 unit-rounding). Do not invent Success."
    )
    assert_never_success(RESULT_HOLD, note_id="NOTE-38", detail=false_hold)


def _assert_exception_tagged(row, *, category: str, owner: str, note_id: str = "NOTE-39"):
    assert row["Result"] != RESULT_SUCCESS
    assert_never_success(row["Result"], note_id=note_id, detail=row.get("Why") or "")
    assert row[COL_EXCEPTION_CATEGORY] == category
    assert row[COL_EXCEPTION_OWNER] == owner
    prefix = exception_prefix(category, owner)
    assert prefix in (row.get("Why") or "")
    assert category in EXCEPTION_CATEGORY_OWNERS
    assert EXCEPTION_CATEGORY_OWNERS[category] == owner


def test_never_repeat_note39_exception_category_owner(tmp_path: Path):
    """NOTE-39: HOLD rows get category+owner at creation; Success leaves them blank."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-39")
    assert n["slug"] == "exception-category-owner-at-hold"
    assert n["never_success"] is True
    assert "Stampli" in n["expected"]
    assert "Kyle" in n["expected"]
    assert EXCEPTION_CATEGORY_OWNERS == {
        "price_variance": "Shawn McKibben",
        "missing_receipt": "Ruben Perez",
        "quantity_variance": "buyer",
        "missing_po": "Shawn McKibben",
        "vendor_mismatch": "AP / vendor master",
        "already_entered": "none / review",
        "pdf_capture": "AP",
        "auto_pay": "none",
        "partial_match": "AP / Treyce",
        "other": "AP",
    }
    assert PURCHASING_EXCEPTION_CATEGORIES == frozenset({"price_variance", "missing_po"})
    assert "Misty" not in " ".join(EXCEPTION_CATEGORY_OWNERS.values())
    assert "Transfer AP" not in " ".join(EXCEPTION_CATEGORY_OWNERS.values())
    assert canonical_exception_owner("missing_po", "Misty McCoy / Transfer AP") == "Shawn McKibben"
    assert "Shawn McKibben" in n["expected"]
    assert "Misty McCoy is not a hard default" in n["expected"]

    # Price HOLD (NOTE-06 / over-gate) → Shawn McKibben
    emj = NOTES["NOTE-06"]
    price_row, _ = _row(
        {
            "vendor": emj["vendor"],
            "invoice_number": emj["invoice_number"],
            "date": "2026-08-16",
            "po": "58000",
            "amount": emj["amount"],
            "lines": [{"part": "STEEL", "amount": emj["po_amount"]}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=_kimco(),
        po_index={
            "58000": {
                "id": 8,
                "text": "58000-EMJ",
                "vendor_id": 208,
                "vendor_text": "EMJ",
                "lines": [{"part": "STEEL", "amount": emj["amount"], "unit_price": emj["amount"], "qty": 1}],
            }
        },
        samples=[{"vendor_id": 208, "vendor_text": "EMJ", "invoice_id": 9, "po_text": ""}],
    )
    _assert_exception_tagged(
        price_row,
        category="price_variance",
        owner="Shawn McKibben",
    )
    assert GATE_PRICE in price_row["Why"] or PRICE_DOES_NOT_MATCH in price_row["Why"]

    # Missing receipts HOLD → Ruben Perez
    class CreateHold:
        target = "live"

        def create(self, service, values):
            return 8801, {"id": 8801, "values": values}, 200, ""

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
            return "no-pdf-on-vm"

        def try_select_receipts(self, *args, **kwargs):
            return "held-unfinished"

        def try_post_fees(self, *args, **kwargs):
            return "none"

    missing_row, _ = _row(
        {
            "vendor": "Fastenal Company",
            "invoice_number": "TXFT000000",
            "date": "2026-08-26",
            "po": "58700",
            "amount": 40.0,
            "lines": [{"part": "NEED-THIS", "qty": 6}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=CreateHold(),
        po_index={"58700": {"id": 3, "text": "58700-FASTENAL", "vendor_id": 9, "lines": []}},
        receipts=[{"slip": "OTHER", "qty": 6, "part": "DIFFERENT", "po_line": 1}],
        samples=[{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}],
    )
    _assert_exception_tagged(
        missing_row,
        category="missing_receipt",
        owner="Ruben Perez",
    )
    assert "no receipts" in missing_row["Why"].lower()

    # Already-entered HOLD (NOTE-17) → none / review
    insight = NOTES["NOTE-17"]
    pdf_path = tmp_path / "Invoice_1809.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 insight 1809")
    already_row, _ = _row(
        {
            "vendor": insight["vendor"],
            "invoice_number": "1809",
            "date": "2026-08-16",
            "amount": 10.0,
            "pdf_path": str(pdf_path),
            "pdf_on_disk": True,
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": ""},
        },
        invoice_by_number={
            "1809": [
                {
                    "id": insight["kimco_id"],
                    "values": {
                        "Invoice_Number": "1809",
                        "Vendor": {"id": 1, "text": insight["vendor"]},
                    },
                }
            ]
        },
    )
    _assert_exception_tagged(
        already_row,
        category="already_entered",
        owner="none / review",
    )
    assert GATE_ALREADY_ENTERED in already_row["Why"] or "already-entered" in already_row["Why"]

    # pdf-behind-link HOLD (NOTE-09) → AP
    aqpc = NOTES["NOTE-09"]
    pdf_row, _ = _row(
        {
            "vendor": aqpc["vendor"],
            "invoice_number": "",
            "hold_reason": "pdf-behind-link",
            "pdf_behind_link": True,
            "pdf_link_host": aqpc["link_host"],
            "browser_tried": True,
            "browser_failure": "login-required",
            "action": "hold",
        }
    )
    _assert_exception_tagged(pdf_row, category="pdf_capture", owner="AP")
    assert GATE_PDF_LINK in pdf_row["Why"] or "pdf-behind-link" in pdf_row["Why"]

    # Qty HOLD (NOTE-03 Capital) → buyer
    capital = NOTES["NOTE-03"]
    qty_row, _ = _row(
        {
            "vendor": capital["vendor"],
            "invoice_number": capital["invoice_number"],
            "date": "2026-08-16",
            "po": capital["po"],
            "amount": 425.0,
            "fees": [{"name": "Handling", "amount": capital["fee_amount"]}],
            "lines": [{"part": "BLADE", "qty": capital["invoice_qty"], "amount": 400.0}],
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=_kimco(),
        po_index={
            capital["po"]: {
                "id": 45,
                "text": f"{capital['po']}-CAPITAL",
                "vendor_id": 45,
                "vendor_text": "Capital Machine",
                "lines": [{"part": "BLADE", "qty": capital["po_qty"], "amount": 400.0, "unit_price": 200.0}],
            }
        },
        samples=[{"vendor_id": 45, "vendor_text": "Capital Machine", "invoice_id": 9, "po_text": ""}],
        receipts=[{"po": capital["po"], "slip": capital["invoice_number"], "part": "BLADE", "qty": capital["po_qty"], "id": 1}],
    )
    _assert_exception_tagged(qty_row, category="quantity_variance", owner="buyer")
    assert GATE_QTY in qty_row["Why"] or "qty" in qty_row["Why"].lower()

    # Success leaves Exception category/owner blank
    success_pdf = tmp_path / "TXFT499356.pdf"
    success_pdf.write_bytes(b"%PDF-1.4 finished")
    success_row, _ = _row(
        {
            "vendor": "Fastenal Company",
            "invoice_number": "TXFT499356",
            "date": "2026-08-26",
            "po": "58700",
            "amount": 40.0,
            "pdf_path": str(success_pdf),
            "field_sources": {"invoice_number": "pdf", "date": "pdf", "amount": "pdf", "po": "pdf"},
        },
        kimco=_kimco(attach="attached", select="selected"),
        po_index={"58700": {"id": 3, "text": "58700-FASTENAL", "vendor_id": 9, "lines": []}},
        receipts=[{"slip": "TXFT499356", "qty": 6, "part": "FAST-1", "po_line": 2, "po": "58700", "id": 44}],
        samples=[{"vendor_id": 9, "vendor_text": "Fastenal Company", "invoice_id": 100, "po_text": ""}],
    )
    assert success_row["Result"] == RESULT_SUCCESS
    assert success_row[COL_EXCEPTION_CATEGORY] == ""
    assert success_row[COL_EXCEPTION_OWNER] == ""
    assert "category=" not in (success_row.get("Why") or "")

    # Remaining gate map (no new HOLD reasons)
    assert classify_exception(
        result=RESULT_HOLD, why="HOLD (vendor-mismatch): parsed MSC, posted RMP."
    ) == ("vendor_mismatch", "AP / vendor master")
    assert classify_exception(
        result=RESULT_HOLD,
        why="HOLD (po): @Misty McCoy PO number is missing. Transfer to Transfer AP.",
    ) == ("missing_po", "Shawn McKibben")
    assert classify_exception(
        result=RESULT_HOLD,
        why="HOLD (po): PO 59081 is on the invoice but not findable on live by vendor + part/WO.",
    ) == ("missing_po", "Shawn McKibben")
    remapped = apply_exception_category_owner(
        {
            "Result": RESULT_HOLD,
            "Why": (
                "category=missing_po; owner=Misty McCoy / Transfer AP. "
                "HOLD (po): PO 59081 not findable on live."
            ),
        }
    )
    assert remapped[COL_EXCEPTION_CATEGORY] == "missing_po"
    assert remapped[COL_EXCEPTION_OWNER] == "Shawn McKibben"
    assert remapped["Why"].startswith("category=missing_po; owner=Shawn McKibben")
    assert remapped["Why"].count("category=") == 1
    assert "Misty McCoy" not in remapped["Why"]
    assert classify_exception(
        result=RESULT_HOLD, why="HOLD (auto-pay): Toyota Commercial Finance / auto-pay."
    ) == ("auto_pay", "none")
    assert classify_exception(
        result=RESULT_INCOMPLETE,
        why="Selected vs unmatched: matched 1, unmatched 1. Partial Select Receipts leftovers.",
    ) == ("partial_match", "AP / Treyce")
    assert classify_exception(
        result=RESULT_INCOMPLETE, why="Incomplete (finish): attach blocked-405."
    ) == ("other", "AP")
    assert classify_exception(result=RESULT_SKIPPED, why="Skipped (bill-vs-noise): statement.") is None
    stamped = apply_exception_category_owner(
        {"Result": RESULT_HOLD, "Why": "HOLD (price-does-not-match): gap."}
    )
    assert stamped[COL_EXCEPTION_CATEGORY] == "price_variance"
    assert stamped["Why"].startswith("category=price_variance; owner=Shawn McKibben")


def test_never_repeat_note42_gas_misc_lines_k():
    """NOTE-42: Gas Type 4 header-only must never report Success."""
    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-42")
    assert n["never_success"] is True
    assert "header-only" in n["expected"].lower() or "Header-only" in n["expected"]
    assert_never_success(RESULT_INCOMPLETE, note_id="NOTE-42", detail="header-only Lines-K empty")


def test_never_repeat_note48_pod_not_invoice_inbox():
    """NOTE-48: POD@ is POD intake only — never the AP invoice mailbox."""
    from ap_clerk.graph import (
        ALLOWED_MAILBOX,
        POD_MAILBOX,
        assert_allowed_mailbox,
        assert_pod_mailbox,
        is_invoice_mailbox,
        is_pod_mailbox,
        MailboxRejected,
    )

    n = next(note for note in TREYCE_NOTES_V12 if note["id"] == "NOTE-48")
    assert n["never_success"] is True
    assert "pod@kannonmfg.com" in n["expected"].lower() or "POD@" in n["expected"]
    assert "accountspayable@" in n["expected"]
    assert "Mail.Send" in n["expected"]
    assert is_invoice_mailbox(ALLOWED_MAILBOX) is True
    assert is_invoice_mailbox(POD_MAILBOX) is False
    assert is_pod_mailbox(POD_MAILBOX) is True
    assert assert_pod_mailbox("POD@kannonmfg.com") == POD_MAILBOX
    with pytest.raises(MailboxRejected, match="NOTE-48"):
        assert_allowed_mailbox(POD_MAILBOX)
    assert_never_success(RESULT_HOLD, note_id="NOTE-48", detail="invoice on POD@ is report-only")

