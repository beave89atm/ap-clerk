"""Unit tests for Intuit/AQPC browser click-through. No live Playwright / no secrets."""

from __future__ import annotations

import json
from pathlib import Path

from ap_clerk.browser_pdf import (
    BROWSER_FAIL_LABELS,
    ENV_COOKIE_JAR,
    ENV_STORAGE_STATE,
    FAIL_LOGIN,
    FAIL_MFA,
    FAIL_NO_SESSION,
    classify_browser_page,
    format_intuit_session_presence,
    session_file_present,
    storage_state_path,
    try_browser_download,
)
from ap_clerk.pdf_links import (
    REASON_PDF_BEHIND_LINK,
    download_first_pdf,
    extract_https_links,
    prefer_pdf_links,
)
from ap_clerk.rules import has_invoice_link


INTUIT_10917 = "https://links.notification.intuit.com/ls/click?upn=invoice-10917"
INTUIT_10918 = "https://links.notification.intuit.com/ls/click?upn=invoice-10918"
PDF_10917 = b"%PDF-1.4 AMERICAN QUALITY POWDER COATING Invoice Number 10917 Amount Due 125.00"


class _AuthHtml:
    status_code = 401
    content = b"<html>please sign in</html>"
    headers = {"Content-Type": "text/html"}
    text = "please sign in"


def _unauth_wall(url, **kwargs):
    return _AuthHtml()


def test_intuit_click_link_is_preferred_invoice_link():
    body = f"View invoice: {INTUIT_10917}"
    links = prefer_pdf_links(extract_https_links(body))
    assert links and links[0] == INTUIT_10917
    assert has_invoice_link(preview=body)


def test_classify_browser_page_login_and_mfa():
    assert classify_browser_page(url="https://accounts.intuit.com/app/sign-in", title="Sign in", text="") == FAIL_LOGIN
    assert (
        classify_browser_page(
            url="https://accounts.intuit.com/verify",
            title="Verify",
            text="Enter the verification code we texted",
        )
        == FAIL_MFA
    )
    assert classify_browser_page(url="https://payments.intuit.com/invoice", title="Invoice", text="Amount Due") is None


def test_storage_state_path_from_env_never_hardcoded(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(ENV_STORAGE_STATE, raising=False)
    assert storage_state_path() is None
    dest = tmp_path / "intuit-storage-state.json"
    dest.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    monkeypatch.setenv(ENV_STORAGE_STATE, str(dest))
    assert storage_state_path() == dest
    assert session_file_present() is True
    presence = format_intuit_session_presence()
    assert ENV_STORAGE_STATE in presence
    assert "present (file-ok)" in presence
    assert dest.read_text() not in presence
    assert "cookie" not in presence.lower() or "names only" in presence


def test_download_first_pdf_unauth_ok_skips_browser():
    called = {"browser": 0}

    class Ok:
        status_code = 200
        content = PDF_10917
        headers = {"Content-Type": "application/pdf"}
        text = ""

    def getter(url, **kwargs):
        return Ok()

    def browser(url, **kwargs):
        called["browser"] += 1
        raise AssertionError("browser must not run when unauth GET returns a PDF")

    result = download_first_pdf(f"Invoice: {INTUIT_10917}", getter=getter, browser=browser)
    assert result["ok"] is True
    assert result["method"] == "unauth"
    assert result["browser_tried"] is False
    assert called["browser"] == 0


def test_download_first_pdf_browser_success_after_auth_wall():
    def browser(url, **kwargs):
        assert url == INTUIT_10917
        return {"ok": True, "content": PDF_10917}

    result = download_first_pdf(
        f"Invoice: {INTUIT_10917}",
        getter=_unauth_wall,
        browser=browser,
    )
    assert result["ok"] is True
    assert result["method"] == "browser"
    assert result["browser_tried"] is True
    assert result["content"][:5] == b"%PDF-"


def test_download_first_pdf_browser_login_hold():
    def browser(url, **kwargs):
        return {
            "ok": False,
            "content": None,
            "reason": REASON_PDF_BEHIND_LINK,
            "browser_failure": FAIL_LOGIN,
        }

    result = download_first_pdf(
        f"Invoice: {INTUIT_10918}",
        getter=_unauth_wall,
        browser=browser,
    )
    assert result["ok"] is False
    assert result["reason"] == REASON_PDF_BEHIND_LINK
    assert result["browser_tried"] is True
    assert result["browser_failure"] == FAIL_LOGIN
    assert result["method"] == "browser"


def test_try_browser_download_injects_downloader():
    def downloader(url, **kwargs):
        return {"ok": True, "content": PDF_10917}

    result = try_browser_download(INTUIT_10917, downloader=downloader)
    assert result["ok"] is True
    assert result["browser_tried"] is True


def test_try_browser_download_refuses_http():
    result = try_browser_download("http://links.notification.intuit.com/ls/click")
    assert result["ok"] is False
    assert result["browser_tried"] is True
    assert result["browser_failure"] == "browser-error"


def test_browser_fail_labels_cover_hold_reasons():
    for key in (FAIL_LOGIN, FAIL_MFA, FAIL_NO_SESSION, "timeout"):
        assert key in BROWSER_FAIL_LABELS


def test_cookie_jar_env_presence(monkeypatch, tmp_path: Path):
    jar = tmp_path / "intuit-cookies.json"
    jar.write_text(json.dumps([{"name": "test", "value": "x", "domain": ".intuit.com"}]), encoding="utf-8")
    monkeypatch.setenv(ENV_COOKIE_JAR, str(jar))
    monkeypatch.delenv(ENV_STORAGE_STATE, raising=False)
    assert session_file_present() is True
    text = format_intuit_session_presence()
    assert ENV_COOKIE_JAR in text
    assert "file-ok" in text
    assert "test" not in text or "names only" in text
    assert "x" not in text
