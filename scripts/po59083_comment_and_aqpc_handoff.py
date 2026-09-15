"""Verify 10010 $2600, try PO 59083 @Shawn comment, list next AQPC.

No new AP headers. No void. No treyce@ email. invent=false.
"""

from __future__ import annotations

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
from ap_clerk.cli import _optional_graph_client
from ap_clerk.graph import ALLOWED_MAILBOX, is_already_flagged, message_categories
from ap_clerk.kimco import KimcoClient, KimcoError
from ap_clerk.rules import PRICE_MISMATCH_PO_COMMENT, lookup_id, lookup_text, money

LOGGER = logging.getLogger("ap_clerk.po59083_handoff")

INVOICE_10010 = 10010
INVOICE_10009 = 10009
INV_11004 = "11004"
INV_11003 = "11003"
PO_59083 = "59083"
RECEIPT_24103 = 24103
COMMENT_TEXT = (
    f"{PRICE_MISMATCH_PO_COMMENT} AP invoice {INV_11003} / KIMCO {INVOICE_10009}: "
    "PDF 2 @ $5.00 = $10.00 vs PO/receipt 2 @ $0.78 = $1.55 (84.5%). "
    "Email to Shawn already going from AP Clerk."
)
VENDOR_NEEDLE = "AMERICAN QUALITY POWDER COATING"
ALREADY_ON_KIMCO = {
    "10917",
    "10918",
    "10920",
    "10921",
    "10999",
    "11002",
    "11003",
    "11004",
    "11005",
}


def _invoice_lines(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (item.get("lists") or {}).get("APInvoiceLine") or []:
        lv = (line.get("values") if isinstance(line, dict) else None) or {}
        qty = money(lv.get("Quantity"))
        unit = money(lv.get("Unit_Price"))
        amt = money(lv.get("Amount") or lv.get("Line_Amount"))
        if amt is None and qty is not None and unit is not None:
            amt = round(qty * unit, 2)
        rows.append(
            {
                "line_id": line.get("id"),
                "qty": qty,
                "unit_price": unit,
                "amount": amt,
                "receipt_id": lookup_id(lv.get("Receipt")),
                "po_line": lookup_text(lv.get("Purchase_Order_Line") or lv.get("PO_Item_Number")),
            }
        )
    return rows


def _invoice_summary(item: dict[str, Any]) -> dict[str, Any]:
    vals = item.get("values") if isinstance(item.get("values"), dict) else {}
    lines = _invoice_lines(item)
    return {
        "id": item.get("id"),
        "invoice_number": vals.get("Invoice_Number"),
        "amount": money(vals.get("Invoice_Amount")),
        "verification": money(vals.get("Invoice_Verification_Amount")),
        "total_line_net": money(vals.get("Total_Line_Net_Amounts")),
        "lines_count": vals.get("Lines_Count") if vals.get("Lines_Count") is not None else len(lines),
        "void": vals.get("Void"),
        "lines": lines,
        "list_keys": sorted((item.get("lists") or {}).keys()),
        "value_keys_commentish": [
            k
            for k in sorted(vals.keys())
            if any(tok in k.lower() for tok in ("comment", "note", "remark"))
        ],
    }


def _commentish_keys(record: dict[str, Any]) -> dict[str, Any]:
    vals = record.get("values") if isinstance(record.get("values"), dict) else record
    lists = record.get("lists") if isinstance(record.get("lists"), dict) else {}
    keys = list(vals.keys()) if isinstance(vals, dict) else []
    return {
        "value_keys_commentish": [
            k for k in keys if any(tok in k.lower() for tok in ("comment", "note", "remark", "tag"))
        ],
        "list_keys": sorted(lists.keys()),
        "list_keys_commentish": [
            k for k in lists if any(tok in k.lower() for tok in ("comment", "note", "remark"))
        ],
        "has_comments_field": any(k.lower() == "comments" or k.lower() == "comment" for k in keys),
        "comments_value": vals.get("Comments") or vals.get("Comment") or vals.get("Notes") or vals.get("Note"),
    }


def _subject_inv(subject: str) -> str:
    match = re.search(r"invoice\s+(\d+)", subject or "", flags=re.I)
    return match.group(1) if match else ""


def find_aqpc_candidates(graph) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    needles = [
        f"New payment request from {VENDOR_NEEDLE} - invoice {n}"
        for n in [str(n) for n in range(10990, 11020)]
    ]
    needles.append(f"New payment request from {VENDOR_NEEDLE}")
    needles.append("AMERICAN QUALITY POWDER COATING invoice")
    for needle in needles:
        try:
            hits = graph.search_messages(ALLOWED_MAILBOX, needle, top=25)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Graph search failed for needle: %s", type(exc).__name__)
            continue
        for msg in hits:
            mid = str(msg.get("id") or "")
            subject = str(msg.get("subject") or "")
            if VENDOR_NEEDLE not in subject.upper():
                continue
            if mid:
                seen[mid] = msg
    return list(seen.values())


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    creds = load_credentials(target="live")
    print(format_presence(creds.presence), flush=True)
    print("Target: live  invent=false  no-void  no-new-headers  no-treyce-email", flush=True)
    if not creds.ready:
        print(creds.error or "live credentials missing", flush=True)
        return 2

    client = KimcoClient.authenticate(
        creds.instance_url, creds.key or "", creds.password or "", target="live"
    )

    rec_10010 = _invoice_summary(client.get_item("ap_invoices", INVOICE_10010))
    rec_10009 = _invoice_summary(client.get_item("ap_invoices", INVOICE_10009))
    print(
        f"GET 10010 amount={rec_10010['amount']} net={rec_10010['total_line_net']} "
        f"lines={rec_10010['lines_count']} void={rec_10010['void']}",
        flush=True,
    )
    print(
        f"GET 10009 amount={rec_10009['amount']} net={rec_10009['total_line_net']} "
        f"lines={rec_10009['lines_count']} (leave lines alone)",
        flush=True,
    )

    six = len(rec_10010.get("lines") or []) == 6
    dollars = rec_10010.get("total_line_net") == 2600.0 or rec_10010.get("amount") == 2600.0
    have_leftovers = {24109, 24110} <= {
        ln.get("receipt_id") for ln in rec_10010.get("lines") or []
    }
    result_10010 = (
        "Success"
        if six and dollars and have_leftovers and not rec_10010.get("void")
        else "Entered with issues"
    )

    receipt = client.get_item("receipts", RECEIPT_24103)
    rv = receipt.get("values") if isinstance(receipt.get("values"), dict) else {}
    po_line_id = lookup_id(rv.get("PO_Item_Number"))
    po_line_text = lookup_text(rv.get("PO_Item_Number"))
    po_id = lookup_id(rv.get("PO_Number"))
    print(f"GET receipt 24103 po_line_id={po_line_id} {po_line_text} po_id={po_id}", flush=True)

    po_line = None
    po_line_probe: dict[str, Any] = {"id": po_line_id}
    if po_line_id not in (None, ""):
        po_line = client.get_item("purchase_lines", int(po_line_id))
        po_line_probe = {
            "id": po_line.get("id"),
            **_commentish_keys(po_line),
            "value_keys": sorted((po_line.get("values") or {}).keys())
            if isinstance(po_line.get("values"), dict)
            else [],
        }
        print(
            f"GET purchase_lines/{po_line_id} commentish={po_line_probe.get('value_keys_commentish')} "
            f"lists={po_line_probe.get('list_keys')}",
            flush=True,
        )

    comment_result: dict[str, Any] = {
        "attempted": False,
        "supported": False,
        "status": "not-attempted",
        "why": "No Comments/Notes field found on purchase_lines record yet.",
    }
    # Live 2026-09-15: lists.Comments exists (empty, no /comments URL) but
    # values._PO_Line_Notes accepts a record PUT. Prefer that field.

    # Probe OPTIONS and obvious comment child URLs before any PUT.
    if po_line_id not in (None, ""):
        rec_url = client._record_url("purchase_lines", po_line_id)
        opt = client.request("OPTIONS", rec_url)
        allow = opt.headers.get("Allow") or opt.headers.get("allow") or ""
        po_line_probe["options_status"] = opt.status_code
        po_line_probe["options_allow"] = allow
        print(f"OPTIONS purchase_lines/{po_line_id} HTTP {opt.status_code} Allow={allow}", flush=True)

        child_probes = []
        for suffix in ("comments", "notes", "Comments", "Notes"):
            url = f"{rec_url}/{suffix}"
            try:
                resp = client.request("GET", url)
            except KimcoError as exc:
                child_probes.append({"suffix": suffix, "error": str(exc)[:160]})
                continue
            child_probes.append(
                {
                    "suffix": suffix,
                    "status": resp.status_code,
                    "snippet": (resp.text or "")[:180],
                }
            )
        po_line_probe["child_probes"] = child_probes

        vals = (po_line or {}).get("values") if isinstance((po_line or {}).get("values"), dict) else {}
        comment_field = None
        for name in (
            "_PO_Line_Notes",
            "Comments",
            "Comment",
            "Notes",
            "Note",
            "Buyer_Comments",
            "Line_Comments",
        ):
            if name in vals:
                comment_field = name
                break
        put_allowed = "PUT" in (allow or "").upper()
        if comment_field and put_allowed:
            existing = str(vals.get(comment_field) or "").strip()
            if COMMENT_TEXT in existing or "@Shawn McKibben" in existing and INV_11003 in existing:
                comment_result = {
                    "attempted": False,
                    "supported": True,
                    "status": "already-present",
                    "field": comment_field,
                    "why": "PO line already has the @Shawn McKibben 11003 comment.",
                }
            else:
                new_text = f"{existing}\n{COMMENT_TEXT}".strip() if existing else COMMENT_TEXT
                payload = {
                    "id": int(po_line_id),
                    "state": "Modified",
                    "values": {comment_field: new_text},
                }
                comment_result["attempted"] = True
                comment_result["supported"] = True
                comment_result["field"] = comment_field
                try:
                    body, status, err = client.update("purchase_lines", int(po_line_id), payload)
                    comment_result["http"] = status
                    comment_result["error"] = err
                    if status < 400:
                        after = client.get_item("purchase_lines", int(po_line_id))
                        after_vals = after.get("values") or {}
                        posted = str(after_vals.get(comment_field) or "")
                        comment_result["status"] = "posted" if COMMENT_TEXT[:40] in posted else f"put-{status}-not-visible"
                        comment_result["posted_preview"] = posted[:240]
                    else:
                        comment_result["status"] = f"blocked-{status}"
                        comment_result["why"] = err or f"PUT purchase_lines/{po_line_id} HTTP {status}"
                except KimcoError as exc:
                    comment_result["status"] = "error"
                    comment_result["why"] = str(exc)[:240]
        elif put_allowed and not comment_field:
            comment_result = {
                "attempted": False,
                "supported": False,
                "status": "no-comment-field",
                "why": (
                    "purchase_lines record allows PUT but has no Comments/Notes field. "
                    "Will not invent a comment child list. Email to Shawn already going from AP Clerk."
                ),
                "options_allow": allow,
            }
        else:
            comment_result = {
                "attempted": False,
                "supported": False,
                "status": "no-put-or-no-field",
                "why": (
                    f"purchase_lines/{po_line_id} OPTIONS Allow={allow or 'none'}; "
                    f"comment field={comment_field or 'absent'}. "
                    "API does not expose a PO comment write. Email to Shawn already going from AP Clerk."
                ),
            }

    graph = _optional_graph_client()
    aqpc_rows: list[dict[str, Any]] = []
    next_five: list[dict[str, Any]] = []
    if graph is None:
        aqpc_search = {"error": "graph-authenticate-failed"}
    else:
        messages = find_aqpc_candidates(graph)
        for msg in messages:
            subject = str(msg.get("subject") or "")
            inv = _subject_inv(subject)
            aqpc_rows.append(
                {
                    "invoice": inv,
                    "subject": subject,
                    "receivedDateTime": msg.get("receivedDateTime"),
                    "already_flagged": is_already_flagged(msg),
                    "categories": message_categories(msg),
                    "already_on_kimco": inv in ALREADY_ON_KIMCO,
                }
            )
        aqpc_rows.sort(key=lambda r: (str(r.get("invoice") or ""), str(r.get("receivedDateTime") or "")), reverse=True)
        seen_inv: set[str] = set()
        for row in aqpc_rows:
            inv = str(row.get("invoice") or "")
            if not inv or inv in seen_inv:
                continue
            if row.get("already_flagged") or row.get("already_on_kimco"):
                continue
            seen_inv.add(inv)
            next_five.append(row)
            if len(next_five) >= 5:
                break
        kimco_hits: dict[str, Any] = {}
        for inv in [r.get("invoice") for r in next_five]:
            if not inv:
                continue
            url = client._list_url("ap_invoices")
            try:
                resp = client.request(
                    "GET",
                    url,
                    params={"pageSize": 10, "offset": 0, "q": inv},
                )
                payload = resp.json() if resp.status_code == 200 else {}
                items = payload.get("items") or []
                matches = []
                for item in items:
                    vals = item.get("values") if isinstance(item.get("values"), dict) else {}
                    if str(vals.get("Invoice_Number") or "") == str(inv):
                        matches.append({"id": item.get("id"), "invoice_number": vals.get("Invoice_Number")})
                kimco_hits[str(inv)] = {"http": resp.status_code, "matches": matches}
                if matches:
                    for row in next_five:
                        if row.get("invoice") == inv:
                            row["already_on_kimco"] = True
                            row["kimco_ids"] = [m["id"] for m in matches]
            except KimcoError as exc:
                kimco_hits[str(inv)] = {"error": str(exc)[:160]}
        still_open = [r for r in next_five if not r.get("already_on_kimco")]
        if len(still_open) < 5:
            for row in aqpc_rows:
                inv = str(row.get("invoice") or "")
                if not inv or inv in {r.get("invoice") for r in still_open}:
                    continue
                if row.get("already_flagged") or row.get("already_on_kimco"):
                    continue
                still_open.append(row)
                if len(still_open) >= 5:
                    break
            next_five[:] = still_open[:5]
        aqpc_search = {
            "messages_seen": len(aqpc_rows),
            "distinct_invoices": sorted({r.get("invoice") for r in aqpc_rows if r.get("invoice")}),
            "kimco_lookup_next_five": kimco_hits,
            "no_11006_plus": "11006" not in {r.get("invoice") for r in aqpc_rows},
        }

    proof = {
        "proof": "kimco-10010-success-po59083-comment-aqpc-handoff",
        "invent": False,
        "void": False,
        "new_headers": False,
        "treyce_email": False,
        "10010": {
            "Result": result_10010,
            "amount": rec_10010.get("amount"),
            "total_line_net": rec_10010.get("total_line_net"),
            "lines_count": rec_10010.get("lines_count"),
            "lines": rec_10010.get("lines"),
            "void": rec_10010.get("void"),
        },
        "10009": {
            "amount": rec_10009.get("amount"),
            "total_line_net": rec_10009.get("total_line_net"),
            "lines": rec_10009.get("lines"),
            "untouched": True,
        },
        "po_59083_comment": comment_result,
        "po_line_probe": po_line_probe,
        "aqpc_search": aqpc_search,
        "aqpc_messages": aqpc_rows,
        "next_five_aqpc": next_five,
        "handoff": (
            "Next agent: enter up to 5 unflagged AQPC payment-request invoices "
            "not already on KIMCO (see next_five_aqpc). Reuse batch 711 if Status 0. "
            "Guest Intuit click-through, no treyce@ email, do not touch 10009 price HOLD."
        ),
    }
    out = ROOT / "runs" / "kimco-10010-success-po59083-handoff.json"
    out.write_text(json.dumps(proof, indent=2, default=str))
    print(f"10010 Result={result_10010} net={rec_10010.get('total_line_net')}", flush=True)
    print(f"PO comment status={comment_result.get('status')} supported={comment_result.get('supported')}", flush=True)
    print(f"Next AQPC ({len(next_five)}): {[r.get('invoice') for r in next_five]}", flush=True)
    print(f"Wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
