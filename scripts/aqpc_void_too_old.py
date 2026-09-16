"""Void too-old AQPC headers 10040–10046 on batch 711 (NOTE-28).

Deselect receipts, DELETE/void each header, clear Outlook process
categories, mark the 40-row sheet Voided. invent=false. No Mail.Send.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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
from ap_clerk.cli import _optional_graph_client  # noqa: E402
from ap_clerk.graph import (  # noqa: E402
    ALLOWED_MAILBOX,
    FLAG_CLEARED,
    format_graph_presence,
    has_process_category,
    is_already_flagged,
    message_categories,
)
from ap_clerk.kimco import KimcoClient, KimcoError  # noqa: E402
from ap_clerk.rules import (  # noqa: E402
    AQPC_TOO_OLD_INVOICES,
    AQPC_TOO_OLD_KIMCO_IDS,
)

LOGGER = logging.getLogger("ap_clerk.aqpc_void_too_old")

BATCH_ID = 711
VOID_TARGETS = (
    {"invoice": "10696", "kimco_id": 10040, "date": "2026-06-08"},
    {"invoice": "10523", "kimco_id": 10041, "date": "2026-04-17"},
    {"invoice": "10381", "kimco_id": 10042, "date": "2026-03-10"},
    {"invoice": "9502", "kimco_id": 10043, "date": "2025-06-02"},
    {"invoice": "9498", "kimco_id": 10044, "date": "2025-05-30"},
    {"invoice": "9352", "kimco_id": 10045, "date": "2025-04-23"},
    {"invoice": "9343", "kimco_id": 10046, "date": "2025-04-22"},
)
VOID_WHY = (
    "Voided (too-old / Kyle reverse 2026-09-15). AQPC invoice date before "
    "2026-08-01. Header reversed (deselect receipts + delete/void). Do not "
    "re-enter. NOTE-28."
)
SHEET_40 = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711-40.xlsx"
SHEET_MAIN = ROOT / "runs" / "AP-run-2026-09-15-aqpc-batch711.xlsx"
VENDOR_NEEDLE = "AMERICAN QUALITY POWDER COATING"


def _confirm_get(client: KimcoClient, kimco_id: int) -> dict[str, Any]:
    try:
        proof = live_get_proof(client, kimco_id)
        voided = proof.get("void") is True
        return {
            "status": "voided" if voided else "present",
            "void": proof.get("void"),
            "invoice_number": proof.get("invoice_number"),
            "invoice_amount": proof.get("invoice_amount"),
            "receipts": proof.get("receipt_lines") or [],
        }
    except KimcoError as exc:
        text = str(exc)
        if "HTTP 404" in text:
            return {"status": "gone", "error": "HTTP 404"}
        return {"status": "error", "error": text[:240]}


def _find_invoice_messages(graph, invoice: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        f"New payment request from {VENDOR_NEEDLE} - invoice {invoice}",
        f"{VENDOR_NEEDLE} - invoice {invoice}",
        f"invoice {invoice}",
    ]
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=10)
        except Exception as exc:  # noqa: BLE001 - continue other needles
            LOGGER.info("Graph search %s failed: %s", needle[:40], type(exc).__name__)
            continue
        for msg in hits:
            subject = str(msg.get("subject") or "")
            if VENDOR_NEEDLE not in subject.upper():
                continue
            if not re.search(rf"invoice\s+{re.escape(invoice)}\b", subject, flags=re.I):
                continue
            mid = str(msg.get("id") or "")
            if mid:
                seen[mid] = msg
    return list(seen.values())


def _mark_voided_row(row: dict[str, Any], spec: dict[str, Any], void_result: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["Invoice #"] = spec["invoice"]
    out["date"] = spec["date"] or out.get("date")
    out["KIMCO id"] = spec["kimco_id"]
    out["Result"] = "Voided"
    out["Why"] = VOID_WHY
    out["Flag status"] = "cleared"
    out["Flag in Outlook"] = "No"
    out["outlook"] = void_result.get("outlook") or "cleared"
    out["Notes"] = "voided as too-old"
    out["Receipts"] = ""
    return out


def _refresh_sheet(voids: dict[str, dict[str, Any]]) -> dict[str, Any]:
    sidecar_path = SHEET_40.with_suffix(".json")
    payload = json.loads(sidecar_path.read_text())
    by_inv = {spec["invoice"]: spec for spec in VOID_TARGETS}
    rows = []
    for row in payload.get("rows") or []:
        inv = str(row.get("Invoice #") or "")
        if inv in by_inv:
            rows.append(_mark_voided_row(row, by_inv[inv], voids.get(inv) or {}))
        else:
            rows.append(row)
    enter_rows = []
    for row in payload.get("enter_rows") or []:
        inv = str(row.get("Invoice #") or "")
        if inv in by_inv:
            enter_rows.append(_mark_voided_row(row, by_inv[inv], voids.get(inv) or {}))
        else:
            enter_rows.append(row)
    payload["rows"] = rows
    payload["enter_rows"] = enter_rows
    payload["voided_too_old"] = voids
    payload["invent"] = False
    payload["mail_send"] = False
    write_kyle_sheet(SHEET_40, rows)
    write_kyle_sheet(SHEET_MAIN, rows)
    text = json.dumps(payload, indent=2, default=str) + "\n"
    sidecar_path.write_text(text)
    SHEET_MAIN.with_suffix(".json").write_text(text)
    counts = {
        "Success": sum(1 for r in rows if r.get("Result") == "Success"),
        "HOLD": sum(1 for r in rows if r.get("Result") == "HOLD"),
        "Voided": sum(1 for r in rows if r.get("Result") == "Voided"),
        "other": sum(
            1
            for r in rows
            if r.get("Result") not in {"Success", "HOLD", "Voided"}
        ),
        "rows": len(rows),
    }
    payload["counts"] = counts
    sidecar_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    SHEET_MAIN.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Void too-old AQPC 10040–10046")
    parser.add_argument("--get-only", action="store_true")
    parser.add_argument("--skip-outlook", action="store_true")
    parser.add_argument("--skip-sheet", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print("Target: live. invent=false. No Mail.Send. NOTE-28 void 10040–10046.", flush=True)
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

    graph = None if args.skip_outlook else _optional_graph_client()
    voids: dict[str, dict[str, Any]] = {}
    for spec in VOID_TARGETS:
        kid = spec["kimco_id"]
        inv = spec["invoice"]
        assert inv in AQPC_TOO_OLD_INVOICES
        assert kid in AQPC_TOO_OLD_KIMCO_IDS
        before = _confirm_get(client, kid)
        print(f"GET {kid} / {inv} before={before}", flush=True)
        result: dict[str, Any] = {"invoice": inv, "kimco_id": kid, "before": before}
        if not args.get_only and before.get("status") != "gone":
            result["void"] = client.try_void_invoice(kid)
        result["after"] = _confirm_get(client, kid)
        print(f"GET {kid} / {inv} after={result['after']} void={result.get('void')}", flush=True)

        outlook: list[dict[str, Any]] = []
        if graph is not None:
            messages = _find_invoice_messages(graph, inv)
            for msg in messages:
                mid = str(msg.get("id") or "")
                before_cats = message_categories(msg)
                status = graph.clear_process_categories(ALLOWED_MAILBOX, mid)
                try:
                    after_msg = graph.get_message(
                        ALLOWED_MAILBOX, mid, select="id,subject,categories,flag"
                    )
                except Exception as exc:  # noqa: BLE001
                    after_msg = {"error": type(exc).__name__}
                outlook.append(
                    {
                        "id_present": bool(mid),
                        "subject": str(msg.get("subject") or "")[:120],
                        "before_categories": before_cats,
                        "before_flagged": is_already_flagged(msg),
                        "clear": status,
                        "after_categories": message_categories(after_msg)
                        if isinstance(after_msg, dict)
                        else [],
                        "after_process": has_process_category(after_msg)
                        if isinstance(after_msg, dict)
                        else None,
                    }
                )
        result["outlook"] = outlook
        voids[inv] = result
        print(json.dumps({"invoice": inv, "outlook": outlook}, indent=2, default=str), flush=True)

    if not args.skip_sheet:
        counts = _refresh_sheet(voids)
        print(f"Sheet counts: {counts}", flush=True)
        print(f"Wrote {SHEET_40} and {SHEET_MAIN}", flush=True)

    print(json.dumps({"voids": {k: v.get("after") for k, v in voids.items()}}, indent=2), flush=True)
    bad = [
        inv
        for inv, result in voids.items()
        if (result.get("after") or {}).get("status") not in {"gone", "voided"}
        and not (
            (result.get("after") or {}).get("status") == "present"
            and (result.get("after") or {}).get("void") is True
        )
    ]
    if args.get_only:
        return 0
    if bad:
        print(f"Half-state remaining: {bad}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
