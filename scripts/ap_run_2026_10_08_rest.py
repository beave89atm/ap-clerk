"""Enter the remaining oldest October invoices, up to 25 new headers.

Does not post or close the batch. One sign-in, then at most one re-sign-in.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.cli import _find_or_create_batch
from ap_clerk.rules import lookup_id, lookup_text
from scripts.ap_run_2026_10_08_enter import (
    BATCH_NAME,
    CAP,
    MIN_NEW_ID,
    OUT,
    QC,
    SRC,
    enter_one,
    load_open_receipts,
    sample_for,
    save,
)
from scripts.ap_run_2026_10_08_filemail import file_one
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_missed_entry_2026_10_07 import totals
from ap_clerk.graph import GraphClient, load_graph_credentials
from ap_clerk.kimco import KimcoClient, KimcoError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("ap-rest-1008")

REMAINING = 5


def write_pages(src: Path, dest: Path, indexes: list[int]) -> None:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for index in indexes:
        writer.add_page(reader.pages[index])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)


def prepare() -> None:
    SRC.mkdir(parents=True, exist_ok=True)
    base = Path("/tmp/ap-run-1008")
    nxt = Path("/tmp/ap-next")
    shutil.copyfile(base / "20261001T083342Z-1.pdf", SRC / "avex-3652.pdf")
    write_pages(base / "20261001T145540Z-1.pdf", SRC / "kimco-2390.pdf", [0])
    shutil.copyfile(nxt / "2026-10-03T001800Z-1.pdf", SRC / "unifirst-2810822429.pdf")
    shutil.copyfile(nxt / "2026-10-03T062834Z-1.pdf", SRC / "ryerson-9307130416.pdf")
    shutil.copyfile(nxt / "2026-10-05T115243Z-1.pdf", SRC / "legacy-104085.pdf")
    shutil.copyfile(nxt / "2026-10-05T121658Z-1.pdf", SRC / "legacy-104086.pdf")
    shutil.copyfile(nxt / "2026-10-05T142653Z-1.pdf", SRC / "legacy-104087.pdf")


def display_name(values: dict[str, Any]) -> str:
    raw = values.get("Vendor_$_Display_Name")
    if isinstance(raw, str):
        return raw
    return lookup_text(raw) or ""


def find_names(client: KimcoClient) -> dict[str, list[int]]:
    rows = client.list_items("ap_invoices", fields="Invoice_Number,Vendor_$_Display_Name")
    found = {"avex": [], "kimco": [], "stella": []}
    for row in rows:
        values = row.get("values") or {}
        name = display_name(values).upper()
        if "AVEX" in name:
            found["avex"].append(int(row["id"]))
        if "KIMCO HOLD" in name:
            found["kimco"].append(int(row["id"]))
        if "STELLA" in name:
            found["stella"].append(int(row["id"]))
    for key, ids in found.items():
        LOGGER.info("Name %s hits %s", key, len(ids))
    return found


def sample_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    values = record.get("values") or {}
    terms_id = lookup_id(values.get("Terms_Code"))
    remit_id = lookup_id(values.get("Remit_To_Address"))
    if not terms_id or not remit_id:
        return None
    return {
        "terms_id": terms_id,
        "terms": lookup_text(values.get("Terms_Code")),
        "remit_id": remit_id,
        "invoice_id": record.get("id"),
    }


def misc_item(record: dict[str, Any]) -> dict[str, Any] | None:
    lists = record.get("lists") or {}
    lines = lists.get("APInvoiceLine") or []
    if len(lines) != 1:
        return None
    values = lines[0].get("values") or {}
    if values.get("Receipt"):
        return None
    item = values.get("MFG_Miscellaneous_Item") or {}
    item_id = lookup_id(item)
    if not item_id:
        return None
    gl = lookup_id(values.get("Purchase_GL_Account"))
    return {"item_id": int(item_id), "gl_id": int(gl) if gl else None, "description": lookup_text(item)}


def newest_misc(client: KimcoClient, invoice_ids: list[int]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int | None]:
    vendor_id = None
    sample = None
    coded = None
    for invoice_id in sorted(invoice_ids, reverse=True)[:15]:
        record = client.get_item("ap_invoices", invoice_id)
        values = record.get("values") or {}
        vendor_id = vendor_id or lookup_id(values.get("Vendor"))
        sample = sample or sample_from_record(record)
        posted = values.get("Posted") in (True, "true", 1, "1")
        if posted and coded is None:
            item = misc_item(record)
            if item:
                coded = item
                LOGGER.info("Posted misc sample bill %s item %s", invoice_id, item)
    return sample, coded, int(vendor_id) if vendor_id else None


def service_job(key: str, vendor_id: int, coded: dict[str, Any] | None) -> dict[str, Any]:
    if key == "avex":
        job: dict[str, Any] = {
            "number": "3652",
            "vendor": "AVEX Installations",
            "vendor_id": vendor_id,
            "day": date(2026, 10, 1),
            "amount": 162.34,
            "kind": "misc",
            "pdf": "avex-3652.pdf",
            "received": "2026-10-01T08:33:42Z",
            "po": "",
            "owner": "treyce",
            "due_on_receipt": True,
            "tax": 12.37,
            "taxable": 149.97,
            "tax_rate": 8.25,
        }
        if coded:
            line = {
                "item_id": coded["item_id"],
                "description": "Monitoring fee",
                "qty": 1,
                "unit_price": 149.97,
            }
            if coded.get("gl_id"):
                line["gl_id"] = coded["gl_id"]
            job["lines"] = [line]
            job["note"] = (
                "AP Clerk: AVEX Installations invoice 3652 is entered and is not posted. "
                "The PDF total is $162.34. There is no purchase order. "
                "The merchandise is coded like the vendor's posted bill. "
                "Printed sales tax of $12.37 is on the Taxes tab."
            )
        else:
            job["lines"] = []
            job["hold_without_lines"] = (
                "AVEX is in KIMCO, but there is no posted miscellaneous bill to copy. "
                "The lines were not guessed."
            )
        return job
    job = {
        "number": "2390",
        "vendor": "KIMCO Holdings",
        "vendor_id": vendor_id,
        "day": date(2026, 10, 1),
        "amount": 9800.00,
        "kind": "misc",
        "pdf": "kimco-2390.pdf",
        "received": "2026-10-01T14:55:40Z",
        "po": "",
        "owner": "treyce",
    }
    if coded:
        line = {
            "item_id": coded["item_id"],
            "description": "KIMCO software users, 11/1/2026 through 1/31/2027",
            "qty": 1,
            "unit_price": 9800.00,
        }
        if coded.get("gl_id"):
            line["gl_id"] = coded["gl_id"]
        job["lines"] = [line]
        job["note"] = (
            "AP Clerk: KIMCO Holdings invoice 2390 is entered and is not posted. "
            "The PDF total is $9,800.00. There is no purchase order and the printed tax is $0.00. "
            "The line is coded like the vendor's posted bill."
        )
    else:
        job["lines"] = []
        job["hold_without_lines"] = (
            "KIMCO Holdings is in KIMCO, but there is no posted miscellaneous bill to copy. "
            "The lines were not guessed."
        )
    return job


def fixed_jobs() -> list[dict[str, Any]]:
    return [
        {
            "number": "2810822429",
            "vendor": "UniFirst",
            "vendor_id": 189,
            "day": date(2026, 10, 2),
            "amount": 928.54,
            "kind": "misc",
            "lines": [
                {"item_id": 28, "description": "Uniforms and aprons", "qty": 1, "unit_price": 608.85},
                {"item_id": 31, "description": "Shop supplies", "qty": 1, "unit_price": 250.47},
            ],
            "tax": 69.22,
            "taxable": 859.32,
            "pdf": "unifirst-2810822429.pdf",
            "received": "2026-10-03T00:18:00Z",
            "po": "",
            "owner": "treyce",
            "note": (
                "AP Clerk: UniFirst invoice 2810822429 is entered and is not posted. "
                "The PDF total is $928.54. There is no KIMCO purchase order. "
                "Uniforms and aprons are one miscellaneous line. "
                "Towels, mats, and shop charges are a second miscellaneous line. "
                "Printed sales tax of $69.22 is on the Taxes tab."
            ),
        },
        {
            "number": "9307130416",
            "vendor": "Ryerson",
            "vendor_id": 152,
            "day": date(2026, 10, 2),
            "amount": 1296.68,
            "kind": "po",
            "po": "59329",
            "merch": 1285.52,
            "fee": 11.16,
            "fee_name": "fuel surcharge",
            "lines_match": [{"qty": 8, "amount": 1285.52}],
            "pdf": "ryerson-9307130416.pdf",
            "received": "2026-10-03T06:28:34Z",
            "owner": "treyce",
        },
        {
            "number": "PS-INV104085",
            "vendor": "Legacy Wire Products",
            "vendor_id": 292,
            "day": date(2026, 10, 5),
            "amount": 2205.72,
            "kind": "po",
            "po": "59283",
            "merch": 1804.00,
            "freight": 401.72,
            "lines_match": [{"qty": 27, "amount": 1107.00}, {"qty": 17, "amount": 697.00}],
            "pdf": "legacy-104085.pdf",
            "received": "2026-10-05T11:52:43Z",
            "owner": "treyce",
        },
        {
            "number": "PS-INV104086",
            "vendor": "Legacy Wire Products",
            "vendor_id": 292,
            "day": date(2026, 10, 5),
            "amount": 501.30,
            "kind": "po",
            "po": "59303",
            "merch": 410.00,
            "freight": 91.30,
            "lines_match": [{"qty": 10, "amount": 410.00}],
            "pdf": "legacy-104086.pdf",
            "received": "2026-10-05T12:16:58Z",
            "owner": "treyce",
        },
        {
            "number": "PS-INV104087",
            "vendor": "Legacy Wire Products",
            "vendor_id": 292,
            "day": date(2026, 10, 5),
            "amount": 1523.45,
            "kind": "po",
            "po": "59320",
            "merch": 1271.00,
            "freight": 252.45,
            "lines_match": [{"qty": 8, "amount": 328.00}, {"qty": 23, "amount": 943.00}],
            "pdf": "legacy-104087.pdf",
            "received": "2026-10-05T14:26:53Z",
            "owner": "treyce",
        },
    ]


def seed_sample(client: KimcoClient, samples: dict[int, dict[str, Any]], vendor_id: int, invoice_id: int) -> None:
    if vendor_id in samples:
        return
    try:
        record = client.get_item("ap_invoices", invoice_id)
    except KimcoError:
        return
    sample = sample_from_record(record)
    if sample and lookup_id((record.get("values") or {}).get("Vendor")) == vendor_id:
        samples[vendor_id] = sample


def main() -> None:
    prepare()
    client = login()
    install_401_guard(client)
    batches = client.list_items("ap_batches")
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    record = client.get_item("ap_batches", int(batch["id"]))
    status = (record.get("values") or {}).get("Status")
    if batch["name"] != BATCH_NAME or status not in (0, "0", None, ""):
        raise SystemExit(f"Refusing batch {batch} status {status}")
    names = find_names(client)
    (OUT / "vendor-search.json").write_text(json.dumps({key: ids[:20] for key, ids in names.items()}))
    jobs: list[dict[str, Any]] = []
    samples: dict[int, dict[str, Any]] = {
        292: {"terms_id": 1, "terms": "Net 30-Net 30", "remit_id": 482},
    }
    skipped = []
    for key in ("avex", "kimco"):
        if not names[key]:
            skipped.append({"vendor": key, "reason": "No KIMCO vendor. Invoice was not created."})
            continue
        sample, coded, vendor_id = newest_misc(client, names[key])
        if not vendor_id or not sample:
            skipped.append({"vendor": key, "reason": "Vendor name matched but remit or terms were missing. Invoice was not created."})
            continue
        samples[vendor_id] = sample
        jobs.append(service_job(key, vendor_id, coded))
    jobs.extend(fixed_jobs())
    seed_sample(client, samples, 189, 10457)
    seed_sample(client, samples, 152, 10470)
    po_numbers = {job["po"] for job in jobs if job.get("po")}
    receipts = load_open_receipts(client, po_numbers)
    for po, facts in receipts.items():
        LOGGER.info(
            "PO %s receipts %s",
            po,
            [(row["id"], row.get("qty"), row.get("uom"), row.get("extended"), row.get("open")) for row in facts],
        )
    from scripts.ap_run_2026_10_08_enter import scan_live_bills

    live = scan_live_bills(client)
    progress = json.loads((OUT / "progress.json").read_text())
    created = sum(1 for row in progress if row.get("result") in {"PASS", "HOLD"} and int(row.get("bill_id") or 0) > MIN_NEW_ID)
    LOGGER.info("Already created %s", created)
    for job in jobs:
        if created >= CAP:
            break
        if created >= 20 + REMAINING:
            break
        row = enter_one(client, job, int(batch["id"]), samples, receipts, live)
        progress.append(row)
        save(progress)
        if row["result"] in {"PASS", "HOLD"} and int(row.get("bill_id") or 0) > MIN_NEW_ID:
            created += 1
            LOGGER.info("Running total %s", created)
    # File only the invoices this step finished.
    creds = load_graph_credentials()
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    from scripts.ap_run_2026_10_08_filemail import AUTOPAY_NAME, ARCHIVE_NAME, child_folder
    import json as _json

    listing = {row["received"]: row for row in _json.loads((OUT / "inbox-listing.json").read_text())["rows"]}
    fort = child_folder(graph, ARCHIVE_NAME)
    days: dict[str, list] = {}
    cache: dict[str, dict] = {}
    moves = _json.loads((OUT / "mail-moves.json").read_text()) if (OUT / "mail-moves.json").exists() else []
    done = {row.get("received") for row in progress if row.get("result") in {"PASS", "HOLD"} and row.get("bill_id")}
    for job in jobs:
        if job["received"] not in done:
            continue
        if any(item.get("received") == job["received"] and item.get("moved") == "y" for item in moves):
            continue
        outcome = file_one(
            graph,
            listing[job["received"]],
            days,
            cache,
            fort,
            ARCHIVE_NAME,
            "Entered in AI",
        )
        moves.append(outcome)
        if outcome.get("moved") == "y":
            for row in progress:
                if row.get("received") == job["received"] and row.get("invoice") == job["number"]:
                    row["email_moved"] = "y"
    (OUT / "mail-moves.json").write_text(_json.dumps(moves, indent=2))
    save(progress)
    (OUT / "rest-skipped.json").write_text(json.dumps(skipped, indent=2))
    final_batch = client.get_item("ap_batches", int(batch["id"]))
    print(json.dumps({
        "created": created,
        "batch_status": (final_batch.get("values") or {}).get("Status"),
        "skipped": skipped,
        "new": [(row["invoice"], row["result"], row["bill_id"]) for row in progress if int(row.get("bill_id") or 0) > 10560],
    }, default=str))


if __name__ == "__main__":
    main()
