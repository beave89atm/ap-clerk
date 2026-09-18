"""GET-only NOTE-40 dry-run. No invoice PUT/PATCH/POST. No Mail.Send."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.kimco import KimcoClient, KimcoError  # noqa: E402
from ap_clerk.rules import lookup_id, lookup_text  # noqa: E402
from ap_clerk.transfer_ap import (  # noqa: E402
    TRANSFER_AP_PRIOR_ID_HINT,
    build_over_ppv_transfer_ap_payload,
    find_transfer_ap_batch,
    over_ppv_hold_comment,
    payload_select_receipts_keys,
)
from legacy_wire_0917 import apply_over_ppv_transfer_ap  # noqa: E402

FAKE_INVOICE_ID = 99999


def _comments_shape(item: dict[str, Any]) -> dict[str, Any]:
    vals = item.get("values") if isinstance(item.get("values"), dict) else {}
    comments = (vals or {}).get("Comments")
    mention_keys = sorted(
        k for k in (vals or {}) if "mention" in str(k).lower() or "notif" in str(k).lower() or "tagged" in str(k).lower()
    )
    return {
        "invoice_id": item.get("id"),
        "invoice_number": (vals or {}).get("Invoice_Number"),
        "comments_type": type(comments).__name__,
        "comments_is_str": isinstance(comments, str),
        "comments_len": len(comments) if isinstance(comments, str) else None,
        "comments_preview": (comments[:160] if isinstance(comments, str) else comments),
        "mention_value_keys": mention_keys,
        "batch_id": lookup_id((vals or {}).get("AP_Invoice_Batch")),
        "batch_text": lookup_text((vals or {}).get("AP_Invoice_Batch")),
    }


def main() -> int:
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("NOTE-40 dry-run. GET-only. No Mail.Send. invent=false.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    batches = client.list_items("ap_batches")
    found = find_transfer_ap_batch(batches)
    print(
        json.dumps(
            {
                "transfer_ap_lookup": found,
                "hint_375_used_as_fallback": False,
                "hint_ignored_unless_name_match": TRANSFER_AP_PRIOR_ID_HINT,
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    if not found.get("found"):
        print("Transfer AP name lookup failed. Will not invent 375. Stop.", flush=True)
        return 2

    already: dict[str, Any] | None = None
    for item in client.list_items("ap_invoices"):
        vals = item.get("values") or {}
        bid = lookup_id(vals.get("AP_Invoice_Batch"))
        if bid == found["id"]:
            try:
                rec = client.get_item("ap_invoices", int(item["id"]))
            except KimcoError as exc:
                already = {"get_error": str(exc)[:200], "list_id": item.get("id")}
                break
            already = _comments_shape(rec)
            break
    print(json.dumps({"already_on_transfer_ap_get": already, "writes": False}, indent=2, default=str), flush=True)

    comment = over_ppv_hold_comment(
        invoice_number="DRY-INV-1",
        po="59000",
        pdf_amount=88.0,
        vendor="DRY RUN",
    )
    dry = apply_over_ppv_transfer_ap(
        client,
        kimco_id=FAKE_INVOICE_ID,
        comment=comment,
        dry_run=True,
    )
    if dry.get("writes") or dry.get("put") not in (None, ""):
        print("Refusing: dry_run returned a write. Abort.", flush=True)
        return 2
    payload = dry.get("payload") or build_over_ppv_transfer_ap_payload(
        kimco_id=FAKE_INVOICE_ID,
        batch_id=int(found["id"]),
        comment=comment,
    )
    print(
        json.dumps(
            {
                "dry_run": dry,
                "sample_payload": payload,
                "select_receipts_keys": payload_select_receipts_keys(payload),
                "live_mutations": 0,
                "mail_send": False,
                "invent": False,
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
