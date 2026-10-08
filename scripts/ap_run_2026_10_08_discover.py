"""Read-only Inbox listing for the 2026-10-08 AP run. No KIMCO writes, no mail moves."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials

OUT = ROOT / "runs" / "ap-run-2026-10-08" / "inbox-listing.json"
SELECT = "id,subject,from,receivedDateTime,hasAttachments,categories,parentFolderId,lastModifiedDateTime"
# Oct 1 00:00 America/Chicago (CDT, UTC-5).
START = "2026-10-01T05:00:00Z"
# Mail after 2026-10-06 02:00 CT is younger than 48 hours before the 2am run.
CUTOFF = "2026-10-06T07:00:00Z"
JP_CUTOFF = "2026-10-05T07:00:00Z"


def sender(message: dict[str, Any]) -> str:
    return str((((message.get("from") or {}).get("emailAddress") or {}).get("address") or "")).strip()


def name_of(message: dict[str, Any]) -> str:
    return str((((message.get("from") or {}).get("emailAddress") or {}).get("name") or "")).strip()


def main() -> None:
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    messages: list[dict[str, Any]] = []
    url: str | None = graph._user_url(ALLOWED_MAILBOX, "mailFolders/inbox/messages")
    first = True
    while url:
        kwargs: dict[str, Any] = {}
        if first:
            kwargs["params"] = {
                "$select": SELECT,
                "$orderby": "receivedDateTime asc",
                "$filter": f"receivedDateTime ge {START}",
                "$top": 50,
            }
            first = False
        response = graph.request("GET", url, **kwargs)
        if response.status_code != 200:
            raise SystemExit(f"Inbox list HTTP {response.status_code}: {response.text[:400]}")
        payload = response.json() or {}
        chunk = payload.get("value") or []
        messages.extend(chunk)
        print(f"page {len(chunk)} total {len(messages)}", flush=True)
        url = payload.get("@odata.nextLink")
    rows = []
    for message in messages:
        received = str(message.get("receivedDateTime") or "")
        rows.append(
            {
                "id": message.get("id"),
                "received": received,
                "sender": sender(message),
                "from_name": name_of(message),
                "subject": message.get("subject") or "",
                "hasAttachments": bool(message.get("hasAttachments")),
                "categories": message.get("categories") or [],
                "parentFolderId": message.get("parentFolderId"),
                "lastModifiedDateTime": message.get("lastModifiedDateTime"),
                "too_new": received > CUTOFF,
                "jp_too_new": received > JP_CUTOFF,
            }
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"count": len(rows), "cutoff": CUTOFF, "rows": rows}, indent=2))
    in_scope = [row for row in rows if not row["too_new"]]
    print(
        json.dumps(
            {
                "inbox_from_oct1": len(rows),
                "in_scope_48h": len(in_scope),
                "too_new_messages": len(rows) - len(in_scope),
                "oldest": rows[0]["received"] if rows else None,
                "newest": rows[-1]["received"] if rows else None,
            }
        )
    )


if __name__ == "__main__":
    main()
