"""GET-only accountspayable@ vendor catalog. No Mail.Send / KIMCO / Transfer AP."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (  # noqa: E402
    ALLOWED_MAILBOX,
    GraphClient,
    GraphError,
    assert_allowed_mailbox,
    load_graph_credentials,
)
from ap_clerk.inbox_vendors import (  # noqa: E402
    COUNT_WINDOW_START,
    SCAN_FLOOR,
    catalog_messages,
    catalog_to_json,
    default_sheet_path,
    write_vendor_workbook,
)

LOGGER = logging.getLogger("ap_clerk.inbox_vendors")


def _client() -> GraphClient:
    creds = load_graph_credentials()
    if not creds.ready:
        raise GraphError(creds.error or "Graph credentials missing")
    return GraphClient.authenticate(
        creds.tenant_id or "",
        creds.client_id or "",
        creds.client_secret or "",
    )


def scan_mailbox(
    graph: GraphClient,
    *,
    mailbox: str = ALLOWED_MAILBOX,
    received_from: date = SCAN_FLOOR,
    received_to: date | None = None,
) -> list[dict]:
    mailbox = assert_allowed_mailbox(mailbox)
    LOGGER.info(
        "Listing %s messages received_from=%s received_to=%s (metadata only, no PDF)",
        mailbox,
        received_from,
        received_to,
    )
    messages = graph.list_messages(
        mailbox,
        received_from=received_from,
        received_to=received_to,
        unflagged_only=False,
        include_attachment_names=False,
        oldest_first=True,
    )
    LOGGER.info("Listed %s messages", len(messages))
    return messages


def write_outputs(catalog, *, sheet_path: Path) -> tuple[Path, Path]:
    write_vendor_workbook(sheet_path, catalog)
    sidecar = sheet_path.with_suffix(".json")
    sidecar.write_text(json.dumps(catalog_to_json(catalog), indent=2) + "\n")
    LOGGER.info("Wrote %s and %s", sheet_path, sidecar)
    return sheet_path, sidecar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catalog unique vendors in accountspayable@")
    parser.add_argument("--from-date", default=SCAN_FLOOR.isoformat(), help="Scan floor YYYY-MM-DD")
    parser.add_argument("--to-date", default="", help="Optional inclusive end YYYY-MM-DD")
    parser.add_argument(
        "--count-window",
        default=COUNT_WINDOW_START.isoformat(),
        help="Counts in this column are Aug 1+ unless overridden",
    )
    parser.add_argument("--out", default="", help="xlsx path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mailbox = assert_allowed_mailbox(ALLOWED_MAILBOX)
    received_from = date.fromisoformat(args.from_date)
    received_to = date.fromisoformat(args.to_date) if args.to_date else None
    window_start = date.fromisoformat(args.count_window)
    sheet_path = Path(args.out) if args.out else default_sheet_path(date.today())

    graph = _client()
    messages = scan_mailbox(
        graph,
        mailbox=mailbox,
        received_from=received_from,
        received_to=received_to,
    )
    catalog = catalog_messages(messages, window_start=window_start)
    write_outputs(catalog, sheet_path=sheet_path)
    names = catalog.unique_vendor_names()
    print(f"mailbox={mailbox}")
    print(f"scanned={catalog.scanned_from} .. {catalog.scanned_to}")
    print(f"messages={catalog.messages_scanned} invoice_looking={catalog.invoice_emails} skip={catalog.skip_emails}")
    print(f"unique_vendors={len(names)}")
    print(f"sheet={sheet_path}")
    print("unique_vendor_names:")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
