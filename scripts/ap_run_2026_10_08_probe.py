"""Read-only live probe for the 10/8 run. One sign-in. No writes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.kimco import KimcoClient, KimcoError, comment_author_from_access_token
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text
from scripts.sept25_30_attachment_audit import receipt_fact
from scripts.sept25_30_po59081_receipts import child_receipt_ids

OUT = ROOT / "runs" / "ap-run-2026-10-08" / "probe.json"
NUMBERS = [
    "3652",
    "2390",
    "367745",
    "361302",
    "361804",
    "PS-INV104078",
    "PS-INV104079",
    "1474542",
    "1474543",
    "0040483159",
    "0040484366",
    "0040482925",
    "0040484376",
]
POS = {"59334", "59322", "59081"}
VENDORS = {71, 112, 322, 292, 88, 45}


def login() -> KimcoClient:
    creds = load_credentials(target=resolve_target(live_flag=True))
    if not creds.ready:
        raise SystemExit(creds.error or "Live credentials missing")
    try:
        client = KimcoClient.authenticate(creds.instance_url, creds.key or "", creds.password or "", target="live")
    except KimcoError as exc:
        raise SystemExit("KIMCO sign-in failed. Not retrying the password.") from exc
    author = comment_author_from_access_token(client.access_token)
    if int(author.get("id") or 0) != 175 or str(author.get("name") or "") != "API Agent":
        raise SystemExit("Aborting. Token author is not API Agent user 175.")
    print("Sign-in 1 is API Agent user 175", flush=True)
    return client


def main() -> None:
    client = login()
    page = client.request("GET", client._url("ap_invoices"), params={"pageSize": 1, "offset": 0})
    sample = (page.json().get("items") or [{}])[0]
    print("list value keys", sorted((sample.get("values") or {})), flush=True)
    index = json.loads((ROOT / "runs" / "ap-run-2026-10-08" / "kimco-index.json").read_text())
    by_key: dict[str, list[int]] = {}
    for row in index["invoices"]:
        by_key.setdefault(row["key"], []).append(int(row["id"]))
    dupes = {}
    for number in NUMBERS:
        ids = by_key.get(invoice_number_key(number), [])
        detail = []
        for invoice_id in ids[:3]:
            record = client.get_item("ap_invoices", invoice_id)
            values = record.get("values") or {}
            detail.append(
                {
                    "id": invoice_id,
                    "number": values.get("Invoice_Number"),
                    "vendor": lookup_text(values.get("Vendor")),
                    "vendor_id": lookup_id(values.get("Vendor")),
                    "posted": values.get("Posted"),
                    "amount": values.get("Invoice_Verification_Amount"),
                    "batch": lookup_text(values.get("AP_Invoice_Batch")),
                }
            )
        dupes[number] = detail
        print("dup", number, detail, flush=True)
    # Walk recent invoices backwards by high ids for vendor samples.
    samples = {}
    wanted = set(VENDORS)
    names = {}
    for invoice_id in range(10540, 9000, -1):
        if not wanted and len(names) > 30:
            break
        try:
            record = client.get_item("ap_invoices", invoice_id)
        except KimcoError:
            continue
        values = record.get("values") or {}
        vendor = values.get("Vendor")
        vid = lookup_id(vendor)
        text = lookup_text(vendor) or ""
        low = text.lower()
        if any(token in low for token in ("avex", "kimco hold", "stella", "waste connection")):
            names.setdefault(text, invoice_id)
        if vid in wanted and values.get("Posted") not in (None, "", False):
            samples[vid] = {
                "invoice_id": invoice_id,
                "number": values.get("Invoice_Number"),
                "vendor": text,
                "terms_id": lookup_id(values.get("Terms_Code")),
                "terms": lookup_text(values.get("Terms_Code")),
                "remit_id": lookup_id(values.get("Remit_To_Address")),
                "type": values.get("Invoice_Type"),
            }
            wanted.discard(vid)
            print("sample", vid, samples[vid], flush=True)
        if not wanted and invoice_id < 10400:
            break
    print("still need samples", sorted(wanted), "name hits", names, flush=True)
    lines = client.list_items("purchase_lines", fields="Purchase_Order_Number,Purchase_Line_Number")
    print("purchase lines", len(lines), "keys", sorted((lines[0].get("values") or {})) if lines else [], flush=True)
    po_lines = []
    for row in lines:
        values = row.get("values") or {}
        blob = json.dumps(values)
        if any(po in blob for po in POS):
            po_lines.append({"id": row.get("id"), "values": values})
    print("po line hits", len(po_lines), flush=True)
    receipts = []
    for row in po_lines:
        record = client.get_item("purchase_lines", int(row["id"]))
        for receipt_id in child_receipt_ids(record):
            fact = receipt_fact(client.get_item("receipts", receipt_id))
            fact.pop("fields", None)
            fact["po_values"] = row["values"]
            receipts.append(fact)
            print("receipt", fact, flush=True)
    OUT.write_text(json.dumps({"dupes": dupes, "samples": samples, "names": names, "po_lines": po_lines, "receipts": receipts}, indent=2, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
