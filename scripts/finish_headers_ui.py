"""Live UI finish: Select Receipts + PDF attach on headers this dry run created.

Uses KIMCO_LIVE_USERNAME/PASSWORD. Never prints secrets.
Form 218 is the live AP Invoice edit page (confirmed 2026-08-28).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LIVE_SIGNIN = "https://live.kimcoerp.com/User/SignIn"
LIVE_FORM = "https://live.kimcoerp.com/Form/Edit/218?i={id}"
CHROME = os.environ.get("CHROME_PATH") or "/usr/bin/google-chrome"


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"{name} is absent")
    return value


def _login(page) -> None:
    user = _env("KIMCO_LIVE_USERNAME")
    password = _env("KIMCO_LIVE_PASSWORD")
    page.goto(LIVE_SIGNIN, wait_until="domcontentloaded")
    page.locator('input[name="username"]').fill(user)
    page.locator('input[name="password"]').fill(password)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url("**/Dashboard", timeout=30000)


def _select_receipts(page, invoice_number: str, po: str | None) -> tuple[bool, str]:
    needles = [n for n in (str(invoice_number or "").strip(), str(po or "").strip()) if n]
    try:
        page.get_by_text("Select Receipts", exact=True).first.click(timeout=8000)
    except PlaywrightTimeout:
        loc = page.locator("text=Select Receipts")
        if loc.count() == 0:
            return False, "no-select-receipts-button"
        loc.first.click()
    page.wait_for_timeout(1500)
    frame = None
    for _ in range(20):
        for fr in page.frames:
            if "ProcessSelect" in (fr.url or ""):
                frame = fr
                break
        if frame:
            break
        page.wait_for_timeout(250)
    if frame is None:
        return False, "no-processselect-iframe"
    checked = 0
    rows = frame.locator("tr")
    count = rows.count()
    for i in range(count):
        row = rows.nth(i)
        text = (row.inner_text() or "").replace("\n", " ")
        if not text.strip():
            continue
        if needles and not any(n.lower() in text.lower() for n in needles):
            continue
        box = row.locator("input[type=checkbox]")
        if box.count() == 0:
            continue
        try:
            box.first.check(force=True)
            checked += 1
        except Exception:
            continue
    if checked == 0 and needles:
        # Second pass: any open row on this PO (receipt second pass).
        for i in range(count):
            row = rows.nth(i)
            text = (row.inner_text() or "").replace("\n", " ")
            if po and str(po) in text:
                box = row.locator("input[type=checkbox]")
                if box.count():
                    try:
                        box.first.check(force=True)
                        checked += 1
                    except Exception:
                        continue
    if checked == 0:
        return False, "no-matching-receipt-rows"
    select_btn = frame.locator("button").filter(has_text="Select")
    if select_btn.count() == 0:
        return False, "no-select-button"
    select_btn.first.click()
    page.wait_for_timeout(1200)
    return True, f"selected-{checked}"


def _attach_pdf(page, pdf_path: Path) -> tuple[bool, str]:
    if not pdf_path.exists():
        return False, "pdf-missing"
    tab = page.locator('a[href="#ktab-attachments"]')
    if tab.count() == 0:
        tab = page.locator('a[href="#Attachments"]')
    if tab.count():
        tab.first.click()
        page.wait_for_timeout(400)
    file_input = page.locator("input[type=file]")
    if file_input.count() == 0:
        return False, "no-file-input"
    file_input.first.set_input_files(str(pdf_path))
    page.wait_for_timeout(800)
    return True, "attached"


def _save(page) -> None:
    btn = page.locator("button").filter(has_text="Save")
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(1500)


def finish_one(page, row: dict[str, Any], invoice: dict[str, Any] | None) -> dict[str, Any]:
    kid = row.get("KIMCO id")
    if kid in (None, ""):
        return {"kimco_id": kid, "skipped": True, "why": "no-header"}
    invoice = invoice or {}
    page.goto(LIVE_FORM.format(id=kid), wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    po = str(row.get("PO") or invoice.get("po") or "").strip()
    number = str(row.get("Invoice #") or invoice.get("invoice_number") or "")
    receipts_selected = False
    receipt_note = "no-po"
    if po:
        ok, receipt_note = _select_receipts(page, number, po)
        receipts_selected = ok
        _save(page)
    pdf = invoice.get("pdf_path") or ""
    attach_ok = False
    attach_note = row.get("Attach status") or "missing"
    if pdf:
        attach_ok, attach_note = _attach_pdf(page, Path(pdf))
        if attach_ok:
            attach_note = "attached"
        _save(page)
    return {
        "kimco_id": kid,
        "invoice_number": number,
        "po": po,
        "receipts_selected": receipts_selected,
        "attach_status": attach_note,
        "why": f"UI finish receipts={receipt_note} attach={attach_note}.",
        "graph_message_id": invoice.get("graph_message_id") or row.get("graph_message_id"),
        "multi_po": bool(invoice.get("multi_po")),
        "force_hold": bool(po) and receipt_note == "no-matching-receipt-rows",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live UI Select Receipts + PDF attach")
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    sidecar_path = Path(args.sidecar)
    payload = json.loads(sidecar_path.read_text())
    rows = payload.get("rows") or []
    invoices = payload.get("invoices") or []
    inv_by_number = {str(i.get("invoice_number") or ""): i for i in invoices}
    targets = [
        r
        for r in rows
        if r.get("KIMCO id") not in (None, "") and str(r.get("Result") or "") in {"Incomplete", "Success"}
    ]
    print(f"UI finish targets: {len(targets)} header(s). Secrets not printed.", flush=True)
    updates: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            executable_path=CHROME if Path(CHROME).exists() else None,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        _login(page)
        print("Live UI login success (credentials not printed).", flush=True)
        for row in targets:
            inv = inv_by_number.get(str(row.get("Invoice #") or ""))
            try:
                update = finish_one(page, row, inv)
            except Exception as exc:  # noqa: BLE001 - one header must not abort the rest
                update = {
                    "kimco_id": row.get("KIMCO id"),
                    "invoice_number": row.get("Invoice #"),
                    "receipts_selected": False,
                    "attach_status": row.get("Attach status") or "blocked-405",
                    "why": f"UI finish error: {type(exc).__name__}.",
                    "graph_message_id": (inv or {}).get("graph_message_id"),
                }
            updates.append(update)
            print(
                f"  id={update.get('kimco_id')} inv={update.get('invoice_number')} "
                f"receipts={update.get('receipts_selected')} attach={update.get('attach_status')}",
                flush=True,
            )
            time.sleep(0.4)
        context.close()
        browser.close()
    payload["ui_updates"] = updates
    sidecar_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"Wrote {len(updates)} UI update(s) to {sidecar_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
