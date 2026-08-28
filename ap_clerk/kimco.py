"""Thin KIMCO REST client. Never logs secrets or token values.

Default target is prototype. Live writes require target=live and live.kimcoerp.com.
Kyle said go for live writes on 2026-08-28 (explicit --live + KIMCO_LIVE_* only).
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

    def _url(self, service: str, item_id: int | str | None = None, suffix: str = "") -> str:
        guid = self.services[service]
        path = f"{self.base_url}/api/v2/{guid}"
        if item_id is not None:
            path = f"{path}/{item_id}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

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
        """POST a header. Returns (id, body, status, error_text)."""
        response = self.request("POST", self._url(service), json=values)
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
            wrapped = self.request("POST", self._url(service), json={"values": values})
            if wrapped.status_code < 400:
                return _created_id(wrapped.json()), wrapped.json(), wrapped.status_code, ""
            return None, body, status, text[:500]
        return _created_id(body), body, status, ""

    def options(self, service: str, item_id: int | None = None) -> tuple[int, str]:
        """Probe allowed methods. Returns (status, Allow header). Never prints bodies."""
        response = self.request("OPTIONS", self._url(service, item_id))
        allow = response.headers.get("Allow") or response.headers.get("allow") or ""
        return response.status_code, allow

    def try_put_probe_rejected(self, service: str, item_id: int) -> str:
        """Read-only capability hint: GET then reject if the list is not editable.

        Does not send a PUT of invented values. Uses a HEAD/OPTIONS probe first.
        """
        status, allow = self.options(service, item_id)
        allow_u = allow.upper()
        if "PUT" in allow_u or "PATCH" in allow_u:
            return f"editable-options-{status}:{allow}"
        if status in {404, 405}:
            return f"blocked-{status}"
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
        """Official 7.7 attach. On some AP lists, upload notify is 405."""
        notify = self.request(
            "POST",
            self._url("ap_invoices", invoice_id, "attachments/upload"),
            json={"name": name, "contentType": content_type, "size": size},
        )
        if notify.status_code == 405:
            return "blocked-405"
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
            headers={"Content-Type": content_type},
            timeout=self.timeout,
        )
        if upload.status_code >= 400:
            return f"upload-failed-{upload.status_code}"
        complete = self.request(
            "POST",
            self._url("ap_invoices", invoice_id, "attachments"),
            json={"fileId": file_id},
        )
        if complete.status_code >= 400:
            return f"complete-failed-{complete.status_code}"
        return "attached"


def _created_id(body: Any) -> int | None:
    if not isinstance(body, dict):
        return None
    if body.get("id") is not None:
        return int(body["id"])
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    if values.get("id") is not None:
        return int(values["id"])
    return None
