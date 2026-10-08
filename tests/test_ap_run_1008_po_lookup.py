"""PO lines with no receipts are not reported as a missing purchase order."""

from scripts.ap_run_2026_10_08_enter import match_lines, po_absence_reason


def test_empty_receipts_name_the_po_and_the_qty():
    reason = po_absence_reason(
        "59043",
        [{"qty": 99, "uom": "EA", "token": "A-04421-000"}],
    )
    assert reason == (
        "No receipt recorded yet on PO 59043 for 99 EA A-04421-000. Nothing was selected."
    )
    assert "was not found" not in reason


def test_invoiced_qty_match_is_named_instead_of_open_receipts_none():
    job = {"merch": 7171.56, "lines_match": [{"qty": 66, "amount": 7171.56, "uom": "EA"}]}
    receipts = [
        {
            "id": 24982,
            "qty": 66,
            "uom": "EA-Each",
            "extended": 7171.56,
            "part": "A-04421-000",
            "open": False,
            "invoiced": True,
            "invoiced_bill": {"id": 10491, "text": "18564"},
        }
    ]
    ids, ppv, reason = match_lines(job, receipts)
    assert ids == []
    assert ppv == 0.0
    assert "24982" in reason
    assert "18564" in reason
    assert "Already invoiced" in reason
    assert "was not found" not in reason
