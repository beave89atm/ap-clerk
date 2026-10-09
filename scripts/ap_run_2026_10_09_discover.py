"""Read-only Inbox listing for the 2026-10-09 AP run. No KIMCO writes, no mail moves."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import ALLOWED_MAILBOX, GraphClient, load_graph_credentials
from ap_clerk.rules import classify_mail

OUT = ROOT / "runs" / "ap-run-2026-10-09" / "inbox-listing.json"
SELECT = "id,subject,from,receivedDateTime,hasAttachments,categories,parentFolderId,lastModifiedDateTime,bodyPreview"
# Oct 1 00:00 America/Chicago (CDT, UTC-5).
START = "2026-10-01T05:00:00Z"
# Mail after 2026-10-07 02:00 CT is younger than 48 hours before the 2am 10/9 run.
CUTOFF = "2026-10-07T07:00:00Z"
JP_CUTOFF = "2026-10-06T07:00:00Z"


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
        subject = str(message.get("subject") or "")
        from_name = name_of(message)
        preview = str(message.get("bodyPreview") or "")
        kind = classify_mail(subject=subject, preview=preview, from_name=from_name)
        blob = f"{from_name} {sender(message)} {subject}".lower()
        if any(token in blob for token in ("spectrum", "culligan", "toyota", "waste connection", "engie")):
            kind = "auto-pay"
        rows.append(
            {
                "id": message.get("id"),
                "received": received,
                "sender": sender(message),
                "from_name": from_name,
                "subject": subject,
                "preview": preview[:240],
                "hasAttachments": bool(message.get("hasAttachments")),
                "categories": message.get("categories") or [],
                "parentFolderId": message.get("parentFolderId"),
                "lastModifiedDateTime": message.get("lastModifiedDateTime"),
                "kind": kind,
                "too_new": received > CUTOFF,
                "jp_too_new": received > JP_CUTOFF and "jp steel" in blob,
            }
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"count": len(rows), "cutoff": CUTOFF, "jp_cutoff": JP_CUTOFF, "rows": rows}, indent=2))
    in_scope = [row for row in rows if not row["too_new"] and not row["jp_too_new"]]
    print(
        json.dumps(
            {
                "inbox_from_oct1": len(rows),
                "in_scope_48h": len(in_scope),
                "too_new_messages": sum(1 for row in rows if row["too_new"] or row["jp_too_new"]),
                "oldest": rows[0]["received"] if rows else None,
                "newest": rows[-1]["received"] if rows else None,
                "kinds": {
                    kind: sum(1 for row in rows if row["kind"] == kind)
                    for kind in sorted({row["kind"] for row in rows})
                },
            }
        )
    )


if __name__ == "__main__":
    main()
