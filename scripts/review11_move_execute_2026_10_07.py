"""Move the 10 emails Kyle approved on 2026-10-07.

Sets categories to only Entered in AI and moves each message to
Inbox/9 - FORT WORTH ARCHIVE. Does not touch Castle 10476, the statement
emails, or any other message. No KIMCO writes and no Mail.Send.

Before each move the message is found again by received time, sender, and
subject. It is skipped when the folder, categories, or lastModifiedDateTime
differ from the review.
"""

from __future__ import annotations

import csv
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
    GraphClient,
    load_graph_credentials,
)
from scripts.sept25_30_email_move_execute import archive_folder, folder_path, sender_of, stamp
from scripts.sept_archive_check_2026_10_07 import plain

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("review11-move")

EXECUTE = ROOT / "runs" / "review11-move-execute.csv"
VERIFY = ROOT / "runs" / "review11-move-verify.csv"
ARCHIVE = "Inbox/9 - FORT WORTH ARCHIVE"
SELECT = "id,subject,from,receivedDateTime,parentFolderId,categories,lastModifiedDateTime"
COLUMNS = [
    "received time",
    "sender",
    "subject",
    "old folder",
    "old categories",
    "new folder",
    "new categories",
    "new message id",
    "result",
]

# lastModifiedDateTime is the stamp recorded when these messages were identified.
# The review only read them, so a change means the message was edited afterward.
APPROVED = [
    {"bill": 10470, "received": "2026-09-05T06:29:32Z", "sender": "donotreplyinvoice@ryerson.com", "subject": "PO 59064 Inv# 9307057099", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:57:44Z"},
    {"bill": 10358, "received": "2026-09-08T16:49:17Z", "sender": "quickbooks@notification.intuit.com", "subject": "Invoice from Greentree Packaging & Lumber", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:55:55Z"},
    {"bill": 10013, "received": "2026-09-11T18:39:08Z", "sender": "quickbooks@notification.intuit.com", "subject": "New payment request from AMERICAN QUALITY POWDER COATING - invoice 10991", "folder": "Inbox", "categories": ["Entered with issues"], "lastModifiedDateTime": "2026-09-15T22:44:38Z"},
    {"bill": 10009, "received": "2026-09-15T18:10:45Z", "sender": "quickbooks@notification.intuit.com", "subject": "New payment request from AMERICAN QUALITY POWDER COATING - invoice 11003", "folder": "Inbox", "categories": ["Entered with issues"], "lastModifiedDateTime": "2026-09-15T21:07:12Z"},
    {"bill": 10471, "received": "2026-09-11T19:11:40Z", "sender": "ar@capitalmachine.com", "subject": "Capital Machine - Sales Invoice PS-INV103317 - KANNON MANUFACTURING - AMTECH", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:58:16Z"},
    {"bill": 10382, "received": "2026-09-11T19:36:12Z", "sender": "quickbooks@notification.intuit.com", "subject": "New payment request from Green Valley Compressor LLC - invoice 2216", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:58:19Z"},
    {"bill": 10107, "received": "2026-09-16T20:15:30Z", "sender": "krista.woodruff@jpsteel.us", "subject": "JP Steel Invoice# (125315) Transmission for KANNON MFG", "folder": "Inbox", "categories": ["Entered with issues"], "lastModifiedDateTime": "2026-09-16T22:03:11Z"},
    {"bill": 10468, "received": "2026-09-02T18:53:07Z", "sender": "kstarkey@tricormetals.com", "subject": "Invoice 00044427", "folder": "Inbox/3 - RECEIPT INVOICE ISSUES", "categories": [], "lastModifiedDateTime": "2026-10-06T14:57:31Z"},
    {"bill": 10280, "received": "2026-09-21T18:59:20Z", "sender": "lorena@legacywireproducts.com", "subject": "Legacy Wire Products - Sales Invoice PS-INV104046", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:58:43Z"},
    {"bill": 10275, "received": "2026-09-22T13:58:43Z", "sender": "lorena@legacywireproducts.com", "subject": "Legacy Wire Products - Sales Invoice PS-INV104051", "folder": "Inbox", "categories": [], "lastModifiedDateTime": "2026-10-06T14:58:46Z"},
]
DO_NOT_TOUCH = {
    "2026-07-31T05:11:01Z",
    "2026-10-01T02:07:51Z",
    "2026-10-01T12:05:24Z",
    "2026-10-01T18:17:43Z",
}


def categories_of(message: dict[str, Any]) -> list[str]:
    return [str(item) for item in (message.get("categories") or [])]


def category_text(values: list[str]) -> str:
    return " | ".join(values)


def same_message(message: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        str(message.get("receivedDateTime") or "") == expected["received"]
        and sender_of(message).lower() == expected["sender"].lower()
        and plain(str(message.get("subject") or "")) == plain(expected["subject"])
    )


def find_messages(graph: GraphClient, expected: dict[str, Any], days: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    day = date.fromisoformat(expected["received"][:10])
    key = day.isoformat()
    if key not in days:
        days[key] = graph.list_messages(ALLOWED_MAILBOX, received_from=day, received_to=day)
        LOGGER.info("Day %s has %s messages", key, len(days[key]))
    found = []
    for row in days[key]:
        if str(row.get("receivedDateTime") or "") != expected["received"]:
            continue
        if sender_of(row).lower() != expected["sender"].lower():
            continue
        if plain(str(row.get("subject") or "")) != plain(expected["subject"]):
            continue
        full = graph.get_message(ALLOWED_MAILBOX, str(row.get("id") or ""), select=SELECT)
        if same_message(full, expected):
            found.append(full)
    unique = {str(item.get("id") or ""): item for item in found if item.get("id")}
    return list(unique.values())


def mismatch(message: dict[str, Any], path: str, expected: dict[str, Any]) -> str:
    if path != expected["folder"]:
        return f"folder-changed:{path}"
    if categories_of(message) != expected["categories"]:
        return "categories-changed:" + category_text(categories_of(message))
    if stamp(message.get("lastModifiedDateTime")) != stamp(expected["lastModifiedDateTime"]):
        return "lastModified-changed:" + str(message.get("lastModifiedDateTime") or "")
    return ""


def set_only_entered_in_ai(graph: GraphClient, message_id: str) -> bool:
    response = graph.request(
        "PATCH",
        graph._messages_url(ALLOWED_MAILBOX, message_id),
        json={"categories": [ENTERED_IN_AI_CATEGORY]},
        headers={"Content-Type": "application/json"},
    )
    LOGGER.info("Category PATCH bill HTTP %s", response.status_code)
    return response.status_code < 400


def base_row(expected: dict[str, Any], message: dict[str, Any] | None, path: str) -> dict[str, str]:
    return {
        "received time": expected["received"],
        "sender": expected["sender"],
        "subject": expected["subject"],
        "old folder": path,
        "old categories": category_text(categories_of(message)) if message else "",
        "new folder": "",
        "new categories": "",
        "new message id": "",
        "result": "",
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    received = [row["received"] for row in APPROVED]
    if len(received) != 10 or len(set(received)) != 10:
        raise SystemExit("Approved list is not 10 distinct messages. No mail changed.")
    if DO_NOT_TOUCH.intersection(received):
        raise SystemExit("Approved list includes a message Kyle said not to touch. No mail changed.")
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    graph = GraphClient.authenticate(creds.tenant_id or "", creds.client_id or "", creds.client_secret or "")
    archive_id = archive_folder(graph)
    cache: dict[str, dict[str, Any]] = {}
    days: dict[str, list[dict[str, Any]]] = {}
    executed: list[dict[str, str]] = []
    for expected in APPROVED:
        matches = find_messages(graph, expected, days)
        if len(matches) != 1:
            row = base_row(expected, None, "")
            row["result"] = "skipped:not-found" if not matches else "skipped:several-messages"
            executed.append(row)
            LOGGER.info("Skip bill %s %s", expected["bill"], row["result"])
            continue
        message = matches[0]
        path = folder_path(graph, str(message.get("parentFolderId") or ""), cache)
        reason = mismatch(message, path, expected)
        row = base_row(expected, message, path)
        if reason:
            row["new folder"] = path
            row["new categories"] = row["old categories"]
            row["new message id"] = str(message.get("id") or "")
            row["result"] = "skipped:" + reason
            executed.append(row)
            LOGGER.info("Skip bill %s %s", expected["bill"], row["result"])
            continue
        old_id = str(message.get("id") or "")
        if not set_only_entered_in_ai(graph, old_id):
            row["result"] = "skipped:category-patch-failed"
            executed.append(row)
            continue
        outcome = graph.move_message(ALLOWED_MAILBOX, old_id, archive_id)
        new_id = str(outcome.get("new_id") or "")
        if outcome.get("status") != "moved-fort-worth" or not new_id:
            row["new categories"] = ENTERED_IN_AI_CATEGORY
            row["result"] = "skipped:move-failed-after-category"
            executed.append(row)
            LOGGER.info("Move failed for bill %s", expected["bill"])
            continue
        row["new folder"] = ARCHIVE
        row["new categories"] = ENTERED_IN_AI_CATEGORY
        row["new message id"] = new_id
        row["result"] = "moved"
        executed.append(row)
        LOGGER.info("Moved bill %s", expected["bill"])

    verified: list[dict[str, str]] = []
    for expected, done in zip(APPROVED, executed):
        days.clear()
        matches = find_messages(graph, expected, days)
        row = {
            "received time": expected["received"],
            "sender": expected["sender"],
            "subject": expected["subject"],
            "old folder": done["old folder"],
            "old categories": done["old categories"],
            "new folder": "",
            "new categories": "",
            "new message id": "",
            "result": "",
        }
        if len(matches) != 1:
            row["result"] = "missing" if not matches else "several-messages"
            verified.append(row)
            continue
        hit = matches[0]
        folder = folder_path(graph, str(hit.get("parentFolderId") or ""), cache)
        categories = categories_of(hit)
        row["new folder"] = folder
        row["new categories"] = category_text(categories)
        row["new message id"] = str(hit.get("id") or "")
        if done["result"] != "moved":
            row["result"] = "left-in-place"
        elif folder != ARCHIVE:
            row["result"] = "folder-mismatch"
        elif categories != [ENTERED_IN_AI_CATEGORY]:
            row["result"] = "category-mismatch"
        elif row["new message id"] != done["new message id"]:
            row["result"] = "id-mismatch"
        else:
            row["result"] = "ok"
        verified.append(row)
        LOGGER.info("Verify bill %s %s", expected["bill"], row["result"])

    write_csv(EXECUTE, executed)
    write_csv(VERIFY, verified)
    moved = sum(1 for row in executed if row["result"] == "moved")
    ok = sum(1 for row in verified if row["result"] == "ok")
    LOGGER.info("Moved %s verified %s", moved, ok)


if __name__ == "__main__":
    main()
