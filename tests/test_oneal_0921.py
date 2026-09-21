"""O'Neal Steel 9/21 dedicated-batch first-five gates."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from ap_clerk.comments_tab import (  # noqa: E402
    RUBEN_MENTION,
    RUBEN_MENTION_ID,
    SHAWN_MENTION_ID,
    apply_missing_receipt_comment_tab,
    plain_comment_html,
)
from ap_clerk.quality_v12 import exception_owner_for  # noqa: E402
from ap_clerk.rules import chicago_today  # noqa: E402
from oneal_0921 import (  # noqa: E402
    CAP,
    FORBIDDEN_BATCH_IDS,
    FORBIDDEN_REUSE_NAMES,
    MIN_INVOICE_DATE,
    VENDOR_NAME,
    exact_invoice_number,
    is_oneal_invoice_email,
    is_oneal_vendor_text,
    leftover_from_catalog,
    missing_receipt_hold_comment,
    pick_recent,
    preferred_batch_name,
)


def test_preferred_batch_name_is_chicago_oneal_suffix():
    assert preferred_batch_name(date(2026, 9, 21)) == "API Agent - 9/21/26 O'Neal"
    assert preferred_batch_name(date(2026, 10, 1)) == "API Agent - 10/1/26 O'Neal"
    today = chicago_today()
    assert preferred_batch_name().endswith(" O'Neal")
    assert str(today.month) in preferred_batch_name()


def test_forbidden_batches_include_gas_mcmaster_transfer_ap():
    assert {375, 720, 721, 715, 716, 717} <= FORBIDDEN_BATCH_IDS
    assert "API Agent - 9/18/26 McMaster" in FORBIDDEN_REUSE_NAMES
    assert "API Agent - 9/17/26 Gas & Supply" in FORBIDDEN_REUSE_NAMES
    assert "TRANSFER AP" in FORBIDDEN_REUSE_NAMES


def test_oneal_invoice_number_is_15xxxxxx():
    assert exact_invoice_number("Invoice 15452509") == "15452509"
    assert exact_invoice_number("15439109") == "15439109"
    assert exact_invoice_number("71254641") == ""
    assert exact_invoice_number("68981481") == ""


def test_vendor_text_is_oneal_only():
    assert is_oneal_vendor_text("O'Neal Steel - Dallas (GP)")
    assert is_oneal_vendor_text("1135-ONEAL STEEL, LLC.")
    assert not is_oneal_vendor_text("McMaster-Carr Supply Company")
    assert not is_oneal_vendor_text("Gas & Supply")
    assert not is_oneal_vendor_text("JP Steel")
    assert VENDOR_NAME.startswith("O'Neal")


def test_noise_and_flagged_subjects_are_not_invoices():
    assert is_oneal_invoice_email(
        {"subject": "O'Neal Steel Invoice For Account # 14748440", "from": {"emailAddress": {"name": "O'Neal Steel"}}}
    )
    assert not is_oneal_invoice_email(
        {"subject": "O'Neal Steel statement / past due", "from": {"emailAddress": {"name": "O'Neal Steel"}}}
    )


def test_pick_recent_cap_5_skips_pre_august_and_already():
    bills = [
        {
            "invoice_number": f"1545000{i}",
            "date": "2026-09-10",
            "receivedDateTime": f"2026-09-1{i}T06:00:00Z",
            "po": "59100",
            "amount": 10.0,
            "subject": f"O'Neal Steel Invoice {15450000 + i}",
        }
        for i in range(6)
    ]
    bills.append(
        {
            "invoice_number": "15449999",
            "date": "2026-07-15",
            "po": "58700",
            "amount": 10.0,
            "subject": "O'Neal Steel Invoice 15449999",
        }
    )
    recent, leftover, credits = pick_recent(
        bills,
        already={"15450000"},
        cap=CAP,
    )
    assert len(recent) == 5
    assert "15450000" not in [b["invoice_number"] for b in recent]
    assert "15449999" not in [b["invoice_number"] for b in recent]
    assert MIN_INVOICE_DATE == date(2026, 8, 1)
    assert credits == []
    leftover_rows = leftover_from_catalog(
        bills,
        chosen={b["invoice_number"] for b in recent},
        older=leftover,
        credits=credits,
    )
    assert any(r["invoice_number"] == "15449999" and "too-old" in r["why"] for r in leftover_rows)


def test_missing_receipt_owner_is_ruben_not_shawn():
    assert exception_owner_for("missing_receipt", vendor=VENDOR_NAME) == "Ruben Perez"
    assert exception_owner_for("price_variance", vendor=VENDOR_NAME) == "Shawn McKibben"
    text = missing_receipt_hold_comment(
        invoice_number="15452509",
        po="59100",
        pdf_amount=100.10,
    )
    assert "Ruben Perez" in text
    assert "do not Transfer AP" in text
    assert "O'Neal" in text
    assert "Shawn" not in text
    assert RUBEN_MENTION_ID is None


def test_missing_receipt_writes_plain_comments_1_when_ruben_id_unknown():
    class _Fake:
        def __init__(self):
            self.payloads = []

        def get_item(self, _svc, kid):
            comments = []
            if self.payloads:
                comments = [{"id": 900, "values": self.payloads[-1]["lists"]["Comments_1"][0]["values"]}]
            return {
                "id": kid,
                "values": {"AP_Invoice_Batch": {"id": 800, "text": "API Agent - 9/21/26 O'Neal"}},
                "lists": {"Comments_1": comments},
            }

        def update(self, _svc, _kid, payload):
            self.payloads.append(payload)
            return {}, 200, ""

    fake = _Fake()
    body = missing_receipt_hold_comment(
        invoice_number="15452509", po="59100", pdf_amount=12.34
    )
    out = apply_missing_receipt_comment_tab(
        fake,
        invoice_id=10990,
        body=body,
        mention=RUBEN_MENTION,
    )
    assert out["transfer_ap"] is False
    assert out["mail_send"] is False
    assert out["tab_persisted"] is True
    assert out["plain"] is True
    assert out["mention_node"] is False
    html = fake.payloads[0]["lists"]["Comments_1"][0]["values"]["HtmlValue"]
    assert "data-mention-id" not in html
    assert "Ruben Perez" in html
    assert "HOLD (receipt)" in html
    assert "AP_Invoice_Batch" not in (fake.payloads[0].get("values") or {})
    assert f'data-mention-id="{SHAWN_MENTION_ID}"' not in html
    assert plain_comment_html("hello") == "<p>hello</p>"
