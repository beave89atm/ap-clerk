"""Link unposted AFT bills to the Customer P.O. No. on the invoice.

One live login as API Agent user 175. Does not post. Does not close a batch.
Does not change the vendor. Selects a receipt only when quantity and price
match the invoice line to the penny.
"""

from __future__ import annotations

import json
import re
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ap_clerk.aft_customer_po import (
    assign_receipts,
    build_treyce_note,
    customer_po_from_words,
    merchandise_lines,
    payable_amount,
)
from ap_clerk.auth import load_credentials
from ap_clerk.kimco import (
    API_AGENT_COMMENT_AUTHOR_IDS,
    API_AGENT_COMMENT_AUTHOR_NAME,
    KimcoClient,
    KimcoError,
    added_comment_payload,
    comment_author_from_access_token,
)
from ap_clerk.rules import TREYCE_MENTION_ID, lookup_id, lookup_text, money

HOST = "https://live.kimcoerp.com"
BATCH_ID = 375
OUT_JSON = ROOT / "artifacts" / "aft-customer-po-10398-10399.json"
BILLS = (
    {"id": 10399, "invoice": "52005", "amount": 363.00, "expect_po": "59106"},
    {"id": 10398, "invoice": "52004", "amount": 190.30, "expect_po": None},
)


def load_live() -> KimcoClient:
    creds = load_credentials(target="live")
    if not creds.ready:
        raise SystemExit(creds.error or "live credentials missing")
    if HOST not in (creds.instance_url or ""):
        raise SystemExit("refusing non-live host")
    try:
        client = KimcoClient.authenticate(
            creds.instance_url, creds.key or "", creds.password or "", target="live"
        )
    except KimcoError as exc:
        raise SystemExit(f"auth failed; stopping. {exc}") from exc
    author = comment_author_from_access_token(client.access_token)
    expected = API_AGENT_COMMENT_AUTHOR_IDS["live"]
    if int(author.get("id") or 0) != expected or author.get("name") != API_AGENT_COMMENT_AUTHOR_NAME:
        raise SystemExit(
            f"stopping: login resolved to {author.get('id')} {author.get('name')}, not API Agent {expected}"
        )
    return client


def _po_token(text: str, number: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", text or "") is not None


def words_from_pdf(content: bytes) -> list[dict[str, Any]]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    words: list[dict[str, Any]] = []

    def absorb(text: str, x: float, y: float, size: float) -> None:
        line_y = y
        step = max(size, 8.0) * 1.25
        for row in str(text or "").splitlines():
            cursor = x
            previous = 0
            for match in re.finditer(r"\S+", row):
                gap = match.start() - previous
                cursor += max(size, 8.0) * 0.45 * gap
                words.append({"text": match.group(), "x": cursor, "y": line_y})
                cursor += max(size, 8.0) * 0.45 * len(match.group())
                previous = match.end()
            line_y -= step

    for page in reader.pages:
        def visitor(text: str, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
            del cm, font_dict
            if not text or not str(text).strip():
                return
            try:
                x_pos = float(tm[4])
                y_pos = float(tm[5])
            except (TypeError, ValueError, IndexError):
                x_pos, y_pos = 0.0, 0.0
            try:
                size = float(font_size or 10)
            except (TypeError, ValueError):
                size = 10.0
            absorb(text, x_pos, y_pos, size)

        page.extract_text(visitor_text=visitor) or ""
    return words


def _attachment_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("fileName") or item.get("filename") or "attachment.pdf")


def _looks_pdf(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def download_pdfs(client: KimcoClient, bill_id: int, attachments: list[dict[str, Any]]) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for item in attachments:
        name = _attachment_name(item)
        content = _attachment_bytes(client, bill_id, item)
        if content and _looks_pdf(content):
            found.append((name, content))
    return found


def _attachment_bytes(client: KimcoClient, bill_id: int, item: dict[str, Any]) -> bytes | None:
    for key in ("url", "downloadUrl", "contentUrl", "fileUrl", "href"):
        url = item.get(key)
        if isinstance(url, str) and url.startswith("http"):
            response = requests.get(url, timeout=60)
            if response.status_code == 200 and _looks_pdf(response.content):
                return response.content
    raw = item.get("content") or item.get("bytes")
    if isinstance(raw, str):
        import base64

        try:
            decoded = base64.b64decode(raw)
        except ValueError:
            decoded = b""
        if _looks_pdf(decoded):
            return decoded
    attachment_id = item.get("id")
    if attachment_id in (None, ""):
        return None
    suffixes = (
        f"attachments/{attachment_id}",
        f"attachments/{attachment_id}/download",
        f"attachments/{attachment_id}/content",
    )
    for suffix in suffixes:
        response = client.request("GET", client._record_url("ap_invoices", bill_id, suffix))
        if response.status_code != 200:
            continue
        if _looks_pdf(response.content):
            return response.content
        try:
            payload = response.json()
        except ValueError:
            continue
        if isinstance(payload, dict):
            nested = _attachment_bytes(client, bill_id, payload)
            if nested:
                return nested
    return None


def snapshot(record: dict[str, Any]) -> dict[str, Any]:
    values = record.get("values") or {}
    lists = record.get("lists") or {}
    lines = []
    for line in lists.get("APInvoiceLine") or []:
        lv = line.get("values") or {}
        lines.append(
            {
                "id": line.get("id"),
                "receipt": lookup_id(lv.get("Receipt")),
                "qty": money(lv.get("Quantity")),
                "price": money(lv.get("Unit_Price")),
                "ext": money(lv.get("Extended_Amount")),
            }
        )
    comments = []
    for comment in lists.get("Comments_1") or []:
        cv = comment.get("values") or {}
        author = cv.get("Created_By") or cv.get("Author") or cv.get("User") or cv.get("Entered_By")
        comments.append(
            {
                "id": comment.get("id"),
                "html": cv.get("HtmlValue") or "",
                "author_id": lookup_id(author) if isinstance(author, dict) else author,
                "author": lookup_text(author) if isinstance(author, dict) else None,
            }
        )
    vendor = values.get("Vendor")
    po = values.get("Purchase_Order")
    return {
        "invoice": str(values.get("Invoice_Number") or "").strip(),
        "vendor_id": lookup_id(vendor),
        "vendor": lookup_text(vendor),
        "po_id": lookup_id(po),
        "po": lookup_text(po),
        "amount": money(values.get("Invoice_Amount")),
        "net": money(values.get("Invoice_Net_Amount")),
        "balance": money(values.get("Invoice_Balance")),
        "verification": money(values.get("Invoice_Verification_Amount")),
        "batch_id": lookup_id(values.get("AP_Invoice_Batch")),
        "batch": lookup_text(values.get("AP_Invoice_Batch")),
        "invoice_type": values.get("Invoice_Type"),
        "posted": values.get("Posted"),
        "lines": lines,
        "comments": comments,
        "header_comments": str(values.get("Comments") or ""),
    }


def mentions_shawn(snap: dict[str, Any]) -> bool:
    blob = snap.get("header_comments") or ""
    for comment in snap.get("comments") or []:
        blob += "\n" + str(comment.get("html") or "")
    return bool(re.search(r"shawn", blob, re.I) or 'data-mention-id="104"' in blob)


def header_matches(snap: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    problems = []
    if snap["invoice"] != spec["invoice"]:
        problems.append(f"invoice {snap['invoice']} != {spec['invoice']}")
    if snap["batch_id"] != BATCH_ID:
        problems.append(f"batch {snap['batch_id']} != {BATCH_ID}")
    if snap["posted"] not in (None, "", False):
        problems.append("bill is posted")
    expected = money(spec["amount"])
    amounts = [snap["amount"], snap["net"], snap["balance"], snap["verification"]]
    if expected not in amounts:
        problems.append(f"amount {amounts} does not include {expected}")
    return problems


def _type_id(value: Any) -> Any:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _scan(client: KimcoClient, service: str, numbers: list[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    offset = 0
    total = None
    url = client._url(service)
    while total is None or offset < total:
        response = client.request("GET", url, params={"pageSize": 2000, "offset": offset})
        if response.status_code != 200:
            raise SystemExit(f"{service} list HTTP {response.status_code}; stopping")
        payload = response.json()
        items = payload.get("items") or []
        total = int(payload.get("totalCount") or 0)
        for item in items:
            blob = json.dumps(item.get("values") or {})
            if any(_po_token(blob, number) for number in numbers):
                hits.append(item)
        print(f"{service} offset {offset} total {total} hits {len(hits)}", flush=True)
        if not items:
            break
        offset += len(items)
    return hits


def po_header(items: list[dict[str, Any]], number: str) -> dict[str, Any] | None:
    found: dict[int, str] = {}
    vendor_text = ""
    for item in items:
        values = item.get("values") or {}
        text = lookup_text(values.get("Purchase_Order_Number")) or lookup_text(values.get("Display_Name")) or ""
        if not text:
            text = json.dumps(values)
        if not _po_token(text, number):
            continue
        po_id = lookup_id(values.get("Purchase_Order_Number"))
        if po_id is None:
            continue
        found[int(po_id)] = lookup_text(values.get("Purchase_Order_Number")) or text
        vendor = values.get("Vendor") or values.get("Purchase_Order_$_Vendor")
        if lookup_text(vendor):
            vendor_text = lookup_text(vendor)
    if len(found) != 1:
        return None
    po_id, text = next(iter(found.items()))
    return {"id": po_id, "text": text, "vendor_text": vendor_text}


def receipt_row(record: dict[str, Any], number: str) -> dict[str, Any] | None:
    values = record.get("values") if isinstance(record.get("values"), dict) else record
    blob = " ".join(
        str(part)
        for part in (
            lookup_text(values.get("PO_Number")),
            lookup_text(values.get("PO_Item_Number")),
            values.get("Name"),
        )
        if part
    )
    if not _po_token(blob, number):
        return None
    ap = values.get("AP_Invoice_Number")
    ap_text = lookup_text(ap) if isinstance(ap, dict) else str(ap or "")
    if ap_text in {"None", "null"}:
        ap_text = ""
    if values.get("Invoiced") is True or ap_text:
        return None
    qty = values.get("Quantity_Received")
    if money(qty) in (None, 0, 0.0):
        return None
    price = values.get("PO_Item_Number_$_Unit_Price")
    if price in (None, ""):
        price = values.get("Purchase_Cost")
    if price in (None, ""):
        price = values.get("Unit_Cost")
    if money(price) is None:
        return None
    qty_num = float(qty)
    price_num = float(price)
    return {
        "id": record.get("id"),
        "qty": qty_num,
        "unit_price": money(price_num),
        "ext": money(qty_num * price_num),
        "po": blob,
    }


def put_record(client: KimcoClient, bill_id: int, payload: dict[str, Any]) -> tuple[int, str]:
    if "Posted" in json.dumps(payload):
        raise SystemExit(f"refusing payload that mentions Posted on {bill_id}")
    _body, status, error = client.update("ap_invoices", bill_id, payload)
    return status, error


def link_po(client: KimcoClient, bill_id: int, before: dict[str, Any], po_id: int) -> str:
    if before["po_id"] == po_id:
        return "already"
    values: dict[str, Any] = {"Purchase_Order": {"id": int(po_id)}}
    if _type_id(before["invoice_type"]) == 4:
        values["Invoice_Type"] = 3
    payload = {"state": "Modified", "id": int(bill_id), "values": values}
    status, error = put_record(client, bill_id, payload)
    if status >= 400:
        return f"put-{status}:{error[:180]}"
    return "linked"


def restore_vendor(client: KimcoClient, bill_id: int, vendor_id: int) -> str:
    payload = {
        "state": "Modified",
        "id": int(bill_id),
        "values": {"Vendor": {"id": int(vendor_id)}},
    }
    status, error = put_record(client, bill_id, payload)
    if status >= 400:
        return f"restore-{status}:{error[:180]}"
    return "restored"


def batch_status(client: KimcoClient) -> dict[str, Any]:
    record = client.get_item("ap_batches", BATCH_ID)
    values = record.get("values") or {}
    return {
        "id": record.get("id"),
        "name": values.get("AP_Invoice_Batch_ID"),
        "status": values.get("Status"),
    }


def shawn_guard(before: dict[str, Any], html: str) -> None:
    if mentions_shawn(before):
        return
    if re.search(r"shawn", html, re.I) or 'data-mention-id="104"' in html:
        raise SystemExit("refusing a note that mentions Shawn")


def _save_note(
    client: KimcoClient,
    bill_id: int,
    before: dict[str, Any],
    html: str,
) -> dict[str, Any]:
    shawn_guard(before, html)
    prior_ids = {comment["id"] for comment in before["comments"]}
    current = snapshot(client.get_item("ap_invoices", bill_id))
    existing = next(
        (
            comment
            for comment in current["comments"]
            if "Customer P.O. No." in str(comment.get("html") or "")
            and f'data-mention-id="{TREYCE_MENTION_ID}"' in str(comment.get("html") or "")
            and "AP Clerk:" in str(comment.get("html") or "")
        ),
        None,
    )
    if existing:
        return {"comment_id": existing["id"], "comment_status": "already", "final": current}
    status_code, error = put_record(client, bill_id, added_comment_payload(bill_id, html))
    final = snapshot(client.get_item("ap_invoices", bill_id))
    fresh = [
        comment
        for comment in final["comments"]
        if comment["id"] not in prior_ids and "Customer P.O. No." in str(comment.get("html") or "")
    ]
    return {
        "comment_id": fresh[-1]["id"] if fresh else None,
        "comment_status": "added" if status_code < 400 else f"put-{status_code}:{error[:180]}",
        "final": final,
        "comment_author_id": (fresh[-1].get("author_id") if fresh else None),
        "comment_author": (fresh[-1].get("author") if fresh else None),
    }


def _public_snap(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "invoice": snap["invoice"],
        "vendor_id": snap["vendor_id"],
        "vendor": snap["vendor"],
        "po_id": snap["po_id"],
        "po": snap["po"],
        "amount": snap["amount"],
        "net": snap["net"],
        "balance": snap["balance"],
        "verification": snap["verification"],
        "batch_id": snap["batch_id"],
        "batch": snap["batch"],
        "invoice_type": snap["invoice_type"],
        "posted": snap["posted"],
        "lines": snap["lines"],
        "comment_ids": [comment["id"] for comment in snap["comments"]],
        "shawn_on_bill": mentions_shawn(snap),
    }


def main() -> None:
    client = load_live()
    author = comment_author_from_access_token(client.access_token)
    batch_before = batch_status(client)
    numbers = []
    # PO numbers are known only after the PDF read. Scan after both PDFs are read
    # when the stated 59106 is not the only number. Read PDFs inside process, so
    # collect numbers first with a read-only pass, then one write pass.
    prepared = []
    for spec in BILLS:
        record = client.get_item("ap_invoices", int(spec["id"]))
        before = snapshot(record)
        problems = header_matches(before, spec)
        attachments = client.list_attachments(int(spec["id"]))
        pdfs = download_pdfs(client, int(spec["id"]), attachments)
        words: list[dict[str, Any]] = []
        for _name, content in pdfs:
            words.extend(words_from_pdf(content))
        po = customer_po_from_words(words, invoice_number=spec["invoice"]) if words else None
        if spec["expect_po"] and not po:
            po = spec["expect_po"]
        if po:
            numbers.append(po)
        prepared.append(
            {
                "spec": spec,
                "before": before,
                "problems": problems,
                "pdf_names": [name for name, _content in pdfs],
                "words": words,
                "po": po,
                "attachments": len(attachments),
            }
        )
    unique_numbers = list(dict.fromkeys(numbers))
    po_items = _scan(client, "purchase_lines", unique_numbers) if unique_numbers else []
    receipt_items = _scan(client, "receipts", unique_numbers) if unique_numbers else []
    # Full receipt records are fetched inside process when the list row is sparse.
    reports = []
    for item in prepared:
        report = _process_prepared(client, item, po_items, receipt_items)
        reports.append(report)
        print(
            json.dumps(
                {
                    "bill_id": report.get("bill_id"),
                    "customer_po": report.get("customer_po"),
                    "po_linked": report.get("po_linked"),
                    "po_text": report.get("po_text"),
                    "receipts_selected": report.get("receipts_selected"),
                    "selected_receipt_ids": report.get("selected_receipt_ids"),
                    "payable": report.get("payable"),
                    "comment_id": report.get("comment_id"),
                    "status": report.get("status"),
                }
            ),
            flush=True,
        )
    batch_after = batch_status(client)
    out = {
        "login_user_id": author["id"],
        "login_user_name": author["name"],
        "batch_before": batch_before,
        "batch_after": batch_after,
        "batch_closed": batch_before.get("status") != batch_after.get("status"),
        "bills": reports,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out, indent=2))
    if batch_before.get("status") != batch_after.get("status"):
        raise SystemExit("batch status changed")


def _process_prepared(
    client: KimcoClient,
    item: dict[str, Any],
    po_items: list[dict[str, Any]],
    receipt_items: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = item["spec"]
    before = item["before"]
    bill_id = int(spec["id"])
    result: dict[str, Any] = {
        "bill_id": bill_id,
        "invoice": spec["invoice"],
        "expected_amount": spec["amount"],
        "before": _public_snap(before),
        "pdfs": item["pdf_names"],
        "attachment_count": item["attachments"],
        "wrote": False,
    }
    if item["problems"]:
        result["status"] = "stopped"
        result["problems"] = item["problems"]
        return result
    po = item["po"]
    if spec["expect_po"] and po and po != spec["expect_po"]:
        result["status"] = "stopped"
        result["problems"] = [f"Customer P.O. No. {po} != {spec['expect_po']}"]
        return result
    result["po_source"] = "pdf-customer-po-no" if item["words"] and customer_po_from_words(item["words"], invoice_number=spec["invoice"]) else (
        "stated-customer-po-no" if po else "missing"
    )
    if not po:
        result["status"] = "stopped"
        result["customer_po"] = None
        result["problems"] = ["Customer P.O. No. was not on the attached PDF"]
        return result
    result["customer_po"] = po
    header = po_header(po_items, po)
    if header is None and po_items:
        candidates: dict[int, dict[str, Any]] = {}
        for raw in po_items:
            if raw.get("id") in (None, ""):
                continue
            full = client.get_item("purchase_lines", int(raw["id"]))
            one = po_header([full], po)
            if one:
                candidates[int(one["id"])] = one
        header = next(iter(candidates.values())) if len(candidates) == 1 else None
        if len(candidates) > 1:
            result["po_id_candidates"] = sorted(candidates)
    result["po_header"] = header
    lines = merchandise_lines(item["words"], float(spec["amount"])) if item["words"] else []
    result["invoice_lines"] = [
        {"qty": row["qty"], "unit_price": row["unit_price"], "ext": row["ext"]} for row in lines
    ]
    open_receipts: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for raw in receipt_items:
        if raw.get("id") in (None, "") or raw.get("id") in seen:
            continue
        blob = json.dumps(raw.get("values") or {})
        if not _po_token(blob, po) and not _po_token(json.dumps(raw), po):
            continue
        seen.add(raw.get("id"))
        full = raw if receipt_row(raw, po) else client.get_item("receipts", int(raw["id"]))
        row = receipt_row(full, po)
        if row:
            open_receipts.append(row)
    result["open_receipts"] = open_receipts
    chosen = assign_receipts(lines, open_receipts) if lines and header else []
    if not header:
        payable, payable_field = payable_amount(before)
        note_result = _save_note(
            client,
            bill_id,
            before,
            build_treyce_note(
                po=po,
                invoice=spec["invoice"],
                linked=False,
                selected=[],
                payable=float(payable or 0),
                match_reason="no-lines",
            ),
        ) if payable is not None else {"comment_id": None, "comment_status": "not-added", "final": before}
        final = note_result["final"]
        result.update(
            {
                "status": "po-not-found",
                "po_linked": False,
                "link_status": "po-not-found",
                "receipts_selected": False,
                "selected_receipt_ids": [],
                "payable": payable_amount(final)[0],
                "payable_field": payable_field,
                "comment_id": note_result["comment_id"],
                "comment_status": note_result["comment_status"],
                "comment_author_login": API_AGENT_COMMENT_AUTHOR_IDS["live"],
                "after": _public_snap(final),
                "wrote": note_result["comment_status"] == "added",
            }
        )
        return result
    link_status = link_po(client, bill_id, before, int(header["id"]))
    result["wrote"] = True
    after_link = snapshot(client.get_item("ap_invoices", bill_id))
    if after_link["vendor_id"] != before["vendor_id"] and before["vendor_id"] is not None:
        result["vendor_restore"] = restore_vendor(client, bill_id, int(before["vendor_id"]))
        after_link = snapshot(client.get_item("ap_invoices", bill_id))
    if after_link["vendor_id"] != before["vendor_id"]:
        result["status"] = "vendor-changed"
        result["after"] = _public_snap(after_link)
        return result
    if after_link["batch_id"] != BATCH_ID or after_link["posted"] not in (None, "", False):
        result["status"] = "batch-or-posted-changed"
        result["after"] = _public_snap(after_link)
        return result
    linked = after_link["po_id"] == int(header["id"])
    already_ids = {line["receipt"] for line in before["lines"]}
    to_add = [row for row in chosen if row["id"] not in already_ids]
    if to_add and linked:
        result["select_status"] = client.try_select_receipts(bill_id, [{"id": row["id"]} for row in to_add])
    elif chosen and not to_add:
        result["select_status"] = "already"
    else:
        result["select_status"] = "not-selected"
    after_select = snapshot(client.get_item("ap_invoices", bill_id))
    if after_select["vendor_id"] != before["vendor_id"] and before["vendor_id"] is not None:
        result["vendor_restore"] = restore_vendor(client, bill_id, int(before["vendor_id"]))
        after_select = snapshot(client.get_item("ap_invoices", bill_id))
    if after_select["posted"] not in (None, "", False) or after_select["batch_id"] != BATCH_ID:
        result["status"] = "batch-or-posted-changed"
        result["after"] = _public_snap(after_select)
        return result
    if after_select["vendor_id"] != before["vendor_id"]:
        result["status"] = "vendor-changed"
        result["after"] = _public_snap(after_select)
        return result
    selected_ids = {line["receipt"] for line in after_select["lines"]}
    selected = [row for row in chosen if row["id"] in selected_ids]
    if not selected:
        match_reason = "matched-none" if lines else "no-lines"
    else:
        match_reason = "selected"
    payable, payable_field = payable_amount(after_select)
    if payable is None:
        result["status"] = "no-payable"
        result["after"] = _public_snap(after_select)
        return result
    html = build_treyce_note(
        po=po,
        invoice=spec["invoice"],
        linked=linked,
        selected=selected,
        payable=float(payable),
        match_reason=match_reason,
    )
    note_result = _save_note(client, bill_id, before, html)
    comment_id = note_result["comment_id"]
    comment_status = note_result["comment_status"]
    final = note_result["final"]
    if final["vendor_id"] != before["vendor_id"] or final["batch_id"] != BATCH_ID:
        result["status"] = "drift-after-comment"
    elif final["posted"] not in (None, "", False):
        result["status"] = "posted"
    else:
        result["status"] = "done"
    result.update(
        {
            "link_status": "linked" if linked else link_status,
            "po_linked": linked,
            "po_text": final["po"],
            "receipts_selected": bool(selected),
            "selected_receipt_ids": [row["id"] for row in selected],
            "payable": payable_amount(final)[0],
            "payable_field": payable_field,
            "comment_id": comment_id,
            "comment_status": comment_status,
            "comment_author_login": API_AGENT_COMMENT_AUTHOR_IDS["live"],
            "after": _public_snap(final),
        }
    )
    return result


if __name__ == "__main__":
    try:
        main()
    except KimcoError as exc:
        raise SystemExit(str(exc)[:300]) from exc
