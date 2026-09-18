"""NOTE-40 Transfer AP dry-run: payload shape only. No live writes."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.rules import SHAWN_MCKIBBEN, TRANSFER_AP_BATCH_NAME  # noqa: E402
from ap_clerk.transfer_ap import (  # noqa: E402
    TRANSFER_AP_PRIOR_ID_HINT,
    build_over_ppv_transfer_ap_payload,
    find_transfer_ap_batch,
    over_ppv_hold_comment,
    payload_select_receipts_keys,
)
from legacy_wire_0917 import apply_over_ppv_transfer_ap  # noqa: E402


FAKE_INVOICE_ID = 99999


def test_find_transfer_ap_uses_name_not_hint():
    missing = find_transfer_ap_batch(
        [{"id": 717, "values": {"AP_Invoice_Batch_ID": "API Agent - 9/17/26 Legacy Wire"}}]
    )
    assert missing["found"] is False
    assert missing["invent"] is False
    assert missing["hint_ignored"] == TRANSFER_AP_PRIOR_ID_HINT == 375

    found = find_transfer_ap_batch(
        [
            {"id": 720, "values": {"AP_Invoice_Batch_ID": "API Agent - 9/17/26 Gas & Supply"}},
            {"id": 401, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}},
        ]
    )
    assert found == {"found": True, "id": 401, "name": "TRANSFER AP", "invent": False}
    assert found["id"] != TRANSFER_AP_PRIOR_ID_HINT or found["name"].casefold() == TRANSFER_AP_BATCH_NAME.casefold()


def test_build_payload_shape_tags_shawn_and_has_no_receipts():
    comment = over_ppv_hold_comment(
        invoice_number="DRY-INV-1",
        po="59000",
        pdf_amount=88.0,
        vendor="Gas and Supply",
    )
    found = find_transfer_ap_batch(
        [{"id": 401, "values": {"AP_Invoice_Batch_ID": "Transfer AP"}}]
    )
    payload = build_over_ppv_transfer_ap_payload(
        kimco_id=FAKE_INVOICE_ID,
        batch_id=int(found["id"]),
        comment=comment,
    )
    assert payload["state"] == "Modified"
    assert payload["id"] == FAKE_INVOICE_ID
    assert payload["values"]["AP_Invoice_Batch"] == {"id": 401}
    assert payload["values"]["Comments"] == comment
    assert SHAWN_MCKIBBEN in comment
    assert "NOT selected" in comment
    assert "price-does-not-match" in comment
    assert payload_select_receipts_keys(payload) == []
    assert "lists" not in payload
    assert "Receipt" not in payload["values"]


def test_apply_over_ppv_dry_run_does_not_put():
    class _NoWrite:
        def list_items(self, name):
            assert name == "ap_batches"
            return [{"id": 401, "values": {"AP_Invoice_Batch_ID": "TRANSFER AP"}}]

        def update(self, *args, **kwargs):
            raise AssertionError("dry_run must not PUT/PATCH")

        def get_item(self, *args, **kwargs):
            raise AssertionError("dry_run must not GET an invoice after write")

    comment = over_ppv_hold_comment(
        invoice_number="DRY-INV-1", po="59000", pdf_amount=12.5, vendor="Legacy Wire"
    )
    out = apply_over_ppv_transfer_ap(
        _NoWrite(), kimco_id=FAKE_INVOICE_ID, comment=comment, dry_run=True
    )
    assert out["status"] == "dry-run"
    assert out["writes"] is False
    assert out["put"] is None
    assert out["invent"] is False
    assert out["batch_id"] == 401
    assert out["batch_name"] == "TRANSFER AP"
    assert out["payload"]["values"]["AP_Invoice_Batch"]["id"] == 401
    assert SHAWN_MCKIBBEN in out["payload"]["values"]["Comments"]
    assert "NOT selected" in out["payload"]["values"]["Comments"]
    assert payload_select_receipts_keys(out["payload"]) == []
