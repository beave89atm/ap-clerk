"""Read-only redo of the September filing check under Kyle's 2026-10-07 rule.

No KIMCO writes. No mail moves, category changes, or sends. One API Agent
sign-in. An email qualifies for Inbox/9 - FORT WORTH ARCHIVE with only the
category "Entered in AI" when every invoice on it has a KIMCO bill, that bill
has its own invoice attached, and every open HOLD has an issue note. Transfer
AP versus a regular batch does not matter. Statements and letters (10475,
10477) and any email with an invoice that has no bill stay put.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sept25_30_attachment_audit import attachment_bytes, attachment_name, page_texts
from scripts.sept25_30_finish_2026_10_06 import install_401_guard, login
from scripts.sept_archive_check_2026_10_07 import (
    ARCHIVE,
    CACHE,
    DELETE_PENDING,
    disposition,
    load_cache,
    plain,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("sept-archive-v2")
logging.getLogger("ap_clerk").setLevel(logging.WARNING)
logging.getLogger("pypdf").setLevel(logging.ERROR)

SOURCE = ROOT / "runs" / "sept-archive-check-2026-10-07.csv"
OUT = ROOT / "runs" / "sept-archive-check-v2-2026-10-07.csv"
ATTACH_CACHE = Path("/tmp/sept-archive-attachments-v2.jsonl")
ONLY_CATEGORY = "Entered in AI"
COLUMNS = [
    "message received time",
    "sender",
    "subject",
    "current folder",
    "current categories",
    "bills",
    "per-bill attachment present y/n",
    "hold note present y/n",
    "any un-entered invoice",
    "proposed action",
]

# A note states an open AP issue. Incidental "was not selected" inside a
# match write-up is not enough: those balanced, posted bills are not open holds.
ISSUE_BITS = (
    "on hold",
    " hold ",
    "hold (",
    "hold missing",
    "does not match",
    "do not match",
    "cannot be matched",
    "cannot be finished",
    "no matching",
    "no receipt",
    "has no receipt",
    "missing receipt",
    "missing line",
    "no purchase order",
    "needs a purchase order",
    "missing purchase order",
    "has no purchase order",
    "please receive",
    "please create",
    "please confirm",
    "price gap",
    "billed wrong",
    "dock miss",
    "every receipt is already invoiced",
    "still not selected",
    "not on po",
    "quantity gap",
    "open leftover",
)


def states_issue(text: str) -> bool:
    low = f" {(text or '').lower()} "
    if "every receipt is already invoiced" in low:
        return True
    if "matches" in low and "on hold" not in low and "please " not in low and "cannot be" not in low:
        # A match write-up can still be the issue note when it states a real problem.
        problem = any(
            bit in low
            for bit in ISSUE_BITS
            if bit
            not in {
                "every receipt is already invoiced",
            }
        )
        if problem and any(
            bit in low
            for bit in (
                "no purchase order",
                "needs a purchase order",
                "missing purchase order",
                "has no purchase order",
                "no receipt",
                "has no receipt",
                "missing receipt",
                "price gap",
                "does not match",
                "cannot be",
                "billed wrong",
                "on hold",
            )
        ):
            return True
        return False
    return any(bit in low for bit in ISSUE_BITS)


def money_gap(bill: dict[str, Any]) -> float | None:
    verification = bill.get("verification")
    covered = bill.get("covered")
    if verification is None or covered is None:
        return None
    return round(abs(float(verification) - float(covered)), 2)


def needs_issue_note(bill: dict[str, Any]) -> bool:
    """Open HOLD: dollar gap, or a note that states the problem. Balanced match notes do not."""
    if disposition(bill) != "HOLD":
        return False
    if any(states_issue(note) for note in bill.get("notes") or []):
        return True
    gap = money_gap(bill)
    return gap is not None and gap > 0.05


def hold_note_yn(bill: dict[str, Any]) -> str:
    return "y" if any(states_issue(note) for note in bill.get("notes") or []) else "n"


def contains_invoice(haystack: str, invoice: str) -> bool:
    invoice = str(invoice or "").strip()
    if not invoice or not haystack:
        return False
    if re.search(rf"(?<![A-Z0-9]){re.escape(invoice)}(?![A-Z0-9])", haystack, flags=re.I):
        return True
    digits = re.sub(r"\D", "", invoice)
    if digits and len(digits) >= 4 and re.search(rf"(?<!\d){re.escape(digits)}(?!\d)", haystack):
        return True
    head = invoice.split("/")[0].strip()
    if head and head != invoice and len(re.sub(r"\D", "", head)) >= 6:
        if re.search(rf"(?<![A-Z0-9]){re.escape(head)}(?![A-Z0-9])", haystack, flags=re.I):
            return True
        # Tricor files the slash invoice under the first 8 digits (00044427).
        prefix = re.sub(r"\D", "", head)[:8]
        if len(prefix) >= 8 and re.search(rf"(?<!\d){re.escape(prefix)}(?!\d)", haystack):
            return True
    compact = re.sub(r"[^A-Z0-9]", "", invoice.upper())
    hay = re.sub(r"[^A-Z0-9]", "", haystack.upper())
    return len(compact) >= 8 and compact in hay


def unbilled_numbers(flag: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"email contains invoice (.+?) with no bill", flag or ""):
        for number in match.group(1).split("|"):
            number = number.strip()
            if number and number not in found:
                found.append(number)
    return found


def category_set(value: str) -> set[str]:
    return {part.strip() for part in (value or "").split("|") if part.strip()}


def propose(folder: str, categories: str, bills: list[dict[str, Any]], unbilled: list[str], attached: dict[int, str]) -> str:
    reasons: list[str] = []
    letters = [int(bill["id"]) for bill in bills if int(bill["id"]) in DELETE_PENDING]
    if letters:
        kinds = []
        for bill_id in letters:
            kinds.append("letter" if bill_id == 10477 else "statement")
        reasons.append("statement or letter " + ", ".join(f"{bill_id} ({kind})" for bill_id, kind in zip(letters, kinds)))
    if unbilled:
        reasons.append("un-entered invoice " + ", ".join(unbilled))
    missing_files = [str(bill["id"]) for bill in bills if attached.get(int(bill["id"])) != "y"]
    if missing_files:
        reasons.append("bill " + ", ".join(missing_files) + " missing its own invoice attachment")
    missing_notes = [str(bill["id"]) for bill in bills if needs_issue_note(bill) and hold_note_yn(bill) != "y"]
    if missing_notes:
        reasons.append("HOLD " + ", ".join(missing_notes) + " has no AP Clerk issue note")
    if reasons:
        return "KEEP (" + "; ".join(reasons) + ")"
    cats = category_set(categories)
    if folder == ARCHIVE and cats == {ONLY_CATEGORY}:
        return "OK"
    if folder == ARCHIVE:
        return "RETAG (Entered in AI only)"
    return "MOVE (Entered in AI)"


def load_source() -> list[dict[str, str]]:
    with SOURCE.open(newline="") as handle:
        return list(csv.DictReader(handle))


def group_emails(rows: list[dict[str, str]], headers: dict[int, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """One group per message. Category differences mean two messages, as with McMaster 59103."""
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    unmatched: list[dict[str, str]] = []
    for row in rows:
        if not row["email received"]:
            unmatched.append(row)
            continue
        key = (
            row["email received"],
            row["sender"],
            row["subject"],
            row["current folder"],
            row["categories"],
        )
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "received": row["email received"],
                "sender": row["sender"],
                "subject": row["subject"],
                "folder": row["current folder"],
                "categories": row["categories"],
                "bills": [],
                "unbilled": [],
            }
            grouped[key] = bucket
        header = headers[int(row["bill"])]
        bucket["bills"].append(header)
        for number in unbilled_numbers(row["flag"]):
            if number not in bucket["unbilled"]:
                bucket["unbilled"].append(number)
    emails = list(grouped.values())
    for email in emails:
        email["bills"].sort(key=lambda bill: int(bill["id"]))
    emails.sort(key=lambda email: (email["received"], email["sender"], email["subject"], str(email["bills"][0]["id"])))
    return emails, unmatched


def load_attachment_cache() -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    if not ATTACH_CACHE.is_file():
        return found
    for line in ATTACH_CACHE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        found[int(row["id"])] = row
    return found


def refuse_writes(client) -> None:
    guarded = client.request

    def readonly(method: str, url: str, **kwargs: Any):
        if (method or "").upper() != "GET":
            raise SystemExit(f"Refusing KIMCO {method}. Dry run only.")
        return guarded(method, url, **kwargs)

    client.request = readonly  # type: ignore[method-assign]


def attachment_present(client, bill: dict[str, Any]) -> dict[str, Any]:
    bill_id = int(bill["id"])
    invoice = str(bill.get("invoice") or "")
    items = client.list_attachments(bill_id)
    names = [attachment_name(item) for item in items]
    if any(contains_invoice(name, invoice) for name in names):
        return {"id": bill_id, "invoice": invoice, "present": "y", "via": "name", "names": names}
    for item, name in zip(items, names):
        content = attachment_bytes(client, bill_id, item)
        if not content:
            continue
        if looks_like_text(content) and contains_invoice(content.decode("latin-1", errors="ignore"), invoice):
            return {"id": bill_id, "invoice": invoice, "present": "y", "via": "text", "names": names}
        if content[:5] == b"%PDF-":
            try:
                text = "\n".join(page_texts(content))
            except Exception as exc:  # noqa: BLE001 — record the miss and keep going
                LOGGER.info("Bill %s attachment %s unreadable: %s", bill_id, name, type(exc).__name__)
                continue
            if contains_invoice(text, invoice):
                return {"id": bill_id, "invoice": invoice, "present": "y", "via": "pdf", "names": names}
    return {"id": bill_id, "invoice": invoice, "present": "n", "via": "", "names": names}


def looks_like_text(content: bytes) -> bool:
    if content[:5] == b"%PDF-":
        return False
    sample = content[:200]
    return bool(sample) and not any(byte == 0 for byte in sample)


def check_attachments(client, bills: list[dict[str, Any]]) -> dict[int, str]:
    cache = load_attachment_cache()
    pending = [bill for bill in bills if int(bill["id"]) not in cache]
    LOGGER.info("Attachment cache %s, still to check %s", len(cache), len(pending))
    with ATTACH_CACHE.open("a") as handle:
        for index, bill in enumerate(pending, start=1):
            row = attachment_present(client, bill)
            cache[int(bill["id"])] = row
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            if index % 25 == 0 or index == len(pending):
                LOGGER.info("Checked %s/%s attachments", index, len(pending))
    return {bill_id: str(row.get("present") or "n") for bill_id, row in cache.items()}


def bill_cell(bills: list[dict[str, Any]]) -> str:
    return "; ".join(f"{int(bill['id'])}:{bill.get('invoice')}" for bill in bills)


def yn_cell(bills: list[dict[str, Any]], values: dict[int, str]) -> str:
    return "; ".join(f"{int(bill['id'])}={values[int(bill['id'])]}" for bill in bills)


def build_rows(emails: list[dict[str, Any]], attached: dict[int, str]) -> list[dict[str, str]]:
    rows = []
    for email in emails:
        bills = email["bills"]
        notes = {int(bill["id"]): hold_note_yn(bill) for bill in bills}
        rows.append(
            {
                "message received time": email["received"],
                "sender": email["sender"],
                "subject": email["subject"],
                "current folder": email["folder"],
                "current categories": email["categories"],
                "bills": bill_cell(bills),
                "per-bill attachment present y/n": yn_cell(bills, attached),
                "hold note present y/n": yn_cell(bills, notes),
                "any un-entered invoice": ", ".join(email["unbilled"]),
                "proposed action": propose(email["folder"], email["categories"], bills, email["unbilled"], attached),
            }
        )
    return rows


def self_check() -> None:
    match_note = (
        "AP Clerk: Telecom Products invoice 18224 matches PO 58824 receipt 24125. "
        "The earlier receipt of 88 was already invoiced and was not selected."
    )
    assert states_issue(match_note) is False
    assert states_issue("AP Clerk: @Shawn invoice 000444279 has no matching PO. On hold in Transfer AP.")
    assert states_issue("@Ruben Perez HOLD missing_receipt on AQPC invoice 11020.")
    assert states_issue("AP Clerk: Air Products invoice 436495428 has no purchase order. The payable is the printed $1,500.29.")
    assert contains_invoice("Invoice11002.pdf", "11002")
    assert contains_invoice("scan.pdf", "2216") is False
    assert contains_invoice("PO 12216 receipt", "2216") is False
    assert contains_invoice("Invoice 00044427.pdf", "000444279/1/202659035")
    no_hold = {"id": 1, "verification": 10, "covered": 10, "notes": [match_note], "invoice": "1"}
    # disposition() needs the full shape; the note helper is what this guards.
    assert hold_note_yn({"notes": [match_note]}) == "n"
    assert hold_note_yn({"notes": []}) == "n"
    assert propose("Inbox", "", [{"id": 10475, "notes": [], "verification": 1, "covered": 1}], [], {10475: "y"}).startswith("KEEP (statement or letter 10475")
    assert propose(ARCHIVE, ONLY_CATEGORY, [no_hold], [], {1: "y"}) == "OK"
    assert propose(ARCHIVE, "Entered with issues", [no_hold], [], {1: "y"}) == "RETAG (Entered in AI only)"
    assert propose("Inbox", "", [no_hold], [], {1: "y"}) == "MOVE (Entered in AI)"
    assert "no AP Clerk issue note" in propose("Inbox", "", [{"id": 10472, "notes": [], "verification": 100, "covered": 1}], [], {10472: "y"})
    LOGGER.info("Self-check passed")


def main() -> None:
    self_check()
    if not CACHE.is_file():
        raise SystemExit(f"Missing header cache {CACHE}")
    headers = load_cache()
    emails, unmatched = group_emails(load_source(), headers)
    wanted: list[dict[str, Any]] = []
    seen: set[int] = set()
    for email in emails:
        for bill in email["bills"]:
            if int(bill["id"]) not in seen:
                seen.add(int(bill["id"]))
                wanted.append(bill)
    client = login()
    install_401_guard(client)
    refuse_writes(client)
    attached = check_attachments(client, wanted)
    missing = [bill_id for bill_id in seen if attached.get(bill_id) not in {"y", "n"}]
    if missing:
        raise SystemExit(f"Attachment check incomplete for {missing[:10]}")
    rows = build_rows(emails, attached)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    keeps = []
    for row in rows:
        action = row["proposed action"].split(" ", 1)[0]
        counts[action] = counts.get(action, 0) + 1
        if action == "KEEP":
            keeps.append(
                {
                    "received": row["message received time"],
                    "sender": row["sender"],
                    "subject": row["subject"],
                    "folder": row["current folder"],
                    "bills": row["bills"],
                    "action": row["proposed action"],
                }
            )
    summary = {
        "emails": len(rows),
        "counts": counts,
        "keeps": keeps,
        "unmatched_bills": [
            {"bill": row["bill"], "vendor": row["vendor"], "invoice": row["invoice"]} for row in unmatched
        ],
    }
    Path("/tmp/sept-archive-v2-summary.json").write_text(json.dumps(summary, indent=2))
    LOGGER.info("Wrote %s emails=%s counts=%s unmatched=%s", OUT.name, len(rows), counts, [row["bill"] for row in unmatched])


if __name__ == "__main__":
    main()
