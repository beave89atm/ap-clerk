"""AP Invoice Comments TAB (lists.Comments_1) — not the header Comments string.

Live 2026-09-21 (Kyle screenshot 10140): the UI Comments tab is the related
list `lists.Comments_1`. PATCH of header `values.Comments` is the wrong
surface — Kyle saw "Comments currently has no items". Dedicated GET
.../comments, .../mentions, .../notifications → 404.

Proven GET (9931 / 10009): each tab row has HtmlValue with a ProseMirror
mention span. Shawn McKibben mention-id **104** (same id as CreatorId on
Shawn-authored rows). Entity id 203 / FormId 218 = AP Invoice.

Proven PUT 2026-09-21 on 10140 (item 884) and 10143 (item 886):
HtmlValue-only Added → 400 Unexpected Error.
Added with HtmlValue + Entity + ObjectId + FormId → 200, then GET showed
the tab row. invent=false — do not claim a user-alert fired; suffix notify
GETs are still 404.
"""

from __future__ import annotations

from typing import Any

from ap_clerk.rules import SHAWN_MCKIBBEN

# Related list behind the AP Invoice Comments tab (Add Comment).
COMMENT_TAB_LIST = "Comments_1"

# NOTE-45: exception action is Comments_1 @tags. Do not Mail.Send.
EXCEPTION_MAIL_SEND = False

# Live GET lists.Comments_1 on 9931 comment 773 / 10009 comment 857.
SHAWN_MENTION_ID = 104
SHAWN_MENTION_NAME = "Shawn McKibben"
SHAWN_MENTION_EMAIL = "Shawn.McKibben@kannonmfg.com"
SHAWN_MENTION = {
    "id": SHAWN_MENTION_ID,
    "name": SHAWN_MENTION_NAME,
    "email": SHAWN_MENTION_EMAIL,
    "tag": SHAWN_MCKIBBEN,
}

# Live Comments_1 scan 2026-09-21 (273 invoices + 9931/10009): no @Ruben Perez
# mention-node. Graph /users 403. Do not invent an id or email.
RUBEN_MENTION_NAME = "Ruben Perez"
RUBEN_MENTION_TAG = "@Ruben Perez"
RUBEN_MENTION_ID = None
RUBEN_MENTION_EMAIL = None
RUBEN_MENTION = {
    "id": RUBEN_MENTION_ID,
    "name": RUBEN_MENTION_NAME,
    "email": RUBEN_MENTION_EMAIL,
    "tag": RUBEN_MENTION_TAG,
}

# Live GET lists.Comments_1 values on 9931 / 10009. Required on Added PUT.
AP_INVOICE_ENTITY = {"id": 203, "text": "AP Invoice"}
AP_INVOICE_FORM = {"id": 218, "text": "AP Invoice"}


def comment_tab_items(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    lists = record.get("lists") if isinstance(record.get("lists"), dict) else {}
    rows = lists.get(COMMENT_TAB_LIST) or []
    return [row for row in rows if isinstance(row, dict)]


def comment_tab_htmls(record: dict[str, Any] | None) -> list[str]:
    htmls: list[str] = []
    for row in comment_tab_items(record):
        vals = row.get("values") if isinstance(row.get("values"), dict) else {}
        html = str(vals.get("HtmlValue") or "")
        if html:
            htmls.append(html)
    return htmls


def header_comments_string(record: dict[str, Any] | None) -> str:
    """Wrong surface. Exposed only so callers can ignore it on purpose."""
    if not isinstance(record, dict):
        return ""
    vals = record.get("values") if isinstance(record.get("values"), dict) else {}
    return str(vals.get("Comments") or "")


def mention_html(body: str, *, mention: dict[str, Any] | None = None) -> str:
    """Treyce-style mention node. invent=false — mention-id must be proven."""
    person = mention or SHAWN_MENTION
    mid = person.get("id")
    name = str(person.get("name") or "").strip()
    email = str(person.get("email") or "")
    tag = str(person.get("tag") or (f"@{name}" if name else "")).strip()
    if mid in (None, "") or not name:
        raise ValueError("Refusing mention-node without a proven mention-id")
    text = (body or "").strip()
    if tag and text.startswith(tag):
        text = text[len(tag) :].strip()
    span = (
        f'<span data-mention-id="{int(mid)}" '
        f'data-mention-name="{name}" '
        f'data-mention-email="{email}" '
        f'class="prosemirror-mention-node">@{name}</span>'
    )
    return f"<p>{span} {text}</p>"


def shawn_mention_html(body: str) -> str:
    """Treyce-style mention node. mention-id 104 from live Comments_1 GETs."""
    return mention_html(body, mention=SHAWN_MENTION)


def comment_tab_add_payload(html: str, *, invoice_id: int) -> dict[str, Any]:
    """Record PUT body. Never includes header values.Comments."""
    return {
        "state": "Modified",
        "id": int(invoice_id),
        "lists": {
            COMMENT_TAB_LIST: [
                {
                    "state": "Added",
                    "values": {
                        "HtmlValue": html,
                        "Entity": dict(AP_INVOICE_ENTITY),
                        "ObjectId": int(invoice_id),
                        "FormId": dict(AP_INVOICE_FORM),
                    },
                }
            ]
        },
    }


def transfer_ap_batch_only_payload(*, invoice_id: int, batch_id: int) -> dict[str, Any]:
    """Move to Transfer AP. Do not stamp the header Comments string."""
    return {
        "state": "Modified",
        "id": int(invoice_id),
        "values": {"AP_Invoice_Batch": {"id": int(batch_id)}},
    }


def comment_tab_already_has(record: dict[str, Any] | None, *needles: str) -> bool:
    htmls = comment_tab_htmls(record)
    wanted = [n for n in needles if n]
    if not wanted:
        return False
    return any(all(n in html for n in wanted) for html in htmls)


def comment_tab_proof(
    record: dict[str, Any] | None,
    *,
    needles: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """GET-only proof. Header Comments string never counts as persisted."""
    items = comment_tab_items(record)
    htmls = comment_tab_htmls(record)
    header = header_comments_string(record)
    required = list(needles or (SHAWN_MCKIBBEN, "HOLD (price-does-not-match)"))
    matched = [
        {"id": row.get("id"), "html": html}
        for row, html in zip(items, htmls)
        if all(n in html for n in required)
    ]
    mention_nodes = [
        html
        for html in htmls
        if f'data-mention-id="{SHAWN_MENTION_ID}"' in html and SHAWN_MCKIBBEN in html
    ]
    tab_ok = bool(matched)
    report = format_comment_tab_report(
        tab_ok=tab_ok,
        matched=matched,
        mention_nodes=mention_nodes,
        header=header,
    )
    return {
        "tab_persisted": tab_ok,
        "comments_persisted": tab_ok,
        "header_string_ignored": True,
        "header_has_shawn": SHAWN_MCKIBBEN in header,
        "item_ids": [m["id"] for m in matched],
        "mention_node": bool(mention_nodes),
        "mention_id": SHAWN_MENTION_ID if mention_nodes else None,
        "worked": False,
        "notify_confirmed": False,
        "report": report,
    }


def format_comment_tab_report(
    *,
    tab_ok: bool,
    matched: list[dict[str, Any]],
    mention_nodes: list[str],
    header: str,
) -> str:
    """Never say 'Comments persisted' — that phrase was the header-string lie."""
    if tab_ok:
        ids = ",".join(str(m.get("id")) for m in matched if m.get("id") not in (None, ""))
        mention = (
            f"mention-node id {SHAWN_MENTION_ID}"
            if mention_nodes
            else "plain @Shawn text (no mention-node)"
        )
        return (
            f"Comments tab persisted {SHAWN_MCKIBBEN} "
            f"(list {COMMENT_TAB_LIST} item {ids or 'n/a'}, {mention}). "
            "User-alert/notify API not confirmed — dedicated GET "
            "comments/mentions/notifications are 404. "
            "Header Comments string is the wrong surface and is ignored."
        )
    extra = ""
    if SHAWN_MCKIBBEN in (header or ""):
        extra = (
            " Header Comments string has @Shawn but that is the wrong surface "
            "(Comments tab still empty)."
        )
    return (
        f"Comments tab missing {SHAWN_MCKIBBEN} on {COMMENT_TAB_LIST}.{extra} "
        "Do not treat the header Comments field as a Comments-tab item."
    )


def add_invoice_comment_tab(
    client: Any,
    *,
    invoice_id: int,
    body: str,
    needles: list[str] | tuple[str, ...] | None = None,
    mention: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """PUT lists.Comments_1 Added, then GET-prove. invent=false. No Mail.Send."""
    try:
        before = client.get_item("ap_invoices", int(invoice_id))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "get-before-failed",
            "error": str(exc)[:240],
            "invent": False,
            "tab_persisted": False,
            "comments_persisted": False,
            "report": f"GET before Comments tab failed: {type(exc).__name__}",
        }
    person = mention or SHAWN_MENTION
    try:
        html = mention_html(body, mention=person)
    except ValueError as exc:
        return {
            "status": "mention-id-unproven",
            "error": str(exc)[:240],
            "invent": False,
            "tab_persisted": False,
            "comments_persisted": False,
            "mail_send": False,
            "report": "Refused Comments_1 mention-node — mention-id not proven (invent=false).",
        }
    tag = str(person.get("tag") or SHAWN_MCKIBBEN)
    required = list(needles or (tag, "HOLD (price-does-not-match)"))
    if comment_tab_already_has(before, *required):
        proof = comment_tab_proof(before, needles=required)
        return {
            "status": "already-on-tab",
            "put": None,
            "html": html,
            "invent": False,
            "mail_send": False,
            **proof,
        }
    payload = comment_tab_add_payload(html, invoice_id=int(invoice_id))
    if "AP_Invoice_Batch" in (payload.get("values") or {}):
        return {
            "status": "refused-batch-move",
            "invent": False,
            "tab_persisted": False,
            "comments_persisted": False,
            "mail_send": False,
            "report": "Comments_1 payload must not move AP_Invoice_Batch.",
        }
    if "Comments" in (payload.get("values") or {}):
        return {
            "status": "refused-header-comments",
            "invent": False,
            "tab_persisted": False,
            "comments_persisted": False,
            "report": "Refused to write header Comments string.",
        }
    body_resp, status, error = client.update("ap_invoices", int(invoice_id), payload)
    try:
        after = client.get_item("ap_invoices", int(invoice_id))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": f"put-{status}-get-failed",
            "put": status,
            "error": (error or str(exc))[:240],
            "invent": False,
            "tab_persisted": False,
            "comments_persisted": False,
            "report": "PUT returned but GET proof failed — not claiming Comments tab.",
        }
    proof = comment_tab_proof(after, needles=required)
    return {
        "status": "added" if proof.get("tab_persisted") else f"put-{status}-tab-missing",
        "put": status,
        "error": error,
        "html": html,
        "put_body_keys": sorted(body_resp) if isinstance(body_resp, dict) else [],
        "invent": False,
        "mail_send": False,
        **proof,
    }


def apply_missing_receipt_comment_tab(
    client: Any,
    *,
    invoice_id: int,
    body: str,
    mention: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """NOTE-45: missing_receipt stays on the current API Agent batch.

    Comments_1 @tag only. Never Transfer AP. Never Mail.Send.
    """
    person = mention or SHAWN_MENTION
    tag = str(person.get("tag") or SHAWN_MCKIBBEN)
    out = add_invoice_comment_tab(
        client,
        invoice_id=int(invoice_id),
        body=body,
        needles=(tag, "HOLD (receipt)"),
        mention=person,
    )
    out["transfer_ap"] = False
    out["mail_send"] = False
    if "AP_Invoice_Batch" in str(out.get("put_body_keys") or []):
        out["status"] = "refused-batch-move"
        out["tab_persisted"] = False
    return out
