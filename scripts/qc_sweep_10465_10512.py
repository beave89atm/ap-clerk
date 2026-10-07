"""Read-only QC sweep of bills 10465-10512. No KIMCO writes."""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.rules import lookup_text, money
from scripts.sept25_30_attachment_audit import attachment_bytes, attachment_name, numbers_on_page, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("qc-sweep")

OUT = ROOT / "runs" / "qc-sweep-10465-10512.csv"
AUDIT = ROOT / "runs" / "sept25-30-attachment-audit.csv"
CLASSES = ROOT / "runs" / "statement-15409416-check.json"
BILL_IDS = list(range(10465, 10513))
MENTION_RE = re.compile(r'data-mention-id="(\d+)"[^>]*data-mention-name="([^"]*)"', flags=re.I)
RECEIPT_RE = re.compile(r"receipt\s+(\d+)", flags=re.I)

HOLD_BITS = (
    "on hold",
    "does not match",
    "do not match",
    "was not selected",
    "not selected",
    "please receive",
    "no matching receipt",
    "no open matching",
    "needs a purchase order",
    "please confirm the unit",
    "quantity does not match",
    "cannot be matched",
)
RESOLVE_BITS = (
    "disregard",
    "ready to process",
    "no action needed",
    "selected receipt",
    "selected receipts",
    "selected quantity",
    "please delete this bill",
    "matches the pdf",
    "the bill matches",
    "coded to",
    "removed the extra",
)


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def load_classes() -> dict[int, str]:
    payload = json.loads(CLASSES.read_text())
    return {int(row["id"]): str(row.get("document_class") or "") for row in payload.get("bills") or []}


def load_audit() -> dict[int, dict[str, str]]:
    found: dict[int, dict[str, str]] = {}
    with AUDIT.open(newline="") as handle:
        for row in csv.DictReader(handle):
            found[int(row["bill id"])] = row
    return found


def comment_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for comment in (record.get("lists") or {}).get("Comments_1") or []:
        if not isinstance(comment, dict):
            continue
        values = comment.get("values") or {}
        html = str(values.get("HtmlValue") or "")
        text = plain(html)
        lowered = text.lower()
        creator = values.get("CreatorId") if isinstance(values.get("CreatorId"), dict) else {}
        rows.append(
            {
                "id": comment.get("id"),
                "text": text,
                "created_on": str(values.get("CreatedOn") or ""),
                "creator_id": creator.get("id"),
                "mentions": [
                    {"id": int(match.group(1)), "name": match.group(2)}
                    for match in MENTION_RE.finditer(html)
                ],
                "hold": any(bit in lowered for bit in HOLD_BITS),
                "resolve": any(bit in lowered for bit in RESOLVE_BITS),
                "anthony": "@Anthony" in text,
            }
        )
    rows.sort(key=lambda row: (row["created_on"], int(row["id"] or 0)))
    return rows


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    batch = values.get("AP_Invoice_Batch") or {}
    lines = []
    charges = []
    taxes = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        receipt = vals.get("Receipt")
        lines.append(
            {
                "id": line.get("id"),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": money(vals.get("Extended_Amount")),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
            }
        )
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        kind = vals.get("Additional_Charges")
        charges.append(
            {
                "id": charge.get("id"),
                "kind_id": kind.get("id") if isinstance(kind, dict) else None,
                "kind": (kind.get("text") or kind.get("name")) if isinstance(kind, dict) else vals.get("Name"),
                "amount": money(vals.get("Amount")),
            }
        )
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append(money(vals.get("Tax_Amount")) or 0)
    line_sum = round(sum(float(row["extended"] or 0) for row in lines), 2)
    charge_sum = round(sum(float(row["amount"] or 0) for row in charges), 2)
    tax_sum = round(sum(float(amount) for amount in taxes), 2)
    return {
        "id": int(record["id"]),
        "invoice": str(values.get("Invoice_Number") or ""),
        "vendor": lookup_text(values.get("Vendor")),
        "posted": values.get("Posted"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": (batch.get("text") or batch.get("name")) if isinstance(batch, dict) else "",
        "po": lookup_text(values.get("Purchase_Order")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "invoice_amount": money(values.get("Invoice_Amount")),
        "lines": lines,
        "charges": charges,
        "covered": round(line_sum + charge_sum + tax_sum, 2),
        "comments": comment_rows(record),
    }


def receipt_fact(client, receipt_id: int) -> dict[str, Any]:
    record = client.get_item("receipts", int(receipt_id))
    values = record.get("values") or {}
    qty = values.get("Quantity_Received")
    unit = values.get("PO_Item_Number_$_Unit_Price") or values.get("Purchase_Cost") or values.get("Unit_Cost")
    ext = money(values.get("Extended_Purchase_Cost"))
    if ext is None and qty not in (None, "") and unit not in (None, ""):
        ext = round(float(qty) * float(unit), 2)
    return {
        "id": int(receipt_id),
        "qty": qty,
        "unit": unit,
        "ext": ext,
        "invoiced": values.get("Invoiced"),
        "po": lookup_text(values.get("PO_Number")),
    }


def attachment_extra(client, bill: dict[str, Any], audit: dict[int, dict[str, str]]) -> tuple[str, str]:
    cached = audit.get(bill["id"])
    if cached:
        extra = cached.get("other invoice numbers") or ""
        own = cached.get("own invoice present") or ""
        return ("yes" if not extra and own == "yes" else "no"), extra
    extras: list[str] = []
    own_seen = False
    own = bill["invoice"]
    for item in client.list_attachments(bill["id"]):
        content = attachment_bytes(client, bill["id"], item)
        if not content or content[:5] != b"%PDF-":
            continue
        for text in page_texts(content):
            numbers = numbers_on_page(text, {own}, own)
            for number in numbers:
                if number == own:
                    own_seen = True
                elif number not in extras:
                    extras.append(number)
    return ("yes" if own_seen and not extras else "no"), "|".join(extras)


def owners(comments: list[dict[str, Any]]) -> str:
    names = []
    for comment in comments:
        if not comment["hold"]:
            continue
        for mention in comment["mentions"]:
            if mention["name"] not in names:
                names.append(mention["name"])
        if comment["anthony"] and "Anthony" not in names:
            names.append("Anthony")
    return "; ".join(names)


def contradictory(comments: list[dict[str, Any]]) -> str:
    pairs = []
    holds = [row for row in comments if row["hold"]]
    resolves = [row for row in comments if row["resolve"]]
    for hold in holds:
        later = [row for row in resolves if (row["created_on"], int(row["id"] or 0)) > (hold["created_on"], int(hold["id"] or 0))]
        if later:
            pairs.append(f"{hold['id']} then {later[-1]['id']}")
    return "; ".join(pairs)


def open_hold(comments: list[dict[str, Any]]) -> bool:
    if not any(row["hold"] for row in comments):
        return False
    last_hold = max((row for row in comments if row["hold"]), key=lambda row: (row["created_on"], int(row["id"] or 0)))
    return not any(
        row["resolve"] and (row["created_on"], int(row["id"] or 0)) > (last_hold["created_on"], int(last_hold["id"] or 0))
        for row in comments
    )


def main() -> None:
    classes = load_classes()
    audit = load_audit()
    client = login()
    install_401_guard(client)
    bills = []
    for bill_id in BILL_IDS:
        try:
            record = client.get_item("ap_invoices", bill_id)
        except Exception as exc:
            if "HTTP 404" not in str(exc):
                raise
            bills.append({"id": bill_id, "missing": True})
            LOGGER.info("Bill %s missing", bill_id)
            continue
        bill = snapshot(record)
        bill["document_class"] = classes.get(bill_id, "")
        own_only, extra = attachment_extra(client, bill, audit)
        bill["own_only"] = own_only
        bill["extra"] = extra
        bill["missing"] = False
        bills.append(bill)
        LOGGER.info("Bill %s %s batch %s", bill_id, bill["invoice"], bill["batch_id"])

    receipt_ids: set[int] = set()
    for bill in bills:
        if bill.get("missing"):
            continue
        for line in bill["lines"]:
            if line.get("receipt_id"):
                receipt_ids.add(int(line["receipt_id"]))
        if open_hold(bill["comments"]):
            for comment in bill["comments"]:
                for match in RECEIPT_RE.finditer(comment["text"]):
                    receipt_ids.add(int(match.group(1)))
    receipts = {rid: receipt_fact(client, rid) for rid in sorted(receipt_ids)}

    rows = []
    for bill in bills:
        if bill.get("missing"):
            rows.append(
                {
                    "bill_id": bill["id"],
                    "invoice": "",
                    "vendor": "",
                    "batch_id": "",
                    "batch": "",
                    "posted": "",
                    "document_class": "missing",
                    "attachment_own_only": "",
                    "extra_invoice_numbers": "",
                    "covered": "",
                    "verification": "",
                    "receipts": "",
                    "open_hold": "",
                    "uom_candidate": "",
                    "uom_conversion": "",
                    "dollar_gap": "",
                    "owner_tagged": "",
                    "owner_hold_on_747": "",
                    "contradictory_notes": "",
                    "problem": "Bill id does not exist.",
                    "proposed_fix": "No bill to change.",
                }
            )
            continue
        comments = bill["comments"]
        hold = open_hold(comments)
        conflict = contradictory(comments)
        owner = owners(comments)
        on_747 = "yes" if hold and owner and int(bill["batch_id"] or 0) == 747 else ""
        uom, conversion, gap = uom_case(bill, receipts)
        problems = []
        fixes = []
        if bill["document_class"] in {"statement", "letter"}:
            problems.append(f"Source document is a {bill['document_class']}, not an invoice.")
            fixes.append("Do not match a receipt. Ask Treyce to delete the bill.")
        if bill["extra"]:
            if bill["document_class"] in {"statement", "letter"}:
                problems.append(f"The letter lists other invoice numbers: {bill['extra']}.")
            else:
                problems.append(f"Attachment also contains {bill['extra']}.")
                fixes.append("Replace the file with pages for this invoice only. The API cannot edit an attachment in place.")
        if hold and uom == "yes":
            problems.append(f"Hold is a unit difference within $75. {conversion}")
            fixes.append("Select the matching receipt with the PPV shown, then clear the hold.")
        elif hold and uom == "already":
            problems.append(f"Hold is a unit difference and the dollars already match. {conversion}")
            fixes.append("Leave the receipt selected. Clear the hold. Do not add another PPV.")
        elif hold and uom == "no":
            problems.append(f"Hold is not a within-$75 unit match. {conversion}")
            if int(bill["batch_id"] or 0) == 375:
                fixes.append(f"Already on TRANSFER AP 375. {conversion}")
            else:
                fixes.append(conversion)
        if on_747:
            problems.append(f"Open hold tags {owner} and the bill is still on batch 747.")
            if uom == "yes":
                fixes.append("Select it with the PPV on batch 747. Do not move it to Transfer AP.")
            elif uom == "already":
                fixes.append("The dollars already match, so leave the bill on batch 747.")
            else:
                fixes.append("Move this owner-tagged hold to TRANSFER AP batch 375.")
        if conflict:
            problems.append(f"Contradictory notes: {conflict}.")
            fixes.append("The later note is the current instruction. KIMCO will not delete the earlier note.")
        rows.append(
            {
                "bill_id": bill["id"],
                "invoice": bill["invoice"],
                "vendor": bill["vendor"],
                "batch_id": bill["batch_id"],
                "batch": bill["batch"],
                "posted": bill["posted"] if bill["posted"] is not None else "",
                "document_class": bill["document_class"],
                "attachment_own_only": bill["own_only"],
                "extra_invoice_numbers": bill["extra"],
                "covered": bill["covered"],
                "verification": bill["verification"],
                "receipts": " ".join(str(line["receipt_id"]) for line in bill["lines"] if line.get("receipt_id")),
                "open_hold": "yes" if hold else "",
                "uom_candidate": uom,
                "uom_conversion": conversion,
                "dollar_gap": gap,
                "owner_tagged": owner,
                "owner_hold_on_747": on_747,
                "contradictory_notes": conflict,
                "problem": " ".join(problems),
                "proposed_fix": " ".join(dict.fromkeys(fixes)),
            }
        )

    fields = list(rows[0].keys())
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    flagged = [row["bill_id"] for row in rows if row["problem"]]
    LOGGER.info("Wrote %s problems %s", OUT, flagged)


def uom_case(bill: dict[str, Any], receipts: dict[int, dict[str, Any]]) -> tuple[str, str, str]:
    """Return yes/already/no, the conversion, and the dollar gap."""
    bill_id = bill["id"]
    verification = float(bill["verification"] or 0)
    if bill_id == 10493:
        fact = receipts.get(25161) or {}
        gap = round(verification - float(fact.get("ext") or 0), 2)
        return (
            "already",
            "Invoice is 1 piece / 272.66 lb. Receipt 25161 is quantity 288 (24 ft in inches) "
            f"at {fact.get('unit')}, extended ${fact.get('ext')}. Net PPV is $0.",
            f"{gap:.2f}",
        )
    if bill_id == 10494:
        good = receipts.get(24880) or {}
        other = receipts.get(24879) or {}
        gap = round(verification - float(good.get("ext") or 0), 2)
        inches = round(23.12 * 12, 2)
        return (
            "yes" if abs(gap) < 75 else "no",
            f"23.12 ft = {inches} in. Receipt 24880 is quantity {good.get('qty')} at {good.get('unit')}, "
            f"extended ${good.get('ext')}. PPV would be ${gap:.2f}. "
            f"Receipt 24879 is ${other.get('ext')} and is a different part.",
            f"{gap:.2f}",
        )
    if bill_id == 10503:
        fact = receipts.get(25160) or {}
        ppv = round(sum(float(row["amount"] or 0) for row in bill["charges"] if int(row.get("kind_id") or 0) == 13), 2)
        gap = round(verification - float(fact.get("ext") or 0), 2)
        return (
            "already",
            f"Invoice is 1 bar, 138 lb. Receipt 25160 is quantity {fact.get('qty')} "
            f"(20 ft in inches) at {fact.get('unit')}, extended ${fact.get('ext')}. "
            f"PPV on the bill is ${ppv:.2f}.",
            f"{gap:.2f}",
        )
    if bill_id == 10501:
        fact = receipts.get(25188) or {}
        gap = round(verification - float(fact.get("ext") or 0), 2)
        return (
            "no",
            f"Both sides are 20 pieces. Receipt 25188 is 20 at {fact.get('unit')}, "
            f"extended ${fact.get('ext')}. Price gap ${gap:.2f} is over $75, so this is not a PPV.",
            f"{gap:.2f}",
        )
    if bill_id == 10511:
        fact = receipts.get(24879) or {}
        gap = round(verification - float(fact.get("ext") or 0), 2)
        return (
            "no",
            "24 ft = 288 in, so the quantity matches receipt 24879, but the receipt is "
            f"quantity {fact.get('qty')} at {fact.get('unit')}, extended ${fact.get('ext')}. "
            f"The dollar gap is ${gap:.2f}, which is over $75. Do not select it with PPV.",
            f"{gap:.2f}",
        )
    if bill_id == 10468:
        return (
            "no",
            "Missing purchase order. Only the $212.19 freight is entered against the $3,552.19 invoice. Not a unit conversion.",
            "3340.00",
        )
    if bill_id == 10509:
        bits = []
        for rid in (25179, 25180, 25181, 25182):
            fact = receipts.get(rid) or {}
            bits.append(f"{rid} qty {fact.get('qty')} ${fact.get('ext')}")
        return (
            "no",
            "Invoice lines are $117.93, $4,015.00, $687.53, and $508.00. Open receipts: "
            + "; ".join(bits)
            + ". No receipt is within $75 of the $5,328.46 total, and 25182 is now on bill 10512. Do not guess a receipt.",
            "",
        )
    if bill_id in {10480, 10482, 10483, 10505, 10508, 10475, 10477}:
        reason = {
            10480: "No receipt on PO 59081 is the $213.93 hole-saw kit. Not a unit conversion.",
            10482: "No purchase order. Not a unit conversion.",
            10483: "No purchase order. Not a unit conversion.",
            10505: "PO 59251 has no receipt. Not a unit conversion.",
            10508: "PO 59289 has no receipt. Not a unit conversion.",
            10475: "Payment-status letter. Not a unit conversion.",
            10477: "Pending-orders letter. Not a unit conversion.",
        }[bill_id]
        return "no", reason, ""
    if bill_id == 10512:
        return "", "Already selected. 1 piece = receipt 25182 quantity 240 at $0.3397, PPV -$0.01.", "-0.01"
    return "", "", ""


if __name__ == "__main__":
    main()
