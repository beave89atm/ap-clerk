"""Best-effort public PDF download from email body links.

Used when a vendor (AQPC) sends a download URL instead of a PDF attachment.
Unauthenticated/simple GETs only. Auth walls are HOLD pdf-behind-link, not
silent not-a-bill. No live mailbox I/O lives here — callers pass text/HTTP.
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
    """PDF-looking paths first, then remaining https links."""
    pdfs = [u for u in links if PDF_PATH_RE.search(urlparse(u).path or "")]
    rest = [u for u in links if u not in pdfs]
    return pdfs + rest


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
