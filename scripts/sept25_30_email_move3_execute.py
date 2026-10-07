"""File the three approved Inbox messages into Inbox/9 - FORT WORTH ARCHIVE.

No KIMCO writes. No Mail.Send. Touches only the Gas 2026-09-29T04:48:59Z,
EMJ 2026-09-30T03:11:51Z, and O'Neal 2026-10-01T04:29:10Z messages.
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.graph import (
    ALLOWED_MAILBOX,
    ENTERED_WITH_ISSUES_CATEGORY,
    GraphClient,
    GraphError,
    load_graph_credentials,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("email-move3")

DRYRUN = ROOT / "runs" / "sept25-30-email-move3-dryrun.csv"
EXECUTE = ROOT / "runs" / "sept25-30-email-move3-execute.csv"
VERIFY = ROOT / "runs" / "sept25-30-email-move3-verify.csv"
ARCHIVE_NAME = "9 - FORT WORTH ARCHIVE"
PROPOSED_FOLDER = f"Inbox/{ARCHIVE_NAME}"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime"

EXPECTED = {
    "2026-09-29T04:48:59Z": {
        "sender": "billing@gasandsupply.com",
        "subject": "Gas&Supply Invoice/Statement",
        "lastModifiedDateTime": "2026-10-06T14:56:45Z",
    },
    "2026-09-30T03:11:51Z": {
        "sender": "EMJCreditSouth@emjmetals.com",
        "subject": "EARLE M. JORGENSEN COMPANY - Invoices for 09/29/26",
        "lastModifiedDateTime": "2026-10-06T14:59:14Z",
    },
    "2026-10-01T04:29:10Z": {
        "sender": "vsanders@onealsteel.com",
        "subject": " O'Neal Steel Invoice For Account # 14748440 ",
        "lastModifiedDateTime": "2026-10-06T14:56:57Z",
    },
}


def stamp(value: Any) -> str:
    text = str(value or "").strip().replace("Z", "")
    if "." in text:
        text = text.split(".", 1)[0]
    return text


def sender_of(message: dict[str, Any]) -> str:
    return str((((message.get("from") or {}).get("emailAddress") or {}).get("address") or "")).strip()


def load_messages() -> list[dict[str, str]]:
    with DRYRUN.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 13:
        raise SystemExit(f"Dry run has {len(rows)} rows. Expected 13. No mail changed.")
    by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["flag"]:
            raise SystemExit("Dry run row is flagged. No mail changed.")
        if row["current folder path"] != "Inbox":
            raise SystemExit("Dry run row is not Inbox. No mail changed.")
        if row["categories"]:
            raise SystemExit("Dry run row already has categories. No mail changed.")
        if row["proposed category"] != ENTERED_WITH_ISSUES_CATEGORY:
            raise SystemExit("Dry run category is not Entered with issues. No mail changed.")
        if row["proposed folder"] != PROPOSED_FOLDER:
            raise SystemExit("Dry run folder is not the Fort Worth archive. No mail changed.")
        expected = EXPECTED.get(row["received"])
        if expected is None:
            raise SystemExit("Dry run includes a message outside the three. No mail changed.")
        if row["sender address"].strip().lower() != expected["sender"].lower():
            raise SystemExit("Dry run sender does not match the approved message. No mail changed.")
        if row["subject"] != expected["subject"]:
            raise SystemExit("Dry run subject does not match the approved message. No mail changed.")
        if stamp(row["lastModifiedDateTime"]) != stamp(expected["lastModifiedDateTime"]):
            raise SystemExit("Dry run lastModifiedDateTime does not match. No mail changed.")
        message_id = row["current message id"].strip()
        if not message_id:
            raise SystemExit("Dry run row has no message id. No mail changed.")
        prior = by_id.get(message_id)
        if prior is None:
            by_id[message_id] = row
            continue
        for field in ("received", "sender address", "subject", "lastModifiedDateTime"):
            if prior[field] != row[field]:
                raise SystemExit("Invoice rows for one message disagree. No mail changed.")
    if len(by_id) != 3:
        raise SystemExit(f"Dry run has {len(by_id)} messages. Expected 3. No mail changed.")
    received = {row["received"] for row in by_id.values()}
    if received != set(EXPECTED):
        raise SystemExit("Dry run is missing one of the three messages. No mail changed.")
    return [by_id[key] for key in sorted(by_id, key=lambda item: by_id[item]["received"])]


def folder_path(graph: GraphClient, folder_id: str, cache: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    current = folder_id
    while current and current not in seen and len(parts) < 8:
        seen.add(current)
        if current not in cache:
            response = graph.request(
                "GET",
                graph._user_url(ALLOWED_MAILBOX, f"mailFolders/{current}"),
                params={"$select": "id,displayName,parentFolderId"},
            )
            if response.status_code != 200:
                return ""
            cache[current] = response.json() or {}
        folder = cache[current]
        name = str(folder.get("displayName") or "")
        if name and name.lower() not in {"msgfolderroot", "top of information store"}:
            parts.append(name)
        current = str(folder.get("parentFolderId") or "")
    return "/".join(reversed(parts))


def archive_folder(graph: GraphClient) -> str:
    response = graph.request(
        "GET",
        graph._user_url(ALLOWED_MAILBOX, "mailFolders/inbox/childFolders"),
        params={"$select": "id,displayName,parentFolderId", "$top": 50},
    )
    if response.status_code != 200:
        raise SystemExit(f"Inbox child folder list HTTP {response.status_code}. No mail changed.")
    hits = [
        item
        for item in (response.json() or {}).get("value") or []
        if str(item.get("displayName") or "") == ARCHIVE_NAME
    ]
    if len(hits) != 1 or not hits[0].get("id"):
        raise SystemExit("Archive folder was not found exactly once. No mail changed.")
    return str(hits[0]["id"])


def fetch(graph: GraphClient, message_id: str) -> dict[str, Any] | None:
    try:
        return graph.get_message(ALLOWED_MAILBOX, message_id, select=SELECT)
    except GraphError:
        return None


def mismatch(row: dict[str, str], message: dict[str, Any], path: str) -> str:
    expected = EXPECTED[row["received"]]
    if path != "Inbox":
        return "not-in-inbox"
    if stamp(message.get("lastModifiedDateTime")) != stamp(expected["lastModifiedDateTime"]):
        return "lastModifiedDateTime-changed"
    if sender_of(message).lower() != expected["sender"].lower():
        return "sender-differs"
    if str(message.get("receivedDateTime") or "") != row["received"]:
        return "received-differs"
    if str(message.get("subject") or "").strip() != expected["subject"].strip():
        return "subject-differs"
    return ""


def set_category(graph: GraphClient, message_id: str) -> bool:
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        json={"categories": [ENTERED_WITH_ISSUES_CATEGORY]},
        headers={"Content-Type": "application/json"},
    )
    LOGGER.info("Category PATCH HTTP %s", response.status_code)
    return response.status_code < 400


def move(graph: GraphClient, message_id: str, folder_id: str) -> str:
    outcome = graph.move_message(ALLOWED_MAILBOX, message_id, folder_id)
    if outcome.get("status") != "moved-fort-worth":
        return ""
    return str(outcome.get("new_id") or "")


def find_messages(graph: GraphClient, received: str, subject: str, sender: str) -> list[dict[str, Any]]:
    wanted_subject = subject.strip()
    wanted_sender = sender.strip().lower()
    response = graph.request(
        "GET",
        graph._messages_url(ALLOWED_MAILBOX),
        params={
            "$filter": f"receivedDateTime eq {received}",
            "$select": SELECT,
            "$top": 50,
        },
    )
    rows = (response.json() or {}).get("value") or [] if response.status_code == 200 else []
    found = []
    for row in rows:
        if str(row.get("receivedDateTime") or "") != received:
            continue
        if str(row.get("subject") or "").strip() != wanted_subject:
            continue
        if sender_of(row).lower() != wanted_sender:
            continue
        found.append(row)
    if found:
        return found
    for hit in graph.search_messages(ALLOWED_MAILBOX, wanted_subject[:40], top=25):
        full = fetch(graph, str(hit.get("id") or ""))
        if not full:
            continue
        if str(full.get("receivedDateTime") or "") != received:
            continue
        if str(full.get("subject") or "").strip() != wanted_subject:
            continue
        if sender_of(full).lower() != wanted_sender:
            continue
        found.append(full)
    unique = {str(item.get("id") or ""): item for item in found}
    return list(unique.values())


def main() -> None:
    rows = load_messages()
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    folder_id = archive_folder(graph)
    cache: dict[str, dict[str, Any]] = {}
    executed = []
    for row in rows:
        old_id = row["current message id"]
        received = row["received"]
        subject = row["subject"]
        message = fetch(graph, old_id)
        if message is None:
            executed.append(
                {
                    "old id": old_id,
                    "new id": "",
                    "received": received,
                    "subject": subject,
                    "action": "skipped",
                    "result": "missing",
                }
            )
            continue
        path = folder_path(graph, str(message.get("parentFolderId") or ""), cache)
        reason = mismatch(row, message, path)
        if reason:
            executed.append(
                {
                    "old id": old_id,
                    "new id": "",
                    "received": received,
                    "subject": subject,
                    "action": "skipped",
                    "result": reason,
                }
            )
            continue
        if not set_category(graph, old_id):
            executed.append(
                {
                    "old id": old_id,
                    "new id": "",
                    "received": received,
                    "subject": subject,
                    "action": "skipped",
                    "result": "category-patch-failed",
                }
            )
            continue
        new_id = move(graph, old_id, folder_id)
        if not new_id:
            executed.append(
                {
                    "old id": old_id,
                    "new id": "",
                    "received": received,
                    "subject": subject,
                    "action": "skipped",
                    "result": "move-failed-after-category",
                }
            )
            continue
        executed.append(
            {
                "old id": old_id,
                "new id": new_id,
                "received": received,
                "subject": subject,
                "action": "moved",
                "result": "ok",
            }
        )
        LOGGER.info("Moved %s", received)

    verified = []
    for row, done in zip(rows, executed):
        hits = find_messages(graph, row["received"], row["subject"], row["sender address"])
        if len(hits) != 1:
            folder = ""
            categories = ""
            new_id = done["new id"]
            result = "missing" if not hits else "duplicated"
        else:
            hit = hits[0]
            new_id = str(hit.get("id") or "")
            folder = folder_path(graph, str(hit.get("parentFolderId") or ""), cache)
            categories = " | ".join(str(item) for item in (hit.get("categories") or []))
            if done["action"] != "moved":
                result = "left-in-place"
            elif folder != PROPOSED_FOLDER:
                result = "folder-mismatch"
            elif categories != ENTERED_WITH_ISSUES_CATEGORY:
                result = "category-mismatch"
            elif new_id != done["new id"]:
                result = "id-mismatch"
            else:
                result = "ok"
        verified.append(
            {
                "old id": row["current message id"],
                "new id": new_id,
                "received": row["received"],
                "sender": row["sender address"],
                "subject": row["subject"],
                "folder": folder,
                "categories": categories,
                "expected category": ENTERED_WITH_ISSUES_CATEGORY,
                "result": result,
            }
        )

    with EXECUTE.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["old id", "new id", "received", "subject", "action", "result"],
        )
        writer.writeheader()
        writer.writerows(executed)
    with VERIFY.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "old id",
                "new id",
                "received",
                "sender",
                "subject",
                "folder",
                "categories",
                "expected category",
                "result",
            ],
        )
        writer.writeheader()
        writer.writerows(verified)
    moved = sum(1 for row in executed if row["action"] == "moved")
    LOGGER.info("Moved %s skipped %s", moved, len(executed) - moved)


if __name__ == "__main__":
    main()
