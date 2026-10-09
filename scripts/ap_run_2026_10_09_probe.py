"""Read-only live probe for the 2026-10-09 AP run. One sign-in. No writes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.auth import load_credentials, resolve_target
from ap_clerk.kimco import KimcoClient, KimcoError, comment_author_from_access_token
from ap_clerk.rules import lookup_id, lookup_text
from scripts.ap_run_2026_10_08_enter import load_open_receipts

OUT = ROOT / "runs" / "ap-run-2026-10-09" / "probe.json"
POS = {
    "59320",
    "59013",
    "59199",
    "59012",
    "59149",
    "59134",
    "59283",
    "59303",
    "59275",
    "59255",
    "59065",
    "59111",
    "59340",
    "59341",
    "59241",
    "59174",
    "59282",
    "59271",
    "59356",
}
WANTED_VENDORS = {292, 304, 333, 208, 137, 71, 1383, 331, 1}


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


def sample_of(record: dict) -> dict:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        lines.append(
            {
                "item": lookup_text(vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")),
                "item_id": lookup_id(vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")),
                "gl": lookup_text(vals.get("Purchase_GL_Account")),
                "gl_id": lookup_id(vals.get("Purchase_GL_Account")),
                "desc": vals.get("Misc_Description"),
                "qty": vals.get("Quantity"),
                "price": vals.get("Unit_Price"),
                "ext": vals.get("Extended_Amount"),
            }
        )
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append({"amount": vals.get("Tax_Amount"), "code": lookup_text(vals.get("Tax_Code") or vals.get("AP_Tax_Code"))})
    return {
        "id": record.get("id"),
        "number": values.get("Invoice_Number"),
        "vendor": lookup_text(values.get("Vendor")),
        "vendor_id": lookup_id(values.get("Vendor")),
        "posted": values.get("Posted"),
        "type": values.get("Invoice_Type"),
        "terms_id": lookup_id(values.get("Terms_Code")),
        "terms": lookup_text(values.get("Terms_Code")),
        "remit_id": lookup_id(values.get("Remit_To_Address")),
        "amount": values.get("Invoice_Verification_Amount"),
        "lines": lines,
        "taxes": taxes,
    }


def main() -> None:
    client = login()
    freepoint = sample_of(client.get_item("ap_invoices", 10118))
    print("freepoint", json.dumps(freepoint)[:800], flush=True)
    samples = {}
    name_hits = []
    for invoice_id in range(10566, 9600, -1):
        try:
            record = client.get_item("ap_invoices", invoice_id)
        except KimcoError:
            continue
        values = record.get("values") or {}
        vendor = lookup_text(values.get("Vendor")) or ""
        vid = lookup_id(values.get("Vendor"))
        low = vendor.lower()
        if any(token in low for token in ("phoenix", "pittsburg", "freepoint", "aft ", "aft industries", "arrow plat")):
            name_hits.append({"id": invoice_id, "vendor": vendor, "vendor_id": vid, "number": values.get("Invoice_Number"), "posted": values.get("Posted")})
            print("name", invoice_id, vendor, vid, values.get("Invoice_Number"), flush=True)
        if vid in WANTED_VENDORS and vid not in samples and values.get("Posted") not in (None, "", False):
            if lookup_id(values.get("Remit_To_Address")) and lookup_id(values.get("Terms_Code")):
                samples[vid] = sample_of(record)
                print("sample", vid, samples[vid]["number"], samples[vid]["terms"], flush=True)
        if len(samples) >= len(WANTED_VENDORS) and len(name_hits) >= 6 and invoice_id < 10200:
            break
    receipts = load_open_receipts(client, POS)
    compact = {}
    for po, facts in receipts.items():
        compact[po] = [
            {
                "id": row.get("id"),
                "po_id": row.get("po_id"),
                "qty": row.get("qty"),
                "uom": row.get("uom"),
                "extended": row.get("extended"),
                "part": row.get("part"),
                "open": row.get("open"),
                "placeholder": row.get("placeholder"),
                "invoiced_bill": row.get("invoiced_bill"),
            }
            for row in facts
        ]
        print("PO", po, [(row.get("id"), row.get("qty"), row.get("uom"), row.get("extended"), row.get("open"), row.get("part")) for row in facts], flush=True)
    OUT.write_text(json.dumps({"freepoint": freepoint, "samples": samples, "names": name_hits, "receipts": compact}, indent=2, default=str))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
