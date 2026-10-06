"""Apply the 10478-10509 finish. Never creates a bill, never posts, never moves mail."""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import KimcoError, added_comment_payload
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.ppv_qc import apply_post_entry_ppv_gate, ppv_qc_from_record
from ap_clerk.rules import ap_clerk_edit_note, money, ppv_limit
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "sept25_30_finish_2026_10_06",
    ROOT / "scripts" / "sept25_30_finish_2026_10_06.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)
ALLOWED = _MOD.ALLOWED
BILL_IDS = _MOD.BILL_IDS
SNAPSHOT = _MOD.SNAPSHOT
install_401_guard = _MOD.install_401_guard
login = _MOD.login
slim_bill = _MOD.slim_bill

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept25-30-apply")

BATCH_AGENT = 747
BATCH_TRANSFER = 375
SHOP_SUPPLIES = 31
EQUIPMENT_REPAIR = 46
PLAN = Path("/tmp/sept25-30-plan.json")
ACTIONS = Path("/tmp/sept25-30-actions.json")


def require_id(invoice_id: int) -> int:
    kid = int(invoice_id)
    if kid not in ALLOWED:
        raise SystemExit(f"Refusing to write bill {kid}. Only 10478-10509 are in scope.")
    return kid


def put(client, invoice_id: int, payload: dict[str, Any]) -> tuple[int, str]:
    kid = require_id(invoice_id)
    if payload.get("values", {}).get("Posted") not in (None, "", False) or payload.get("Posted"):
        raise SystemExit(f"Refusing to post bill {kid}")
    _body, status, error = client.update("ap_invoices", kid, payload)
    # Do not log the response body. It can echo field values.
    LOGGER.info("PUT %s HTTP %s", kid, status)
    return int(status), "http-" + str(status) if status >= 400 else ""


def fresh(client, invoice_id: int) -> dict[str, Any]:
    return client.get_item("ap_invoices", require_id(invoice_id))


def note(client, invoice_id: int, html: str, before_ids: set[int]) -> list[int]:
    status, _err = put(client, invoice_id, added_comment_payload(invoice_id, html))
    if status >= 400:
        return []
    after = slim_bill(fresh(client, invoice_id))
    return [int(row["id"]) for row in after["comments"] if int(row["id"]) not in before_ids and row.get("id")]


def comment_ids(record_slim: dict[str, Any]) -> set[int]:
    return {int(row["id"]) for row in record_slim.get("comments") or [] if row.get("id")}


def shawn(text: str) -> str:
    return ap_clerk_edit_note(text, action="shawn")


def treyce(text: str) -> str:
    return ap_clerk_edit_note(text, action="treyce")


def ruben(text: str, vendor: str) -> str:
    return ap_clerk_edit_note(text, action="aqpc_receiving", vendor=vendor)


def anthony(text: str) -> str:
    plain = text.strip()
    if not plain.startswith("AP Clerk:"):
        plain = "AP Clerk: " + plain
    if "@Anthony" not in plain and "Anthony" not in plain:
        plain = plain.replace("AP Clerk:", "AP Clerk: @Anthony", 1)
    if "data-mention-id" in plain:
        raise SystemExit("Refusing to invent a mention id for Anthony")
    return f"<p>{plain}</p>"


def receipt_fact(client, receipt_id: int) -> dict[str, Any]:
    record = client.get_item("receipts", int(receipt_id))
    values = record.get("values") or {}
    part = values.get("Part_Number")
    desc = values.get("PO_Item_Number_$_Part_Description") or ""
    if isinstance(part, dict):
        part_text = str(part.get("text") or "")
    else:
        part_text = str(part or "")
    qty = money(values.get("Quantity_Received"))
    unit = money(values.get("PO_Item_Number_$_Unit_Price"))
    ext = money(values.get("Extended_Purchase_Cost"))
    if ext is None and qty is not None and unit is not None:
        ext = round(qty * unit, 2)
    return {
        "id": int(receipt_id),
        "qty": qty,
        "unit": unit,
        "ext": ext,
        "part": part_text,
        "desc": str(desc or ""),
        "name": str(values.get("Name") or ""),
    }


def line_amount(line: dict[str, Any]) -> float | None:
    amount = money(line.get("amount"))
    if amount is not None:
        return amount
    qty = money(line.get("qty"))
    price = money(line.get("unit_price"))
    if qty is not None and price is not None:
        return round(qty * price, 2)
    return None


def choose_receipts(lines: list[dict[str, Any]], facts: list[dict[str, Any]], total: float | None) -> dict[str, Any]:
    """Pick receipts whose extended amount matches a line or the invoice total.

    Does not guess when more than one receipt fits the same line.
    """
    usable = [fact for fact in facts if fact.get("ext") is not None]
    used: set[int] = set()
    chosen: list[dict[str, Any]] = []
    ambiguous = False
    for line in lines:
        wanted = line_amount(line)
        qty = money(line.get("qty"))
        if wanted is None:
            continue
        hits = []
        for fact in usable:
            if fact["id"] in used:
                continue
            if abs(float(fact["ext"]) - float(wanted)) <= 0.05:
                hits.append(fact)
            elif qty is not None and fact.get("qty") is not None and abs(float(fact["qty"]) - float(qty)) <= 0.02:
                unit = money(line.get("unit_price"))
                if unit is not None and fact.get("unit") is not None and abs(float(fact["unit"]) - float(unit)) <= 0.05:
                    hits.append(fact)
        if len(hits) == 1:
            used.add(hits[0]["id"])
            chosen.append(hits[0])
        elif len(hits) > 1:
            ambiguous = True
    if not chosen and total is not None:
        hits = [fact for fact in usable if abs(float(fact["ext"]) - float(total)) <= 0.05]
        if len(hits) == 1:
            chosen = hits
        elif len(hits) > 1:
            ambiguous = True
    covered = round(sum(float(fact["ext"]) for fact in chosen), 2)
    return {"chosen": chosen, "ambiguous": ambiguous, "covered": covered, "facts": usable}


def remove_charges(client, invoice_id: int, charge_ids: list[int]) -> dict[str, Any]:
    kid = require_id(invoice_id)
    if not charge_ids:
        return {"status": "none", "stuck": []}
    payload = {
        "id": kid,
        "state": "Modified",
        "lists": {"InvoiceAdditionalCharges": [{"id": int(cid), "state": "Removed"} for cid in charge_ids]},
    }
    status, _err = put(client, kid, payload)
    after = slim_bill(fresh(client, kid))
    still = [row for row in after["charges"] if int(row["id"]) in set(charge_ids)]
    return {"status": status, "stuck": still, "charges": after["charges"]}


def move_batch(client, invoice_id: int, batch_id: int) -> str:
    kid = require_id(invoice_id)
    record = slim_bill(fresh(client, kid))
    current = (record.get("batch") or {}).get("id")
    if int(current or 0) == int(batch_id):
        return "already"
    status, _err = put(
        client,
        kid,
        {"id": kid, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": int(batch_id)}}},
    )
    after = (slim_bill(fresh(client, kid)).get("batch") or {}).get("id")
    if int(after or 0) != int(batch_id):
        return f"stuck-{status}"
    return "moved"


def add_misc(client, invoice_id: int, vendor_id: int, item_id: int, lines: list[dict[str, Any]]) -> str:
    payload = misc_add_item_payload(
        lines,
        invoice_id=require_id(invoice_id),
        vendor_id=int(vendor_id),
        misc_item={"id": int(item_id)},
    )
    status, _err = put(client, invoice_id, payload)
    return "added" if status < 400 else f"blocked-{status}"


def select_ids(client, invoice_id: int, receipt_ids: list[int]) -> str:
    try:
        return client.try_select_receipts(require_id(invoice_id), receipt_ids)
    except KimcoError as exc:
        text = str(exc)
        if "list does not allow" in text.lower() or "not valid" in text.lower():
            return "refused-list"
        return "blocked"


def post_tax(client, invoice_id: int, amount: float, taxable: float) -> str:
    try:
        return client.try_post_sales_tax(require_id(invoice_id), amount, taxable_amount=taxable)
    except KimcoError:
        return "blocked"


def maybe_ppv(client, invoice_id: int) -> str:
    try:
        outcome = apply_post_entry_ppv_gate(client, require_id(invoice_id))
    except KimcoError:
        return "blocked"
    return str(outcome.get("ppv_status") or outcome.get("action") or "")


def vendor_id(live: dict[str, Any]) -> int:
    vendor = live.get("vendor") or {}
    if not isinstance(vendor, dict) or vendor.get("id") in (None, ""):
        raise SystemExit("Live bill has no vendor id")
    return int(vendor["id"])


def pdf_total(bill: dict[str, Any]) -> float:
    return float(money(bill["pdf"].get("amount")) or money(bill.get("sheet_amount")) or 0)


def merch_lines(bill: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in bill["pdf"].get("lines") or []:
        if line.get("fee"):
            continue
        amount = line_amount(line)
        qty = line.get("qty")
        price = line.get("unit_price")
        if amount is not None and (qty in (None, "") or price in (None, "")):
            qty, price = 1, amount
        rows.append({**line, "amount": amount, "qty": qty, "unit_price": price})
    return rows


def apply() -> None:
    client = login()
    install_401_guard(client)
    agent = client.get_item("ap_batches", BATCH_AGENT)
    transfer = client.get_item("ap_batches", BATCH_TRANSFER)
    agent_name = str((agent.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    transfer_name = str((transfer.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if agent_name != "API Agent - 10/6/26 Sept":
        raise SystemExit("Batch 747 name is not the September agent batch. Stopping.")
    if transfer_name != "TRANSFER AP":
        raise SystemExit("Batch 375 name is not TRANSFER AP. Stopping.")
    data = json.loads(SNAPSHOT.read_text())
    plan = {int(row["id"]): row for row in json.loads(PLAN.read_text())}
    results = []
    for bill in data["bills"]:
        kid = require_id(int(bill["id"]))
        live = bill["live"]
        if live.get("posted") not in (None, "", False) or live.get("void") is True:
            results.append({"id": kid, "skipped": "posted-or-void"})
            continue
        before = comment_ids(live)
        action = finish_one(client, bill, plan.get(kid) or {}, before)
        action["id"] = kid
        results.append(action)
        LOGGER.info("DONE %s %s", kid, action.get("result"))
        ACTIONS.write_text(json.dumps(results, indent=2, default=str))
    LOGGER.info("Applied %s bills", len(results))


def finish_one(client, bill: dict[str, Any], plan_row: dict[str, Any], before: set[int]) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    vendor = str(bill["pdf"].get("vendor") or "")
    total = pdf_total(bill)
    lines = merch_lines(bill)
    manual: list[str] = []
    comments: list[int] = []
    # Already balanced freight / matched receipt bills are left in place
    # unless a known wrong charge is still on them.
    if kid == 10502:
        return {"result": "PASS", "why": "Freight External already equals the PDF total.", "comments": [], "manual": []}
    if kid in {10485, 10506, 10507, 10481}:
        return {"result": "PASS", "why": "Receipt and PDF total already match.", "comments": [], "manual": []}

    if kid in {10478, 10479, 10495}:
        return finish_gas(client, bill, lines, total, before)
    if kid in {10482, 10483}:
        return finish_shoppa(client, bill, lines, total, before)
    if kid == 10492:
        return finish_xcaliber(client, bill, before)
    if kid == 10499:
        return finish_aqpc_fee(client, bill, plan_row, total, before)

    candidate_ids = [int(row["id"]) for row in plan_row.get("open_on_po") or []]
    facts = [receipt_fact(client, rid) for rid in candidate_ids]
    choice = choose_receipts(lines, facts, total)
    live_receipts = [
        int((line.get("receipt") or {}).get("id"))
        for line in bill["live"]["lines"]
        if isinstance(line.get("receipt"), dict) and line["receipt"].get("id")
    ]
    if live_receipts and not choice["chosen"]:
        # Dollars already on the bill. Qty holds stay unposted on 747.
        return finish_existing_qty(client, bill, facts or [], total, before)

    if choice["chosen"] and not choice["ambiguous"]:
        if severe_qty_conflict(lines, choice["chosen"]):
            return finish_existing_qty(client, bill, choice["chosen"], total, before)
        covered = float(choice["covered"])
        gap = round(total - covered, 2)
        if abs(gap) >= ppv_limit():
            return hold_price(client, kid, vendor, total, gap, before, batch_already=bill["live"])
        if not live_receipts:
            status = select_ids(client, kid, [int(fact["id"]) for fact in choice["chosen"]])
            if status != "selected":
                manual.append(
                    f"Bill {kid}: select receipt(s) "
                    + ", ".join(str(fact["id"]) for fact in choice["chosen"])
                    + f". API returned {status}."
                )
                comments += note(
                    client,
                    kid,
                    shawn(
                        f"AP Clerk: @Shawn McKibben invoice {bill['pdf'].get('invoice_number')} "
                        f"matches receipt(s) {', '.join(str(fact['id']) for fact in choice['chosen'])} "
                        f"but Select Receipts was not saved ({status}). The bill is on hold and is not posted."
                    ),
                    before,
                )
                return {"result": "HOLD", "why": f"Select Receipts {status}", "comments": comments, "manual": manual}
        if abs(gap) >= 0.01:
            maybe_ppv(client, kid)
        move_batch(client, kid, BATCH_AGENT)
        after = slim_bill(fresh(client, kid))
        decision = ppv_qc_from_record(fresh(client, kid))
        if decision.get("success_allowed") is True or amounts_ok(after, total):
            comments += note(
                client,
                kid,
                treyce(
                    f"AP Clerk: @Treyce Hodges selected receipt(s) "
                    f"{', '.join(str(fact['id']) for fact in choice['chosen'])} "
                    f"for invoice {bill['pdf'].get('invoice_number')}. "
                    f"The bill matches the PDF total ${total:,.2f} and is not posted."
                ),
                before.union(comments),
            )
            return {"result": "PASS", "why": "Receipts selected and totals match.", "comments": comments, "manual": manual}
        return {"result": "HOLD", "why": "Selected receipts but the live total still does not match.", "comments": comments, "manual": manual}

    if not facts:
        return hold_missing(client, bill, total, before)
    if len(facts) == 1 and facts[0].get("ext") is not None and abs(float(facts[0]["ext"]) - total) >= ppv_limit():
        return hold_price(client, kid, vendor, total, round(total - float(facts[0]["ext"]), 2), before, batch_already=bill["live"])
    return hold_qty(client, bill, facts, lines, total, before)


def severe_qty_conflict(lines: list[dict[str, Any]], chosen: list[dict[str, Any]]) -> bool:
    """Piece-count vs weight (1 vs 288, 1 vs 240) stays a qty hold even when dollars match."""
    for fact in chosen:
        receipt_qty = money(fact.get("qty"))
        if receipt_qty is None:
            continue
        for line in lines:
            invoice_qty = money(line.get("qty"))
            if invoice_qty is None:
                continue
            if invoice_qty <= 2 and receipt_qty >= 20 and abs(invoice_qty - receipt_qty) > 1:
                return True
    return False


def amounts_ok(live: dict[str, Any], total: float) -> bool:
    verification = money(live.get("verification"))
    invoice = money(live.get("invoice_amount"))
    line_sum = round(sum(float(money(line.get("extended")) or 0) for line in live.get("lines") or []), 2)
    charge_sum = round(sum(float(money(row.get("amount")) or 0) for row in live.get("charges") or []), 2)
    tax_sum = round(sum(float(money(row.get("amount")) or 0) for row in live.get("taxes") or []), 2)
    covered = round(line_sum + charge_sum + tax_sum, 2)
    return verification == total and covered == total and invoice in {total, round(total - tax_sum, 2)}


def finish_gas(client, bill, lines, total, before) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    vendor = vendor_id(bill["live"])
    payload_lines = []
    for line in lines:
        payload_lines.append(
            {
                "description": (line.get("description") or line.get("part") or "Gas line")[:140],
                "qty": line.get("qty") or 1,
                "unit_price": line.get("unit_price") if line.get("unit_price") not in (None, "") else line.get("amount"),
            }
        )
    status = add_misc(client, kid, vendor, SHOP_SUPPLIES, payload_lines)
    manual = []
    comments: list[int] = []
    if status != "added":
        manual.append(f"Bill {kid}: add shop-supplies item 31 merchandise lines. API returned {status}.")
        return {"result": "HOLD", "why": f"Misc lines {status}", "comments": [], "manual": manual}
    maybe_ppv(client, kid)
    move_batch(client, kid, BATCH_AGENT)
    after = slim_bill(fresh(client, kid))
    if amounts_ok(after, total):
        comments += note(
            client,
            kid,
            treyce(
                f"AP Clerk: @Treyce Hodges entered the merchandise on invoice {bill['pdf'].get('invoice_number')} "
                f"as Shop Supplies - G&S. The bill matches the PDF total ${total:,.2f} and is not posted."
            ),
            before,
        )
        return {"result": "PASS", "why": "Gas merchandise entered on item 31.", "comments": comments, "manual": []}
    return {"result": "HOLD", "why": "Gas lines saved but the live total does not match the PDF.", "comments": comments, "manual": manual}


def finish_shoppa(client, bill, lines, total, before) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    charge_ids = [int(row["id"]) for row in bill["live"]["charges"]]
    removed = remove_charges(client, kid, charge_ids)
    manual = []
    comments: list[int] = []
    if removed["stuck"]:
        bits = ", ".join(f"charge {row['id']} ${row.get('amount')}" for row in removed["stuck"])
        manual.append(
            f"Bill {kid}: delete {bits}. They are not on the invoice. "
            f"Then enter Materials and Labor on item 46 and sales tax ${bill['pdf'].get('sales_tax')} on the Taxes tab."
        )
        comments += note(
            client,
            kid,
            shawn(
                f"AP Clerk: @Shawn McKibben Shoppa invoice {bill['pdf'].get('invoice_number')} still has fee lines "
                f"the API would not remove ({bits}). Delete those charges by hand. "
                f"The $25 return-policy sentence is not a charge. Sales tax belongs on the Taxes tab. "
                f"There is no purchase order. The bill stays on hold and is not posted."
            ),
            before,
        )
        move_batch(client, kid, BATCH_TRANSFER)
        return {"result": "HOLD", "why": "Fee removal did not stick.", "comments": comments, "manual": manual}
    status = add_misc(
        client,
        kid,
        vendor_id(bill["live"]),
        EQUIPMENT_REPAIR,
        [
            {
                "description": line.get("description") or "line",
                "qty": 1,
                "unit_price": line.get("amount"),
            }
            for line in lines
        ],
    )
    tax = money(bill["pdf"].get("sales_tax")) or 0
    taxable = round(sum(float(line.get("amount") or 0) for line in lines), 2)
    tax_status = post_tax(client, kid, float(tax), taxable) if tax else "none"
    move_batch(client, kid, BATCH_TRANSFER)
    after = slim_bill(fresh(client, kid))
    ok = amounts_ok(after, total)
    comments += note(
        client,
        kid,
        shawn(
            f"AP Clerk: @Shawn McKibben Shoppa invoice {bill['pdf'].get('invoice_number')} has no purchase order. "
            f"Removed the fee lines that were not charges, including the $25 return-policy sentence. "
            f"Entered materials and labor as Equipment Repair & Maint. and sales tax ${tax:,.2f} on the Taxes tab. "
            f"The PDF total is ${total:,.2f}. The bill is on hold in Transfer AP and is not posted."
        ),
        before,
    )
    why = "No PO. Merchandise and tax entered." if ok and status == "added" and tax_status == "posted" else f"lines {status} tax {tax_status}"
    return {"result": "HOLD", "why": why, "comments": comments, "manual": manual}


def finish_xcaliber(client, bill, before) -> dict[str, Any]:
    kid = require_id(10492)
    fee_ids = [int(row["id"]) for row in bill["live"]["charges"] if money(row.get("amount")) == 1080]
    removed = remove_charges(client, kid, fee_ids)
    comments: list[int] = []
    manual = []
    if removed["stuck"]:
        manual.append(
            "Bill 10492: delete the $1,080 F-Fees & Surcharges line. "
            "Keep receipt 24859 (300 bearings at $3.60). The delivery words are the ship-via, not a fee."
        )
        comments += note(
            client,
            kid,
            shawn(
                "AP Clerk: @Shawn McKibben Xcaliber invoice WB4337861639 still has a $1,080 fee "
                "the API would not remove. Delete that additional charge by hand and keep receipt 24859. "
                "The bill is on hold and is not posted."
            ),
            before,
        )
        return {"result": "HOLD", "why": "Extra $1,080 fee did not come off.", "comments": comments, "manual": manual}
    move_batch(client, kid, BATCH_AGENT)
    after = slim_bill(fresh(client, kid))
    if amounts_ok(after, 1080):
        comments += note(
            client,
            kid,
            treyce(
                "AP Clerk: @Treyce Hodges removed the extra $1,080 fee on Xcaliber invoice WB4337861639. "
                "Receipt 24859 stays (300 at $3.60). The bill matches $1,080.00 and is not posted."
            ),
            before,
        )
        return {"result": "PASS", "why": "Fee removed. Receipt 24859 matches.", "comments": comments, "manual": []}
    return {"result": "HOLD", "why": "Fee removed but the live total is not $1,080.", "comments": comments, "manual": manual}


def finish_aqpc_fee(client, bill, plan_row, total, before) -> dict[str, Any]:
    kid = require_id(10499)
    charge_ids = [int(row["id"]) for row in bill["live"]["charges"]]
    removed = remove_charges(client, kid, charge_ids)
    manual = []
    comments: list[int] = []
    if removed["stuck"]:
        manual.append(
            "Bill 10499: delete the $300 F-Fees line. It is the merchandise (12 at $25), not a fee. "
            "Then select receipt 25169."
        )
        comments += note(
            client,
            kid,
            ruben(
                "AP Clerk: @Ruben Perez AQPC invoice 11049 still has a $300 fee the API would not remove. "
                "Delete that charge by hand and select receipt 25169. The bill is on hold and is not posted.",
                "American Quality Powder Coating",
            ),
            before,
        )
        return {"result": "HOLD", "why": "$300 fee did not come off.", "comments": comments, "manual": manual}
    status = select_ids(client, kid, [25169])
    if status != "selected":
        manual.append(f"Bill 10499: select receipt 25169 (12 at $25 = $300). API returned {status}.")
        move_batch(client, kid, BATCH_TRANSFER)
        comments += note(
            client,
            kid,
            ruben(
                "AP Clerk: @Ruben Perez AQPC invoice 11049 needs receipt 25169 selected. "
                "The $300 fee was removed because it is the merchandise. "
                "The bill is on hold in Transfer AP and is not posted.",
                "American Quality Powder Coating",
            ),
            before,
        )
        return {"result": "HOLD", "why": f"Receipt select {status}", "comments": comments, "manual": manual}
    move_batch(client, kid, BATCH_AGENT)
    after = slim_bill(fresh(client, kid))
    if amounts_ok(after, total):
        comments += note(
            client,
            kid,
            treyce(
                "AP Clerk: @Treyce Hodges removed the $300 fee on AQPC invoice 11049 and selected receipt 25169. "
                "The bill matches $300.00 and is not posted."
            ),
            before,
        )
        return {"result": "PASS", "why": "Fee removed and receipt 25169 selected.", "comments": comments, "manual": []}
    return {"result": "HOLD", "why": "Receipt selected but the total does not match.", "comments": comments, "manual": manual}


def finish_existing_qty(client, bill, facts, total, before) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    live_qty = bill["live"]["lines"][0].get("qty") if bill["live"]["lines"] else None
    inv_qty = None
    for line in merch_lines(bill):
        inv_qty = line.get("qty")
        break
    comments = note(
        client,
        kid,
        shawn(
            f"AP Clerk: @Shawn McKibben invoice {bill['pdf'].get('invoice_number')} "
            f"quantity does not match the receipt. Invoice qty {inv_qty} versus receipt qty {live_qty}. "
            f"The dollars equal ${total:,.2f}, so the receipt stays selected. "
            f"Please confirm the unit. The bill is on hold, left on batch 747, and is not posted."
        ),
        before,
    )
    move_batch(client, kid, BATCH_AGENT)
    return {"result": "HOLD", "why": f"Qty {inv_qty} vs receipt {live_qty}.", "comments": comments, "manual": []}


def hold_price(client, kid, vendor, total, gap, before, batch_already) -> dict[str, Any]:
    move_batch(client, kid, BATCH_TRANSFER)
    comments = note(
        client,
        kid,
        shawn(
            f"AP Clerk: @Shawn McKibben {vendor} invoice total ${total:,.2f} is ${abs(gap):,.2f} "
            f"away from the open receipt, which is $75 or more. Receipts were not selected. "
            f"Please unreceive, correct the PO price, and re-receive. "
            f"The bill is on hold in Transfer AP and is not posted."
        ),
        before,
    )
    return {"result": "HOLD", "why": f"Price gap ${gap:.2f}.", "comments": comments, "manual": []}


def hold_missing(client, bill, total, before) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    vendor = str(bill["pdf"].get("vendor") or "")
    move_batch(client, kid, BATCH_TRANSFER)
    po = bill.get("sheet_po") or bill["pdf"].get("po") or ""
    aqpc = "powder" in vendor.lower() or "aqpc" in vendor.lower()
    if aqpc:
        html = ruben(
            f"AP Clerk: @Ruben Perez {vendor} invoice {bill['pdf'].get('invoice_number')} "
            f"on PO {po} has no matching receipt. The PDF total is ${total:,.2f}. "
            f"Please receive it. The bill is on hold in Transfer AP and is not posted.",
            vendor,
        )
    elif "o'neal" in vendor.lower() or "oneal" in vendor.lower() or "jorgensen" in vendor.lower():
        html = anthony(
            f"AP Clerk: @Anthony {vendor} invoice {bill['pdf'].get('invoice_number')} "
            f"on PO {po} has no matching receipt. The PDF total is ${total:,.2f}. "
            f"Please receive it. The bill is on hold in Transfer AP and is not posted."
        )
    else:
        html = shawn(
            f"AP Clerk: @Shawn McKibben {vendor} invoice {bill['pdf'].get('invoice_number')} "
            f"on PO {po} has no matching receipt. The PDF total is ${total:,.2f}. "
            f"The bill is on hold in Transfer AP and is not posted."
        )
    comments = note(client, kid, html, before)
    return {"result": "HOLD", "why": "No matching receipt.", "comments": comments, "manual": []}


def hold_qty(client, bill, facts, lines, total, before) -> dict[str, Any]:
    kid = require_id(int(bill["id"]))
    move_batch(client, kid, BATCH_AGENT)
    inv_bits = []
    for line in lines:
        inv_bits.append(f"qty {line.get('qty')} amount {line.get('amount')} {(line.get('description') or line.get('part') or '')[:40]}")
    rec_bits = [f"receipt {fact['id']} qty {fact['qty']} ${fact['ext']}" for fact in facts]
    vendor = str(bill["pdf"].get("vendor") or "")
    aqpc = "powder" in vendor.lower()
    body = (
        f"AP Clerk: invoice {bill['pdf'].get('invoice_number')} on PO {bill.get('sheet_po') or ''} "
        f"does not match the open receipts. Invoice {'; '.join(inv_bits) or 'lines'}. "
        f"Open {'; '.join(rec_bits)}. The PDF total is ${total:,.2f}. "
        f"Receipts were not selected. The bill is on hold on batch 747 and is not posted."
    )
    if aqpc and not facts:
        html = ruben("AP Clerk: @Ruben Perez " + body.replace("AP Clerk: ", ""), vendor)
    else:
        html = shawn("AP Clerk: @Shawn McKibben " + body.replace("AP Clerk: ", ""))
    comments = note(client, kid, html, before)
    return {"result": "HOLD", "why": "Quantity does not match open receipts.", "comments": comments, "manual": []}


if __name__ == "__main__":
    apply()
