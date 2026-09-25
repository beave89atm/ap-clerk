"""Mandatory live PPV QC.

Before Finish, re-read the live bill. Header invoice total, PDF total, and
selected receipt lines + all charges must match to the penny. |gap| < $75
posts one signed Purchase Price Variance first. |gap| >= $75 is HOLD
price_variance and does not post. Finish is blocked when that pre-check
fails. The same check runs again after Finish on the live readback.

After a bill is created or fixed, re-read it and post one signed Purchase
Price Variance when the header total misses merchandise lines + charges by
under $75. |gap| >= $75 is HOLD price_variance. Success requires the live
readback gap to be 0.00.

Read-only scan (does not post charges):

    python -m ap_clerk.ppv_qc --live --batch 720 --batch 722 --ids 10284,10283
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from ap_clerk.auth import load_credentials
from ap_clerk.gates import (
    GATE_PRICE,
    RESULT_HOLD,
    RESULT_SUCCESS,
    finish_gate,
    why_hold,
)
from ap_clerk.kimco import (
    ADDITIONAL_CHARGE_LISTS,
    KimcoClient,
    KimcoError,
    invoice_lines_from_record,
)
from ap_clerk.rules import TOTALS_MATCH_BEFORE_FINISH, money, ppv_qc_gap, totals_match_to_the_penny

LOGGER = logging.getLogger("ap_clerk.ppv_qc")

PPV_QC_NOTE_PREFIX = "AP Clerk: PPV QC"


def line_amounts_from_record(record: dict[str, Any] | None) -> list[float]:
    """Merchandise on the bill: Select Receipts lines and Type 4 misc lines."""
    amounts: list[float] = []
    for line in invoice_lines_from_record(record):
        raw = line.get("values") if isinstance(line, dict) and isinstance(line.get("values"), dict) else line
        if not isinstance(raw, dict):
            continue
        ext = money(raw.get("Extended_Amount"))
        if ext is None:
            ext = money(raw.get("Net_Amount"))
        if ext is None:
            qty = money(raw.get("Quantity"))
            price = money(raw.get("Unit_Price"))
            if qty is not None and price is not None:
                ext = round(qty * price, 2)
        if ext is not None:
            amounts.append(ext)
    return amounts


def charge_amounts_from_record(record: dict[str, Any] | None) -> list[float]:
    """Every additional charge: fees, freight, and PPV."""
    if not isinstance(record, dict):
        return []
    lists = record.get("lists") if isinstance(record.get("lists"), dict) else {}
    amounts: list[float] = []
    for key in ADDITIONAL_CHARGE_LISTS:
        for item in lists.get(key) or []:
            if not isinstance(item, dict):
                continue
            child = item.get("values") if isinstance(item.get("values"), dict) else item
            if not isinstance(child, dict):
                continue
            amount = money(child.get("Amount") or child.get("Charge_Amount"))
            if amount is not None:
                amounts.append(amount)
    return amounts


def ppv_qc_from_record(record: dict[str, Any] | None) -> dict[str, Any]:
    values = record.get("values") if isinstance(record, dict) and isinstance(record.get("values"), dict) else {}
    return ppv_qc_gap(
        invoice_amount=values.get("Invoice_Amount"),
        verification_amount=values.get("Invoice_Verification_Amount"),
        line_amounts=line_amounts_from_record(record),
        charge_amounts=charge_amounts_from_record(record),
    )


def _qc_why(decision: dict[str, Any]) -> str:
    gap = decision.get("gap")
    gap_text = "missing" if gap is None else f"{float(gap):.2f}"
    if decision.get("exception_category") == "price_variance":
        return why_hold(
            GATE_PRICE,
            (
                f"PPV QC gap {gap_text} (|gap| >= $75). price_variance. "
                "Do not post PPV. Do not report Success."
            ),
        )
    return why_hold(
        "finish",
        (
            f"PPV QC live gap {gap_text} is not 0.00 "
            f"({decision.get('reason') or decision.get('action')}). Not Success."
        ),
    )


def fix_note(amount: float) -> str:
    return (
        f"{PPV_QC_NOTE_PREFIX} posted {amount:.2f} so lines + charges equal "
        "the invoice header (live gap 0.00)."
    )


def apply_post_entry_ppv_gate(client: Any, invoice_id: int | str) -> dict[str, Any]:
    """Re-read the live bill. Post one PPV when the gap is under $75.

    Does not change the batch, receipt lines, verification, or GL posted flag.
    Re-reads after a charge. Success is allowed only when that readback gap
    is 0.00 and the Invoice_Amount rollup gap is 0.00.
    """
    record = client.get_item("ap_invoices", int(invoice_id))
    before = ppv_qc_from_record(record)
    status = "none"
    mutated = False
    if before.get("action") == "ppv" and before.get("ppv"):
        status = client.try_post_ppv(int(invoice_id), before["ppv"])
        mutated = status == "posted"
    after = before
    if mutated:
        after = ppv_qc_from_record(client.get_item("ap_invoices", int(invoice_id)))
    fixed = bool(mutated and after.get("gap") == 0.0 and after.get("success_allowed") is True)
    note = fix_note(float(before["ppv"])) if fixed else ""
    return {
        **after,
        "before": before,
        "ppv_status": status,
        "mutated": mutated,
        "fixed": fixed,
        "note": note,
        "ppv_posted": float(before["ppv"]) if mutated else 0.0,
        "why": "" if after.get("success_allowed") is not False else _qc_why(after if mutated else before),
        "success_allowed": after.get("success_allowed"),
        "enforced": after.get("enforced"),
    }


def _blocked_why(detail: str) -> str:
    text = (detail or "").strip()
    if "Finish blocked" not in text:
        text = f"{text} Finish blocked.".strip()
    if "totals match before finishing" not in text.lower():
        text = f"{text} {TOTALS_MATCH_BEFORE_FINISH}".strip()
    return text


def pre_finish_totals_check(client: Any, invoice_id: int | str) -> dict[str, Any]:
    """Live totals check that must pass before Finish.

    Header invoice total == PDF total == selected receipt lines + all charges,
    to the penny. |gap| < ppv_limit() posts one signed PPV first and re-reads.
    |gap| >= the limit is HOLD and does not post. ok is True only when that
    readback matches, or the read has no header total to enforce. A failed
    read is not ok, so Finish is blocked.
    """
    try:
        outcome = apply_post_entry_ppv_gate(client, invoice_id)
    except (KimcoError, AttributeError, TypeError, ValueError):
        return {
            "ok": False,
            "blocked": True,
            "enforced": True,
            "gap": None,
            "action": "read-failed",
            "success_allowed": False,
            "fixed": False,
            "note": "",
            "why": _blocked_why(
                why_hold(
                    "finish",
                    f"Pre-Finish totals check could not re-read live bill {invoice_id}.",
                )
            ),
        }
    matched = bool(outcome.get("enforced")) and totals_match_to_the_penny(outcome)
    ok = matched or outcome.get("enforced") is not True
    why = ""
    if not ok:
        why = _blocked_why(str(outcome.get("why") or _qc_why(outcome)))
    return {
        **outcome,
        "ok": ok,
        "blocked": not ok,
        "why": why,
    }


def remember_pre_finish_fix(row: dict[str, Any], pre: dict[str, Any]) -> None:
    """Keep the Notes text when the pre-Finish check posted a PPV."""
    if pre.get("fixed") and pre.get("note"):
        row["_ppv_qc_fixed"] = True
        row["Notes"] = pre["note"]
        posted = float(pre.get("ppv_posted") or 0)
        if posted:
            row["PPV"] = f"{posted:.2f}"


def finish_after_totals_check(
    client: Any,
    invoice_id: int | str,
    row: dict[str, Any],
    **gate_kwargs: Any,
) -> tuple[str, str]:
    """Run the pre-Finish totals check, then Finish.

    finish_gate returns HOLD, never Success, when the pre-check fails.
    """
    pre = pre_finish_totals_check(client, invoice_id)
    remember_pre_finish_fix(row, pre)
    return finish_gate(**gate_kwargs, pre_finish=pre)


def stamp_ppv_qc_on_row(client: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Run the live gate for a report row that already has a KIMCO id.

    A fix is written to Notes and marked `_ppv_qc_fixed` so the spreadsheet
    keeps that text. Success is removed when the live gap is not 0.00.
    """
    kid = row.get("KIMCO id")
    if kid in (None, ""):
        return row
    try:
        invoice_id = int(kid)
    except (TypeError, ValueError):
        return row
    try:
        outcome = apply_post_entry_ppv_gate(client, invoice_id)
    except (KimcoError, AttributeError, TypeError, ValueError) as exc:
        LOGGER.info("PPV QC re-read failed for %s: %s", invoice_id, type(exc).__name__)
        if str(row.get("Result") or "") == RESULT_SUCCESS:
            row["Result"] = RESULT_HOLD
            row["Why"] = why_hold(
                "finish",
                f"PPV QC could not re-read live bill {invoice_id}. Not Success.",
            )
        return row
    row["_ppv_qc"] = {
        "gap": outcome.get("gap"),
        "action": outcome.get("action"),
        "success_allowed": outcome.get("success_allowed"),
    }
    if outcome.get("fixed") and outcome.get("note"):
        row["_ppv_qc_fixed"] = True
        row["Notes"] = outcome["note"]
        row["PPV"] = f"{float(outcome.get('ppv_posted') or 0):.2f}"
    if outcome.get("enforced") and outcome.get("success_allowed") is False and str(row.get("Result") or "") == RESULT_SUCCESS:
        row["Result"] = RESULT_HOLD
        row["Why"] = outcome.get("why") or _qc_why(outcome)
    return row


def apply_batch_ppv_qc(client: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-read every bill in a run before the spreadsheet is written.

    Read-only: does not post PPV. A Success row whose live gap is not 0.00
    becomes HOLD. Notes are not filled here; that column is only for a PPV
    the post-entry gate already posted. Returns the open-gap findings.
    """
    findings: list[dict[str, Any]] = []
    for row in rows:
        kid = row.get("KIMCO id")
        if kid in (None, ""):
            continue
        try:
            invoice_id = int(kid)
        except (TypeError, ValueError):
            continue
        try:
            decision = ppv_qc_from_record(client.get_item("ap_invoices", invoice_id))
        except (KimcoError, AttributeError, TypeError, ValueError) as exc:
            if str(row.get("Result") or "") == RESULT_SUCCESS:
                row["Result"] = RESULT_HOLD
                row["Why"] = why_hold(
                    "finish",
                    f"PPV QC could not re-read live bill {invoice_id}. Not Success.",
                )
            findings.append(
                {
                    "kimco_id": invoice_id,
                    "invoice": row.get("Invoice #"),
                    "gap": None,
                    "action": "read-failed",
                    "reason": type(exc).__name__,
                }
            )
            continue
        gap = decision.get("gap")
        open_gap = decision.get("enforced") and gap not in (0, 0.0)
        success_with_gap = str(row.get("Result") or "") == RESULT_SUCCESS and decision.get("success_allowed") is False
        if not open_gap and not success_with_gap:
            continue
        finding = {
            "kimco_id": invoice_id,
            "invoice": row.get("Invoice #"),
            "gap": gap,
            "rollup_gap": decision.get("rollup_gap"),
            "action": decision.get("action"),
            "exception_category": decision.get("exception_category"),
            "result_before": row.get("Result"),
        }
        if success_with_gap:
            row["Result"] = RESULT_HOLD
            row["Why"] = _qc_why(decision)
            finding["result_after"] = RESULT_HOLD
        findings.append(finding)
    return findings


def _posted(value: Any) -> bool:
    return value not in (None, "", False)


def scan_invoice(client: Any, invoice_id: int, *, batch_id: int | None = None, batch_name: str | None = None) -> dict[str, Any]:
    record = client.get_item("ap_invoices", int(invoice_id))
    values = record.get("values") if isinstance(record.get("values"), dict) else {}
    decision = ppv_qc_from_record(record)
    batch = values.get("AP_Invoice_Batch")
    return {
        "kimco_id": int(invoice_id),
        "invoice": values.get("Invoice_Number"),
        "vendor": (values.get("Vendor") or {}).get("text") if isinstance(values.get("Vendor"), dict) else values.get("Vendor"),
        "batch_id": batch_id if batch_id is not None else (batch.get("id") if isinstance(batch, dict) else None),
        "batch_name": batch_name if batch_name is not None else (batch.get("text") if isinstance(batch, dict) else None),
        "posted": values.get("Posted"),
        "void": values.get("Void"),
        "invoice_amount": decision.get("invoice_amount"),
        "verification_amount": decision.get("verification_amount"),
        "lines": decision.get("lines"),
        "charges": decision.get("charges"),
        "gap": decision.get("gap"),
        "rollup_gap": decision.get("rollup_gap"),
        "action": decision.get("action"),
        "success_allowed": decision.get("success_allowed"),
        "header_field": decision.get("header_field"),
        "reason": decision.get("reason"),
    }


def unposted_ids_on_batch(batch_record: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    lists = batch_record.get("lists") if isinstance(batch_record.get("lists"), dict) else {}
    for item in lists.get("APInvoiceDisplay") or []:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        if _posted(values.get("Posted")):
            continue
        if values.get("Void") is True:
            continue
        ids.append(int(item["id"]))
    return ids


def scan_batches(client: Any, batch_ids: list[int]) -> dict[str, Any]:
    """Read-only. Every unposted bill on the named batches. No PPV writes."""
    scanned: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        batch = client.get_item("ap_batches", int(batch_id))
        values = batch.get("values") if isinstance(batch.get("values"), dict) else {}
        name = str(values.get("AP_Invoice_Batch_ID") or values.get("Display_Name") or "")
        for invoice_id in unposted_ids_on_batch(batch):
            row = scan_invoice(client, invoice_id, batch_id=int(batch_id), batch_name=name)
            if _posted(row.get("posted")) or row.get("void") is True:
                continue
            scanned.append(row)
    gaps = [row for row in scanned if row.get("gap") not in (0, 0.0)]
    return {
        "read_only": True,
        "batches": list(batch_ids),
        "scanned": len(scanned),
        "nonzero_gap_count": len(gaps),
        "gaps": gaps,
        "bills": scanned,
    }


def scan_ids(client: Any, invoice_ids: list[int]) -> dict[str, Any]:
    """Read-only scan of explicit bill ids."""
    scanned = [scan_invoice(client, invoice_id) for invoice_id in invoice_ids]
    gaps = [row for row in scanned if row.get("gap") not in (0, 0.0)]
    return {
        "read_only": True,
        "ids": list(invoice_ids),
        "scanned": len(scanned),
        "nonzero_gap_count": len(gaps),
        "gaps": gaps,
        "bills": scanned,
    }


def format_scan_report(report: dict[str, Any]) -> str:
    lines = [
        f"PPV QC read-only: scanned {report.get('scanned', 0)}, "
        f"nonzero gap {report.get('nonzero_gap_count', 0)}."
    ]
    gaps = report.get("gaps") or []
    if not gaps:
        lines.append("No nonzero gaps.")
        return "\n".join(lines)
    for row in gaps:
        lines.append(
            "  "
            f"id {row.get('kimco_id')} invoice {row.get('invoice')} "
            f"batch {row.get('batch_id')} {row.get('batch_name') or ''} "
            f"gap {row.get('gap')} rollup {row.get('rollup_gap')} "
            f"header {row.get('verification_amount')} amount {row.get('invoice_amount')} "
            f"lines {row.get('lines')} charges {row.get('charges')} "
            f"action {row.get('action')}"
        )
    return "\n".join(lines)


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in str(raw).replace(" ", "").split(","):
        if not part:
            continue
        ids.append(int(part))
    return ids


def run_ppv_qc(
    *,
    live: bool,
    batch_ids: list[int] | None = None,
    invoice_ids: list[int] | None = None,
    out_path: str | None = None,
) -> int:
    if not live:
        print("ppv-qc is read-only and requires --live so it reads live.kimcoerp.com.", flush=True)
        return 2
    batches = list(batch_ids or [])
    ids = list(invoice_ids or [])
    if not batches and not ids:
        print("Pass --batch (repeatable) and/or --ids 10284,10283.", flush=True)
        return 2
    creds = load_credentials(target="live")
    if not creds.ready:
        print(creds.error or "Live credentials missing.", flush=True)
        return 2
    client = KimcoClient.authenticate(
        creds.instance_url,
        creds.key or "",
        creds.password or "",
        target="live",
    )
    reports = []
    if batches:
        reports.append(scan_batches(client, batches))
    if ids:
        reports.append(scan_ids(client, ids))
    combined = {
        "read_only": True,
        "scanned": sum(int(item.get("scanned") or 0) for item in reports),
        "nonzero_gap_count": sum(int(item.get("nonzero_gap_count") or 0) for item in reports),
        "gaps": [row for item in reports for row in (item.get("gaps") or [])],
        "parts": reports,
    }
    text = format_scan_report(combined)
    print(text, flush=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(combined, handle, indent=2, default=str)
            handle.write("\n")
        print(f"Wrote {out_path}", flush=True)
    return 1 if combined["nonzero_gap_count"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only PPV QC for live KIMCO bills")
    parser.add_argument("--live", action="store_true", help="Read live.kimcoerp.com. Never posts PPV.")
    parser.add_argument("--batch", action="append", type=int, default=None, help="AP batch id. Repeatable. Unposted bills only.")
    parser.add_argument("--ids", default=None, help="Comma-separated KIMCO bill ids.")
    parser.add_argument("--out", default=None, help="Optional JSON path for the scan.")
    args = parser.parse_args(argv)
    return run_ppv_qc(
        live=bool(args.live),
        batch_ids=list(args.batch or []),
        invoice_ids=_parse_ids(args.ids),
        out_path=args.out,
    )


if __name__ == "__main__":
    raise SystemExit(main())
