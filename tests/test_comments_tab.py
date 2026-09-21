"""NOTE-43: Comments tab is lists.Comments_1, never the header Comments string."""

from __future__ import annotations

from ap_clerk.comments_tab import (
    COMMENT_TAB_LIST,
    SHAWN_MENTION_ID,
    add_invoice_comment_tab,
    comment_tab_add_payload,
    comment_tab_proof,
    format_comment_tab_report,
    shawn_mention_html,
    transfer_ap_batch_only_payload,
)
from ap_clerk.rules import SHAWN_MCKIBBEN


def test_never_report_comments_persisted_for_header_string():
    record = {
        "values": {
            "Comments": (
                f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on McMaster-Carr "
                "71001379 PO 59069 PDF $85.82."
            )
        },
        "lists": {COMMENT_TAB_LIST: []},
    }
    proof = comment_tab_proof(record)
    assert proof["tab_persisted"] is False
    assert proof["comments_persisted"] is False
    assert proof["header_string_ignored"] is True
    assert proof["header_has_shawn"] is True
    assert "Comments persisted" not in proof["report"]
    assert "wrong surface" in proof["report"]


def test_comment_tab_get_is_the_only_persisted_signal():
    record = {
        "values": {"Comments": "API Agent"},
        "lists": {
            COMMENT_TAB_LIST: [
                {
                    "id": 884,
                    "values": {
                        "HtmlValue": (
                            f'<p><span data-mention-id="{SHAWN_MENTION_ID}" '
                            f'data-mention-name="Shawn McKibben" '
                            f'class="prosemirror-mention-node">{SHAWN_MCKIBBEN}'
                            "</span> HOLD (price-does-not-match) on McMaster-Carr "
                            "71001379</p>"
                        )
                    },
                }
            ]
        },
    }
    proof = comment_tab_proof(record)
    assert proof["tab_persisted"] is True
    assert proof["comments_persisted"] is True
    assert proof["item_ids"] == [884]
    assert proof["mention_node"] is True
    assert proof["notify_confirmed"] is False
    assert proof["worked"] is False
    assert "Comments tab persisted" in proof["report"]
    assert "Comments persisted" not in proof["report"]


def test_comment_tab_payload_never_writes_header_comments():
    html = shawn_mention_html(
        "@Shawn McKibben HOLD (price-does-not-match) on McMaster-Carr 71001379"
    )
    payload = comment_tab_add_payload(html, invoice_id=10140)
    assert "Comments" not in payload
    assert "Comments" not in (payload.get("values") or {})
    assert COMMENT_TAB_LIST in payload["lists"]
    values = payload["lists"][COMMENT_TAB_LIST][0]["values"]
    assert values["HtmlValue"] == html
    assert values["Entity"]["id"] == 203
    assert values["ObjectId"] == 10140
    assert values["FormId"]["id"] == 218
    assert f'data-mention-id="{SHAWN_MENTION_ID}"' in html
    assert SHAWN_MCKIBBEN in html

    batch = transfer_ap_batch_only_payload(invoice_id=10140, batch_id=375)
    assert batch["values"] == {"AP_Invoice_Batch": {"id": 375}}
    assert "Comments" not in batch["values"]


def test_format_report_never_uses_comments_persisted_phrase():
    header_only = format_comment_tab_report(
        tab_ok=False,
        matched=[],
        mention_nodes=[],
        header=f"{SHAWN_MCKIBBEN} HOLD",
    )
    assert "Comments persisted" not in header_only
    tab_ok = format_comment_tab_report(
        tab_ok=True,
        matched=[{"id": 884}],
        mention_nodes=["<span data-mention-id=\"104\">@Shawn McKibben</span>"],
        header=f"{SHAWN_MCKIBBEN} HOLD",
    )
    assert "Comments persisted" not in tab_ok
    assert "Comments tab persisted" in tab_ok


def test_add_invoice_comment_tab_proves_via_get_not_put_status():
    class _Fake:
        def __init__(self):
            self.updates = []

        def get_item(self, _svc, kid):
            if not self.updates:
                return {
                    "id": kid,
                    "values": {"Comments": f"{SHAWN_MCKIBBEN} header only"},
                    "lists": {COMMENT_TAB_LIST: []},
                }
            row = self.updates[-1]["lists"][COMMENT_TAB_LIST][0]
            return {
                "id": kid,
                "values": {"Comments": f"{SHAWN_MCKIBBEN} header only"},
                "lists": {COMMENT_TAB_LIST: [{"id": 884, "values": row["values"]}]},
            }

        def update(self, _svc, _kid, payload):
            self.updates.append(payload)
            return {"id": _kid}, 200, ""

    fake = _Fake()
    out = add_invoice_comment_tab(
        fake,
        invoice_id=10140,
        body=f"{SHAWN_MCKIBBEN} HOLD (price-does-not-match) on McMaster-Carr 71001379",
    )
    assert out["tab_persisted"] is True
    assert out["item_ids"] == [884]
    assert "Comments" not in fake.updates[0]
    assert "Comments" not in (fake.updates[0].get("values") or {})
    assert "Comments persisted" not in out["report"]
