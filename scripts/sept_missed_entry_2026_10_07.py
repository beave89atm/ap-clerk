"""Enter the 27 September invoices Kyle approved on 2026-10-07.

One unposted batch, two waves (first 20, then 7). Does not post, close a
batch, auto-pay, or send mail. Login: one attempt, then at most one
re-sign-in after a 401. Every write is API Agent user 175.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import fitz
from openpyxl import Workbook
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.cli import _find_or_create_batch
from ap_clerk.graph import ALLOWED_MAILBOX, ENTERED_IN_AI_CATEGORY, GraphClient, load_graph_credentials
from ap_clerk.kimco import KimcoError, added_comment_payload
from ap_clerk.misc_lines import misc_add_item_payload
from ap_clerk.rules import (
    ap_clerk_edit_note,
    comments_for,
    due_date_from_terms,
    invoice_number_key,
    kimco_datetime,
    lookup_id,
    lookup_text,
    money,
)
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-missed-entry")

OUT = ROOT / "runs" / "sept-missed-entry-2026-10-07"
BATCH_NAME = "API Agent - 10/7/26 Sept missed"
BATCH_TRANSFER = 375
MIN_NEW_ID = 10512
ARCHIVE = "Inbox/9 - FORT WORTH ARCHIVE"
DO_NOT_TOUCH = "2026-10-01T11:30:28Z"

_SPEC = importlib.util.spec_from_file_location(
    "sept25_30_finish_2026_10_06",
    ROOT / "scripts" / "sept25_30_finish_2026_10_06.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MOD)
login = _MOD.login
install_401_guard = _MOD.install_401_guard

ITEM_53 = 53
ITEM_31 = 31
ITEM_46 = 46


def cents(value: Any) -> float | None:
    amount = money(value)
    return None if amount is None else round(float(amount), 2)


def write_pages(src: Path, dest: Path, indexes: list[int]) -> None:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for index in indexes:
        writer.add_page(reader.pages[index])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        writer.write(handle)


def page_text(path: Path) -> str:
    doc = fitz.open(path)
    return "\n".join(page.get_text("text") for page in doc)


def assert_only_invoice(path: Path, number: str, banned: list[str]) -> None:
    text = page_text(path)
    if number.lower() not in text.lower() and number not in text:
        raise SystemExit(f"Attachment {path.name} does not show invoice {number}")
    for other in banned:
        if other != number and other in text:
            raise SystemExit(f"Attachment {path.name} also shows invoice {other}")
    if re.search(r"(?m)^\s*STATEMENT\b", text) and "CYLINDER RENTAL INVOICE" not in text:
        raise SystemExit(f"Attachment {path.name} is a statement")


def msc_rows(path: Path) -> list[tuple[str, float]]:
    text = page_text(path)
    found = []
    for match in re.finditer(r"([A-Z0-9][^\n]{6,90})\n(\d+\.\d{2})\n(\d+\.\d{2})\nN", text):
        desc = match.group(1).strip()
        if desc.upper().startswith("SUB") or "TOTAL" in desc.upper():
            continue
        found.append((desc, float(match.group(3))))
    return found


def msc_groups(path: Path, total: float) -> list[dict[str, Any]]:
    rows = msc_rows(path)
    grouped = {ITEM_53: 0.0, ITEM_31: 0.0}
    for desc, ext in rows:
        item = ITEM_31 if re.search(r"disc|cross pad|abrasive", desc, flags=re.I) else ITEM_53
        grouped[item] = round(grouped[item] + ext, 2)
    lines = []
    if grouped[ITEM_53]:
        lines.append(
            {
                "item_id": ITEM_53,
                "description": "Inserts, taps, drills, and end mills",
                "qty": 1,
                "unit_price": grouped[ITEM_53],
            }
        )
    if grouped[ITEM_31]:
        lines.append(
            {
                "item_id": ITEM_31,
                "description": "Abrasive discs and cross pads",
                "qty": 1,
                "unit_price": grouped[ITEM_31],
            }
        )
    got = round(sum(line["unit_price"] for line in lines), 2)
    if got != round(total, 2):
        raise SystemExit(f"MSC lines on {path.name} sum to {got}, not {total}")
    return lines


def prepare_pdfs() -> dict[str, Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    specs = {
        "78129701": ("/tmp/sept-missed-pdfs/e71a6d1221860194-1.pdf", [0, 1, 2], []),
        "82970031": ("/tmp/sept-missed-pdfs/f7f2bab142bd88c3-1.pdf", [0, 1], []),
        "PS-INV103344": ("/tmp/sept-missed-pdfs/034c7af815cbea68-1.pdf", [0], []),
        "WB4337861620": ("/tmp/sept-missed-pdfs/b66df43448260f8c-1.pdf", [0], []),
        "S1395419.001": ("/tmp/sept-missed-pdfs/afe11fe30103ab16-1.pdf", [0], ["S1395419.002"]),
        "S1395419.002": ("/tmp/sept-missed-pdfs/f2a84bc464da57cb-1.pdf", [0], ["S1395419.001"]),
        "0099474-IN": ("/tmp/sept-missed-pdfs/06513fa101db5085-1.pdf", [0], []),
        "1474316": ("/tmp/sept-missed-pdfs/5097a89fa68a0c66-1.pdf", [0], ["1474409"]),
        "1474409": ("/tmp/sept-missed-pdfs/56ec0be2ab66826c-1.pdf", [0], ["1474316"]),
        "389070": ("/tmp/sept-missed-pdfs/1edffa0af15347b5-1.pdf", [0], []),
        "IN0000183743": ("/tmp/sept-missed-pdfs/ff84a9e89a303d83-1.pdf", [0], []),
        "28377": ("/tmp/sept-missed-pdfs/fce702843e159c1d-1.pdf", [0, 1], []),
        "1390049": ("/tmp/sept-missed-pdfs/0f617216de01299e-1.pdf", [0], []),
        "470223": ("/tmp/sept-missed-pdfs/3a80ae7c698f585a-1.pdf", [0], ["470471"]),
        "470471": ("/tmp/sept-missed-pdfs/9d6fc8bd45f6ffa7-1.pdf", [0], ["470223"]),
        "4940": ("/tmp/sept-missed-pdfs/3e30526b5216eba4-1.pdf", [0], []),
        "00013160": ("/tmp/sept-missed-pdfs/63bd0510179c915e-1.pdf", [0], []),
        "154258": ("/tmp/sept-missed-pdfs/5697ebf76d79562e-1.pdf", [0], []),
    }
    gas = "/tmp/sept-missed-pdfs/87800a0a4bdd98de-1.pdf"
    gas_numbers = ["0040462826", "0040460840", "0040461079", "0040461906", "0040461132", "0040472242"]
    specs.update(
        {
            "0040462826": (gas, [0], gas_numbers),
            "0040460840": (gas, [1], gas_numbers),
            "0040461079": (gas, [2], gas_numbers),
            "0040461906": (gas, [3, 4], gas_numbers),
            "0040461132": (gas, [5], gas_numbers),
            "0040472242": (gas, [6, 7], [number for number in gas_numbers if number != "0040472242"]),
            "0040401083": ("/tmp/sept-missed-pdfs/55a0625dc7678eb4-1.pdf", [0, 1], ["0040406308", "0040477446"]),
            "0040406308": ("/tmp/sept-missed-pdfs/5aa5853ec5672b33-1.pdf", [0], ["0040477446", "0040401083"]),
            "0040477446": ("/tmp/sept-missed-pdfs/dd3f0dbc39ffae78-1.pdf", [0], ["0040406308", "0040344711", "0040412279", "0040482402"]),
        }
    )
    # Cylinder rental pages cite delivery invoices. Those are this invoice's
    # detail, not a second invoice, so they are not in the banned header list.
    out: dict[str, Path] = {}
    for number, (src, indexes, banned) in specs.items():
        dest = OUT / f"bill_{number.replace('/', '-')}.pdf"
        write_pages(Path(src), dest, indexes)
        if number in {"470223", "470471"}:
            if dest.stat().st_size < 1000:
                raise SystemExit(f"Fabcorp scan {number} was not copied")
            out[number] = dest
            continue
        if number not in {"0040472242", "0040401083", "0040406308", "0040477446"}:
            assert_only_invoice(dest, number, banned)
        else:
            text = page_text(dest)
            if "CYLINDER RENTAL INVOICE" not in text or number not in text:
                raise SystemExit(f"{number} attachment is not its cylinder rental invoice")
            if re.search(r"(?m)^\s*STATEMENT\b", text):
                raise SystemExit(f"{number} attachment includes the statement")
        out[number] = dest
    cross = page_text(out["28377"])
    if "TOTAL:" not in cross or "1,245.81" not in cross:
        raise SystemExit("Crosslink attachment is missing the printed total")
    rental = page_text(out["0040477446"])
    if "131.35" not in rental:
        raise SystemExit("0040477446 page does not print 131.35")
    return out


def jobs(pdfs: dict[str, Path]) -> list[dict[str, Any]]:
    def base(**kwargs: Any) -> dict[str, Any]:
        number = kwargs["number"]
        row = {
            "pdf": pdfs[number],
            "terms_id": 1,
            "terms_text": "Net 30",
            "tax": 0.0,
            "po_id": None,
            "receipts": [],
            "ppv": 0.0,
            "freight": 0.0,
            "fee": 0.0,
            "lines": [],
            "kind": "misc",
            "owner": "treyce",
        }
        row.update(kwargs)
        return row

    msc_a = msc_groups(pdfs["78129701"], 1857.28)
    msc_b = msc_groups(pdfs["82970031"], 70.68)
    ordered = [
        base(vendor="MSC Industrial Supply", vendor_id=128, number="78129701", amount=1857.28, day=date(2026, 9, 10), due=date(2026, 10, 10), remit_id=249, lines=msc_a, note="VENDING/1571 is not a KIMCO purchase order. Machining tools are item 53 and abrasive discs and pads are item 31."),
        base(vendor="MSC Industrial Supply", vendor_id=128, number="82970031", amount=70.68, day=date(2026, 9, 28), due=date(2026, 10, 28), remit_id=249, lines=msc_b, note="VENDING/1575 is not a KIMCO purchase order. Both lines are machining tools, item 53."),
        base(vendor="Capital Machine Technologies", vendor_id=45, number="PS-INV103344", amount=610.00, day=date(2026, 9, 10), due=date(2026, 9, 11), remit_id=166, lines=[
            {"item_id": ITEM_46, "description": "Labor non-warranty break-fix, 2 hours", "qty": 2, "unit_price": 210.00},
            {"item_id": ITEM_46, "description": "Shop supplies break-fix", "qty": 1, "unit_price": 25.00},
            {"item_id": ITEM_46, "description": "Travel non-warranty break-fix", "qty": 1, "unit_price": 165.00},
        ], note="External document 59088 is not used as a receipt match. Labor, shop supplies, and travel are item 46 Equipment Repair & Maint."),
        base(vendor="Xcaliber Industrial", vendor_id=339, number="WB4337861620", amount=419.76, day=date(2026, 9, 11), due=date(2026, 10, 11), remit_id=592, kind="po", po_id=7128, po_text="PO 59126", receipts=[24120], freight=38.32, merch=381.44, owner="treyce", note="Receipt 24120 is 24 valves at $15.8933, $381.44. Freight $38.32 is Freight External, not a price variance."),
        base(vendor="Techni-Tool", vendor_id=323, number="S1395419.001", amount=1639.10, day=date(2026, 9, 29), due=date(2026, 10, 29), remit_id=537, kind="missing", po_id=7301, po_text="PO 59299", owner="shawn", note="PO 59299 line 1 is 1 drum of Q-CUT 216 at $1,581.53 and has no receipt. Shipping and handling $57.57 is a fee, not a price variance. Nothing was selected."),
        base(vendor="Techni-Tool", vendor_id=323, number="S1395419.002", amount=135.58, day=date(2026, 9, 28), due=date(2026, 10, 28), remit_id=537, kind="missing", po_id=7301, po_text="PO 59299", owner="shawn", note="PO 59299 line 2 is the Mixx lockout at $108.00 and has no receipt. Shipping and handling $27.58 is a fee, not a price variance. Nothing was selected."),
        base(vendor="Alternative Parts Inc", vendor_id=215, number="0099474-IN", amount=807.34, day=date(2026, 9, 28), due=date(2026, 10, 28), remit_id=360, kind="po", po_id=7304, po_text="PO 59302", receipts=[25061], freight=27.34, merch=780.00, note="The invoice prints PO 59302, 20 S1.0 FE nozzles at $39.00. Receipt 25061 matches $780.00. Freight $27.34 is Freight External. PO 59295 is an A1 Image contract and was not used."),
        base(vendor="RMP Industrial Supply", vendor_id=322, number="1474316", amount=228.82, day=date(2026, 9, 29), due=date(2026, 10, 29), remit_id=536, freight=15.27, lines=[{"item_id": ITEM_53, "description": "TMC 3-575-030P bull head live center", "qty": 1, "unit_price": 213.55}], note="The invoice PO is verbal. PO 59273 is an EMJ tube purchase, so its receipt was not selected. The live center is machining tools item 53. Freight $15.27 is Freight External."),
        base(vendor="RMP Industrial Supply", vendor_id=322, number="1474409", amount=119.03, day=date(2026, 9, 30), due=date(2026, 10, 30), remit_id=536, freight=14.61, lines=[{"item_id": ITEM_53, "description": "TFM 2413926 FLDC grooving insert, qty 5", "qty": 1, "unit_price": 104.42}], note="The invoice PO is verbal. PO 59298 is an AQPC purchase, so its receipts were not selected. The inserts are machining tools item 53. Freight $14.61 is Freight External."),
        base(vendor="P&B Testing", vendor_id=330, number="389070", amount=340.00, day=date(2026, 9, 30), due=date(2026, 10, 30), remit_id=551, kind="po", po_id=7286, po_text="PO 59284", receipts=[24939], ppv=25.00, merch=315.00, note="Receipt 24939 is 21 at $15.00, $315.00. The invoice quotes the same 21 pieces at $340.00. The $25.00 gap is under $75 and is purchase price variance."),
        base(vendor="Amada America", vendor_id=18, number="IN0000183743", amount=1399.24, day=date(2026, 9, 30), due=date(2026, 10, 30), remit_id=139, kind="po", po_id=6152, po_text="PO 58150", receipts=[25320], freight=26.44, merch=1372.80, note="Receipt 25320 is the aspheric lens repair at $1,372.80. Freight $26.44 is Freight External."),
        base(vendor="Crosslink Powder Coating", vendor_id=278, number="28377", amount=1245.81, day=date(2026, 9, 30), due=date(2026, 10, 30), remit_id=454, kind="po", po_id=7227, po_text="PO 59225", receipts=[25174, 25175], ppv=69.84, fee=12.33, merch=1163.64, note="Receipts 25174 and 25175 are 6 at $193.94, $1,163.64. The coating line is 6 at $205.58, $1,233.48. The $69.84 gap is under $75 and is purchase price variance. Packaging $12.33 is a fee, not a price variance. The printed total is $1,245.81."),
        base(vendor="Lee Spring Company", vendor_id=213, number="1390049", amount=660.50, day=date(2026, 9, 30), due=date(2026, 10, 30), remit_id=353, kind="missing", po_id=7334, po_text="PO 59332", owner="shawn", note="PO 59332 is 50 extension springs at $13.21 and has no receipt. The unit is each on both the invoice and the purchase order. Nothing was selected."),
        base(vendor="Source Metals", vendor_id=338, number="470223", amount=1625.00, day=date(2026, 9, 15), due=date(2026, 10, 15), remit_id=559, kind="po", po_id=7062, po_text="PO 59060", receipts=[24175, 24176, 24177, 24178, 24181, 24182, 24184, 24185, 24186, 24179, 24180, 24183], freight=275.00, merch=1350.00, note="PO 59060 receipts 24175, 24176, 24177, 24178, 24181, and 24182 are 12 pieces of the 1.25 plate at $95.00, $1,140.00. Receipts 24184, 24185, and 24186 are 6 pieces at $20.00, $120.00. Receipts 24179, 24180, and 24183 are 6 pieces at $15.00, $90.00. The unit is each on the invoice and the receipts. Freight $275.00 is Freight External. There is no price variance."),
        base(vendor="Source Metals", vendor_id=338, number="470471", amount=1335.00, day=date(2026, 9, 28), due=date(2026, 10, 28), remit_id=559, kind="po", po_id=7218, po_text="PO 59216", receipts=[25258], freight=285.00, merch=1050.00, note="PO 59216 receipt 25258 is 4 pieces at $262.50, $1,050.00. The unit is each on the invoice and the receipt. Freight $285.00 is Freight External. There is no price variance."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040462826", amount=704.00, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[{"item_id": ITEM_31, "description": "300B12 compressed gas, qty 2", "qty": 2, "unit_price": 352.00}], note="Customer PO GAS is not a KIMCO purchase order. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040460840", amount=296.76, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[{"item_id": ITEM_31, "description": "Harrington hoist, qty 2", "qty": 2, "unit_price": 148.38}], note="Customer PO NEED PO is not a KIMCO purchase order. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040461079", amount=385.15, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[{"item_id": ITEM_31, "description": "Wire Wizard universal drum dolly", "qty": 1, "unit_price": 385.15}], note="Customer PO SHOP is not a KIMCO purchase order. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040461906", amount=220.50, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[
            {"item_id": ITEM_31, "description": "300 compressed gas, qty 6", "qty": 6, "unit_price": 28.00},
            {"item_id": ITEM_31, "description": "300 argon, qty 1", "qty": 1, "unit_price": 30.00},
            {"item_id": ITEM_31, "description": "Fuel surcharge", "qty": 1, "unit_price": 22.50},
        ], note="Customer PO CHECK STOP is not a KIMCO purchase order. Shop supplies item 31, including the fuel surcharge. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040461132", amount=170.85, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[{"item_id": ITEM_31, "description": "Wire Wizard torch wizard", "qty": 1, "unit_price": 170.85}], note="Customer PO SHOP is not a KIMCO purchase order. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040472242", amount=1563.16, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[
            {"item_id": ITEM_31, "description": "12-pack bank rental", "qty": 1, "unit_price": 471.15},
            {"item_id": ITEM_31, "description": "High pressure cylinder rental", "qty": 1, "unit_price": 847.18},
            {"item_id": ITEM_31, "description": "Low pressure cylinder rental", "qty": 1, "unit_price": 94.30},
            {"item_id": ITEM_31, "description": "8 gallon propane cylinder rental", "qty": 1, "unit_price": 104.98},
            {"item_id": ITEM_31, "description": "Maintenance and tracking fee", "qty": 1, "unit_price": 45.55},
        ], note="Cylinder rental invoice pages, not the statement. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040401083", amount=1463.17, day=date(2026, 8, 31), due=date(2026, 10, 30), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[
            {"item_id": ITEM_31, "description": "12-pack bank rental", "qty": 1, "unit_price": 471.15},
            {"item_id": ITEM_31, "description": "High pressure cylinder rental", "qty": 1, "unit_price": 742.88},
            {"item_id": ITEM_31, "description": "Low pressure cylinder rental", "qty": 1, "unit_price": 102.75},
            {"item_id": ITEM_31, "description": "8 gallon propane cylinder rental", "qty": 1, "unit_price": 104.98},
            {"item_id": ITEM_31, "description": "Cylinder maintenance", "qty": 1, "unit_price": 41.41},
        ], note="Cylinder rental invoice pages dated 8/31/26, not the statement. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040406308", amount=130.10, day=date(2026, 8, 31), due=date(2026, 10, 30), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[
            {"item_id": ITEM_31, "description": "8 gallon propane cylinder rental", "qty": 1, "unit_price": 112.22},
            {"item_id": ITEM_31, "description": "Cylinder maintenance", "qty": 1, "unit_price": 17.88},
        ], note="Cylinder rental invoice page dated 8/31/26, not the statement. Shop supplies item 31. No sales tax."),
        base(vendor="Gas and Supply", vendor_id=71, number="0040477446", amount=131.35, day=date(2026, 9, 30), due=date(2026, 11, 29), terms_id=4, terms_text="F-N60-Net 60", remit_id=192, owner="none", lines=[
            {"item_id": ITEM_31, "description": "8 gallon propane cylinder rental", "qty": 1, "unit_price": 112.22},
            {"item_id": ITEM_31, "description": "Maintenance and tracking fee", "qty": 1, "unit_price": 19.13},
        ], note="Cylinder rental invoice page, not the statement line. Shop supplies item 31. No sales tax. The 10/1 G1378 statement email was not moved."),
        base(vendor="Vineyard Advisors & CPAs", vendor_id=193, number="4940", amount=3506.25, day=date(2026, 9, 30), due=date(2026, 9, 30), terms_id=5, terms_text="50% Due Upon PO Receipt", remit_id=314, lines=[{"item_id": 18, "description": "Tax advisory and consulting, fixed asset true-up", "qty": 1, "unit_price": 3506.25}], note="Coded like Vineyard Kindle CPA bill 8771: Legal & Professional item 18. No purchase order. Due on the invoice is 9/30/26."),
        base(vendor="GRM Information Management", vendor_id=78, number="00013160", amount=196.84, day=date(2026, 9, 30), due=date(2026, 10, 30), terms_id=5, terms_text="50% Due Upon PO Receipt", remit_id=199, lines=[
            {"item_id": 44, "description": "Container storage", "qty": 1, "unit_price": 149.15},
            {"item_id": 44, "description": "Account maintenance fee", "qty": 1, "unit_price": 22.68},
            {"item_id": 44, "description": "Check processing fee", "qty": 1, "unit_price": 25.00},
            {"item_id": 44, "description": "File folder tracking", "qty": 1, "unit_price": 0.01},
        ], note="Coded like GRM bill 10243: Facility Maint. & Storage item 44. No purchase order."),
        base(vendor="ABY Benefits", vendor_id=442, number="154258", amount=45.00, day=date(2026, 9, 25), due=date(2026, 9, 25), terms_id=62, terms_text="Due Upon Receipt", remit_id=720, lines=[{"item_id": 30, "description": "COBRA administration fees for August", "qty": 1, "unit_price": 45.00}], note="Coded like ABY bill 10194: Employee Benefits item 30. This invoice is $45.00. The customer balance of $90.00 was not entered."),
    ]
    if len(ordered) != 27:
        raise SystemExit(f"Expected 27 invoices, got {len(ordered)}")
    for index, job in enumerate(ordered):
        job["wave"] = "A" if index < 20 else "B"
        lines = job.get("lines") or []
        extra = round(float(job.get("freight") or 0) + float(job.get("fee") or 0) + float(job.get("ppv") or 0) + float(job.get("tax") or 0), 2)
        if job["kind"] in {"misc", "po"} and job["kind"] == "misc":
            got = round(sum(float(line["qty"]) * float(line["unit_price"]) for line in lines) + extra, 2)
            if got != round(float(job["amount"]), 2):
                raise SystemExit(f"{job['number']} lines sum to {got}, not {job['amount']}")
        if job["kind"] == "po":
            got = round(float(job["merch"]) + extra, 2)
            if got != round(float(job["amount"]), 2):
                raise SystemExit(f"{job['number']} receipt math is {got}, not {job['amount']}")
    return ordered


def existing_map(client) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    fab: list[str] = []
    for item in client.list_items("ap_invoices", page_size=2000):
        values = item.get("values") or {}
        number = invoice_number_key(str(values.get("Invoice_Number") or ""))
        name = lookup_text(values.get("Vendor_$_Display_Name"))
        if number:
            found.setdefault(number, []).append(
                {
                    "id": int(item["id"]),
                    "vendor": name,
                    "posted": values.get("Posted"),
                    "void": values.get("Void"),
                }
            )
        if re.search(r"fab\s*corp|fabcorp", name, flags=re.I):
            fab.append(f"{item['id']} {name}")
    if fab:
        LOGGER.info("Fabcorp-like vendors: %s", fab)
    return found


def require_transfer(client) -> None:
    record = client.get_item("ap_batches", BATCH_TRANSFER)
    name = str((record.get("values") or {}).get("AP_Invoice_Batch_ID") or "")
    if name != "TRANSFER AP":
        raise SystemExit("Batch 375 is not TRANSFER AP. Stopping.")


def put(client, invoice_id: int, payload: dict[str, Any]) -> int:
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if payload.get("Posted") not in (None, "", False) or values.get("Posted") not in (None, "", False):
        raise SystemExit(f"Refusing to post bill {invoice_id}")
    _body, status, _error = client.update("ap_invoices", int(invoice_id), payload)
    return int(status)


def totals(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        vals = line.get("values") or {}
        item = vals.get("MFG_Miscellaneous_Item") or vals.get("Part_ID")
        receipt = vals.get("Receipt")
        lines.append(
            {
                "item": lookup_text(item),
                "description": vals.get("Misc_Description") or lookup_text(item),
                "qty": vals.get("Quantity"),
                "unit_price": vals.get("Unit_Price"),
                "extended": vals.get("Extended_Amount"),
                "receipt_id": receipt.get("id") if isinstance(receipt, dict) else None,
                "gl": lookup_text(vals.get("Purchase_GL_Account")),
            }
        )
    charges = []
    for charge in lists.get("InvoiceAdditionalCharges") or []:
        vals = charge.get("values") or {}
        charges.append({"id": charge.get("id"), "name": lookup_text(vals.get("Additional_Charges")) or vals.get("Name"), "amount": vals.get("Amount")})
    taxes = []
    for tax in lists.get("APInvoiceTaxCodes") or []:
        vals = tax.get("values") or {}
        taxes.append({"amount": vals.get("Tax_Amount")})
    notes = []
    for comment in lists.get("Comments_1") or []:
        vals = comment.get("values") or {}
        html = str(vals.get("HtmlValue") or "")
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        notes.append({"id": comment.get("id"), "text": plain})
    batch = values.get("AP_Invoice_Batch") or {}
    line_sum = round(sum(float(cents(row.get("extended")) or 0) for row in lines), 2)
    charge_sum = round(sum(float(cents(row.get("amount")) or 0) for row in charges), 2)
    tax_sum = round(sum(float(cents(row.get("amount")) or 0) for row in taxes), 2)
    return {
        "bill_id": record.get("id"),
        "invoice": values.get("Invoice_Number"),
        "batch_id": batch.get("id") if isinstance(batch, dict) else None,
        "batch": lookup_text(batch),
        "posted": values.get("Posted"),
        "verification": values.get("Invoice_Verification_Amount"),
        "lines": lines,
        "charges": charges,
        "taxes": taxes,
        "notes": notes,
        "covered": round(line_sum + charge_sum + tax_sum, 2),
        "comments": str(values.get("Comments") or ""),
    }


def create_header(client, job: dict[str, Any], batch_id: int) -> int:
    payload: dict[str, Any] = {
        "AP_Invoice_Batch": {"id": int(batch_id)},
        "Vendor": {"id": int(job["vendor_id"])},
        "Invoice_Number": job["number"],
        "Invoice_Type": 3 if job.get("po_id") else 4,
        "Invoice_Date": kimco_datetime(job["day"]),
        "Invoice_Verification_Amount": float(job["amount"]),
        "Invoice_Due_Date": kimco_datetime(job["due"]),
        "Terms_Code": {"id": int(job["terms_id"])},
        "Currency": {"id": 3},
        "Remit_To_Address": {"id": int(job["remit_id"])},
        "Transaction_Date": kimco_datetime(job["day"]),
        "Comments": comments_for("live"),
    }
    if job.get("po_id"):
        payload["Purchase_Order"] = {"id": int(job["po_id"])}
    created, _body, status, _error = client.create("ap_invoices", payload)
    if created is None and job.get("po_id"):
        LOGGER.info("Header with PO failed HTTP %s for %s. Retrying without the PO link.", status, job["number"])
        payload.pop("Purchase_Order", None)
        payload["Invoice_Type"] = 4
        job["po_dropped"] = True
        created, _body, status, _error = client.create("ap_invoices", payload)
    if created is None:
        raise SystemExit(f"Header create for {job['number']} failed HTTP {status}")
    created = int(created)
    if created <= MIN_NEW_ID:
        raise SystemExit(f"Create for {job['number']} returned existing id {created}. Not editing it.")
    record = None
    last_error = ""
    for attempt in range(4):
        try:
            record = client.get_item("ap_invoices", created)
            break
        except KimcoError as exc:
            last_error = str(exc)
            if "404" not in last_error or attempt == 3:
                raise
            time.sleep(2)
    if record is None:
        raise SystemExit(f"Created bill {created} was not readable: {last_error}")
    values = record.get("values") or {}
    if str(values.get("Invoice_Number") or "") != job["number"]:
        raise SystemExit(f"Created id {created} is not invoice {job['number']}. Stopping.")
    if values.get("Posted") not in (None, "", False):
        raise SystemExit(f"Created bill {created} is posted. Stopping.")
    if str(values.get("Comments") or "") != "API Agent":
        raise SystemExit(f"Created bill {created} is not stamped API Agent. Stopping.")
    LOGGER.info("Created %s as %s", job["number"], created)
    return created


def add_misc(client, invoice_id: int, job: dict[str, Any]) -> str:
    by_item: dict[int, list[dict[str, Any]]] = {}
    for line in job["lines"]:
        by_item.setdefault(int(line["item_id"]), []).append(line)
    for item_id, lines in by_item.items():
        payload = misc_add_item_payload(
            lines,
            invoice_id=invoice_id,
            vendor_id=int(job["vendor_id"]),
            misc_item={"id": int(item_id)},
        )
        status = put(client, invoice_id, payload)
        if status >= 400:
            return f"http-{status}"
    return "added"


def add_charges(client, invoice_id: int, job: dict[str, Any]) -> str:
    if job.get("freight"):
        status = client.try_post_fees(invoice_id, [{"amount": float(job["freight"]), "freight_external": True}])
        if status != "posted":
            return f"freight-{status}"
    if job.get("fee"):
        status = client.try_post_fees(invoice_id, [{"amount": float(job["fee"])}])
        if status != "posted":
            return f"fee-{status}"
    if job.get("ppv"):
        status = client.try_post_ppv(invoice_id, float(job["ppv"]))
        if status != "posted":
            return f"ppv-{status}"
    if job.get("tax"):
        status = client.try_post_sales_tax(invoice_id, float(job["tax"]))
        if status != "posted":
            return f"tax-{status}"
    return "posted"


def attach(client, invoice_id: int, path: Path) -> str:
    content = path.read_bytes()
    try:
        return client.try_official_attach(
            invoice_id,
            name=path.name,
            content_type="application/pdf",
            size=len(content),
            content=content,
        )
    except KimcoError:
        return "blocked"


def move_batch(client, invoice_id: int, batch_id: int) -> str:
    live = totals(client.get_item("ap_invoices", invoice_id))
    if int(live.get("batch_id") or 0) == int(batch_id):
        return "already"
    status = put(
        client,
        invoice_id,
        {"id": invoice_id, "state": "Modified", "values": {"AP_Invoice_Batch": {"id": int(batch_id)}}},
    )
    after = totals(client.get_item("ap_invoices", invoice_id))
    if int(after.get("batch_id") or 0) != int(batch_id):
        return f"stuck-{status}"
    return "moved"


def note_html(job: dict[str, Any], text: str) -> str:
    owner = job.get("owner") or "treyce"
    body = text.strip()
    if not body.startswith("AP Clerk:"):
        body = "AP Clerk: " + body
    if owner == "none":
        if "data-mention-id" in body or body.startswith("AP Clerk: @"):
            raise SystemExit("Gas note must not tag an owner")
        return f"<p>{body}</p>"
    if owner == "shawn":
        return ap_clerk_edit_note(body, action="shawn")
    return ap_clerk_edit_note(body, action="treyce")


def write_note(client, invoice_id: int, html: str) -> int | None:
    before = totals(client.get_item("ap_invoices", invoice_id))
    if any(str(note.get("text") or "").startswith("AP Clerk:") for note in before["notes"]):
        raise SystemExit(f"Bill {invoice_id} already has an AP Clerk note. Not writing a second one.")
    status = put(client, invoice_id, added_comment_payload(invoice_id, html))
    if status >= 400:
        return None
    after = totals(client.get_item("ap_invoices", invoice_id))
    notes = [note for note in after["notes"] if str(note.get("text") or "").startswith("AP Clerk:")]
    return int(notes[-1]["id"]) if notes else None


def blank(job: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    row = {
        "vendor": job["vendor"],
        "invoice": job["number"],
        "amount": job["amount"],
        "bill_id": "",
        "result": "HOLD",
        "batch": job["wave"],
        "reason": "",
        "note_id": "",
        "email_moved": "n",
        "email_ready": False,
        "wave": job["wave"],
        "kind": job["kind"],
    }
    row.update(kwargs)
    return row


def finished_row(client, job: dict[str, Any], invoice_id: int) -> dict[str, Any]:
    final = totals(client.get_item("ap_invoices", invoice_id))
    notes = [note for note in final["notes"] if str(note.get("text") or "").startswith("AP Clerk:")]
    names = [item.get("name") or item.get("fileName") for item in client.list_attachments(invoice_id)]
    passed = (
        final["covered"] == round(float(job["amount"]), 2)
        and cents(final["verification"]) == round(float(job["amount"]), 2)
        and final["posted"] in (None, "", False)
        and int(final.get("batch_id") or 0) != BATCH_TRANSFER
    )
    batch_label = job["wave"] + (" / TRANSFER AP" if int(final.get("batch_id") or 0) == BATCH_TRANSFER else "")
    return {
        "vendor": job["vendor"],
        "invoice": job["number"],
        "amount": job["amount"],
        "bill_id": invoice_id,
        "result": "PASS" if passed else "HOLD",
        "batch": batch_label,
        "reason": job["note"],
        "note_id": notes[-1]["id"] if notes else "",
        "email_moved": "n",
        "email_ready": bool(notes) and bool(names) and final["posted"] in (None, "", False),
        "wave": job["wave"],
        "kind": job["kind"],
        "covered": final["covered"],
        "attachments": names,
        "batch_id": final.get("batch_id"),
        "posted": final.get("posted"),
        "resumed": True,
    }


def enter_job(client, job: dict[str, Any], batch_id: int, existing: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    hits = existing.get(invoice_number_key(job["number"])) or []
    created: int | None = None
    if hits:
        if len(hits) != 1:
            return blank(job, reason=f"More than one KIMCO bill already uses {job['number']}. Not entered again.", bill_id=hits[0]["id"])
        created = int(hits[0]["id"])
        record = client.get_item("ap_invoices", created)
        values = record.get("values") or {}
        if str(values.get("Comments") or "") != "API Agent" or values.get("Posted") not in (None, "", False):
            return blank(job, reason=f"Already in KIMCO as bill {created}. Not edited.", bill_id=created)
        live = totals(record)
        if any(str(note.get("text") or "").startswith("AP Clerk:") for note in live["notes"]):
            LOGGER.info("Keeping finished bill %s %s", job["number"], created)
            return finished_row(client, job, created)
        if live["lines"] or live["charges"]:
            raise SystemExit(f"Bill {created} for {job['number']} is half-written. Stopping so it is not doubled.")
        LOGGER.info("Finishing header %s %s", job["number"], created)
    if job["kind"] == "novendor" or not job.get("vendor_id"):
        return blank(job, reason=job["note"])
    if created is None:
        created = create_header(client, job, batch_id)
    row = blank(job, bill_id=created)
    select_status = ""
    line_status = ""
    charge_status = ""
    if job["kind"] == "po" and not job.get("po_dropped"):
        try:
            select_status = client.try_select_receipts(created, [int(item) for item in job["receipts"]])
        except KimcoError:
            select_status = "blocked"
    elif job["kind"] == "misc":
        line_status = add_misc(client, created, job)
    if job["kind"] == "po" and select_status == "selected":
        charge_status = add_charges(client, created, job)
    elif job["kind"] == "misc" and line_status == "added":
        charge_status = add_charges(client, created, job)
    attach_status = attach(client, created, job["pdf"])
    live = totals(client.get_item("ap_invoices", created))
    matched = (
        cents(live["verification"]) == round(float(job["amount"]), 2)
        and live["covered"] == round(float(job["amount"]), 2)
        and live["posted"] in (None, "", False)
        and attach_status == "attached"
    )
    hold = job["kind"] in {"missing"} or job.get("po_dropped") or (job["kind"] == "po" and select_status != "selected") or not matched
    target = BATCH_TRANSFER if hold else batch_id
    move_batch(client, created, target)
    if hold:
        if job["kind"] == "missing":
            reason = job["note"]
        elif job.get("po_dropped"):
            reason = f"The purchase order could not be linked on the header. Nothing was selected. {job['note']}"
        elif job["kind"] == "po" and select_status != "selected":
            reason = f"Select Receipts returned {select_status}. Nothing was left selected. {job['note']}"
        else:
            reason = (
                f"Live lines, charges, and tax are ${live['covered']:,.2f} against ${job['amount']:,.2f}. "
                f"Lines {line_status or select_status}, charges {charge_status or 'n/a'}, PDF {attach_status}."
            )
        result = "HOLD"
        if job["kind"] in {"missing", "po"} or job.get("po_dropped"):
            job["owner"] = "shawn"
        elif job.get("owner") == "none":
            job["owner"] = "treyce"
    else:
        result = "PASS"
        reason = job["note"]
    text = f"AP Clerk: {job['vendor']} invoice {job['number']}. {reason} The bill is not posted."
    if result == "HOLD":
        text += " It is on hold in Transfer AP."
    html = note_html(job, text)
    note_id = write_note(client, created, html)
    final = totals(client.get_item("ap_invoices", created))
    names = [item.get("name") or item.get("fileName") for item in client.list_attachments(created)]
    ready = bool(note_id) and attach_status == "attached" and final["posted"] in (None, "", False)
    batch_label = job["wave"] + (" / TRANSFER AP" if int(final.get("batch_id") or 0) == BATCH_TRANSFER else "")
    return {
        "vendor": job["vendor"],
        "invoice": job["number"],
        "amount": job["amount"],
        "bill_id": created,
        "result": result,
        "batch": batch_label,
        "reason": reason,
        "note_id": note_id or "",
        "email_moved": "n",
        "email_ready": ready,
        "wave": job["wave"],
        "kind": job["kind"],
        "covered": final["covered"],
        "attachments": names,
        "batch_id": final.get("batch_id"),
        "posted": final.get("posted"),
        "select_status": select_status,
        "line_status": line_status,
        "charge_status": charge_status,
        "attach_status": attach_status,
    }


EMAILS = [
    {"received": "2026-09-11T16:04:00Z", "sender": "donotreply@invoices.mscdirect.com", "subject": "MSC Invoice 78129701, SOUTHFIELD MI  48033-4432, Your PO# VENDING/1571     (DXED#2026254114949717FE29)", "invoices": ["78129701"]},
    {"received": "2026-09-29T15:20:39Z", "sender": "donotreply@invoices.mscdirect.com", "subject": "MSC Invoice 82970031, SOUTHFIELD MI  48033-4432, Your PO# VENDING/1575     (DXED#202627211102638084E6)", "invoices": ["82970031"]},
    {"received": "2026-09-11T19:12:10Z", "sender": "ar@capitalmachine.com", "subject": "Capital Machine - Sales Invoice PS-INV103344 - KANNON MANUFACTURING - AMTECH", "invoices": ["PS-INV103344"]},
    {"received": "2026-09-11T20:59:31Z", "sender": "quickbooks@notification.intuit.com", "subject": "Invoice WB4337861620 from Xcaliber Industrial LLC", "invoices": ["WB4337861620"]},
    {"received": "2026-09-30T12:57:06Z", "sender": "ar@technitoolinc.com", "subject": "Invoice S1395419.001   PO# 59299", "invoices": ["S1395419.001"]},
    {"received": "2026-09-29T13:07:07Z", "sender": "ar@technitoolinc.com", "subject": "Invoice S1395419.002   PO# 59299", "invoices": ["S1395419.002"]},
    {"received": "2026-09-29T13:18:27Z", "sender": "DoNotReply@altparts.com", "subject": "Attached is the Invoice for Kannon Mfg dated 9/28/2026.", "invoices": ["0099474-IN"]},
    {"received": "2026-09-29T23:01:58Z", "sender": "jhencke@rmpis.com", "subject": "RMP Industrial Supply Inc - Invoice# 1474316", "invoices": ["1474316"]},
    {"received": "2026-09-30T18:58:43Z", "sender": "jhencke@rmpis.com", "subject": "RMP Industrial Supply Inc - Invoice# 1474409", "invoices": ["1474409"]},
    {"received": "2026-09-30T21:49:20Z", "sender": "accounting@pbtesting.com", "subject": "INVOICE 389070.00, 59284", "invoices": ["389070"]},
    {"received": "2026-10-01T03:34:41Z", "sender": "noreply@amada.com", "subject": "Amada Invoice#: IN0000183743, Customer: KANNON MANUFACTURING INC, Customer PO#: 58150", "invoices": ["IN0000183743"]},
    {"received": "2026-10-01T13:50:59Z", "sender": "update+accountingcrosslinktxcom@orders.gosteelhead.com", "subject": "Invoice #28377 for 59225 (#8667) from Crosslink Powder Coating", "invoices": ["28377"]},
    {"received": "2026-10-02T02:05:11Z", "sender": "ar@leespring.com", "subject": "Your Lee Spring Invoice #1390049", "invoices": ["1390049"]},
    {"received": "2026-09-17T13:45:52Z", "sender": "Rosana@fabcorp.com", "subject": "INVOICE 470223 PO 59060", "invoices": ["470223"]},
    {"received": "2026-09-29T18:51:22Z", "sender": "Rosana@fabcorp.com", "subject": "INVOICE 470471 PO 59216", "invoices": ["470471"]},
    {"received": "2026-10-01T11:01:33Z", "sender": "billing@gasandsupply.com", "subject": "Gas&Supply Invoice/Statement", "invoices": ["0040462826", "0040460840", "0040461079", "0040461906", "0040461132", "0040472242"]},
    {"received": "2026-09-01T11:13:52Z", "sender": "billing@gasandsupply.com", "subject": "Gas&Supply Invoice/Statement", "invoices": ["0040401083"]},
    {"received": "2026-09-01T11:42:01Z", "sender": "billing@gasandsupply.com", "subject": "Gas&Supply Invoice/Statement", "invoices": ["0040406308"]},
    {"received": "2026-09-30T22:57:44Z", "sender": "quickbooks@notification.intuit.com", "subject": "New payment request from Vineyard Advisors & CPAs, PLLC - invoice 4940", "invoices": ["4940"]},
    {"received": "2026-10-03T16:59:23Z", "sender": "quickbooks@notification.intuit.com", "subject": "Reminder: Invoice 4940 from Vineyard Advisors & CPAs, PLLC", "invoices": ["4940"]},
    {"received": "2026-10-02T00:02:49Z", "sender": "billingdal@grmdocument.com", "subject": "Your GRM invoice 00013160 is now available. Period 09-01-2026 - 09-30-2026", "invoices": ["00013160"]},
    {"received": "2026-09-25T19:46:35Z", "sender": "danae@abybenefits.com", "subject": "Invoice 154258 from ABY Benefits LLC", "invoices": ["154258"]},
]


def find_message(graph: GraphClient, expected: dict[str, Any], cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    day = date.fromisoformat(expected["received"][:10])
    key = day.isoformat()
    if key not in cache:
        cache[key] = graph.list_messages(ALLOWED_MAILBOX, received_from=day, received_to=day)
    found = []
    for row in cache[key]:
        if str(row.get("receivedDateTime") or "") != expected["received"]:
            continue
        if sender_of(row).lower() != expected["sender"].lower():
            continue
        if str(row.get("subject") or "") != expected["subject"]:
            continue
        full = graph.get_message(
            ALLOWED_MAILBOX,
            str(row.get("id") or ""),
            select="id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime",
        )
        if str(full.get("receivedDateTime") or "") == expected["received"]:
            found.append(full)
    unique = {str(item.get("id") or ""): item for item in found if item.get("id")}
    return list(unique.values())


def move_emails(rows: list[dict[str, Any]]) -> None:
    ready = {row["invoice"]: row for row in rows if row.get("email_ready")}
    creds = load_graph_credentials()
    if not creds.ready:
        for row in rows:
            row["email_error"] = creds.error or "graph credentials missing"
        return
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    archive_id = archive_folder(graph)
    folders: dict[str, dict[str, Any]] = {}
    days: dict[str, list[dict[str, Any]]] = {}
    for expected in EMAILS:
        if expected["received"] == DO_NOT_TOUCH:
            raise SystemExit("Refusing to move the 10/1 G1378 statement email")
        if not all(number in ready for number in expected["invoices"]):
            LOGGER.info("Leave email %s; not every invoice has a finished bill", expected["received"])
            continue
        matches = find_message(graph, expected, days)
        if len(matches) != 1:
            for number in expected["invoices"]:
                ready[number]["email_error"] = "not-found" if not matches else "several"
            continue
        message = matches[0]
        before = str(message.get("lastModifiedDateTime") or "")
        again = find_message(graph, expected, {})
        # Recheck the same message. A changed stamp means someone edited it.
        if len(again) != 1 or str(again[0].get("id") or "") != str(message.get("id") or ""):
            for number in expected["invoices"]:
                ready[number]["email_error"] = "changed-before-move"
            continue
        if str(again[0].get("lastModifiedDateTime") or "") != before:
            for number in expected["invoices"]:
                ready[number]["email_error"] = "lastModified-changed"
            continue
        path = folder_path(graph, str(message.get("parentFolderId") or ""), folders)
        if path == ARCHIVE:
            for number in expected["invoices"]:
                ready[number]["email_error"] = "already-archived"
            LOGGER.info("Leave already archived %s", expected["received"])
            continue
        message_id = str(message.get("id") or "")
        patched = graph.request(
            "PATCH",
            graph._messages_url(ALLOWED_MAILBOX, message_id),
            json={"categories": [ENTERED_IN_AI_CATEGORY]},
            headers={"Content-Type": "application/json"},
        )
        if patched.status_code >= 400:
            for number in expected["invoices"]:
                ready[number]["email_error"] = f"category-{patched.status_code}"
            continue
        outcome = graph.move_message(ALLOWED_MAILBOX, message_id, archive_id)
        new_id = str(outcome.get("new_id") or "")
        if outcome.get("status") != "moved-fort-worth" or not new_id:
            for number in expected["invoices"]:
                ready[number]["email_error"] = "move-failed"
            continue
        verified = graph.get_message(
            ALLOWED_MAILBOX,
            new_id,
            select="id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime",
        )
        new_path = folder_path(graph, str(verified.get("parentFolderId") or ""), folders)
        cats = [str(item) for item in (verified.get("categories") or [])]
        ok = (
            new_path == ARCHIVE
            and cats == [ENTERED_IN_AI_CATEGORY]
            and str(verified.get("receivedDateTime") or "") == expected["received"]
            and sender_of(verified).lower() == expected["sender"].lower()
        )
        for number in expected["invoices"]:
            ready[number]["email_moved"] = "y" if ok else "n"
            ready[number]["email_verified_folder"] = new_path
            ready[number]["email_verified_categories"] = cats
            if not ok:
                ready[number]["email_error"] = "verify-failed"


def write_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "Sept missed"
    headers = ["vendor", "invoice", "amount", "bill id", "PASS/HOLD", "batch", "reason", "note id", "email moved y/n"]
    sheet.append(headers)
    for row in rows:
        sheet.append(
            [
                row["vendor"],
                row["invoice"],
                row["amount"],
                row["bill_id"],
                row["result"],
                row["batch"],
                row["reason"],
                row["note_id"],
                row["email_moved"],
            ]
        )
    book.save(path)


def main() -> None:
    pdfs = prepare_pdfs()
    planned = jobs(pdfs)
    client = login()
    install_401_guard(client)
    require_transfer(client)
    existing = existing_map(client)
    batches = client.list_items("ap_batches", page_size=500)
    batch = _find_or_create_batch(client, batches, BATCH_NAME)
    batch_id = int(batch["id"])
    record = client.get_item("ap_batches", batch_id)
    values = record.get("values") or {}
    if str(values.get("AP_Invoice_Batch_ID") or "") != BATCH_NAME:
        raise SystemExit("New batch name does not match. Stopping.")
    if values.get("Status") not in (0, "0", None, ""):
        raise SystemExit(f"Batch {batch_id} is not open. Stopping.")
    LOGGER.info("Using batch %s id %s", BATCH_NAME, batch_id)
    rows = []
    for job in planned:
        if job["wave"] == "B" and not any(row["wave"] == "A" for row in rows):
            raise SystemExit("Wave B started before wave A. Stopping.")
        LOGGER.info("Wave %s %s", job["wave"], job["number"])
        rows.append(enter_job(client, job, batch_id, existing))
        (OUT / "progress.json").write_text(json.dumps(rows, indent=2, default=str))
    move_emails(rows)
    (OUT / "result.json").write_text(json.dumps({"batch_id": batch_id, "batch": BATCH_NAME, "bills": rows}, indent=2, default=str))
    write_xlsx(OUT / "AP-sept-missed-2026-10-07.xlsx", rows)
    for row in rows:
        LOGGER.info("%s %s %s bill %s %s", row["result"], row["invoice"], row["amount"], row["bill_id"], row["batch"])


if __name__ == "__main__":
    main()
