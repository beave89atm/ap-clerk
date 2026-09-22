"""NOTE-50: vendor → receiving-owner map for missing_receipt @tags.

Source: Kyle 2026-09-22 sheet AP-vendor-receiving-owners-2026-09-22.xlsx.
Blank Receiving owner = does not need dock receive — do not @tag.
Multi-owner cell text is intentional. Modern Heat Treat "Anthony?" → Anthony
with uncertainty in Notes until Kyle confirms.

Shawn mention-id 104 is confirmed (Kyle + live Comments_1). Ruben / Anthony /
Monica mention-ids were not found on a live Comments_1 scan — do not invent.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ap_clerk.rules import names_match, normalize_name

DATA_PATH = Path(__file__).resolve().parent / "data" / "receiving_owners.json"
COMMENTS_1_LIST = "Comments_1"

PEOPLE: dict[str, dict[str, Any]] = {
    "shawn": {
        "key": "shawn",
        "display": "Shawn McKibben",
        "tag": "@Shawn McKibben",
        "mention_id": 104,
    },
    "ruben": {
        "key": "ruben",
        "display": "Ruben Perez",
        "tag": "@Ruben Perez",
        "mention_id": None,
    },
    "anthony": {
        "key": "anthony",
        "display": "Anthony",
        "tag": "@Anthony",
        "mention_id": None,
    },
    "monica": {
        "key": "monica",
        "display": "Monica",
        "tag": "@Monica",
        "mention_id": None,
    },
}

NO_DOCK_OWNER = "none / no dock receive"
UNMAPPED_OWNER = "receiving / unmapped"

_EXTRA_ALIASES = {
    "aqpc": "American Quality Powder Coating",
    "american quality powder": "American Quality Powder Coating",
    "gas and supply": "Gas and Supply North Texas, LLC",
    "gas & supply": "Gas and Supply North Texas, LLC",
    "mcmaster": "McMaster-Carr Supply Company",
    "mcmaster-carr": "McMaster-Carr Supply Company",
    "emj": "Earle M. Jorgensen Co",
    "earle m. jorgensen": "Earle M. Jorgensen Co",
    "jpsteel": "JP Steel",
    "jp steel": "JP Steel",
    "o'neal": "O'Neal Steel - Dallas (GP)",
    "oneal": "O'Neal Steel - Dallas (GP)",
    "modern heat": "Modern Heat Treat Inc",
    "modern heat treat": "Modern Heat Treat Inc",
    "legacy wire": "Legacy Wire Products",
    "3p industries": "3P",
}


@lru_cache(maxsize=1)
def load_receiving_owner_map(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else DATA_PATH
    return json.loads(target.read_text())


def vendor_entries(path: str | None = None) -> list[dict[str, Any]]:
    return list(load_receiving_owner_map(path).get("vendors") or [])


def lookup_receiving_owner(vendor: str | None, path: str | None = None) -> dict[str, Any] | None:
    """Exact / alias, then names_match, against Kyle's sheet. None if unmapped.

    Exact normalized name wins first. names_match("American Quality Powder
    Coating", "American Bearing Company") is true (shared first token
    American) — do not let that steal AQPC.
    """
    needle = (vendor or "").strip()
    if not needle:
        return None
    extra = _EXTRA_ALIASES.get(normalize_name(needle))
    wanted = extra or needle
    entries = vendor_entries(path)
    wanted_n = normalize_name(wanted)
    needle_n = normalize_name(needle)
    for entry in entries:
        name_n = normalize_name(str(entry.get("vendor") or ""))
        if name_n and (name_n == wanted_n or name_n == needle_n):
            return entry
    for entry in entries:
        name = str(entry.get("vendor") or "")
        if names_match(wanted, name) or names_match(needle, name):
            return entry
    return None


def needs_dock_receive(vendor: str | None, path: str | None = None) -> bool:
    entry = lookup_receiving_owner(vendor, path)
    return bool(entry and entry.get("needs_dock_receive"))


def missing_receipt_exception_owner(vendor: str | None, path: str | None = None) -> str:
    """Exception owner for a missing_receipt row. Blank sheet → no dock tag.

    Single-key owners use the PEOPLE display name (Ruben → Ruben Perez).
    Multi-owner cells keep Kyle's raw text (Shawn/Monica, role splits).
    Modern Heat Treat (Anthony?) is Anthony; uncertainty lives in Notes.
    """
    entry = lookup_receiving_owner(vendor, path)
    if entry is None:
        return UNMAPPED_OWNER
    if not entry.get("needs_dock_receive"):
        return NO_DOCK_OWNER
    people = owner_people(entry)
    if len(people) == 1:
        return str(people[0]["display"])
    raw = str(entry.get("receiving_owner_raw") or "").strip()
    return raw or NO_DOCK_OWNER


def missing_receipt_notes(vendor: str | None, path: str | None = None) -> str:
    """Sheet Notes only when the owner is uncertain (Modern Heat Treat)."""
    entry = lookup_receiving_owner(vendor, path)
    if not entry or not entry.get("uncertain"):
        return ""
    return (
        str(entry.get("notes") or "").strip()
        or "Sheet marked owner with ? — treat as Anthony until Kyle confirms."
    )


def owner_people(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for key in (entry or {}).get("owner_keys") or []:
        person = PEOPLE.get(str(key).lower())
        if person:
            people.append(person)
    return people


def mention_span(person: dict[str, Any]) -> str:
    tag = person["tag"]
    mention_id = person.get("mention_id")
    name = person["display"]
    if mention_id is None:
        return tag
    return (
        f'<span data-mention-id="{int(mention_id)}" data-mention-name="{name}" '
        f'data-mention-email="" class="prosemirror-mention-node">{tag}</span>'
    )


def missing_receipt_comment_text(vendor: str | None, path: str | None = None) -> str:
    """Plain Comments_1 / Why text. Empty when the sheet says no dock receive."""
    entry = lookup_receiving_owner(vendor, path)
    if entry is None:
        return ""
    if not entry.get("needs_dock_receive"):
        return ""
    tags = " ".join(p["tag"] for p in owner_people(entry))
    bits = [tags, "missing receipt — dock receive."]
    raw = str(entry.get("receiving_owner_raw") or "")
    if " for " in raw.lower() or "/" in raw:
        bits.append(f"Sheet owner: {raw}.")
    if entry.get("uncertain"):
        bits.append("Owner marked Anthony? until Kyle confirms.")
    return " ".join(b for b in bits if b).strip()


def missing_receipt_comments_1_html(vendor: str | None, path: str | None = None) -> str:
    """HtmlValue for lists.Comments_1. Empty when we must not @tag."""
    text = missing_receipt_comment_text(vendor, path)
    if not text:
        return ""
    entry = lookup_receiving_owner(vendor, path)
    html_tags = " ".join(mention_span(p) for p in owner_people(entry))
    rest = text
    for person in owner_people(entry):
        rest = rest.replace(person["tag"], "", 1)
    rest = re.sub(r"\s+", " ", rest).strip()
    body = f"{html_tags} {rest}".strip()
    return f"<p>{body}</p>"


def missing_receipt_comments_1_child(vendor: str | None, path: str | None = None) -> dict[str, Any] | None:
    """Added Comments_1 child, or None when blank-owner / unmapped."""
    html = missing_receipt_comments_1_html(vendor, path)
    if not html:
        return None
    return {"state": "Added", "values": {"HtmlValue": html}}


def should_tag_missing_receipt(vendor: str | None, path: str | None = None) -> bool:
    return bool(missing_receipt_comments_1_child(vendor, path))
