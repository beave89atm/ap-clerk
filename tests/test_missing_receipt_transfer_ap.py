"""NOTE-53: missing_receipt HOLD → Transfer AP (not stay on agent batch)."""

from __future__ import annotations

from ap_clerk.gates import GATE_PRICE, GATE_RECEIPT, RESULT_HOLD, RESULT_SUCCESS
from ap_clerk.quality_v12 import TREYCE_NOTES_V12, classify_exception, note_by_id
from ap_clerk.rules import TRANSFER_AP_BATCH_NAME
from ap_clerk.transfer_ap import (
    TRANSFER_AP_PRIOR_ID_HINT,
    apply_missing_receipt_transfer_ap,
    apply_transfer_ap_batch_move,
    find_transfer_ap_batch,
    should_transfer_ap_missing_receipt,
)


def test_note53_is_registered():
    note = note_by_id("NOTE-53")
    assert note["slug"] == "missing-receipt-hold-transfer-ap"
    assert "Transfer AP" in note["expected"]
    assert "stay-on-current-batch" in note["expected"]
    assert note["never_success"] is True
    assert "NOTE-53" in {n["id"] for n in TREYCE_NOTES_V12}


def test_should_transfer_ap_missing_receipt_only():
    assert should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        category="missing_receipt",
        why="HOLD (receipt): no open receipt leftover",
    )
    assert should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        why="HOLD (receipt): no receipts after second pass slip # / part",
        issue_gate=GATE_RECEIPT,
    )
    assert should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        why="category=missing_receipt; owner=Ruben Perez. no open receipt",
    )
    assert not should_transfer_ap_missing_receipt(
        result=RESULT_SUCCESS,
        category="",
        why="Finished bill",
    )
    assert not should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        category="price_variance",
        why="HOLD (price-does-not-match): over the PPV gate",
        issue_gate=GATE_PRICE,
    )
    assert not should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        category="partial_match",
        why="Selected vs unmatched: matched 1, unmatched 1",
    )
    assert not should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        why="HOLD (receipt): multiple open receipts on the PO differ in qty/cost. Will not guess.",
        issue_gate=GATE_RECEIPT,
    )
    assert not should_transfer_ap_missing_receipt(
        result=RESULT_HOLD,
        category="missing_po",
        why="PO number is missing from this invoice",
    )


def test_classify_missing_receipt_not_missing_po_when_transfer_ap_mentioned():
    assert classify_exception(
        result=RESULT_HOLD,
        why="HOLD (receipt): no receipts after second pass. NOTE-53 missing_receipt → Transfer AP (moved).",
    ) == ("missing_receipt", "Ruben Perez")
    assert classify_exception(
        result=RESULT_HOLD,
        why="PO number is missing from this invoice. Transfer to Transfer AP.",
    ) == ("missing_po", "Shawn McKibben")


def test_find_transfer_ap_batch_never_invents_375():
    found = find_transfer_ap_batch(
        [{"id": 375, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]
    )
    assert found == {"found": True, "id": 375, "name": "TRANSFER AP", "invent": False}

    titled = find_transfer_ap_batch(
        [{"id": 401, "values": {"AP_Invoice_Batch_ID": "Transfer AP"}}]
    )
    assert titled["found"] is True
    assert titled["id"] == 401
    assert titled["invent"] is False

    missing = find_transfer_ap_batch(
        [{"id": 722, "values": {"AP_Invoice_Batch_ID": "API Agent - 9/21/26 O'Neal"}}]
    )
    assert missing["found"] is False
    assert missing["id"] is None
    assert missing["invent"] is False
    assert missing["hint_ignored"] == TRANSFER_AP_PRIOR_ID_HINT == 375


def test_apply_missing_receipt_transfer_ap_puts_batch_keeps_comments():
    class _Fake:
        def list_items(self, _name):
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]

        def update(self, _svc, _kid, payload):
            self.payload = payload
            return {}, 200, ""

        def get_item(self, _svc, kid):
            before_batch = {"id": 722, "text": "API Agent - 9/21/26 O'Neal"}
            if getattr(self, "payload", None):
                before_batch = {"id": 375, "text": "TRANSFER AP"}
            return {
                "id": kid,
                "values": {
                    "Comments": "existing header comment stays",
                    "AP_Invoice_Batch": before_batch,
                },
                "lists": {"Comments_1": [{"id": 940}]},
            }

    fake = _Fake()
    out = apply_missing_receipt_transfer_ap(fake, kimco_id=10160)
    assert out["status"] == "moved"
    assert out["batch_id"] == 375
    assert out["batch_name"] == "TRANSFER AP"
    assert out["invent"] is False
    assert "Comments" not in fake.payload["values"]
    assert fake.payload["values"]["AP_Invoice_Batch"] == {"id": 375}
    assert out["comment_written"] is False


def test_apply_missing_receipt_skips_when_already_on_transfer_ap():
    class _Fake:
        def list_items(self, _name):
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": TRANSFER_AP_BATCH_NAME}}]

        def update(self, *_a, **_k):
            raise AssertionError("must not PUT when already on Transfer AP")

        def get_item(self, _svc, kid):
            return {
                "id": kid,
                "values": {"AP_Invoice_Batch": {"id": 375, "text": "TRANSFER AP"}},
            }

    out = apply_missing_receipt_transfer_ap(_Fake(), kimco_id=10162)
    assert out["status"] == "already-on-transfer-ap"
    assert out["batch_id"] == 375
    assert out["invent"] is False


def test_apply_transfer_ap_does_not_invent_when_batch_missing():
    class _Fake:
        def list_items(self, _name):
            return [{"id": 722, "values": {"AP_Invoice_Batch_ID": "API Agent - 9/21/26 O'Neal"}}]

        def update(self, *_a, **_k):
            raise AssertionError("must not PUT without a Transfer AP lookup")

        def get_item(self, *_a, **_k):
            return {}

    out = apply_transfer_ap_batch_move(_Fake(), kimco_id=10160)
    assert out["status"] == "batch-not-found"
    assert out["invent"] is False
    assert out["hint_ignored"] == 375


def test_process_invoice_missing_receipt_moves_batch():
    from ap_clerk.cli import _process_invoice

    class _Fake:
        target = "live"
        created = []
        updates = []

        def create(self, service, values):
            self.created.append(values)
            return 10160, {"id": 10160, "values": values}, 200, ""

        def list_items(self, name):
            assert name == "ap_batches"
            return [{"id": 375, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]

        def update(self, service, kid, payload):
            self.updates.append((service, kid, payload))
            return {}, 200, ""

        def get_item(self, service, item_id):
            batch = {"id": 722, "text": "API Agent - 9/21/26 O'Neal"}
            if self.updates:
                batch = {"id": 375, "text": "TRANSFER AP"}
            return {
                "id": item_id,
                "values": {
                    "Remit_To_Address": {"id": 1, "text": "remit"},
                    "Terms_Code": {"id": 2, "text": "Net 30"},
                    "Vendor": {"id": 137, "text": "O'Neal Steel"},
                    "AP_Invoice_Batch": batch,
                },
            }

        def try_official_attach(self, *args, **kwargs):
            return "attached"

        def try_select_receipts(self, *args, **kwargs):
            return "held-unfinished"

        def try_post_fees(self, *args, **kwargs):
            return "none"

    client = _Fake()
    row = _process_invoice(
        client,
        {
            "vendor": "O'Neal Steel",
            "invoice_number": "15469453",
            "date": "2026-09-15",
            "po": "59000",
            "amount": 100.0,
            "lines": [{"part": "PLATE", "qty": 1}],
        },
        batch={"id": 722},
        batch_label="API Agent - 9/21/26 O'Neal (722)",
        invoice_by_number={},
        vendor_samples=[
            {"vendor_id": 137, "vendor_text": "O'Neal Steel", "invoice_id": 100, "po_text": ""}
        ],
        po_index={"59000": {"id": 3, "text": "59000-ONEAL", "vendor_id": 137, "lines": []}},
        receipts=[{"slip": "OTHER", "qty": 1, "part": "DIFFERENT", "po_line": 1}],
        pdf_dir=None,
        flag_outlook=False,
    )
    assert row["Result"] == RESULT_HOLD
    assert row["Result"] != RESULT_SUCCESS
    assert "NOTE-53" in row["Why"]
    assert row["Batch"] == "Transfer AP (375)"
    assert client.updates
    assert client.updates[0][2]["values"]["AP_Invoice_Batch"] == {"id": 375}
    assert "Comments" not in client.updates[0][2]["values"]


def test_apply_missing_receipt_survives_client_without_list_items():
    class _Bare:
        def get_item(self, *_a, **_k):
            return {}

    out = apply_missing_receipt_transfer_ap(_Bare(), kimco_id=10160)
    assert out["status"] == "batch-list-failed"
    assert out["invent"] is False
