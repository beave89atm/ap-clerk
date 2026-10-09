"""Guards on the AP run path. Fixtures only — no KIMCO and no mailbox calls."""

from pathlib import Path

import pytest

from ap_clerk.ap_run import (
    attachment_page_check,
    classify_duplicate,
    cross_unit_selection,
    end_of_run_report,
    intake_action,
    line_price_variances,
    misc_versus_po,
    plan_email_moves,
    po_lookup_hold,
    purchase_gl_hold,
    qc_printed_total,
    quantities_match,
    resolve_invoice_number,
    unifirst_tax_gap,
)
from scripts.ap_run_2026_10_08_enter import match_lines, qty_matches
from scripts.ap_run_2026_10_09_enter import guard_job, piece_inch_match

FIXTURES = Path(__file__).parent / "fixtures" / "ap_run"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_oneal_ship_to_is_not_the_invoice_number():
    text = _read("oneal_15491464.txt")
    decision = resolve_invoice_number("O'Neal Steel", text, claimed="885055")
    assert decision["ok"] is True
    assert decision["number"] == "15491464"
    assert decision["rejected"] == "885055"
    assert "885055" in decision["reason"]


def test_customer_ship_to_codes_are_rejected():
    for code in ("885055", "14748440"):
        decision = resolve_invoice_number("O'Neal Steel", claimed=code)
        assert decision["ok"] is False
        assert decision["number"] is None
        assert code in decision["reason"]


def test_repeated_vendor_number_is_rejected():
    cohort = [
        {"vendor": "O'Neal Steel", "vendor_id": 137, "number": "15490001", "amount": 9490.50, "po": "59341"},
    ]
    decision = resolve_invoice_number(
        "O'Neal Steel",
        claimed="15490001",
        cohort=cohort,
        vendor_id=137,
        amount=10503.04,
        po="59137",
    )
    assert decision["ok"] is False
    assert "15490001" in decision["reason"]
    assert "repeats" in decision["reason"]


def test_daily_script_rejects_oneal_ship_to_number():
    decision = guard_job(
        {
            "number": "885055",
            "vendor": "O'Neal Steel",
            "vendor_id": 137,
            "amount": 10503.04,
            "po": "59137",
            "day": "2026-10-07",
            "page_text": _read("oneal_15492001.txt"),
        }
    )
    assert decision["action"] == "enter"
    assert decision["number"] == "15492001"


def test_duplicate_requires_vendor_and_invoice_number():
    candidate = {"vendor": "Legacy Wire", "vendor_id": 292, "number": "PS-INV104087", "amount": 1523.45, "day": "2026-10-05"}
    existing = [dict(candidate)]
    decision = classify_duplicate(candidate, existing)
    assert decision["action"] == "already"
    assert decision["kind"] == "duplicate"


def test_number_alone_is_not_a_duplicate():
    candidate = {"vendor": "RMP Industrial Supply", "vendor_id": 322, "number": "1474542", "amount": 226.14, "day": "2026-10-01"}
    existing = [{"vendor": "UniFirst", "vendor_id": 189, "number": "1474542", "amount": 226.14, "day": "2026-10-01"}]
    decision = classify_duplicate(candidate, existing)
    assert decision["action"] == "enter"
    assert decision["kind"] == "number-only"
    assert decision["action"] != "already"


def test_near_duplicate_holds_instead_of_skipping():
    candidate = {
        "vendor": "Legacy Wire",
        "vendor_id": 292,
        "number": "PS-INV104099",
        "amount": 1080.00,
        "day": "2026-10-06",
        "po": "59271",
        "po_line": "59271-03",
    }
    existing = [
        {
            "vendor": "Legacy Wire",
            "vendor_id": 292,
            "number": "PS-INV104050",
            "amount": 1080.00,
            "day": "2026-09-01",
            "po": "59271",
            "po_line": "59271-03",
        }
    ]
    decision = classify_duplicate(candidate, existing)
    assert decision["action"] == "hold"
    assert decision["kind"] == "near-duplicate"
    assert decision["action"] != "already"


def test_ppv_limit_comes_from_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AP_PPV_LIMIT", "10")
    decision = line_price_variances([{"invoice_amount": 12.00, "receipt_amount": 0.00}])
    assert decision["limit"] == 10.0
    assert decision["action"] == "hold"
    under = line_price_variances([{"invoice_amount": 9.00, "receipt_amount": 0.00}])
    assert under["action"] == "ppv"
    assert under["ppv"] == 9.0


def test_per_line_ppv_does_not_let_gaps_cancel():
    job = {
        "merch": 200.00,
        "lines_match": [
            {"qty": 1, "amount": 180.00, "uom": "EA"},
            {"qty": 2, "amount": 20.00, "uom": "EA"},
        ],
    }
    receipts = [
        {"id": 1, "qty": 1, "uom": "EA", "extended": 100.00, "open": True, "part": "A"},
        {"id": 2, "qty": 2, "uom": "EA", "extended": 100.00, "open": True, "part": "B"},
    ]
    ids, _ppv, reason = match_lines(job, receipts)
    assert ids == []
    assert "cannot cancel" in reason


def test_equal_qty_different_units_do_not_match():
    assert quantities_match(40, 40, "PCS", "IN") is False
    assert qty_matches(40, 40, "PCS", "IN") is False
    assert qty_matches(40, 40, "EA", "EA-Each") is True


def test_oneal_single_bar_allows_unit_difference_when_dollars_match():
    item = {
        "vendor": "O'Neal Steel",
        "po": "59341",
        "merch": 400.00,
        "lines_match": [{"qty": 1, "amount": 400.00, "uom": "PCS"}],
    }
    facts = [{"id": 77, "open": True, "qty": 240, "uom": "IN", "extended": 400.00}]
    decision = cross_unit_selection(item, facts)
    assert decision["allowed"] is True
    assert decision["ids"] == [77]


def test_morgan_steel_is_not_the_oneal_bar_rule():
    item = {
        "vendor": "Morgan Steel",
        "po": "59199",
        "merch": 6040.00,
        "lines_match": [{"qty": 40, "amount": 6040.00, "uom": "PCS"}],
    }
    facts = [{"id": 88, "open": True, "qty": 40, "uom": "IN", "extended": 6040.00}]
    ids, _ppv, reason = piece_inch_match(item, facts)
    assert ids == []
    assert "Morgan Steel" in reason
    assert "not covered" in reason


def test_linked_receipt_is_not_selected():
    job = {"merch": 100.00, "lines_match": [{"qty": 1, "amount": 100.00, "uom": "EA"}]}
    receipts = [
        {
            "id": 24982,
            "qty": 1,
            "uom": "EA",
            "extended": 100.00,
            "open": True,
            "part": "A",
            "invoiced_bill": {"id": 10491, "text": "18564"},
        }
    ]
    ids, _ppv, reason = match_lines(job, receipts)
    assert ids == []
    assert "24982" in reason
    assert "10491" in reason


def test_receipt_quantity_is_not_changed():
    job = {"merch": 100.00, "lines_match": [{"qty": 1, "amount": 100.00, "uom": "EA"}]}
    receipts = [{"id": 50, "qty": 4, "select_qty": 1, "uom": "EA", "extended": 100.00, "open": True, "part": "A"}]
    # Quantity 4 does not match the invoice, and the override must not make it match.
    ids, _ppv, reason = match_lines(job, receipts)
    assert ids == []
    assert "was not changed" in reason or "No open receipt" in reason
    forced = dict(receipts[0])
    forced["qty"] = 1
    forced["select_qty"] = 4
    ids, _ppv, reason = match_lines(job, [forced])
    assert ids == []
    assert "was not changed" in reason


def test_po_with_no_receipt_child_is_not_missing():
    reason = po_lookup_hold("59088", [{"qty": 2, "uom": "EA"}], po_in_kimco=True, receipts=[{"placeholder": True, "po_id": 1}])
    assert reason is not None
    assert "no receipt yet" in reason
    assert "was not found" not in reason
    missing = po_lookup_hold("59088", [], po_in_kimco=False, receipts=[])
    assert missing is not None
    assert "was not found" in missing


def test_printed_kimco_po_is_not_coded_misc():
    text = _read("capital_59088.txt")
    number = resolve_invoice_number("Capital Machine", text, claimed="misc")
    assert number["number"] == "26199"
    decision = misc_versus_po(printed_po="59088", po_in_kimco=True, has_receipt=False, proposed_kind="misc")
    assert decision["kind"] == "po"
    assert decision["action"] == "hold"
    assert decision["owner"] == "shawn"
    assert decision["batch"] == "Transfer AP"
    assert "59088" in decision["reason"]
    assert "miscellaneous" in decision["reason"].lower() or "Not coded miscellaneous" in decision["reason"]


def test_printed_total_comes_from_the_rendered_page():
    text = _read("oneal_15491464.txt")
    decision = qc_printed_total(page_text=text, typed_amount=9490.50)
    assert decision["ok"] is True
    assert decision["printed_total"] == 9490.50
    mismatch = qc_printed_total(page_text=text, typed_amount=1.00)
    assert mismatch["ok"] is False
    assert mismatch["printed_total"] == 9490.50
    assert mismatch["printed_total"] != 1.00


def test_attachment_split_shows_only_this_invoice():
    own = [_read("bill_a_page1.txt"), _read("bill_a_page2.txt")]
    good = attachment_page_check(own, "10421", ["10422"])
    assert good["ok"] is True
    shifted = [_read("bill_a_page1.txt"), _read("bill_b_page1.txt")]
    bad = attachment_page_check(shifted, "10421", ["10422"])
    assert bad["ok"] is False
    assert "10422" in bad["reason"]
    assert "page 2" in bad["reason"]


def _move(invoice: str, *, dest: str = "Inbox/Entered", amount: float = 10.0, vendor: str = "Xcaliber") -> dict:
    return {
        "vendor": vendor,
        "invoice_number": invoice,
        "amount": amount,
        "source_folder": "Inbox",
        "dest_folder": dest,
    }


def _bill(invoice: str, *, note: str = "AP Clerk: entered.", note_id: int = 1, attachment_ok: bool = True) -> dict:
    return {
        "vendor": "Xcaliber",
        "invoice_number": invoice,
        "amount": 10.0,
        "attachment_ok": attachment_ok,
        "note": note,
        "note_id": note_id,
    }


def test_email_move_cap_stops_the_run():
    candidates = [_move("1"), _move("2"), _move("3")]
    bills = [_bill("1"), _bill("2"), _bill("3")]
    plan = plan_email_moves(candidates, kimco_bills=bills, cap=2)
    assert [item["allow"] for item in plan["moves"]] == [True, True, False]
    assert "cap" in plan["moves"][2]["reason"].lower()
    assert plan["allowed_count"] == 2


def test_email_move_stops_on_first_failed_verify():
    candidates = [_move("1"), _move("2"), _move("3")]
    bills = [_bill("1"), _bill("2", note="", note_id=None), _bill("3")]
    plan = plan_email_moves(candidates, kimco_bills=bills, cap=10)
    assert plan["moves"][0]["allow"] is True
    assert plan["moves"][1]["allow"] is False
    assert "note" in plan["moves"][1]["reason"]
    assert plan["moves"][2]["allow"] is False
    assert plan["moves"][2]["reason"] == "Stopped after a failed verify."


def test_email_move_verifies_kimco_state_not_a_hand_typed_list():
    plan = plan_email_moves([_move("10472")], kimco_bills=[], cap=10)
    assert plan["moves"][0]["allow"] is False
    assert "hand-typed" in plan["moves"][0]["reason"]


def test_email_move_never_touches_archive_folders():
    candidates = [
        _move("1", dest="Inbox/9 - FORT WORTH ARCHIVE"),
        _move("2", dest="Inbox/Entered"),
    ]
    plan = plan_email_moves(candidates, kimco_bills=[_bill("1"), _bill("2")], cap=10)
    assert all(item["allow"] is False for item in plan["moves"])
    assert "Archive" in plan["stop_reason"]
    source = plan_email_moves(
        [{**_move("1"), "source_folder": "Inbox/AutoPay Archive"}],
        kimco_bills=[_bill("1")],
        cap=10,
    )
    assert source["moves"][0]["allow"] is False


def test_orphan_bill_without_a_note_is_reported():
    report = end_of_run_report(
        [
            {"bill_id": 10472, "vendor": "Xcaliber", "invoice": "WB10472", "note": "", "note_id": ""},
            {"bill_id": 10473, "vendor": "Xcaliber", "invoice": "WB10473", "note": "AP Clerk: entered.", "note_id": 9},
        ]
    )
    assert report["ok"] is False
    assert report["orphan_count"] == 1
    assert report["orphans"][0]["bill_id"] == 10472
    assert "no AP Clerk note" in report["orphans"][0]["reason"]


def test_statement_autopay_and_non_invoice_are_filtered():
    for kind in ("statement", "auto-pay", "autopay", "not-a-bill", "payment", "pod"):
        decision = intake_action(kind)
        assert decision["enter"] is False
    assert intake_action("invoice")["enter"] is True


def test_blank_purchase_gl_is_not_a_hold():
    blank = purchase_gl_hold(None)
    assert blank["blank"] is True
    assert blank["hold"] is False
    assert purchase_gl_hold("")["hold"] is False
    assert purchase_gl_hold({"id": 200, "text": "5081100"})["hold"] is False


def test_unifirst_cents_tax_gap_is_ppv():
    decision = unifirst_tax_gap(vendor="UniFirst", printed_total=928.54, covered=928.50, tax_gap=0.04)
    assert decision["action"] == "ppv"
    assert decision["ppv"] == 0.04
    other = unifirst_tax_gap(vendor="Gas and Supply", printed_total=10.04, covered=10.00, tax_gap=0.04)
    assert other["action"] == "unchanged"
