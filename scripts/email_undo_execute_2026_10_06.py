"""Phase 2 of the 2026-10-06 AP mail undo. Approved by Kyle 9:48 AM CT.

Mutations on accountspayable@kannonmfg.com are limited to:
  POST /messages/{id}/move
  PATCH /messages/{id} with {"categories": [...]}

No isRead change, flag, delete, send, reply, or forward. No KIMCO.
Each row is re-read immediately before it is changed.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap_clerk.graph import (  # noqa: E402
    ALLOWED_MAILBOX,
    GRAPH_BASE,
    GraphClient,
    GraphError,
    load_graph_credentials,
)

OUT = Path(__file__).resolve().parents[1] / "runs" / "email-undo-2026-10-06"
PLAN = OUT / "dryrun.csv"
LOG_PATH = OUT / "execute_log.csv"
VERIFY_PATH = OUT / "verify_after.csv"
JOURNAL = Path("/tmp/email-undo-2026-10-06-journal.jsonl")
CUTOFF = datetime(2026, 10, 6, 13, 35, 0, tzinfo=timezone.utc)
SELECT = "id,parentFolderId,lastModifiedDateTime,categories,subject"
LOG_FIELDS = ["row_id", "action", "old_id", "new_id", "result"]
VERIFY_FIELDS = [
    "row_id",
    "folder",
    "categories",
    "planned_destination",
    "categories_to_clear",
    "result",
    "reason",
]
DEST_PATH = {
    "Inbox": "Inbox",
    "9 - FORT WORTH ARCHIVE": "Inbox/9 - FORT WORTH ARCHIVE",
    "Sent Items": "Sent Items",
    "UNNAMED (FID 0x0113)": "Junk Email",
    "UNNAMED (FID 0x376dc5ed3)": "Inbox/3 - RECEIPT INVOICE ISSUES",
}
REQUIRED_PATHS = set(DEST_PATH.values()) | {"Inbox/AutoPay Archive"}
N070_CREDIT = "Credit from Your Order 58889"
N070_INVOICE = "Invoice for Your Order 59125"
N070_RECEIVED = "2026-09-05T06:21:34Z"
N070_CLEAR = ["AI Skipped 2"]
SECRET_NAMES = (
    "KIMCO_LIVE_USERNAME",
    "KIMCO_PROTOTYPE_USERNAME",
    "OUTLOOK_AP_USERNAME",
    "MICROSOFT_GRAPH_CLIENT_SECRET",
    "MICROSOFT_GRAPH_CLIENT_ID",
    "MICROSOFT_GRAPH_TENANT_ID",
)


def redact(value: str) -> str:
    out = value or ""
    for name in SECRET_NAMES:
        secret = os.environ.get(name) or ""
        if secret and secret in out:
            out = out.replace(secret, "[redacted]")
    return out


def parse_time(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def split_cats(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def cleared(existing: list[str], remove: list[str]) -> list[str]:
    drop = set(remove)
    return [cat for cat in existing if cat not in drop]


def plan_action(row: dict) -> str:
    if row["row_id"] == "N070":
        if row["status"] != "AMBIGUOUS" or row["planned_move"] != "yes":
            raise SystemExit("N070 no longer matches the approved ambiguous row")
        return "n070"
    status, move = row["status"], row["planned_move"]
    if status == "CATEGORY_ONLY" or (status == "CONFLICT" and move == "no"):
        return "clear"
    if status in ("OK_TO_MOVE", "CONFLICT") and move == "yes":
        return "move"
    raise SystemExit(f"row {row['row_id']} status {status} move {move} is not approved")


def categories_to_remove(row: dict) -> list[str]:
    if row["row_id"] == "N070":
        return list(N070_CLEAR)
    return split_cats(row.get("categories_to_clear"))


def unchanged(parent_id: str, folder_path: str, modified: str | None, expected_path: str) -> tuple[bool, str]:
    reasons = []
    if folder_path != expected_path:
        reasons.append(f"folder is {folder_path or 'UNKNOWN'} not {expected_path}")
    when = parse_time(modified)
    if when is None or when > CUTOFF:
        reasons.append("lastModified later than 2026-10-06T13:35:00Z" if when else "lastModified missing")
    if reasons:
        return False, "SKIPPED_CHANGED: " + "; ".join(reasons)
    return True, ""


def load_plan() -> list[dict]:
    with PLAN.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 476:
        raise SystemExit(f"dryrun.csv has {len(rows)} rows, expected 476")
    return rows


def self_test() -> None:
    assert cleared(
        ["Problems/Issues", "Investigating", "AI Skipped 2"],
        ["AI Skipped 2"],
    ) == ["Problems/Issues", "Investigating"]
    assert cleared(["AI Needs Review"], ["AI Needs Review"]) == []
    assert cleared(["AI HOLD", "Entered in AI"], ["Entered in AI"]) == ["AI HOLD"]
    ok, _ = unchanged("id", "Inbox", "2026-10-06T13:35:00Z", "Inbox")
    assert ok
    ok, reason = unchanged("id", "Inbox", "2026-10-06T13:35:01Z", "Inbox")
    assert not ok and reason.startswith("SKIPPED_CHANGED")
    ok, reason = unchanged("id", "Sent Items", "2026-10-06T13:26:13Z", "Inbox")
    assert not ok and "folder" in reason
    rows = load_plan()
    counts: dict[str, int] = {}
    for row in rows:
        action = plan_action(row)
        counts[action] = counts.get(action, 0) + 1
        if row["row_id"] != "N070" and not row["current_id"]:
            raise SystemExit(f"{row['row_id']} has no current_id")
        if row["planned_destination"] not in DEST_PATH:
            raise SystemExit(f"unknown destination {row['planned_destination']}")
    if counts != {"move": 236, "clear": 239, "n070": 1}:
        raise SystemExit(f"unexpected action counts {counts}")
    n070 = next(row for row in rows if row["row_id"] == "N070")
    assert categories_to_remove(n070) == ["AI Skipped 2"]
    assert n070["subject_recorded"] == N070_CREDIT
    print(json.dumps({"self_test": "ok", "actions": counts}))


class Mail:
    """Graph calls used by this undo. Writes are move and category PATCH only."""

    def __init__(self, client: GraphClient, *, writes: bool):
        self.client = client
        self.writes = writes
        self.base = f"{GRAPH_BASE}/users/{ALLOWED_MAILBOX}"
        self.protected: set[str] = set()

    def call(self, method: str, url: str, **kwargs):
        method = method.upper()
        body = kwargs.get("json")
        if method == "GET":
            pass
        elif not self.writes:
            raise SystemExit("verification refused a write")
        elif method == "POST":
            path = url.split("?")[0].rstrip("/")
            if not path.endswith("/move") or set(body or {}) != {"destinationId"}:
                raise SystemExit("refusing POST that is not a folder move")
        elif method == "PATCH":
            if set(body or {}) != {"categories"} or not isinstance((body or {}).get("categories"), list):
                raise SystemExit("refusing PATCH that is not a category list")
        else:
            raise SystemExit(f"refusing {method}")
        delay = 1.0
        response = None
        for attempt in range(6):
            response = self.client.request(method, url, **kwargs)
            if response.status_code == 401:
                raise SystemExit("Graph HTTP 401; stopping without another sign-in")
            if response.status_code in (429, 503) and attempt < 5:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            return response
        return response

    def get_all(self, url: str, params: dict | None = None, *, fatal: bool = True) -> list[dict]:
        out, first = [], True
        while url:
            response = self.call("GET", url, params=params if first else None)
            first = False
            if response.status_code != 200:
                if fatal:
                    raise SystemExit(f"GET failed HTTP {response.status_code}")
                raise RuntimeError(f"HTTP {response.status_code}")
            payload = response.json() or {}
            out.extend(payload.get("value") or [])
            url = payload.get("@odata.nextLink")
        return out

    def folders(self) -> dict[str, dict]:
        found: dict[str, dict] = {}

        def walk(url: str, prefix: str) -> None:
            for folder in self.get_all(
                url,
                {"$top": 200, "includeHiddenFolders": "true", "$select": "id,displayName,parentFolderId"},
            ):
                path = f"{prefix}/{folder['displayName']}" if prefix else folder["displayName"]
                found[folder["id"]] = {"name": folder["displayName"], "path": path}
                walk(f"{self.base}/mailFolders/{quote(folder['id'], safe='')}/childFolders", path)

        walk(f"{self.base}/mailFolders", "")
        return found

    def get_message(self, message_id: str) -> tuple[int, dict]:
        response = self.call(
            "GET",
            f"{self.base}/messages/{quote(message_id, safe='')}",
            params={"$select": SELECT},
        )
        if response.status_code != 200:
            return response.status_code, {}
        return 200, response.json() or {}

    def move(self, message_id: str, destination_id: str) -> tuple[int, str, str]:
        if message_id in self.protected:
            raise SystemExit("refusing to move a protected message")
        response = self.call(
            "POST",
            f"{self.base}/messages/{quote(message_id, safe='')}/move",
            json={"destinationId": destination_id},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            return response.status_code, "", error_code(response)
        new_id = str((response.json() or {}).get("id") or "")
        return response.status_code, new_id, ""

    def patch_categories(self, message_id: str, categories: list[str]) -> tuple[int, str]:
        if message_id in self.protected:
            raise SystemExit("refusing to recategorize a protected message")
        response = self.call(
            "PATCH",
            f"{self.base}/messages/{quote(message_id, safe='')}",
            json={"categories": categories},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            return response.status_code, error_code(response)
        return response.status_code, ""


def error_code(response) -> str:
    try:
        code = str(((response.json() or {}).get("error") or {}).get("code") or "")
    except (ValueError, TypeError, AttributeError):
        code = ""
    return code


def path_index(folders: dict[str, dict]) -> dict[str, str]:
    index: dict[str, str] = {}
    duplicates: list[str] = []
    for folder_id, info in folders.items():
        path = info["path"]
        if path in index:
            duplicates.append(path)
        index[path] = folder_id
    missing = sorted(REQUIRED_PATHS - set(index))
    if missing or duplicates:
        raise SystemExit(
            "folder resolution failed missing="
            + ",".join(missing)
            + " duplicate="
            + ",".join(sorted(set(duplicates)))
        )
    return index


def folder_path(folders: dict[str, dict], folder_id: str) -> str:
    return folders.get(folder_id, {}).get("path", "")


def message_categories(message: dict) -> list[str]:
    return [str(cat) for cat in (message.get("categories") or []) if cat]


def load_journal() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not JOURNAL.exists():
        return done
    for line in JOURNAL.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        done[item["row_id"]] = item
    return done


def journal(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
        handle.flush()


def apply_categories(mail: Mail, message_id: str, existing: list[str], remove: list[str]) -> str:
    updated = cleared(existing, remove)
    if updated == existing:
        return ""
    status, code = mail.patch_categories(message_id, updated)
    if status >= 400:
        return f"FAILED category HTTP {status} {code}".strip()
    return ""


def finish(row_id: str, action: str, old_id: str, new_id: str, result: str, pending: dict[str, dict]) -> dict:
    entry = {
        "row_id": row_id,
        "action": action,
        "old_id": old_id,
        "new_id": new_id,
        "result": redact(result),
        "done": True,
    }
    journal(entry)
    pending[row_id] = entry
    print(f"{row_id} {action} {entry['result']}", flush=True)
    return entry


def execute_move(
    mail: Mail,
    folders: dict[str, dict],
    paths: dict[str, str],
    row: dict,
    old_id: str,
    remove: list[str],
    pending: dict[str, dict],
) -> dict:
    dest_path = DEST_PATH[row["planned_destination"]]
    dest_id = paths[dest_path]
    prior = pending.get(row["row_id"]) or {}
    if prior.get("stage") == "moved" and prior.get("new_id"):
        new_id = prior["new_id"]
    else:
        status, new_id, code = mail.move(old_id, dest_id)
        if status >= 400 or not new_id:
            return finish(row["row_id"], "MOVE_AND_CLEAR", old_id, "", f"FAILED move HTTP {status} {code}".strip(), pending)
        journal({"row_id": row["row_id"], "stage": "moved", "old_id": old_id, "new_id": new_id, "done": False})
    status, moved = mail.get_message(new_id)
    if status != 200:
        return finish(row["row_id"], "MOVE_AND_CLEAR", old_id, new_id, f"FAILED reread moved HTTP {status}", pending)
    if folder_path(folders, moved.get("parentFolderId") or "") != dest_path:
        return finish(row["row_id"], "MOVE_AND_CLEAR", old_id, new_id, "FAILED move landed in an unexpected folder", pending)
    failed = apply_categories(mail, new_id, message_categories(moved), remove)
    if failed:
        return finish(row["row_id"], "MOVE_AND_CLEAR", old_id, new_id, failed, pending)
    return finish(row["row_id"], "MOVE_AND_CLEAR", old_id, new_id, "OK", pending)


def execute_clear(
    mail: Mail,
    row: dict,
    message_id: str,
    existing: list[str],
    remove: list[str],
    pending: dict[str, dict],
) -> dict:
    failed = apply_categories(mail, message_id, existing, remove)
    result = failed or "OK"
    return finish(row["row_id"], "CLEAR_ONLY", message_id, message_id, result, pending)


def prechecked_message(mail: Mail, folders: dict[str, dict], row: dict) -> tuple[dict | None, str]:
    status, message = mail.get_message(row["current_id"])
    if status != 200:
        return None, f"FAILED reread HTTP {status}"
    live = folder_path(folders, message.get("parentFolderId") or "")
    ok, reason = unchanged(message.get("parentFolderId") or "", live, message.get("lastModifiedDateTime"), row["current_folder"])
    if not ok:
        return None, reason
    return message, ""


def find_n070(mail: Mail, folders: dict[str, dict], paths: dict[str, str]) -> tuple[dict | None, str]:
    fort_worth = paths["Inbox/9 - FORT WORTH ARCHIVE"]
    try:
        matches = mail.get_all(
            f"{mail.base}/mailFolders/{quote(fort_worth, safe='')}/messages",
            {
                "$select": SELECT,
                "$filter": f"receivedDateTime eq {N070_RECEIVED}",
                "$top": 50,
            },
            fatal=False,
        )
    except RuntimeError as exc:
        return None, f"FAILED lookup {exc}"
    credit = [item for item in matches if (item.get("subject") or "") == N070_CREDIT]
    credit_ids = {item.get("id") for item in credit}
    mail.invoice_snapshots = []
    for item in matches:
        item_id = item.get("id")
        if not item_id or item_id in credit_ids:
            continue
        mail.protected.add(item_id)
        if (item.get("subject") or "") == N070_INVOICE:
            mail.invoice_snapshots.append(
                {
                    "id": item_id,
                    "parent": item.get("parentFolderId") or "",
                    "categories": message_categories(item),
                    "modified": item.get("lastModifiedDateTime") or "",
                }
            )
    if len(credit) != 1:
        return None, f"SKIPPED_N070: found {len(credit)} messages with the credit subject"
    chosen = credit[0]
    if "AI Skipped 2" not in message_categories(chosen):
        return None, "SKIPPED_CHANGED: credit message category is not AI Skipped 2"
    status, fresh = mail.get_message(chosen["id"])
    if status != 200:
        return None, f"FAILED reread HTTP {status}"
    live = folder_path(folders, fresh.get("parentFolderId") or "")
    ok, reason = unchanged(fresh.get("parentFolderId") or "", live, fresh.get("lastModifiedDateTime"), "Inbox/9 - FORT WORTH ARCHIVE")
    if not ok:
        return None, reason
    if (fresh.get("subject") or "") != N070_CREDIT:
        return None, "SKIPPED_N070: reread subject was not the credit message"
    if fresh["id"] in mail.protected:
        return None, "SKIPPED_N070: credit id collided with the invoice"
    return fresh, ""


def execute(mail: Mail) -> list[dict]:
    rows = load_plan()
    folders = mail.folders()
    paths = path_index(folders)
    pending = load_journal()
    mail.invoice_snapshots = []
    results = execute_rows(mail, rows, folders, paths, pending)
    print(json.dumps(invoice_unchanged(mail)), flush=True)
    return results


def execute_rows(mail: Mail, rows: list[dict], folders: dict[str, dict], paths: dict[str, str], pending: dict[str, dict]) -> list[dict]:
    results = []
    try:
        for row in rows:
            prior = pending.get(row["row_id"]) or {}
            if prior.get("done"):
                results.append(prior)
                print(f"{row['row_id']} RESUME {prior['result']}", flush=True)
                continue
            remove = categories_to_remove(row)
            if prior.get("stage") == "moved" and prior.get("new_id"):
                results.append(
                    execute_move(
                        mail,
                        folders,
                        paths,
                        row,
                        prior.get("old_id") or prior["new_id"],
                        remove,
                        pending,
                    )
                )
                continue
            action = plan_action(row)
            if action == "n070":
                message, reason = find_n070(mail, folders, paths)
                if message is None:
                    action_name = "SKIP" if reason.startswith("SKIPPED") else "MOVE_AND_CLEAR"
                    results.append(finish(row["row_id"], action_name, "", "", reason, pending))
                    continue
                results.append(execute_move(mail, folders, paths, row, message["id"], remove, pending))
                continue
            message, reason = prechecked_message(mail, folders, row)
            if message is None:
                action_name = "SKIP" if reason.startswith("SKIPPED") else ("MOVE_AND_CLEAR" if action == "move" else "CLEAR_ONLY")
                results.append(finish(row["row_id"], action_name, row["current_id"], "", reason, pending))
                continue
            if action == "move":
                results.append(execute_move(mail, folders, paths, row, message["id"], remove, pending))
            else:
                results.append(execute_clear(mail, row, message["id"], message_categories(message), remove, pending))
    finally:
        if results:
            write_log(results)
    return results


def invoice_unchanged(mail: Mail) -> dict:
    snapshots = getattr(mail, "invoice_snapshots", [])
    if not snapshots:
        return {"invoice_candidate_checked": 0, "invoice_candidate_unchanged": None}
    unchanged_count = 0
    for snapshot in snapshots:
        status, message = mail.get_message(snapshot["id"])
        same = (
            status == 200
            and (message.get("parentFolderId") or "") == snapshot["parent"]
            and message_categories(message) == snapshot["categories"]
            and (message.get("lastModifiedDateTime") or "") == snapshot["modified"]
        )
        unchanged_count += int(same)
    return {
        "invoice_candidate_checked": len(snapshots),
        "invoice_candidate_unchanged": unchanged_count == len(snapshots),
    }


def write_log(results: list[dict]) -> None:
    with LOG_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for entry in results:
            writer.writerow({key: redact(str(entry.get(key) or "")) for key in LOG_FIELDS})


def verify(mail: Mail, results: list[dict]) -> list[dict]:
    if mail.writes:
        raise SystemExit("verification must be read-only")
    rows = load_plan()
    by_id = {entry["row_id"]: entry for entry in results}
    folders = mail.folders()
    paths = path_index(folders)
    out = []
    for row in rows:
        entry = by_id.get(row["row_id"]) or {}
        message_id = entry.get("new_id") or entry.get("old_id") or row.get("current_id") or ""
        remove = categories_to_remove(row)
        expected = DEST_PATH[row["planned_destination"]]
        if row["row_id"] == "N070" and not message_id:
            message, _reason = find_n070_readonly(mail, paths)
            message_id = (message or {}).get("id") or ""
        if not message_id:
            out.append(verify_row(row, "", "", remove, "FAIL", "message id missing"))
            continue
        status, message = mail.get_message(message_id)
        if status != 200:
            out.append(verify_row(row, "", "", remove, "FAIL", f"reread HTTP {status}"))
            continue
        live = folder_path(folders, message.get("parentFolderId") or "")
        cats = message_categories(message)
        remaining = [cat for cat in remove if cat in cats]
        reasons = []
        if live != expected:
            reasons.append(f"folder is {live or 'UNKNOWN'} expected {expected}")
        if remaining:
            reasons.append("categories still present: " + "; ".join(remaining))
        if reasons:
            out.append(verify_row(row, live, cats, remove, "FAIL", "; ".join(reasons)))
        else:
            out.append(verify_row(row, live, cats, remove, "PASS", ""))
    with VERIFY_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VERIFY_FIELDS)
        writer.writeheader()
        writer.writerows(out)
    return out


def find_n070_readonly(mail: Mail, paths: dict[str, str]) -> tuple[dict | None, str]:
    fort_worth = paths.get("Inbox/9 - FORT WORTH ARCHIVE")
    inbox = paths.get("Inbox")
    found = []
    for folder_id in (inbox, fort_worth):
        if not folder_id:
            continue
        found.extend(
            mail.get_all(
                f"{mail.base}/mailFolders/{quote(folder_id, safe='')}/messages",
                {"$select": SELECT, "$filter": f"receivedDateTime eq {N070_RECEIVED}", "$top": 50},
            )
        )
    credit = [item for item in found if (item.get("subject") or "") == N070_CREDIT]
    if len(credit) == 1:
        return credit[0], ""
    return None, f"found {len(credit)}"


def verify_row(row: dict, folder: str, categories: list[str], remove: list[str], result: str, reason: str) -> dict:
    return {
        "row_id": row["row_id"],
        "folder": redact(folder),
        "categories": redact(";".join(categories)),
        "planned_destination": row["planned_destination"],
        "categories_to_clear": ";".join(remove),
        "result": result,
        "reason": redact(reason),
    }


def summarize(results: list[dict], verified: list[dict]) -> dict:
    moved = sum(1 for entry in results if entry["action"] == "MOVE_AND_CLEAR" and entry["result"] == "OK")
    cleared = sum(1 for entry in results if entry["action"] == "CLEAR_ONLY" and entry["result"] == "OK")
    skipped = [entry for entry in results if str(entry["result"]).startswith("SKIPPED")]
    failed = [entry for entry in results if str(entry["result"]).startswith("FAILED")]
    passes = sum(1 for entry in verified if entry["result"] == "PASS")
    fails = [entry for entry in verified if entry["result"] != "PASS"]
    return {
        "rows": len(results),
        "moved": moved,
        "category_only_cleared": cleared,
        "skipped": len(skipped),
        "failed_actions": len(failed),
        "verify_pass": passes,
        "verify_fail": len(fails),
    }


def connect(*, writes: bool) -> Mail:
    creds = load_graph_credentials()
    if not creds.ready:
        raise SystemExit(creds.error or "Graph credentials missing")
    try:
        client = GraphClient.authenticate(creds.tenant_id, creds.client_id, creds.client_secret)
    except GraphError as exc:
        raise SystemExit(f"Graph authentication failed ({exc})") from exc
    return Mail(client, writes=writes)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "--self-test":
        self_test()
        return
    if command != "execute":
        raise SystemExit("usage: email_undo_execute_2026_10_06.py --self-test|execute")
    self_test()
    mail = connect(writes=True)
    results = execute(mail)
    readonly = Mail(mail.client, writes=False)
    verified = verify(readonly, results)
    print(json.dumps(summarize(results, verified)), flush=True)


if __name__ == "__main__":
    main()
