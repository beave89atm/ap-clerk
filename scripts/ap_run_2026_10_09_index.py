"""One live sign-in. Read invoice numbers and batch names. No writes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.kimco import KimcoClient, KimcoError, comment_author_from_access_token
from ap_clerk.rules import invoice_number_key, lookup_text

OUT = ROOT / "runs" / "ap-run-2026-10-09" / "kimco-index.json"


def main() -> None:
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error or "Live credentials missing")
    try:
        client = KimcoClient.authenticate(
            creds.instance_url,
            creds.key or "",
            creds.password or "",
            target="live",
        )
    except KimcoError as exc:
        raise SystemExit("KIMCO sign-in failed. Not retrying the password.") from exc
    author = comment_author_from_access_token(client.access_token)
    if int(author.get("id") or 0) != 175 or str(author.get("name") or "") != "API Agent":
        raise SystemExit("Aborting. Token author is not API Agent user 175.")
    print("Sign-in 1 is API Agent user 175", flush=True)
    invoices = client.list_items("ap_invoices", fields="Invoice_Number,Vendor")
    batches = client.list_items("ap_batches", fields="Description,Status,Batch_Owner")
    compact = []
    for item in invoices:
        values = item.get("values") or {}
        vendor = values.get("Vendor")
        compact.append(
            {
                "id": item.get("id"),
                "number": values.get("Invoice_Number"),
                "key": invoice_number_key(values.get("Invoice_Number")),
                "vendor_id": vendor.get("id") if isinstance(vendor, dict) else None,
                "vendor": lookup_text(vendor) if isinstance(vendor, dict) else vendor,
            }
        )
    batch_rows = []
    for item in batches:
        values = item.get("values") or {}
        batch_rows.append(
            {
                "id": item.get("id"),
                "name": values.get("Description") or values.get("AP_Invoice_Batch_ID"),
                "status": values.get("Status"),
            }
        )
    named = [row for row in batch_rows if "API Agent - 10/9" in str(row.get("name") or "") or "API Agent - 10/8" in str(row.get("name") or "")]
    ids = [int(row["id"]) for row in compact]
    OUT.write_text(json.dumps({"invoices": compact, "named_batches": named, "batch_count": len(batch_rows)}, indent=2))
    print(json.dumps({"invoices": len(compact), "max_id": max(ids), "batches": len(batch_rows), "named": named}))


if __name__ == "__main__":
    main()
