"""Finish AQPC 10938 (false JMOR 4779 dup) and 10939 leftover receipts.

No Intuit login. No Mail.Send. invent=false. Does not recreate 10007–10030.
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

from aqpc_batch711_plus5 import (  # noqa: E402
    BATCH_ID,
    BATCH_NAME,
    format_receipts,
    write_kyle_sheet,
)
from aqpc_batch711_plus5_20 import quality_row, try_finish_receipts  # noqa: E402
from aqpc_batch711_plus5_25 import (  # noqa: E402
    HOLD_PDF_AMOUNTS,
    KNOWN_TWENTY,
    find_aqpc_payment_requests,
    prior_rows_from_sheet,
)
from aqpc_plus4 import bill_from_message, live_get_proof, summarize_parse  # noqa: E402
from ap_clerk.auth import load_credentials  # noqa: E402
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter  # noqa: E402
from ap_clerk.graph import ALLOWED_MAILBOX, is_already_flagged  # noqa: E402
from ap_clerk.kimco import KimcoClient  # noqa: E402
from ap_clerk.rules import normalize_receipt  # noqa: E402

REPORT = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-25.xlsx"
ALSO = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.xlsx"
SIDECAR_IN = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-25.json"
WANTED_10938 = "10938"
RECEIPTS_10939 = [23280, 23281, 23282]


def _subject_inv(subject: str) -> str:
    import re

    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials(target="live")
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2
    graph = _optional_graph_client()
    if graph is None:
        print("Graph authenticate failed", flush=True)
        return 2
    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    prior_sidecars = json.loads(SIDECAR_IN.read_text()) if SIDECAR_IN.exists() else {}
    messages = find_aqpc_payment_requests(graph)
    msg_10938 = None
    for msg in messages:
        if _subject_inv(str(msg.get("subject") or "")) == WANTED_10938:
            msg_10938 = msg
            break
    if msg_10938 is None:
        print("10938 message not found", flush=True)
        return 2
    print(
        f"10938 flagged={is_already_flagged(msg_10938)} "
        f"cats={msg_10938.get('categories')}",
        flush=True,
    )

    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    invoice = bill_from_message(graph, msg_10938, pdf_dir)
    parsed = summarize_parse(invoice)
    print(json.dumps({"parsed_10938": parsed}, indent=2, default=str), flush=True)
    enter_rows = run_enter(
        client,
        [invoice],
        batch_name=BATCH_NAME,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    _print_summary(enter_rows)

    print("Selecting 10939 leftover receipts 23280/23281/23282", flush=True)
    select_10939 = client.try_select_receipts(10029, RECEIPTS_10939)
    print(f"10939 select={select_10939}", flush=True)

    receipts = [normalize_receipt(item) for item in client.list_items("receipts")]
    proof_10938 = {}
    finish_10938 = None
    row_10938 = enter_rows[0]
    kid_10938 = row_10938.get("KIMCO id")
    if kid_10938 not in (None, "", 4779):
        proof_10938 = live_get_proof(client, int(kid_10938))
        if len(proof_10938.get("receipt_lines") or []) < 2:
            finish_10938 = try_finish_receipts(
                client,
                kimco_id=int(kid_10938),
                parsed=invoice,
                receipts=receipts,
            )
            proof_10938 = finish_10938.get("after") or live_get_proof(client, int(kid_10938))
        row_10938 = quality_row(
            graph,
            parsed=invoice,
            enter_row=row_10938,
            proof=proof_10938,
            finish=finish_10938,
        )

    proof_10939 = live_get_proof(client, 10029)
    parsed_10939 = None
    for p in prior_sidecars.get("parsed") or []:
        if str(p.get("invoice_number")) == "10939":
            parsed_10939 = p
            break
    old_10939 = None
    for r in prior_sidecars.get("rows") or []:
        if str(r.get("Invoice #")) == "10939":
            old_10939 = r
            break
    row_10939 = quality_row(
        graph,
        parsed=parsed_10939 or {},
        enter_row=old_10939 or {
            "Vendor": "American Quality Powder Coating",
            "Invoice #": "10939",
            "date": "2026-08-24",
            "PO": "58998",
            "Amount": 45.0,
            "KIMCO id": 10029,
            "Batch": f"{BATCH_NAME} ({BATCH_ID})",
            "Fees and surcharges": "none",
            "PPV": "none",
        },
        proof=proof_10939,
        finish={"wanted": RECEIPTS_10939, "status": select_10939},
    )

    prior = prior_rows_from_sheet()
    known_rows: list[dict[str, Any]] = []
    known_gets: dict[str, Any] = {}
    for spec in KNOWN_TWENTY:
        kid = spec["kimco_id"]
        inv = spec["invoice"]
        got = live_get_proof(client, kid)
        known_gets[str(kid)] = got
        old = prior.get(inv) or {}
        row = dict(old) if old else {
            "Vendor": "American Quality Powder Coating",
            "Invoice #": inv,
            "KIMCO id": kid,
            "Batch": f"{BATCH_NAME} ({BATCH_ID})",
        }
        row["Receipts"] = format_receipts(got) or old.get("Receipts") or ""
        row["Attach"] = "attached" if got.get("attachments") else old.get("Attach") or ""
        row["Attach status"] = row["Attach"]
        row["Fees"] = old.get("Fees") or old.get("Fees and surcharges") or "none"
        row["Fees and surcharges"] = row["Fees"]
        row["PPV"] = old.get("PPV") or "none"
        row["Flag in Outlook"] = "Yes"
        row["Notes"] = ""
        if kid in HOLD_PDF_AMOUNTS:
            row["Amount"] = HOLD_PDF_AMOUNTS[kid]
            row["Result"] = "HOLD"
            row["Flag status"] = "entered-with-issues"
        known_rows.append(row)

    # New five from prior sidecar, then overlay 10938/10939 finishes.
    new_by_inv = {}
    for r in prior_sidecars.get("rows") or []:
        inv = str(r.get("Invoice #") or "")
        if inv in {"10946", "10945", "10939", "10938", "10934"}:
            new_by_inv[inv] = dict(r)
    new_by_inv["10938"] = row_10938
    new_by_inv["10939"] = row_10939
    for inv in ("10946", "10945", "10934"):
        kid = new_by_inv[inv].get("KIMCO id")
        if kid not in (None, ""):
            got = live_get_proof(client, int(kid))
            new_by_inv[inv]["Receipts"] = format_receipts(got)
            new_by_inv[inv]["Attach"] = "attached" if got.get("attachments") else new_by_inv[inv].get("Attach")
            new_by_inv[inv]["Attach status"] = new_by_inv[inv]["Attach"]

    sheet_new = [new_by_inv[k] for k in ("10946", "10945", "10939", "10938", "10934")]
    all_rows = known_rows + sheet_new
    write_kyle_sheet(REPORT, all_rows)
    write_kyle_sheet(ALSO, all_rows)

    sidecar = dict(prior_sidecars)
    sidecar.update(
        {
            "proof": "aqpc-batch711-25",
            "invent": False,
            "mail_send": False,
            "treyce_emailed": False,
            "false_dup_4779": live_get_proof(client, 4779),
            "finish_10938": {
                "enter": enter_rows,
                "proof": proof_10938,
                "finish": {k: v for k, v in (finish_10938 or {}).items() if k != "after"},
            },
            "finish_10939": {"select": select_10939, "proof": proof_10939},
            "new_gets": {
                **(prior_sidecars.get("new_gets") or {}),
                str(kid_10938): proof_10938,
                "10029": proof_10939,
            },
            "rows": all_rows,
            "report": str(REPORT),
        }
    )
    REPORT.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    ALSO.with_suffix(".json").write_text(json.dumps(sidecar, indent=2, default=str) + "\n")
    success = sum(1 for r in sheet_new if r.get("Result") == "Success")
    hold = sum(1 for r in sheet_new if r.get("Result") == "HOLD")
    all_success = sum(1 for r in all_rows if r.get("Result") == "Success")
    all_hold = sum(1 for r in all_rows if r.get("Result") == "HOLD")
    print(f"Wrote {REPORT}", flush=True)
    print(f"NEW five: Success={success} HOLD={hold}", flush=True)
    print(f"ALL 25: Success={all_success} HOLD={all_hold}", flush=True)
    print(json.dumps({"new_five": sheet_new}, indent=2, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
