"""NOTE-52 dry-lookup: resolve Inbox 9 - FORT WORT… folder on accountspayable@.

Live Graph list of Inbox child folders only. No message move. No Mail.Send.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    GraphClient,
    GraphError,
    is_fort_worth_inbox_folder,
    load_graph_credentials,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if not args.live:
        raise SystemExit("Refusing: pass --live for Graph folder lookup (no move).")
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials not ready")
    client = GraphClient.authenticate(
        creds.tenant_id or "", creds.client_id or "", creds.client_secret or ""
    )
    try:
        folders = client.list_inbox_child_folders(ALLOWED_MAILBOX)
        resolved = client.resolve_fort_worth_folder(ALLOWED_MAILBOX)
    except GraphError as exc:
        raise SystemExit(f"Graph folder lookup failed: {exc}") from exc
    proof = {
        "proof": "fort-worth-folder-lookup-2026-09-22",
        "mailbox": ALLOWED_MAILBOX,
        "mail_send": False,
        "moved_any_message": False,
        "inbox_child_folders": [
            {
                "id": f.get("id"),
                "displayName": f.get("displayName"),
                "match": is_fort_worth_inbox_folder(str(f.get("displayName") or "")),
            }
            for f in folders
        ],
        "resolved": resolved,
    }
    print(json.dumps(proof, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
