"""Best-effort PDF download from email body links.

Used when a vendor (AQPC) sends a download URL instead of a PDF attachment.
Cheap unauthenticated GET first. Auth/bot walls or intermediate HTML
escalate to a guest browser (`browser_pdf`) when enabled — no vendor
login for AQPC. True failure after that is HOLD pdf-behind-link, not
silent not-a-bill. No live mailbox I/O lives here — callers pass
text/HTTP and optional injected fetchers.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urlparse

import requests

REASON_OK = "ok"
REASON_PDF_BEHIND_LINK = "pdf-behind-link"
REASON_NOT_PDF = "not-pdf"
REASON_ERROR = "error"

HTTPS_RE = re.compile(r"https://[^\s<>\"']+", flags=re.I)
AUTH_HINT_RE = re.compile(
    r"(log[\s-]?in|sign[\s-]?in|password|sso|auth0|okta|accounts\.google|please\s+sign)",
    flags=re.I,
)
PDF_PATH_RE = re.compile(r"\.pdf(\b|$)", flags=re.I)
TRACKING_PATH_RE = re.compile(
    r"(sale/viewed|invoice/viewed|tracking/notification|/ss/o/|"
    r"\.(?:gif|png|jpe?g|svg|webp)(\b|$))",
    flags=re.I,
)
INTUIT_CLICK_RE = re.compile(
    r"links\.notification\.intuit\.com/(?:ls/click|ss/c/)",
    flags=re.I,
)


def is_tracking_or_asset(url: str) -> bool:
    """Open-pixel / logo / viewed-beacon — never a guest invoice PDF."""
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if TRACKING_PATH_RE.search(path):
        return True
    if "ips-logos" in host or "plugin-qbo.intuit.com" in host:
        return True
    return False


def is_intuit_notification_click(url: str) -> bool:
    """Human-facing AQPC payment-request click (not a tracking gif)."""
    return bool(INTUIT_CLICK_RE.search(url or "")) and not is_tracking_or_asset(url or "")


def extract_https_links(text: str | None) -> list[str]:
    """Unique https URLs from HTML or plain body text. http is ignored."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in HTTPS_RE.findall(text or ""):
        url = raw.rstrip(").,;]>\"'")
        if not url or url.lower() in seen:
            continue
        seen.add(url.lower())
        found.append(url)
    return found


def prefer_pdf_links(links: list[str]) -> list[str]:
    """PDF paths, then Intuit guest click links, then other invoice hosts.

    Tracking pixels (`sale/viewed`, logos, `.gif`) stay last so guest
    click-through opens `links.notification.intuit.com`, not a beacon.
    """
    pdfs = [
        u
        for u in links
        if PDF_PATH_RE.search(urlparse(u).path or "") and not is_tracking_or_asset(u)
    ]
    clicks = [u for u in links if u not in pdfs and is_intuit_notification_click(u)]
    pay = [
        u
        for u in links
        if u not in pdfs
        and u not in clicks
        and not is_tracking_or_asset(u)
        and re.search(r"invoice|payment|pay\.|download|aqpowder|intuit", u, flags=re.I)
    ]
    rest = [u for u in links if u not in pdfs and u not in clicks and u not in pay]
    return pdfs + clicks + pay + rest


def classify_download(*, status_code: int, content: bytes | None, content_type: str = "", text: str = "") -> str:
    """Return ok / pdf-behind-link / not-pdf / error. Never logs body bytes."""
    blob = (text or "")[:4000]
    ctype = (content_type or "").lower()
    if status_code in {401, 403}:
        return REASON_PDF_BEHIND_LINK
    if status_code >= 400:
        return REASON_ERROR
    if AUTH_HINT_RE.search(blob) and b"%PDF" not in (content or b"")[:8]:
        return REASON_PDF_BEHIND_LINK
    if content and content[:5] == b"%PDF-":
        return REASON_OK
    if "pdf" in ctype and content:
        return REASON_OK
    if "html" in ctype or blob.lstrip().lower().startswith("<!doctype") or blob.lstrip().lower().startswith("<html"):
        if AUTH_HINT_RE.search(blob):
            return REASON_PDF_BEHIND_LINK
        return REASON_NOT_PDF
    if content and content[:5] == b"%PDF-":
        return REASON_OK
    return REASON_NOT_PDF


def try_download_public_pdf(
    url: str,
    *,
    getter: Callable[..., Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Unauthenticated GET. No cookies, no credentials.

    getter(url, timeout=...) may return a requests-like response for tests.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        return {"ok": False, "content": None, "reason": REASON_ERROR, "status_code": 0}
    fetch = getter or requests.get
    try:
        response = fetch(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "ap-clerk-pdf-link/1.2"},
        )
    except (requests.RequestException, OSError, ValueError):
        return {"ok": False, "content": None, "reason": REASON_ERROR, "status_code": 0}
    status = int(getattr(response, "status_code", 0) or 0)
    content = getattr(response, "content", None) or b""
    headers = getattr(response, "headers", {}) or {}
    ctype = str(headers.get("Content-Type") or headers.get("content-type") or "")
    try:
        text = response.text if "html" in ctype.lower() or not content.startswith(b"%PDF") else ""
    except Exception:  # noqa: BLE001 - body decode must not raise into inbox
        text = ""
    reason = classify_download(status_code=status, content=content, content_type=ctype, text=text)
    if reason == REASON_OK:
        return {"ok": True, "content": content, "reason": REASON_OK, "status_code": status}
    return {"ok": False, "content": None, "reason": reason, "status_code": status}


def download_first_public_pdf(
    text: str | None,
    *,
    getter: Callable[..., Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Try https links in body order (PDF paths first). Stop at first PDF or auth wall."""
    links = prefer_pdf_links(extract_https_links(text))
    if not links:
        return {"ok": False, "content": None, "reason": REASON_ERROR, "status_code": 0, "url": ""}
    saw_auth = False
    last = {"ok": False, "content": None, "reason": REASON_ERROR, "status_code": 0, "url": links[0]}
    for url in links[:6]:
        result = try_download_public_pdf(url, getter=getter, timeout=timeout)
        result["url"] = url
        if result.get("ok"):
            return result
        if result.get("reason") == REASON_PDF_BEHIND_LINK:
            saw_auth = True
            last = result
            continue
        last = result
    if saw_auth:
        last["reason"] = REASON_PDF_BEHIND_LINK
    return last


def download_first_pdf(
    text: str | None,
    *,
    getter: Callable[..., Any] | None = None,
    browser: Callable[..., Any] | None = None,
    timeout: float = 15.0,
    browser_timeout: float | None = None,
) -> dict[str, Any]:
    """Unauth GET first; escalate to browser when that does not yield a PDF.

    ``browser`` is a ``try_browser_download``-compatible callable for tests.
    Production uses Playwright as a guest (no Intuit login for AQPC).
    """
    result = download_first_public_pdf(text, getter=getter, timeout=timeout)
    if result.get("ok") and result.get("content"):
        result["method"] = "unauth"
        result["browser_tried"] = False
        result["browser_failure"] = ""
        return result
    links = prefer_pdf_links(extract_https_links(text))
    if not links:
        result["method"] = "unauth"
        result["browser_tried"] = False
        result["browser_failure"] = ""
        return result

    from ap_clerk.browser_pdf import browser_pdf_enabled, try_browser_download

    if browser is None and not browser_pdf_enabled():
        result["method"] = "unauth"
        result["browser_tried"] = False
        result["browser_failure"] = "disabled"
        return result

    last = dict(result)
    for url in links[:6]:
        br = try_browser_download(url, downloader=browser, timeout=browser_timeout)
        br["url"] = url
        if br.get("ok") and br.get("content"):
            br["method"] = "browser"
            br["browser_tried"] = True
            return br
        last = br
    last["ok"] = False
    last["content"] = None
    last["reason"] = REASON_PDF_BEHIND_LINK
    last["method"] = "browser"
    last["browser_tried"] = True
    last.setdefault("url", links[0])
    return last
