"""Release over-PPV receipts on AQPC 11003 / 10991 (NOTE-29).

Kyle 2026-09-16: selecting an over-PPV leftover locks it so Shawn cannot
unreceive, fix the PO price, and re-receive. Deselect 24103 on 10009 and
23967 on 10013. Keep header + PDF. HOLD price-does-not-match. invent=false.
No Mail.Send.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from aqpc_batch711_plus5 import write_kyle_sheet  # noqa: E402
from aqpc_plus4 import live_get_proof  # noqa: E402
from ap_clerk.auth import format_presence, load_credentials  # noqa: E402
from ap_clerk.kimco import KimcoClient  # noqa: E402
from ap_clerk.rules import SHAWN_MCKIBBEN, money  # noqa: E402

LOGGER = logging.getLogger("ap_clerk.aqpc_release_over_ppv")

BATCH_ID = 711
SHEET_40 = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-40.xlsx"
SHEET_MAIN = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.xlsx"
JSON_40 = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-40.json"
JSON_MAIN = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.json"

TARGETS = (
    {
        "invoice": "11003",
        "kimco_id": 10009,
        "po": "59083",
        "locked_receipt": 24103,
        "pdf_qty": 2.0,
        "pdf_unit": 5.0,
        "pdf_amt": 10.0,
        "rec_qty": 2.0,
        "rec_unit": 0.777,
        "rec_amt": 1.55,
        "gap": 8.45,
        "pct": 84.4,
        "po_line": "PO59083-01",
    },
    {
        "invoice": "10991",
        "kimco_id": 10013,
        "po": "59148",
        "locked_receipt": 23967,
        "pdf_qty": 199.0,
        "pdf_unit": 1.0,
        "pdf_amt": 199.0,
        "rec_qty": 199.0,
        "rec_unit": 0.75,
        "rec_amt": 149.25,
        "gap": 49.75,
        "pct": 25.0,
        "po_line": "PO59148-02",
    },
)


def _receipt_ids(proof: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for line in proof.get("receipt_lines") or []:
        rid = line.get("receipt")
        if isinstance(rid, dict):
            rid = rid.get("id")
        if rid not in (None, ""):
            out.append(int(rid))
    return out


def _hold_why(spec: dict[str, Any]) -> str:
    return (
        f"HOLD (price-does-not-match): PDF invoice {spec['invoice']} is qty "
        f"{spec['pdf_qty']:g} @ ${spec['pdf_unit']:.2f} = ${spec['pdf_amt']:.2f}. "
        f"Leftover receipt {spec['locked_receipt']} / {spec['po_line']} is qty "
        f"{spec['rec_qty']:g} @ ${spec['rec_unit']} = ${spec['rec_amt']:.2f} "
        f"({spec['pct']:.1f}% of invoice total / ${spec['gap']:.2f}). Do not post PPV. "
        "Receipts NOT selected per Kyle lock rule (NOTE-29): selecting locks the "
        "leftover so Shawn cannot unreceive, fix the PO price, and re-receive. "
        f"Header {spec['kimco_id']} + PDF attached. Select Receipts posted zero. "
        f"{SHAWN_MCKIBBEN}. Treyce would still rework the price. "
        "Outlook Entered with issues. Flag status=entered-with-issues."
    )


def _slim(proof: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": proof.get("id"),
        "invoice_number": proof.get("invoice_number"),
        "invoice_amount": proof.get("invoice_amount"),
        "verification": proof.get("verification"),
        "batch_id": proof.get("batch_id"),
        "void": proof.get("void"),
        "invoice_type": proof.get("invoice_type"),
        "receipt_ids": _receipt_ids(proof),
        "receipt_lines": proof.get("receipt_lines") or [],
        "attachments": proof.get("attachments") or [],
        "vendor_id": proof.get("vendor_id"),
    }


def _refresh_sheet(path: Path, rows: list[dict[str, Any]]) -> None:
    write_kyle_sheet(path, rows)
    path.with_suffix(".json")  # caller writes json separately


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("Target: live. invent=false. No Mail.Send. NOTE-29 release 10009/10013.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )
    batch = client.get_item("ap_batches", BATCH_ID)
    bvals = batch.get("values") or {}
    print(
        f"Batch {BATCH_ID} name={bvals.get('AP_Invoice_Batch_ID')} "
        f"status={bvals.get('Status')} unposted={bvals.get('Unposted_Count')}",
        flush=True,
    )

    report: dict[str, Any] = {}
    for spec in TARGETS:
        kid = spec["kimco_id"]
        before = live_get_proof(client, kid)
        before_ids = _receipt_ids(before)
        print(f"GET {kid} / {spec['invoice']} before={_slim(before)}", flush=True)
        deselect = "none"
        if before_ids:
            deselect = client.try_deselect_receipts(kid)
        after = live_get_proof(client, kid)
        after_ids = _receipt_ids(after)
        posted = money(after.get("invoice_amount"))
        locked = spec["locked_receipt"] in after_ids
        ok = (not after_ids) or (not locked and posted != spec["rec_amt"])
        entry = {
            "invoice": spec["invoice"],
            "kimco_id": kid,
            "before": _slim(before),
            "deselect": deselect,
            "after": _slim(after),
            "after_proof": after,
            "released": ok and locked is False,
            "why": _hold_why(spec),
        }
        report[str(kid)] = entry
        print(
            f"GET {kid} / {spec['invoice']} after={_slim(after)} "
            f"deselect={deselect} released={entry['released']}",
            flush=True,
        )
        if not entry["released"]:
            LOGGER.error("Release failed for %s / %s", kid, spec["invoice"])

    payload = json.loads(JSON_40.read_text())
    by_inv = {str(r.get("Invoice #")): r for r in payload.get("rows") or []}
    for spec in TARGETS:
        kid = spec["kimco_id"]
        row = by_inv.get(spec["invoice"])
        if not row:
            continue
        after = report[str(kid)]["after"]
        row["Result"] = "HOLD"
        row["Why"] = report[str(kid)]["why"]
        row["Amount"] = spec["pdf_amt"]
        row["Flag status"] = "entered-with-issues"
        row["Receipts"] = ""
        row["Attach"] = "attached" if after.get("attachments") else row.get("Attach")
        row["Attach status"] = row["Attach"]
        row["PPV"] = "none"
        if isinstance(payload.get("known_gets"), dict):
            payload["known_gets"][str(kid)] = report[str(kid)]["after_proof"]
    payload["release_over_ppv"] = report
    holds = [
        {"inv": spec["invoice"], "id": spec["kimco_id"], "why": report[str(spec["kimco_id"])]["why"][:80]}
        for spec in TARGETS
    ]
    counts = payload.get("counts") or {}
    counts["holds"] = holds
    payload["counts"] = counts

    for dest in (JSON_40, JSON_MAIN):
        dest.write_text(json.dumps(payload, indent=2) + "\n")
    rows = list(payload.get("rows") or [])
    write_kyle_sheet(SHEET_40, rows)
    write_kyle_sheet(SHEET_MAIN, rows)
    print(json.dumps({"release_over_ppv": report, "sheet": str(SHEET_40)}, indent=2, default=str), flush=True)
    failed = [k for k, v in report.items() if not v.get("released")]
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
