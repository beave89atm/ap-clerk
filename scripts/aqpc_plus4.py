"""Enter 4 AQPC payment-request invoices on batch API Agent - 9/15/26 (711).

Prefers unflagged mail after 11002 (11003+). Graph has no 11006+ yet, so the
fourth is the next newest unflagged AQPC not already on KIMCO (10999).
Touches only these 4 emails. No Mail.Send. No Intuit login.
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

from ap_clerk.auth import format_presence, load_credentials
from ap_clerk.browser_pdf import format_intuit_session_presence
from ap_clerk.cli import _optional_graph_client, _print_summary, run_enter
from ap_clerk.graph import ALLOWED_MAILBOX, format_graph_presence, is_already_flagged
from ap_clerk.inbox import _pdfs_from_body_link, sender_address, sender_name
from ap_clerk.kimco import KimcoClient
from ap_clerk.pdf_invoice import parse_invoice_pdf
from ap_clerk.report import write_report
from ap_clerk.rules import invoice_number_key, lookup_id, lookup_text

LOGGER = logging.getLogger("ap_clerk.aqpc_plus4")

PREFERRED = ["11003", "11004", "11005"]
FALLBACK = ["10999"]
ALREADY = {"10917", "10918", "10920", "10921", "11002"}
BATCH_NAME = "API Agent - 9/15/26"
BATCH_ID = 711
VENDOR_NEEDLE = "AMERICAN QUALITY POWDER COATING"


def _subject_inv(subject: str) -> str:
    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "")[:80]


def find_aqpc_messages(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        f"New payment request from {VENDOR_NEEDLE} - invoice {n}"
        for n in [*PREFERRED, *FALLBACK, "11006", "11007", "11008"]
    ]
    for needle in needles:
        for msg in graph.search_messages(ALLOWED_MAILBOX, needle, top=10):
            mid = str(msg.get("id") or "")
            subject = str(msg.get("subject") or "")
            if VENDOR_NEEDLE not in subject.upper():
                continue
            if mid:
                seen[mid] = msg
    return list(seen.values())


def pick_four(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One email per invoice. Prefer 11003+; fill with newest unflagged not already entered."""
    by_inv: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        inv = _subject_inv(str(msg.get("subject") or ""))
        if not inv or inv in ALREADY:
            continue
        if is_already_flagged(msg):
            continue
        by_inv.setdefault(inv, []).append(msg)
    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    for inv in PREFERRED:
        cands = by_inv.get(inv) or []
        if not cands:
            continue
        cands.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
        pick = cands[0]
        pick["_wanted_invoice"] = inv
        chosen.append(pick)
        used.add(inv)
    if len(chosen) < 4:
        extras: list[tuple[str, dict[str, Any]]] = []
        for inv, cands in by_inv.items():
            if inv in used:
                continue
            cands.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
            extras.append((inv, cands[0]))
        extras.sort(key=lambda pair: (0 if pair[0] in FALLBACK else 1, -int(pair[0] or 0)))
        for inv, msg in extras:
            if len(chosen) >= 4:
                break
            msg["_wanted_invoice"] = inv
            chosen.append(msg)
            used.add(inv)
    chosen.sort(key=lambda m: str(m.get("_wanted_invoice") or ""))
    return chosen[:4]


def bill_from_message(graph, message: dict[str, Any], pdf_dir: Path) -> dict[str, Any]:
    subject = str(message.get("subject") or "")
    message_id = str(message.get("id") or "")
    preview = str(message.get("bodyPreview") or "")
    from_name = sender_name(message)
    from_addr = sender_address(message)
    wanted = str(message.get("_wanted_invoice") or _subject_inv(subject))
    pdfs, link_hold = _pdfs_from_body_link(
        graph, ALLOWED_MAILBOX, message_id, preview, subject=subject
    )
    if link_hold or not pdfs:
        return {
            "vendor": from_name or "American Quality Powder Coating",
            "invoice_number": wanted,
            "date": None,
            "po": None,
            "amount": None,
            "hold_reason": "pdf-behind-link",
            "pdf_behind_link": True,
            "pdf_link_url": str((link_hold or {}).get("url") or ""),
            "pdf_link_host": str((link_hold or {}).get("host") or ""),
            "browser_tried": bool((link_hold or {}).get("browser_tried")),
            "browser_failure": str((link_hold or {}).get("browser_failure") or "no-pdf"),
            "download_method": str((link_hold or {}).get("method") or ""),
            "action": "hold",
            "graph_message_id": message_id,
            "subject": subject,
            "receivedDateTime": message.get("receivedDateTime"),
            "from_name": from_name,
            "field_sources": {"invoice_number": "subject"} if wanted else {},
        }
    filename, content = pdfs[0]
    dest = pdf_dir / f"{_safe_filename(str(message.get('receivedDateTime') or '')[:10])}_{_safe_filename(filename)}"
    dest.write_bytes(content)
    parsed = parse_invoice_pdf(dest, subject=subject, from_name=from_name, from_address=from_addr)
    parsed["pdf_path"] = str(dest)
    parsed["graph_message_id"] = message_id
    parsed["subject"] = subject
    parsed["receivedDateTime"] = message.get("receivedDateTime")
    parsed["from_name"] = from_name
    parsed["action"] = "create"
    parsed["id"] = message_id
    parsed["download_method"] = "browser"
    parsed["pdf_bytes"] = len(content)
    if not parsed.get("invoice_number"):
        parsed["invoice_number"] = wanted
        sources = dict(parsed.get("field_sources") or {})
        sources.setdefault("invoice_number", "subject")
        parsed["field_sources"] = sources
    return parsed


def summarize_parse(inv: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice_number": inv.get("invoice_number"),
        "date": inv.get("date"),
        "po": inv.get("po"),
        "amount": inv.get("amount"),
        "lines": inv.get("lines") or [],
        "fees": inv.get("fees") or [],
        "pdf_path": inv.get("pdf_path"),
        "pdf_bytes": inv.get("pdf_bytes"),
        "download_method": inv.get("download_method"),
        "hold_reason": inv.get("hold_reason"),
        "browser_tried": inv.get("browser_tried"),
        "browser_failure": inv.get("browser_failure"),
        "subject": inv.get("subject"),
        "graph_message_id_present": bool(inv.get("graph_message_id")),
    }


def live_get_proof(client: KimcoClient, invoice_id: Any) -> dict[str, Any]:
    if invoice_id in (None, ""):
        return {}
    item = client.get_item("ap_invoices", int(invoice_id))
    vals = item.get("values") or {}
    attachments = []
    try:
        attachments = [str(a.get("name") or a.get("fileName") or "") for a in client.list_attachments(invoice_id)]
    except Exception:  # noqa: BLE001 - proof only
        attachments = []
    lines = []
    for line in item.get("lists", {}).get("APInvoiceLine") or []:
        lv = (line.get("values") if isinstance(line, dict) else None) or {}
        receipt = lv.get("Receipt") or {}
        lines.append(
            {
                "qty": lv.get("Quantity"),
                "unit": lv.get("Unit_Price"),
                "receipt": lookup_id(receipt) or receipt,
                "po_line": lookup_text(lv.get("PO_Item")) or lv.get("PO_Item"),
            }
        )
    lists = item.get("lists") or {}
    charges = []
    for key in ("InvoiceAdditionalCharges", "APInvoiceAdditionalCharge"):
        charges.extend(lists.get(key) or [])
    return {
        "id": item.get("id"),
        "invoice_number": vals.get("Invoice_Number"),
        "vendor_id": lookup_id(vals.get("Vendor")),
        "vendor_text": lookup_text(vals.get("Vendor")),
        "po": lookup_text(vals.get("Purchase_Order")) or vals.get("Purchase_Order"),
        "invoice_type": vals.get("Invoice_Type"),
        "invoice_amount": vals.get("Invoice_Amount"),
        "verification": vals.get("Invoice_Verification_Amount")
        or vals.get("Verification_Total")
        or vals.get("Invoice_Balance"),
        "invoice_date": vals.get("Invoice_Date"),
        "posted": vals.get("Posted"),
        "void": vals.get("Void"),
        "batch_id": lookup_id(vals.get("AP_Invoice_Batch")),
        "batch_text": lookup_text(vals.get("AP_Invoice_Batch")),
        "receipt_lines": lines,
        "fee_count": len(charges),
        "charges": charges,
        "attachments": attachments,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enter 4 AQPC invoices on batch 711")
    parser.add_argument("--parse-only", action="store_true", help="Download+parse only; no KIMCO writes")
    parser.add_argument(
        "--report",
        default=str(ROOT / "runs" / "AP-run-2026-09-15-aqpc-plus4.xlsx"),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print(format_graph_presence(), flush=True)
    print(format_intuit_session_presence(), flush=True)
    print("Target: live", flush=True)
    print("No Intuit login / no storage-state. No Mail.Send.", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    graph = _optional_graph_client()
    if graph is None:
        print("Graph authenticate failed", flush=True)
        return 2

    messages = find_aqpc_messages(graph)
    picked = pick_four(messages)
    print("Picked:", [m.get("_wanted_invoice") for m in picked], flush=True)
    for msg in picked:
        print(
            f"  {msg.get('_wanted_invoice')} recv={msg.get('receivedDateTime')} "
            f"flagged={is_already_flagged(msg)} subj={str(msg.get('subject') or '')[:80]}",
            flush=True,
        )
    if len(picked) != 4:
        print(f"Need exactly 4 AQPC emails; found {len(picked)}", flush=True)
        return 2

    pdf_dir = ROOT / "runs" / "inbox-pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    invoices = [bill_from_message(graph, msg, pdf_dir) for msg in picked]
    parsed = [summarize_parse(inv) for inv in invoices]
    print(json.dumps({"parsed": parsed}, indent=2, default=str), flush=True)
    if args.parse_only:
        return 0

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
    rows = run_enter(
        client,
        invoices,
        batch_name=BATCH_NAME,
        pdf_dir=pdf_dir,
        graph_client=graph,
        mailbox=ALLOWED_MAILBOX,
        flag_outlook=True,
    )
    report_path = Path(args.report)
    write_report(report_path, rows)
    print(f"Wrote {report_path}", flush=True)
    _print_summary(rows)

    gets = {}
    for row in rows:
        kid = row.get("KIMCO id")
        if kid not in (None, ""):
            gets[str(kid)] = live_get_proof(client, kid)

    proof = {
        "proof": "aqpc-guest-plus4",
        "invent": False,
        "mail_send": False,
        "storage_state": False,
        "intuit_login": False,
        "skipped_already_entered": {
            "10917": {"kimco_id": 10003, "why": "do not recreate"},
            "10918": {"kimco_id": 10002, "why": "do not recreate"},
            "10920": {"kimco_id": 10005, "why": "do not recreate"},
            "10921": {"kimco_id": 10004, "why": "do not recreate"},
            "11002": {"kimco_id": 10007, "why": "do not recreate"},
        },
        "chosen": [inv.get("invoice_number") for inv in invoices],
        "chosen_because": (
            "Next unflagged AQPC payment-requests after 11002 are 11003/11004/11005; "
            "Graph has no 11006+. Fourth is 10999 (newest remaining unflagged AQPC not on KIMCO). "
            "11004 had two emails; used the later receivedDateTime. Did not walk unrelated vendors."
        ),
        "batch_name": BATCH_NAME,
        "batch_id": BATCH_ID,
        "parsed": parsed,
        "rows": rows,
        "kimco_gets": gets,
        "treyce_emailed": False,
        "report": str(report_path),
    }
    sidecar = report_path.with_suffix(".json")
    sidecar.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    print(f"Wrote {sidecar}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
