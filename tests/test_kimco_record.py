"""KIMCO AP Invoice record-endpoint client. Mocked HTTP only. No live writes."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ap_clerk.kimco import (
    LIST_EDIT_PERMISSIONS_HINT,
    LIVE_SERVICES,
    PROTOTYPE_SERVICES,
    KimcoClient,
    KimcoError,
    invoice_lines_from_record,
    receipt_ids_from_invoice_lines,
    select_receipts_payload,
)

LIVE_URL = "https://live.kimcoerp.com"
PROTO_URL = "https://prototype.kimcoerp.com"
LIVE_GUID = LIVE_SERVICES["ap_invoices"]
PROTO_GUID = PROTOTYPE_SERVICES["ap_invoices"]
INVOICE_ID = 9897


class FakeResp:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or "{}"
        self.headers = headers or {}

    def json(self):
        return self._payload


def _live_client() -> KimcoClient:
    return KimcoClient(LIVE_URL, "token", target="live")


def _proto_client() -> KimcoClient:
    return KimcoClient(PROTO_URL, "token", target="prototype")


def _urls(req) -> list[str]:
    urls: list[str] = []
    for call in req.call_args_list:
        args = call.args
        kwargs = call.kwargs
        if len(args) >= 2:
            urls.append(args[1])
        elif "url" in kwargs:
            urls.append(kwargs["url"])
    return urls


def test_update_put_url_contains_id_after_service_guid() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(200, {"id": INVOICE_ID})) as req:
        body, status, error = client.update("ap_invoices", INVOICE_ID, {"Comments": "record-only"})
    assert status == 200
    assert error == ""
    assert body.get("id") == INVOICE_ID
    url = req.call_args.args[1]
    assert req.call_args.args[0] == "PUT"
    assert f"/{LIVE_GUID}/{INVOICE_ID}" in url
    assert url.rstrip("/").endswith(f"/{LIVE_GUID}/{INVOICE_ID}")
    assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")


def test_update_put_refuses_missing_id() -> None:
    client = _live_client()
    with pytest.raises(KimcoError, match="item id"):
        client.update("ap_invoices", "", {"Comments": "no"})


def test_add_invoice_lines_url_contains_id_after_service_guid() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(200, {"ok": True})) as req:
        status = client.add_invoice_lines(INVOICE_ID, [{"Receipt": {"id": 44}}])
    assert status == "added"
    url = req.call_args.args[1]
    assert req.call_args.args[0] == "PUT"
    assert f"/{LIVE_GUID}/{INVOICE_ID}" in url
    assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    body = req.call_args.kwargs.get("json")
    assert body == {"lists": {"APInvoiceLine": [{"values": {"Receipt": {"id": 44}}}]}}


def test_select_receipts_puts_record_lists_apinvoiceline() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(200, {"ok": True})) as req:
        status = client.try_select_receipts(INVOICE_ID, [44, 45])
    assert status == "selected"
    urls = _urls(req)
    assert urls
    for url in urls:
        assert f"/{LIVE_GUID}/{INVOICE_ID}" in url
        assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    assert req.call_args.args[0] == "PUT"
    assert req.call_args.kwargs.get("json") == select_receipts_payload([44, 45])


def test_select_receipts_payload_requires_receipt_id() -> None:
    assert select_receipts_payload([23879]) == {
        "lists": {"APInvoiceLine": [{"values": {"Receipt": {"id": 23879}}}]}
    }
    assert select_receipts_payload([{"values": {"Receipt": {"id": 23228}}}]) == {
        "lists": {"APInvoiceLine": [{"values": {"Receipt": {"id": 23228}}}]}
    }
    with pytest.raises(KimcoError, match="Receipt.id"):
        select_receipts_payload([{"Part_ID": {"id": 1}, "Quantity": 16}])


def test_invoice_lines_from_record_reads_lists_apinvoiceline() -> None:
    record = {
        "id": 9663,
        "lists": {
            "APInvoiceLine": [
                {
                    "id": 19771,
                    "values": {
                        "Receipt": {"id": 23228, "text": "PO58514-TPI - 2026/8/21"},
                        "Quantity": 16.0,
                    },
                }
            ]
        },
        "values": {"Lines_Count": 1},
    }
    lines = invoice_lines_from_record(record)
    assert len(lines) == 1
    assert receipt_ids_from_invoice_lines(lines) == [23228]
    assert invoice_lines_from_record({"values": {"Lines_Count": 0}}) == []


def test_attach_uses_record_attachments_endpoints() -> None:
    client = _live_client()
    notify = FakeResp(
        200,
        {"uploadUrl": "https://files.example.test/upload", "fileId": "file-1"},
    )
    complete = FakeResp(200, {"ok": True})
    upload = FakeResp(200)

    def kimco_request(method, url, **kwargs):
        if url.endswith("/attachments/upload"):
            return notify
        if url.endswith("/attachments"):
            return complete
        raise AssertionError(f"unexpected KIMCO URL {url}")

    with patch.object(client.session, "request", side_effect=kimco_request) as req:
        with patch("ap_clerk.kimco.requests.put", return_value=upload) as put:
            status = client.try_official_attach(
                INVOICE_ID,
                name="TXFT499356.pdf",
                content_type="application/pdf",
                size=4,
                content=b"%PDF",
            )
    assert status == "attached"
    urls = _urls(req)
    assert any(url.endswith(f"/{LIVE_GUID}/{INVOICE_ID}/attachments/upload") for url in urls)
    assert any(url.endswith(f"/{LIVE_GUID}/{INVOICE_ID}/attachments") for url in urls)
    for url in urls:
        assert f"/{LIVE_GUID}/{INVOICE_ID}" in url
        assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    assert put.call_args.args[0] == "https://files.example.test/upload"
    headers = put.call_args.kwargs.get("headers") or {}
    assert headers.get("x-ms-blob-type") == "BlockBlob"
    assert headers.get("Content-Type") == "application/pdf"


def test_attach_405_mentions_can_edit_items_inline() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(405, text="not editable")) as req:
        status = client.try_official_attach(
            INVOICE_ID,
            name="bill.pdf",
            content_type="application/pdf",
            size=1,
            content=b"%",
        )
    assert status.startswith("blocked-405")
    assert "Can Edit Items" in status
    assert "Inline" in status
    assert LIST_EDIT_PERMISSIONS_HINT in status
    url = req.call_args.args[1]
    assert f"/{LIVE_GUID}/{INVOICE_ID}/attachments/upload" in url


def test_update_405_mentions_can_edit_items_inline() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(405, text="not editable")):
        with pytest.raises(KimcoError, match="Can Edit Items") as exc:
            client.update("ap_invoices", INVOICE_ID, {"Comments": "x"})
    assert LIST_EDIT_PERMISSIONS_HINT in str(exc.value)
    assert "Inline" in str(exc.value)


def test_line_add_405_mentions_can_edit_items_inline() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(405)):
        status = client.add_invoice_lines(INVOICE_ID, [{"Receipt": {"id": 1}}])
    assert status.startswith("blocked-405")
    assert LIST_EDIT_PERMISSIONS_HINT in status


def test_create_stays_on_list_endpoint() -> None:
    client = _live_client()
    with patch.object(client.session, "request", return_value=FakeResp(200, {"id": 1})) as req:
        created_id, _body, status, error = client.create("ap_invoices", {"Invoice_Number": "x"})
    assert created_id == 1
    assert status == 200
    assert error == ""
    url = req.call_args.args[1]
    assert req.call_args.args[0] == "POST"
    assert url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    assert f"/{LIVE_GUID}/" not in url


def test_list_get_stays_on_list_endpoint() -> None:
    client = _live_client()
    payload = {"items": [{"id": 1, "values": {}}], "totalCount": 1}
    with patch.object(client.session, "request", return_value=FakeResp(200, payload)) as req:
        items = client.list_items("ap_invoices", page_size=10)
    assert len(items) == 1
    url = req.call_args.args[1]
    assert req.call_args.args[0] == "GET"
    assert url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    assert f"/{LIVE_GUID}/" not in url


def test_request_refuses_put_on_list_url() -> None:
    client = _live_client()
    list_url = f"{LIVE_URL}/api/v2/{LIVE_GUID}"
    with pytest.raises(KimcoError, match="list-endpoint edit"):
        client.request("PUT", list_url, json={"values": {}})
    with pytest.raises(KimcoError, match="list-endpoint attach"):
        client.request("POST", f"{list_url}/attachments/upload", json={})


def test_prototype_record_urls_use_prototype_guid() -> None:
    client = _proto_client()
    with patch.object(client.session, "request", return_value=FakeResp(200, {"id": 12})) as req:
        client.update("ap_invoices", 12, {"Comments": "proto"})
    url = req.call_args.args[1]
    assert url.startswith(PROTO_URL)
    assert f"/{PROTO_GUID}/12" in url
    assert LIVE_GUID not in url
    assert "live.kimcoerp.com" not in url


def test_record_url_helper_and_suffix() -> None:
    client = _live_client()
    record = client._record_url("ap_invoices", INVOICE_ID)
    attach = client._record_url("ap_invoices", INVOICE_ID, "attachments/upload")
    listed = client._list_url("ap_invoices")
    assert record == f"{LIVE_URL}/api/v2/{LIVE_GUID}/{INVOICE_ID}"
    assert attach == f"{LIVE_URL}/api/v2/{LIVE_GUID}/{INVOICE_ID}/attachments/upload"
    assert listed == f"{LIVE_URL}/api/v2/{LIVE_GUID}"
    with pytest.raises(KimcoError, match="record id"):
        client._url("ap_invoices", suffix="attachments")
