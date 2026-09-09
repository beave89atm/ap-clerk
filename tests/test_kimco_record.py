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
    receipt_line_values_from_records,
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
    line = {
        "Receipt": {"id": 44},
        "Purchase_Order_Number": {"id": 6638},
        "Purchase_Order_Line": {"id": 17666},
        "Part_ID": {"id": 20560},
        "Quantity": 24.0,
        "Unit_Price": 54.0,
        "Invoice_Number": {"id": INVOICE_ID},
        "Vendor": {"id": 434},
    }
    with patch.object(client.session, "request", return_value=FakeResp(200, {"ok": True})) as req:
        status = client.add_invoice_lines(INVOICE_ID, [line])
    assert status == "added"
    url = req.call_args.args[1]
    assert req.call_args.args[0] == "PUT"
    assert f"/{LIVE_GUID}/{INVOICE_ID}" in url
    assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    assert req.call_args.kwargs.get("json") == select_receipts_payload([line], invoice_id=INVOICE_ID)


def test_select_receipts_puts_record_lists_apinvoiceline() -> None:
    client = _live_client()
    invoice = {
        "id": INVOICE_ID,
        "values": {"Vendor": {"id": 434}, "Purchase_Order": {"id": 6638}},
    }
    receipt = {
        "id": 23879,
        "values": {
            "PO_Item_Number": {"id": 17666},
            "Part_Number": {"id": 20560},
            "Quantity_Received": 24.0,
            "PO_Item_Number_$_Unit_Price": 54.0,
        },
    }

    def kimco_request(method, url, **kwargs):
        if method == "GET" and url.endswith(f"/{LIVE_GUID}/{INVOICE_ID}"):
            return FakeResp(200, invoice)
        if method == "GET" and url.endswith(f"/{LIVE_SERVICES['receipts']}/23879"):
            return FakeResp(200, receipt)
        if method == "PUT" and url.endswith(f"/{LIVE_GUID}/{INVOICE_ID}"):
            return FakeResp(200, {"ok": True})
        raise AssertionError(f"unexpected {method} {url}")

    with patch.object(client.session, "request", side_effect=kimco_request) as req:
        status = client.try_select_receipts(INVOICE_ID, [23879])
    assert status == "selected"
    urls = _urls(req)
    assert urls
    for url in urls:
        assert LIVE_GUID in url or LIVE_SERVICES["receipts"] in url
        assert not url.rstrip("/").endswith(f"/api/v2/{LIVE_GUID}")
    put_calls = [c for c in req.call_args_list if c.args[0] == "PUT"]
    assert put_calls
    body = put_calls[0].kwargs.get("json")
    assert body["id"] == INVOICE_ID
    assert body["state"] == "Modified"
    child = body["lists"]["APInvoiceLine"][0]
    assert child["state"] == "Added"
    assert child["values"]["Receipt"] == {"id": 23879}
    assert child["values"]["Purchase_Order_Line"] == {"id": 17666}
    assert child["values"]["Part_ID"] == {"id": 20560}
    assert child["values"]["Quantity"] == 24.0


def test_select_receipts_payload_requires_receipt_id() -> None:
    payload = select_receipts_payload([23879], invoice_id=9931)
    assert payload["id"] == 9931
    assert payload["state"] == "Modified"
    assert payload["lists"]["APInvoiceLine"] == [
        {"state": "Added", "values": {"Receipt": {"id": 23879}}}
    ]
    with pytest.raises(KimcoError, match="Receipt.id"):
        select_receipts_payload([{"Part_ID": {"id": 1}, "Quantity": 16}])


def test_receipt_line_values_from_records_copies_po_part_qty() -> None:
    values = receipt_line_values_from_records(
        {"id": 9931, "values": {"Vendor": {"id": 434}, "Purchase_Order": {"id": 6638}}},
        {
            "id": 23879,
            "values": {
                "PO_Item_Number": {"id": 17666},
                "Part_Number": {"id": 20560},
                "Quantity_Received": 24.0,
                "PO_Item_Number_$_Unit_Price": 54.0,
            },
        },
    )
    assert values["Receipt"] == {"id": 23879}
    assert values["Invoice_Number"] == {"id": 9931}
    assert values["Vendor"] == {"id": 434}
    assert values["Purchase_Order_Number"] == {"id": 6638}
    assert values["Purchase_Order_Line"] == {"id": 17666}
    assert values["Part_ID"] == {"id": 20560}
    assert values["Quantity"] == 24.0
    assert values["Unit_Price"] == 54.0


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
