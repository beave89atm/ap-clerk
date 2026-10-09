"""Download selected October invoice PDFs. Read-only. No mail changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials
from ap_clerk.pdf_invoice import parse_invoice_pdf

LISTING = ROOT / "runs" / "ap-run-2026-10-09" / "inbox-listing.json"
DEST = Path("/tmp/ap-run-1009")
WANTED = {
    "2026-10-01T14:55:40Z",
    "2026-10-01T19:11:57Z",
    "2026-10-02T00:59:06Z",
    "2026-10-02T02:02:33Z",
    "2026-10-02T14:08:59Z",
    "2026-10-05T14:26:53Z",
    "2026-10-05T14:32:08Z",
    "2026-10-05T14:32:12Z",
    "2026-10-05T14:32:21Z",
    "2026-10-05T14:32:22Z",
    "2026-10-05T14:32:33Z",
    "2026-10-05T14:49:56Z",
    "2026-10-05T17:43:56Z",
    "2026-10-05T18:06:54Z",
    "2026-10-05T19:00:16Z",
    "2026-10-05T19:14:46Z",
    "2026-10-05T19:50:36Z",
    "2026-10-05T20:02:28Z",
    "2026-10-05T20:52:55Z",
    "2026-10-05T21:15:51Z",
    "2026-10-05T23:25:45Z",
    "2026-10-06T01:12:44Z",
    "2026-10-06T03:09:51Z",
    "2026-10-06T04:30:39Z",
    "2026-10-06T04:38:48Z",
    "2026-10-06T05:06:44Z",
    "2026-10-06T13:01:46Z",
    "2026-10-06T14:33:31Z",
    "2026-10-06T14:37:04Z",
    "2026-10-06T14:46:00Z",
    "2026-10-06T15:15:32Z",
    "2026-10-06T17:47:14Z",
    "2026-10-06T18:26:30Z",
    "2026-10-06T18:36:30Z",
    "2026-10-06T20:31:05Z",
    "2026-10-06T21:13:36Z",
    "2026-10-06T23:02:04Z",
    "2026-10-07T04:27:55Z",
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
        stamp = row["received"].replace(":", "").replace("-", "")
        pdfs = graph.download_pdf_attachments(ALLOWED_MAILBOX, row["id"]) if row["hasAttachments"] else []
        docs = []
        for index, (name, content) in enumerate(pdfs, start=1):
            path = DEST / f"{stamp}-{index}.pdf"
            path.write_bytes(content)
            document = pymupdf.open(path)
            images = []
            texts = []
            for page_index, page in enumerate(document, start=1):
                image = DEST / f"{stamp}-{index}-p{page_index}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).save(str(image))
                images.append(str(image))
                texts.append(page.get_text("text"))
            parsed = parse_invoice_pdf(
                path,
                subject=row["subject"],
                from_name=row.get("from_name") or "",
                from_address=row["sender"],
            )
            docs.append(
                {
                    "file": name,
                    "path": str(path),
                    "pages": document.page_count,
                    "vendor": parsed.get("vendor"),
                    "invoice_number": parsed.get("invoice_number"),
                    "date": str(parsed.get("date") or ""),
                    "po": parsed.get("po"),
                    "amount": parsed.get("amount"),
                    "sales_tax": parsed.get("sales_tax"),
                    "lines": parsed.get("lines"),
                    "text_head": "\n".join(texts)[:2500],
                }
            )
            print(f"{row['received']} {name} pages={document.page_count} inv={parsed.get('invoice_number')} amt={parsed.get('amount')} po={parsed.get('po')}", flush=True)
        if not pdfs:
            print(f"{row['received']} NO PDF {row['subject'][:80]}", flush=True)
        summary.append(
            {
                "received": row["received"],
                "sender": row["sender"],
                "subject": row["subject"],
                "docs": docs,
            }
        )
    (DEST / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("wrote", len(summary))


if __name__ == "__main__":
    main()
