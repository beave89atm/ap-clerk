"""Thin KIMCO REST client. Never logs secrets or token values.

Default target is prototype. Live writes require target=live and live.kimcoerp.com.
Kyle said go for live writes on 2026-08-28 (explicit --live + KIMCO_LIVE_* only).

Record-endpoint rule: create/search use `/api/v2/{serviceId}`. Updates, line
additions, Select Receipts-equivalent edits, and attachments use
`/api/v2/{serviceId}/{id}` (prototype or live host). Select Receipts is a
record PUT of `lists.APInvoiceLine` with `values.Receipt.id` (receipt LINE id).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from ap_clerk.auth import LIVE_HOST

LOGGER = logging.getLogger("ap_clerk")

PROTOTYPE_SERVICES = {
    "ap_invoices": "4898fd433bff417daa1689dece54b840",
    "ap_batches": "23245dfe2158496cbf949e7091d0542c",
    "purchase_lines": "f5b4b6f631be45f58d10e019060bd761",
    "receipts": "0a74fb9972974950a5e24e8f4981aaff",
    "document_types": "c2c451ebb51d42fb96e2651490ee1477",
}

# Identified 2026-08-28 via GET-only on live.kimcoerp.com (no writes).
LIVE_SERVICES = {
    "ap_invoices": "bcca4094b6ec4564942b19f5d7bb255c",
    "ap_batches": "31bf524dcd5b464580d4a1b55c01881e",
    "purchase_lines": "f1f8732f8daa4e2b9d8065037f7bb43d",
    "receipts": "494eafafa31a42bba7eb8697a36a3f0a",
}

# Kept for older HOLD rows / tests. Live writes are allowed when Kyle said go
# and the client target is live (never on the prototype target).
LIVE_WRITE_BLOCKED = "Live writes are off until Kyle says go"
LIVE_WRITES_ENABLED = True

# Kyle / KIMCO admin: AP Invoice list needs these four checkboxes on.
# Live probe 2026-09-09 (Orthman 9931): record GET/PUT/attach succeeded after
# Kyle enabled them. A 405 after a record URL still means check these.
LIST_EDIT_CHECKBOXES = (
    "Can View Items",
    "Can Edit Items",
    "Quick Add",
    "Can Edit Items Inline",
)
LIST_EDIT_PERMISSIONS_HINT = "check Can Edit Items / Inline on the list"


def services_for(target: str) -> dict[str, str]:
    if target == "live":
        return LIVE_SERVICES
    return PROTOTYPE_SERVICES


class KimcoError(RuntimeError):
    pass


class KimcoClient:
    def __init__(self, base_url: str, token: str, timeout: int = 90, *, target: str = "prototype"):
        self.target = target if target in {"prototype", "live"} else "prototype"
        self.services = services_for(self.target)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        if self.target == "live":
            if LIVE_HOST not in (self.base_url or "").lower():
                raise KimcoError("Live target requires live.kimcoerp.com")
        elif LIVE_HOST in (self.base_url or "").lower():
            raise KimcoError("Refusing live.kimcoerp.com on prototype target")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def authenticate(
        cls,
        base_url: str,
        key: str,
        password: str,
        *,
        target: str = "prototype",
    ) -> "KimcoClient":
        if target == "live":
            if LIVE_HOST not in (base_url or "").lower():
                raise KimcoError("Live target requires live.kimcoerp.com")
        elif LIVE_HOST in (base_url or "").lower():
            raise KimcoError("Refusing live.kimcoerp.com on prototype target")
        url = f"{base_url.rstrip('/')}/api/v2/authenticate"
        response = requests.post(url, json={"key": key, "password": password}, timeout=60)
        if response.status_code != 200:
            raise KimcoError(f"Authenticate failed HTTP {response.status_code}")
        payload = response.json()
        token = payload.get("token")
        if not token:
            raise KimcoError("Authenticate response had no token field")
        LOGGER.info("Authenticated to %s (token present, not printed)", target)
        return cls(base_url, token, target=target)

    def _list_url(self, service: str) -> str:
        """List endpoint: create + search only. Never used for edit/attach."""
        return f"{self.base_url}/api/v2/{self.services[service]}"

    def _record_url(self, service: str, item_id: int | str, suffix: str = "") -> str:
        """Record endpoint `/api/v2/{serviceId}/{id}` (+ optional suffix)."""
        if item_id in (None, ""):
            raise KimcoError(f"{service} record URL requires an item id")
        path = f"{self.base_url}/api/v2/{self.services[service]}/{item_id}"
        if suffix:
            path = f"{path}/{str(suffix).lstrip('/')}"
        return path

    def _url(self, service: str, item_id: int | str | None = None, suffix: str = "") -> str:
        if suffix and item_id in (None, ""):
            raise KimcoError(f"{service} suffix {suffix!r} requires a record id")
        if item_id is not None:
            return self._record_url(service, item_id, suffix)
        return self._list_url(service)

    def _looks_like_record_url(self, url: str) -> bool:
        path = (url or "").split("?", 1)[0]
        reserved = {"attachments", "items", "upload"}
        for guid in self.services.values():
            marker = f"/api/v2/{guid}/"
            if marker not in path:
                continue
            rest = path.split(marker, 1)[1].strip("/")
            if not rest:
                return False
            first = rest.split("/", 1)[0]
            if first.lower() in reserved:
                return False
            return bool(first)
        return False

    def _blocked_405(self, action: str, item_id: int | str | None = None) -> str:
        where = f" record {item_id}" if item_id not in (None, "") else ""
        return (
            f"blocked-405: {action}{where} returned 405 after using the record URL; "
            f"{LIST_EDIT_PERMISSIONS_HINT}"
        )

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        method_upper = (method or "").upper()
        live_url = LIVE_HOST in (url or "").lower()
        if self.target == "live":
            if not live_url:
                raise KimcoError("Live client refuses non-live hosts")
        elif live_url:
            raise KimcoError("Refusing live.kimcoerp.com on prototype target")
        if method_upper not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            raise KimcoError(f"Unsupported HTTP method {method_upper}")
        if method_upper in {"PUT", "PATCH"} and not self._looks_like_record_url(url):
            raise KimcoError(
                "Refusing list-endpoint edit; updates use /api/v2/{serviceId}/{id}"
            )
        if method_upper == "POST" and "attachments" in (url or "").lower():
            if not self._looks_like_record_url(url):
                raise KimcoError(
                    "Refusing list-endpoint attach; attachments use "
                    "/api/v2/{serviceId}/{id}/attachments"
                )
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        return response

    def get_item(self, service: str, item_id: int) -> dict[str, Any]:
        response = self.request("GET", self._url(service, item_id))
        if response.status_code != 200:
            raise KimcoError(f"GET {service}/{item_id} HTTP {response.status_code}: {response.text[:300]}")
        return response.json()

    def list_items(
        self,
        service: str,
        *,
        page_size: int = 2000,
        fields: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        total = None
        while total is None or offset < total:
            params: dict[str, Any] = {"pageSize": page_size, "offset": offset}
            if fields:
                params["fields"] = fields
            response = self.request("GET", self._url(service), params=params)
            if response.status_code != 200:
                raise KimcoError(f"GET {service} HTTP {response.status_code}: {response.text[:300]}")
            payload = response.json()
            chunk = payload.get("items") or []
            total = int(payload.get("totalCount") or 0)
            items.extend(chunk)
            LOGGER.info("Listed %s offset=%s got=%s total=%s", service, offset, len(chunk), total)
            if not chunk:
                break
            offset += len(chunk)
        return items

    def create(self, service: str, values: dict[str, Any]) -> tuple[int | None, dict[str, Any], int, str]:
        """POST a header on the list endpoint only. Returns (id, body, status, error_text)."""
        response = self.request("POST", self._list_url(service), json=values)
        status = response.status_code
        text = response.text
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            parsed = None
        if status >= 400:
            wrapped = self.request("POST", self._list_url(service), json={"values": values})
            if wrapped.status_code < 400:
                return _created_id(wrapped.json()), wrapped.json(), wrapped.status_code, ""
            return None, body, status, text[:500]
        return _created_id(body), body, status, ""

    def update(
        self,
        service: str,
        item_id: int | str,
        values: dict[str, Any],
        *,
        method: str = "PUT",
    ) -> tuple[dict[str, Any], int, str]:
        """PUT/PATCH an existing record. Never the list GUID."""
        url = self._record_url(service, item_id)
        verb = (method or "PUT").upper()
        if verb not in {"PUT", "PATCH"}:
            raise KimcoError(f"update requires PUT or PATCH, not {verb}")
        response = self.request(verb, url, json=values)
        if response.status_code >= 400:
            wrapped = self.request(verb, url, json={"values": values})
            if wrapped.status_code < 400:
                return _json_dict(wrapped), wrapped.status_code, ""
            if response.status_code == 405 or wrapped.status_code == 405:
                raise KimcoError(self._blocked_405(f"{verb} {service}", item_id))
            return {}, response.status_code, (response.text or "")[:500]
        return _json_dict(response), response.status_code, ""

    def get_invoice_lines(self, invoice_id: int | str) -> list[dict[str, Any]]:
        """Child APInvoiceLine rows from a record GET (`lists.APInvoiceLine`)."""
        return invoice_lines_from_record(self.get_item("ap_invoices", int(invoice_id)))

    def list_attachments(self, invoice_id: int | str) -> list[dict[str, Any]]:
        """GET attachments on the invoice RECORD. Never the list GUID."""
        if invoice_id in (None, ""):
            raise KimcoError("Attachment list requires an invoice record id")
        response = self.request(
            "GET",
            self._record_url("ap_invoices", invoice_id, "attachments"),
        )
        if response.status_code != 200:
            LOGGER.info("GET attachments HTTP %s for record %s", response.status_code, invoice_id)
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        items = payload.get("items") or payload.get("attachments") or payload.get("value") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def add_invoice_lines(self, invoice_id: int | str, lines: list[dict[str, Any]]) -> str:
        """Select Receipts-equivalent lines on the invoice RECORD.

        Live 2026-09-09: PUT `{id, state: Modified, lists.APInvoiceLine:
        [{state: Added, values: {Receipt, Purchase_Order_*, Part_ID,
        Quantity, Unit_Price, Invoice_Number, Vendor}}]}` on Orthman 9931
        returned 200 and GET then showed the receipt-linked line. Receipt-only
        values returned 400 "items in this list are not valid". OPTIONS Allow
        is DELETE, GET, PUT. Never the list GUID. Never typed Add Item
        (Receipt.id is required).
        """
        if invoice_id in (None, ""):
            raise KimcoError("Line add requires an invoice record id")
        payload = select_receipts_payload(lines, invoice_id=invoice_id)
        url = self._record_url("ap_invoices", invoice_id)
        put = self.request("PUT", url, json=payload)
        if put.status_code < 400:
            return "added"
        if put.status_code == 405:
            return self._blocked_405("line add", invoice_id)
        return f"blocked-{put.status_code}"

    def options(self, service: str, item_id: int | None = None) -> tuple[int, str]:
        """Probe allowed methods. Returns (status, Allow header). Never prints bodies."""
        response = self.request("OPTIONS", self._url(service, item_id))
        allow = response.headers.get("Allow") or response.headers.get("allow") or ""
        return response.status_code, allow

    def try_put_probe_rejected(self, service: str, item_id: int) -> str:
        """Read-only capability hint against the record URL. No invented PUT body."""
        status, allow = self.options(service, item_id)
        allow_u = allow.upper()
        if "PUT" in allow_u or "PATCH" in allow_u:
            return f"editable-options-{status}:{allow}"
        if status == 405:
            return self._blocked_405(f"OPTIONS {service}", item_id)
        if status == 404:
            return "blocked-404"
        return f"options-{status}:{allow or 'no-Allow'}"

    def try_official_attach(
        self,
        invoice_id: int,
        *,
        name: str,
        content_type: str,
        size: int,
        content: bytes,
    ) -> str:
        """Official 7.7 attach on the invoice RECORD (never the list GUID).

        Notify `POST .../{id}/attachments/upload` → PUT uploadUrl →
        complete `POST .../{id}/attachments`.

        Azure Blob uploadUrl requires `x-ms-blob-type: BlockBlob` (live probe
        2026-09-09: PUT without it returned 400 MissingRequiredHeader).
        """
        if invoice_id in (None, ""):
            raise KimcoError("PDF attach requires an invoice record id")
        notify = self.request(
            "POST",
            self._record_url("ap_invoices", invoice_id, "attachments/upload"),
            json={"name": name, "contentType": content_type, "size": size},
        )
        if notify.status_code == 405:
            return self._blocked_405("PDF attach notify", invoice_id)
        if notify.status_code >= 400:
            return f"blocked-{notify.status_code}"
        payload = notify.json()
        upload_url = payload.get("uploadUrl") or payload.get("upload_url")
        file_id = payload.get("fileId") or payload.get("file_id")
        if not upload_url:
            return f"notify-missing-uploadUrl-{notify.status_code}"
        upload = requests.put(
            upload_url,
            data=content,
            headers={
                "Content-Type": content_type,
                "x-ms-blob-type": "BlockBlob",
            },
            timeout=self.timeout,
        )
        if upload.status_code >= 400:
            return f"upload-failed-{upload.status_code}"
        complete = self.request(
            "POST",
            self._record_url("ap_invoices", invoice_id, "attachments"),
            json={"fileId": file_id},
        )
        if complete.status_code == 405:
            return self._blocked_405("PDF attach complete", invoice_id)
        if complete.status_code >= 400:
            return f"complete-failed-{complete.status_code}"
        return "attached"

    def try_select_receipts(self, invoice_id: int, receipt_ids: list[Any] | None = None) -> str:
        """Select Receipts-equivalent on the invoice RECORD.

        GETs the invoice + each receipt LINE, then PUTs `lists.APInvoiceLine`
        with Receipt.id plus PO/part/qty/price copied from those records.
        Never the list GUID. Never invents typed Add Item merchandise lines.
        """
        if invoice_id in (None, ""):
            raise KimcoError("Select Receipts requires an invoice record id")
        ids = [rid for rid in (receipt_ids or []) if rid not in (None, "")]
        if not ids:
            return "blocked-no-receipt-ids"
        invoice = self.get_item("ap_invoices", int(invoice_id))
        lines = []
        for rid in ids:
            receipt = self.get_item("receipts", int(rid))
            lines.append(receipt_line_values_from_records(invoice, receipt))
        status = self.add_invoice_lines(invoice_id, lines)
        if status == "added":
            return "selected"
        return status


def select_receipts_payload(lines_or_ids: list[Any], *, invoice_id: int | str | None = None) -> dict[str, Any]:
    """Proven live record PUT body (Orthman 9931, 2026-09-09, HTTP 200).

    Parent: `{id, state: "Modified"}`.
    Each child: `{state: "Added", values: {Receipt.id, ...}}`.
    Receipt.id is required so this cannot invent typed Add Item rows.
    Extra values (PO line, part, qty, price, vendor) are kept when present —
    Receipt-only children return 400 on live.
    """
    items: list[dict[str, Any]] = []
    for raw in lines_or_ids or []:
        receipt_id = _receipt_id_from_line(raw)
        if receipt_id in (None, ""):
            raise KimcoError("Select Receipts lines must include Receipt.id; do not type Add Item")
        values = _line_values(raw)
        values["Receipt"] = {"id": receipt_id}
        items.append({"state": "Added", "values": values})
    if not items:
        raise KimcoError("Select Receipts requires at least one Receipt.id")
    payload: dict[str, Any] = {"state": "Modified", "lists": {"APInvoiceLine": items}}
    if invoice_id not in (None, ""):
        payload["id"] = int(invoice_id)
    return payload


def receipt_line_values_from_records(invoice: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Copy Select Receipts fields from invoice + receipt GETs. Receipt.id required."""
    inv = _unwrap_record(invoice)
    rv = _unwrap_record(receipt)
    receipt_id = receipt.get("id") if isinstance(receipt, dict) else None
    if receipt_id in (None, ""):
        receipt_id = rv.get("id")
    if receipt_id in (None, ""):
        raise KimcoError("Select Receipts lines must include Receipt.id; do not type Add Item")
    values: dict[str, Any] = {"Receipt": {"id": receipt_id}}
    invoice_id = invoice.get("id") if isinstance(invoice, dict) else None
    if invoice_id in (None, ""):
        invoice_id = inv.get("id")
    if invoice_id not in (None, ""):
        values["Invoice_Number"] = {"id": invoice_id}
    vendor = inv.get("Vendor")
    if isinstance(vendor, dict) and vendor.get("id") not in (None, ""):
        values["Vendor"] = {"id": vendor["id"]}
    po = inv.get("Purchase_Order") if isinstance(inv.get("Purchase_Order"), dict) else None
    if not po:
        po = rv.get("PO_Number") if isinstance(rv.get("PO_Number"), dict) else None
    if isinstance(po, dict) and po.get("id") not in (None, ""):
        values["Purchase_Order_Number"] = {"id": po["id"]}
    pol = rv.get("PO_Item_Number")
    if isinstance(pol, dict) and pol.get("id") not in (None, ""):
        values["Purchase_Order_Line"] = {"id": pol["id"]}
    part = rv.get("Part_Number")
    if isinstance(part, dict) and part.get("id") not in (None, ""):
        values["Part_ID"] = {"id": part["id"]}
    qty = rv.get("Quantity_Received")
    if qty not in (None, ""):
        values["Quantity"] = qty
    price = rv.get("PO_Item_Number_$_Unit_Price")
    if price in (None, ""):
        price = rv.get("Purchase_Cost")
    if price in (None, ""):
        price = rv.get("Unit_Cost")
    if price not in (None, ""):
        values["Unit_Price"] = price
    return values


def invoice_lines_from_record(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(record, dict):
        return []
    lists = record.get("lists") if isinstance(record.get("lists"), dict) else {}
    items = lists.get("APInvoiceLine") or []
    return list(items) if isinstance(items, list) else []


def receipt_ids_from_invoice_lines(lines: list[dict[str, Any]] | None) -> list[Any]:
    ids: list[Any] = []
    for line in lines or []:
        rid = _receipt_id_from_line(line)
        if rid not in (None, ""):
            ids.append(rid)
    return ids


def _unwrap_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    values = record.get("values") if isinstance(record.get("values"), dict) else None
    if values:
        merged = dict(values)
        if record.get("id") not in (None, "") and "id" not in merged:
            merged["id"] = record["id"]
        return merged
    return record


def _line_values(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (int, str)):
        return {"Receipt": {"id": raw}}
    if not isinstance(raw, dict):
        return {}
    values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
    keep = (
        "Receipt",
        "Purchase_Order_Number",
        "Purchase_Order_Line",
        "Part_ID",
        "Quantity",
        "Unit_Price",
        "Invoice_Number",
        "Vendor",
    )
    out: dict[str, Any] = {}
    for key in keep:
        if values.get(key) not in (None, ""):
            item = values[key]
            if isinstance(item, dict) and "id" in item:
                out[key] = {"id": item["id"]}
            else:
                out[key] = item
    return out


def _receipt_id_from_line(raw: Any) -> Any:
    if isinstance(raw, (int, str)) and raw not in (None, ""):
        return raw
    if not isinstance(raw, dict):
        return None
    values = raw.get("values") if isinstance(raw.get("values"), dict) else raw
    receipt = values.get("Receipt") if isinstance(values, dict) else None
    if isinstance(receipt, dict):
        return receipt.get("id")
    return None


def _json_dict(response: Any) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (ValueError, AttributeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _created_id(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    if body.get("id") is not None:
        return int(body["id"])
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    if values.get("id") is not None:
        return int(values["id"])
    return None
