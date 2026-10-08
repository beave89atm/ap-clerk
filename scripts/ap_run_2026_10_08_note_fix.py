"""Set missing Hudson and AVEX GL accounts, and correct the UniFirst hold note."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.kimco import added_comment_payload
from ap_clerk.rules import TREYCE_MENTION_HTML, lookup_id, lookup_text
from scripts.ap_run_2026_10_08_enter import OUT
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_missed_entry_2026_10_07 import put

NOTE = (
    "AP Clerk: UniFirst invoice 2810822429 is on hold and is not posted. "
    "The PDF total is $928.54. Uniforms and aprons are $608.85 and shop supplies are $250.47. "
    "The invoice prints sales tax of $69.22. KIMCO calculated $69.26, so the bill is $0.04 high. "
    "The bill is in Transfer AP. "
    + "@Treyce Hodges"
)


def line_gl(record: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for line in (record.get("lists") or {}).get("APInvoiceLine") or []:
        values = line.get("values") or {}
        gl = values.get("Purchase_GL_Account")
        rows.append({"id": line.get("id"), "gl_id": lookup_id(gl), "gl": lookup_text(gl)})
    return rows


def main() -> None:
    client = login()
    install_401_guard(client)
    sample = client.get_item("ap_invoices", 9254)
    sample_gl = line_gl(sample)
    print("avex sample gl", sample_gl)
    for invoice_id, wanted in ((10553, 75), (10561, sample_gl[0]["gl_id"] if sample_gl else None)):
        record = client.get_item("ap_invoices", invoice_id)
        current = line_gl(record)
        print("before", invoice_id, current)
        if not wanted:
            continue
        for line in current:
            if line["gl_id"] == wanted:
                continue
            status = put(
                client,
                invoice_id,
                {
                    "id": invoice_id,
                    "state": "Modified",
                    "lists": {
                        "APInvoiceLine": [
                            {
                                "id": line["id"],
                                "state": "Modified",
                                "values": {"Purchase_GL_Account": {"id": int(wanted)}},
                            }
                        ]
                    },
                },
            )
            print("gl put", invoice_id, line["id"], status)
        print("after", invoice_id, line_gl(client.get_item("ap_invoices", invoice_id)))
    record = client.get_item("ap_invoices", 10562)
    comments = (record.get("lists") or {}).get("Comments_1") or []
    for comment in comments:
        html = str((comment.get("values") or {}).get("HtmlValue") or "")
        if "UniFirst invoice 2810822429" not in html:
            continue
        status = put(
            client,
            10562,
            {"id": 10562, "state": "Modified", "lists": {"Comments_1": [{"id": comment["id"], "state": "Removed"}]}},
        )
        print("removed", comment["id"], status)
    html = f"<p>{NOTE.replace('@Treyce Hodges', TREYCE_MENTION_HTML, 1)}</p>"
    if 'data-mention-id="33"' not in html:
        raise SystemExit("UniFirst note lost the Treyce mention")
    status = put(client, 10562, added_comment_payload(10562, html))
    print("added note", status)
    after = client.get_item("ap_invoices", 10562)
    for comment in (after.get("lists") or {}).get("Comments_1") or []:
        text = str((comment.get("values") or {}).get("HtmlValue") or "")
        if "2810822429" in text:
            print("note id", comment.get("id"), "mention", 'data-mention-id="33"' in text)
    progress = json.loads((OUT / "progress.json").read_text())
    for row in progress:
        if row.get("invoice") == "2810822429":
            row["result"] = "HOLD"
            row["reason"] = "Printed sales tax is $69.22. KIMCO calculated $69.26, a $0.04 difference. Receipts were not involved."
            row["note"] = NOTE.replace("@Treyce Hodges", "@Treyce Hodges")
            row["covered"] = 928.58
    (OUT / "progress.json").write_text(json.dumps(progress, indent=2))


if __name__ == "__main__":
    main()
