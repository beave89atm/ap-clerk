"""File completed 2026-10-08 AP emails. No KIMCO writes and no Mail.Send.

Entered invoices: category only 'Entered in AI', then Inbox/9 - FORT WORTH ARCHIVE.
Autopay invoices: category only 'AI Skipped', then Inbox/AutoPay Archive.
A message is left in the Inbox when it cannot be re-found, or when
lastModifiedDateTime changed after the inbox listing.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    ENTERED_IN_AI_CATEGORY,
    LEGACY_AI_SKIPPED_CATEGORY,
    GraphClient,
    load_graph_credentials,
)
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of, stamp
from scripts.sept_archive_check_2026_10_07 import plain

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("ap-file-1008")

OUT = ROOT / "runs" / "ap-run-2026-10-08"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime"
ARCHIVE_NAME = "9 - FORT WORTH ARCHIVE"
AUTOPAY_NAME = "AutoPay Archive"

ENTERED_RECEIVED = [
    "2026-10-01T16:09:29Z",
    "2026-10-01T20:02:59Z",
    "2026-10-01T20:17:17Z",
    "2026-10-01T23:02:00Z",
    "2026-10-02T04:39:10Z",
    "2026-10-02T04:40:07Z",
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
]
AUTOPAY_RECEIVED = [
    "2026-10-01T13:21:40Z",
    "2026-10-01T13:29:47Z",
    "2026-10-01T22:35:20Z",
]


def categories_of(message: dict[str, Any]) -> list[str]:
    return [str(item) for item in (message.get("categories") or [])]


def find_one(graph: GraphClient, expected: dict[str, Any], days: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    day = date.fromisoformat(expected["received"][:10])
    key = day.isoformat()
    if key not in days:
        days[key] = graph.list_messages(ALLOWED_MAILBOX, received_from=day, received_to=day)
    found = []
    for row in days[key]:
        if str(row.get("receivedDateTime") or "") != expected["received"]:
            continue
        if sender_of(row).lower() != expected["sender"].lower():
            continue
        if plain(str(row.get("subject") or "")) != plain(expected["subject"]):
            continue
        full = graph.get_message(ALLOWED_MAILBOX, str(row.get("id") or ""), select=SELECT)
        if (
            str(full.get("receivedDateTime") or "") == expected["received"]
            and sender_of(full).lower() == expected["sender"].lower()
            and plain(str(full.get("subject") or "")) == plain(expected["subject"])
        ):
            found.append(full)
    unique = {str(item.get("id") or ""): item for item in found if item.get("id")}
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def child_folder(graph: GraphClient, name: str) -> str:
    if name == ARCHIVE_NAME:
        return archive_folder(graph)
    response = graph.request(
        "GET",
        graph._user_url(ALLOWED_MAILBOX, "mailFolders/inbox/childFolders"),
        params={"$select": "id,displayName", "$top": 50},
    )
    if response.status_code != 200:
        raise SystemExit(f"Inbox child folders HTTP {response.status_code}")
    hits = [
        item
        for item in (response.json() or {}).get("value") or []
        if str(item.get("displayName") or "") == name and item.get("id")
    ]
    if len(hits) != 1:
        raise SystemExit(f"Folder {name} was found {len(hits)} times. No mail changed.")
    return str(hits[0]["id"])


def file_one(
    graph: GraphClient,
    expected: dict[str, Any],
    days: dict[str, list[dict[str, Any]]],
    cache: dict[str, dict[str, Any]],
    dest_id: str,
    dest_name: str,
    category: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"received": expected["received"], "subject": expected["subject"], "moved": "n"}
    message = find_one(graph, expected, days)
    if message is None:
        result["result"] = "not-found"
        return result
    path = folder_path(graph, str(message.get("parentFolderId") or ""), cache)
    result["folder_before"] = path
    if path != "Inbox":
        result["result"] = f"not-inbox:{path}"
        return result
    if stamp(message.get("lastModifiedDateTime")) != stamp(expected["lastModifiedDateTime"]):
        result["result"] = "lastModified-changed"
        result["lastModified"] = message.get("lastModifiedDateTime")
        return result
    if categories_of(message) != list(expected.get("categories") or []):
        result["result"] = "categories-changed"
        return result
    old_id = str(message.get("id") or "")
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, old_id),
        json={"categories": [category]},
        headers={"Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        result["result"] = f"category-http-{response.status_code}"
        return result
    outcome = graph.move_message(ALLOWED_MAILBOX, old_id, dest_id)
    new_id = str(outcome.get("new_id") or "")
    if outcome.get("status") != "moved-fort-worth" or not new_id:
        result["result"] = f"move-failed:{outcome.get('status')}"
        return result
    after = graph.get_message(ALLOWED_MAILBOX, new_id, select=SELECT)
    after_path = folder_path(graph, str(after.get("parentFolderId") or ""), cache)
    after_cats = categories_of(after)
    ok = after_path == f"Inbox/{dest_name}" and after_cats == [category]
    result.update(
        {
            "moved": "y" if ok else "n",
            "result": "moved" if ok else f"verify-failed:{after_path}:{after_cats}",
            "new_id": new_id,
            "folder_after": after_path,
            "categories_after": after_cats,
        }
    )
    LOGGER.info("%s %s", result["result"], expected["subject"])
    return result


def main() -> None:
    listing = json.loads((OUT / "inbox-listing.json").read_text())["rows"]
    by_received = {row["received"]: row for row in listing}
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    fort = child_folder(graph, ARCHIVE_NAME)
    autopay = child_folder(graph, AUTOPAY_NAME)
    days: dict[str, list[dict[str, Any]]] = {}
    cache: dict[str, dict[str, Any]] = {}
    rows = []
    for received in ENTERED_RECEIVED:
        rows.append(
            file_one(graph, by_received[received], days, cache, fort, ARCHIVE_NAME, ENTERED_IN_AI_CATEGORY)
        )
    for received in AUTOPAY_RECEIVED:
        rows.append(
            file_one(graph, by_received[received], days, cache, autopay, AUTOPAY_NAME, LEGACY_AI_SKIPPED_CATEGORY)
        )
    (OUT / "mail-moves.json").write_text(json.dumps(rows, indent=2))
    progress_path = OUT / "progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
        moved = {row["received"] for row in rows if row.get("moved") == "y" and row["received"] in ENTERED_RECEIVED}
        for row in progress:
            if row.get("received") in moved:
                row["email_moved"] = "y"
        progress_path.write_text(json.dumps(progress, indent=2))
    print(json.dumps([{"received": row["received"], "result": row["result"], "moved": row["moved"]} for row in rows], indent=2))


if __name__ == "__main__":
    main()
