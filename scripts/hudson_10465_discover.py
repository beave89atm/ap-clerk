"""Read-only look at bill 10465 and posted Hudson Energy bills. No writes."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sept25_30_attachment_audit import attachment_bytes, attachment_name, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login, slim_bill

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("hudson-discover")

OUT = Path("/tmp/hudson-10465-discover.json")
PDF_DIR = Path("/tmp/hudson-10465")


def vendor_text(item: dict[str, Any]) -> str:
    values = item.get("values") if isinstance(item.get("values"), dict) else {}
    vendor = values.get("Vendor_$_Display_Name") or values.get("Vendor")
    if isinstance(vendor, dict):
        return str(vendor.get("text") or "")
    return str(vendor or "")


def main() -> None:
    client = login()
    install_401_guard(client)
    current = client.get_item("ap_invoices", 10465)
    before = slim_bill(current)
    attachments = client.list_attachments(10465)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in attachments:
        content = attachment_bytes(client, 10465, item)
        name = attachment_name(item) or "attachment.pdf"
        path = PDF_DIR / name
        pages = []
        if content:
            path.write_bytes(content)
            if content[:5] == b"%PDF-":
                texts = page_texts(content)
                pages = [{"page": index, "text": text} for index, text in enumerate(texts, start=1)]
        saved.append(
            {
                "id": item.get("id"),
                "name": name,
                "bytes": len(content or b""),
                "pages": pages,
            }
        )
        LOGGER.info("Attachment %s bytes %s pages %s", name, len(content or b""), len(pages))
    rows = client.list_items("ap_invoices")
    if rows:
        LOGGER.info("Invoice list keys %s", sorted((rows[0].get("values") or {})))
    hits = []
    for row in rows:
        if "hudson" not in vendor_text(row).lower():
            continue
        hits.append(int(row["id"]))
    LOGGER.info("Hudson list hits %s", len(hits))
    priors = []
    for bill_id in hits:
        record = current if bill_id == 10465 else client.get_item("ap_invoices", bill_id)
        slim = before if bill_id == 10465 else slim_bill(record)
        priors.append(slim)
        LOGGER.info(
            "Hudson %s invoice %s posted %s type %s lines %s charges %s taxes %s",
            bill_id,
            slim.get("invoice"),
            slim.get("posted"),
            slim.get("type"),
            len(slim.get("lines") or []),
            len(slim.get("charges") or []),
            len(slim.get("taxes") or []),
        )
    OUT.write_text(json.dumps({"before": before, "attachments": saved, "hudson": priors}, indent=2, default=str))
    LOGGER.info("Wrote %s", OUT)


if __name__ == "__main__":
    main()
