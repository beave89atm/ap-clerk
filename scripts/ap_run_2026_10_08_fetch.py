"""Download the oldest October invoice PDFs. Read-only. No mail changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import fitz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials
from ap_clerk.pdf_invoice import extract_pdf_text, parse_invoice_pdf

LISTING = ROOT / "runs" / "ap-run-2026-10-08" / "inbox-listing.json"
DEST = Path("/tmp/ap-run-1008")
WANTED = {
    "2026-10-01T08:33:42Z",
    "2026-10-01T13:15:16Z",
    "2026-10-01T13:21:40Z",
    "2026-10-01T13:29:47Z",
    "2026-10-01T14:55:40Z",
    "2026-10-01T16:09:29Z",
    "2026-10-01T18:13:33Z",
    "2026-10-01T19:11:57Z",
    "2026-10-01T20:02:59Z",
    "2026-10-01T20:17:17Z",
    "2026-10-01T22:35:20Z",
    "2026-10-01T23:02:00Z",
    "2026-10-02T04:39:10Z",
    "2026-10-02T04:40:07Z",
    "2026-10-02T13:43:21Z",
    "2026-10-02T13:43:28Z",
    "2026-10-02T13:52:50Z",
    "2026-10-02T14:46:05Z",
    "2026-10-02T15:47:30Z",
    "2026-10-02T16:01:51Z",
    "2026-10-02T16:12:16Z",
    "2026-10-02T16:13:24Z",
    "2026-10-02T16:32:39Z",
    "2026-10-02T16:36:46Z",
    "2026-10-02T16:37:58Z",
    "2026-10-02T16:38:57Z",
    "2026-10-02T23:01:51Z",
}


def main() -> None:
    rows = json.loads(LISTING.read_text())["rows"]
    chosen = [row for row in rows if row["received"] in WANTED]
    missing = WANTED - {row["received"] for row in chosen}
    if missing:
        raise SystemExit(f"Missing messages {sorted(missing)}")
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    DEST.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for row in chosen:
        mid = row["id"]
        stamp = row["received"].replace(":", "").replace("-", "")
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, mid) if row["hasAttachments"] else []
        body = ""
        if not pdfs:
            message = graph.get_message(
                ALLOWED_MAILBOX,
                mid,
                select="id,subject,bodyPreview,body",
            )
            body = str(((message.get("body") or {}).get("content") or message.get("bodyPreview") or ""))
            (DEST / f"{stamp}-body.txt").write_text(body[:20000])
        parsed_docs = []
        for index, (name, content) in enumerate(pdfs, start=1):
            path = DEST / f"{stamp}-{index}.pdf"
            path.write_bytes(content)
            doc = fitz.open(path)
            pages = []
            for page_index, page in enumerate(doc, start=1):
                image = DEST / f"{stamp}-{index}-p{page_index}.png"
                page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).save(str(image))
                pages.append(str(image))
            parsed = parse_invoice_pdf(path, subject=row["subject"], from_name=row.get("from_name") or "", from_address=row["sender"])
            parsed_docs.append(
                {
                    "file": name,
                    "path": str(path),
                    "pages": doc.page_count,
                    "images": pages,
                    "vendor": parsed.get("vendor"),
                    "invoice_number": parsed.get("invoice_number"),
                    "date": str(parsed.get("date") or ""),
                    "po": parsed.get("po"),
                    "amount": parsed.get("amount"),
                    "sales_tax": parsed.get("sales_tax"),
                    "lines": len(parsed.get("lines") or []),
                    "hold_reason": parsed.get("hold_reason"),
                }
            )
            print(
                f"{row['received']} {name} pages={doc.page_count} vendor={parsed.get('vendor')} "
                f"inv={parsed.get('invoice_number')} amt={parsed.get('amount')} po={parsed.get('po')}",
                flush=True,
            )
        summary.append(
            {
                "received": row["received"],
                "sender": row["sender"],
                "subject": row["subject"],
                "pdfs": parsed_docs,
                "body_saved": bool(body),
            }
        )
    (DEST / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"saved {len(summary)} messages")


if __name__ == "__main__":
    main()
