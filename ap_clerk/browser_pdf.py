"""Browser click-through PDF download for Intuit / QuickBooks payment-request links.

Unauthenticated GET is tried first (`pdf_links`). This module is the escalate
path: open the https invoice link in Playwright (system Chrome when present)
**as a guest** — no Intuit/QuickBooks login. A human opening an AQPC payment-
request link (`links.notification.intuit.com`) sees the invoice without signing
in; the runner does the same, then clicks View/Download invoice.

``AP_CLERK_INTUIT_STORAGE_STATE`` / cookie-jar env vars are optional for other
vendor portals later. They are **not** required for AQPC success and must not
be treated as a blocker.

Env (names only — values are secrets):

- ``AP_CLERK_INTUIT_STORAGE_STATE`` — optional Playwright ``storage_state`` JSON
  path (other portals). Unused for AQPC guest success.
- ``AP_CLERK_INTUIT_COOKIE_JAR`` — optional cookie JSON (other portals).
- ``AP_CLERK_BROWSER_PDF`` — set ``0`` / ``false`` / ``no`` to skip the
  browser escalate (unauth GET only). Default: enabled.
- ``AP_CLERK_BROWSER_PDF_TIMEOUT`` — seconds (default 45).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ap_clerk.pdf_links import REASON_PDF_BEHIND_LINK, classify_download

LOGGER = logging.getLogger("ap_clerk")

ENV_STORAGE_STATE = "AP_CLERK_INTUIT_STORAGE_STATE"
ENV_COOKIE_JAR = "AP_CLERK_INTUIT_COOKIE_JAR"
ENV_BROWSER_ENABLED = "AP_CLERK_BROWSER_PDF"
ENV_BROWSER_TIMEOUT = "AP_CLERK_BROWSER_PDF_TIMEOUT"
INTUIT_SESSION_ENV_NAMES = (ENV_STORAGE_STATE, ENV_COOKIE_JAR, ENV_BROWSER_ENABLED)

FAIL_LOGIN = "login-required"
FAIL_MFA = "mfa"
FAIL_TIMEOUT = "timeout"
FAIL_NO_SESSION = "no-session"
FAIL_NO_PLAYWRIGHT = "playwright-missing"
FAIL_NO_PDF = "no-pdf-after-browser"
FAIL_ERROR = "browser-error"
FAIL_DISABLED = "disabled"

BROWSER_FAIL_LABELS = {
    FAIL_LOGIN: "guest browser landed on a login page",
    FAIL_MFA: "MFA required",
    FAIL_TIMEOUT: "timeout",
    FAIL_NO_SESSION: "guest browser found no invoice PDF",
    FAIL_NO_PLAYWRIGHT: "Playwright not installed",
    FAIL_NO_PDF: "no PDF after guest browser click-through",
    FAIL_ERROR: "browser error",
    FAIL_DISABLED: "browser download disabled",
}

# Dedicated login form — not header chrome. Guest Intuit pages always say "Sign in".
LOGIN_FORM_RE = re.compile(
    r"(enter your password|forgot (?:your )?password|keep me signed in|"
    r"sign in to (?:your )?intuit|email or user id|user id or email|"
    r"accounts\.intuit\.com/(?:app/)?sign-in)",
    flags=re.I,
)
LOGIN_HOST_RE = re.compile(r"(^|\.)accounts\.intuit\.com$", flags=re.I)
MFA_RE = re.compile(
    r"(verif(?:y|ication) code|two[\s-]?factor|two[\s-]?step|authenticator|"
    r"one[\s-]?time code|enter the code|\bmfa\b|approve this sign)",
    flags=re.I,
)
GUEST_INVOICE_RE = re.compile(
    r"(view\s*(?:/|and\s+)?\s*download\s+invoice|view\s+invoice|download\s+invoice|"
    r"download\s+(?:the\s+)?pdf|view\s+pdf|print\s+invoice|amount\s+due|"
    r"invoice\s*(?:#|number)|pay\s+(?:this\s+)?invoice|review\s+and\s+pay|"
    r"see\s+invoice|open\s+invoice)",
    flags=re.I,
)
GUEST_HOST_HINTS = (
    "payments.intuit.com",
    "pay.intuit.com",
    "links.notification.intuit.com",
    "app.qbo.intuit.com",
    "qbo.intuit.com",
)
DOWNLOAD_SELECTORS = (
    "a[href*='.pdf' i]",
    "a[download]",
    "button:has-text('Download PDF')",
    "a:has-text('Download PDF')",
    "button:has-text('Download invoice')",
    "a:has-text('Download invoice')",
    "button:has-text('View invoice')",
    "a:has-text('View invoice')",
    "button:has-text('View details')",
    "a:has-text('View details')",
    "button:has-text('Review and pay')",
    "a:has-text('Review and pay')",
    "button:has-text('View/Download invoice')",
    "a:has-text('View/Download invoice')",
    "button:has-text('View PDF')",
    "a:has-text('View PDF')",
    "button:has-text('Print invoice')",
    "a:has-text('Print invoice')",
    "button:has-text('Download')",
    "a:has-text('Download')",
    "[data-testid*='download' i]",
    "[data-testid*='view-invoice' i]",
    "[aria-label*='View invoice' i]",
    "[aria-label*='Download invoice' i]",
)
GUEST_CLICK_TEXTS = (
    "View invoice",
    "View Invoice",
    "View details",
    "View Details",
    "Download invoice",
    "Download Invoice",
    "View/Download invoice",
    "Review and pay",
    "Download PDF",
    "View PDF",
    "Print invoice",
    "See invoice",
    "Open invoice",
    "Pay now",
)

_FALSEY = frozenset({"0", "false", "no", "off"})


def storage_state_path() -> Path | None:
    raw = (os.environ.get(ENV_STORAGE_STATE) or "").strip()
    return Path(raw).expanduser() if raw else None


def cookie_jar_path() -> Path | None:
    raw = (os.environ.get(ENV_COOKIE_JAR) or "").strip()
    return Path(raw).expanduser() if raw else None


def browser_timeout_seconds(default: float = 45.0) -> float:
    raw = (os.environ.get(ENV_BROWSER_TIMEOUT) or "").strip()
    if not raw:
        return default
    try:
        return max(5.0, float(raw))
    except ValueError:
        return default


def browser_pdf_enabled() -> bool:
    raw = (os.environ.get(ENV_BROWSER_ENABLED) or "").strip().lower()
    if raw in _FALSEY:
        return False
    return True


def session_file_present() -> bool:
    state = storage_state_path()
    jar = cookie_jar_path()
    return bool((state and state.is_file()) or (jar and jar.is_file()))


def format_intuit_session_presence() -> str:
    """Present/absent only. Never prints cookie or path values."""
    lines = [
        "Optional vendor-portal session presence (names only, values never printed).",
        "AQPC Intuit payment-request links are guest — no login / no session file required:",
    ]
    for name in INTUIT_SESSION_ENV_NAMES:
        raw = (os.environ.get(name) or "").strip()
        if name == ENV_BROWSER_ENABLED:
            lines.append(f"  {name}: {'present' if raw else 'absent'}")
            continue
        if not raw:
            lines.append(f"  {name}: absent")
            continue
        exists = Path(raw).expanduser().is_file()
        lines.append(f"  {name}: present ({'file-ok' if exists else 'file-missing'})")
    return "\n".join(lines)


def looks_like_guest_invoice(*, url: str = "", title: str = "", text: str = "") -> bool:
    """True when the rendered page is a guest invoice / payment-request, not a login form."""
    blob = f"{url}\n{title}\n{text or ''}"[:8000]
    if GUEST_INVOICE_RE.search(blob):
        return True
    host = (urlparse(url).netloc or "").lower()
    return any(hint in host for hint in GUEST_HOST_HINTS) and not LOGIN_FORM_RE.search(blob)


def classify_browser_page(*, url: str = "", title: str = "", text: str = "") -> str | None:
    """Return login-required / mfa only for a dedicated auth wall, not guest chrome."""
    blob = f"{url}\n{title}\n{text or ''}"[:8000]
    if b"%PDF" in (text or "").encode("utf-8", "ignore")[:8]:
        return None
    if looks_like_guest_invoice(url=url, title=title, text=text):
        return None
    if MFA_RE.search(blob):
        return FAIL_MFA
    host = (urlparse(url).netloc or "").lower()
    if LOGIN_HOST_RE.search(host) or LOGIN_FORM_RE.search(blob):
        return FAIL_LOGIN
    return None


def _fail(failure: str, detail: str = "", *, url: str = "", status_code: int = 0) -> dict[str, Any]:
    return {
        "ok": False,
        "content": None,
        "reason": REASON_PDF_BEHIND_LINK,
        "browser_tried": True,
        "browser_failure": failure,
        "browser_detail": detail or BROWSER_FAIL_LABELS.get(failure, failure),
        "method": "browser",
        "status_code": status_code,
        "url": url,
    }


def _ok(content: bytes, *, url: str = "", status_code: int = 200) -> dict[str, Any]:
    return {
        "ok": True,
        "content": content,
        "reason": "ok",
        "browser_tried": True,
        "browser_failure": "",
        "browser_detail": "",
        "method": "browser",
        "status_code": status_code,
        "url": url,
    }


def _load_storage_state() -> dict[str, Any] | str | None:
    """Load optional Playwright storage_state. Never required for AQPC. Never logs values."""
    state = storage_state_path()
    jar = cookie_jar_path()
    for path in (state, jar):
        if path is None or not path.is_file():
            continue
        loaded = _read_session_file(path)
        if loaded is not None:
            return loaded
    return None


def _read_session_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.info("Optional vendor-portal session file is not readable")
        return None
    text = raw.strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.info("Optional vendor-portal session file is not valid JSON")
            return None
        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            return payload
        if isinstance(payload, list):
            return {"cookies": payload, "origins": []}
        LOGGER.info("Optional vendor-portal session JSON has no cookies list")
        return None
    cookies = _parse_netscape_cookies(text)
    if cookies:
        return {"cookies": cookies, "origins": []}
    LOGGER.info("Optional vendor-portal session file is not a recognized cookie format")
    return None


def _parse_netscape_cookies(text: str) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": str(secure).upper() == "TRUE",
            "httpOnly": False,
        }
        try:
            cookie["expires"] = float(expires)
        except ValueError:
            cookie["expires"] = -1
        cookies.append(cookie)
    return cookies


def _persist_storage_state(context: Any, path: Path | None) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))
    except OSError:
        LOGGER.info("Could not persist optional vendor-portal storage state (path not writable)")
    except Exception:  # noqa: BLE001 - persist must not raise into inbox
        LOGGER.info("Could not persist optional vendor-portal storage state")


def try_browser_download(
    url: str,
    *,
    downloader: Callable[..., Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Open ``url`` in a guest browser and return PDF bytes or a HOLD reason.

    ``downloader(url, timeout=...)`` injects tests. Production uses Playwright.
    Storage-state env is optional and must not be required for success.
    """
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        return _fail(FAIL_ERROR, "refusing non-https browser download", url=url or "")
    wait = browser_timeout_seconds() if timeout is None else float(timeout)
    if downloader is not None:
        try:
            result = downloader(url, timeout=wait)
        except Exception:  # noqa: BLE001 - test/injected downloader must not crash inbox
            return _fail(FAIL_ERROR, "browser downloader raised", url=url)
        if not isinstance(result, dict):
            return _fail(FAIL_ERROR, "browser downloader returned a non-dict", url=url)
        out = dict(result)
        out.setdefault("url", url)
        out.setdefault("method", "browser")
        out.setdefault("browser_tried", True)
        if out.get("ok") and out.get("content"):
            out["reason"] = "ok"
            return out
        out["ok"] = False
        out["content"] = None
        out["reason"] = REASON_PDF_BEHIND_LINK
        out.setdefault("browser_failure", FAIL_NO_PDF)
        return out
    if not browser_pdf_enabled():
        return _fail(FAIL_DISABLED, url=url)
    return _playwright_download(url, timeout=wait)


def _playwright_download(url: str, *, timeout: float) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _fail(FAIL_NO_PLAYWRIGHT, "Playwright is not installed", url=url)

    storage = _load_storage_state()
    state_path = storage_state_path()
    captured: dict[str, bytes | None] = {"pdf": None}
    ms = max(1000, int(timeout * 1000))

    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context_kwargs: dict[str, Any] = {
                "accept_downloads": True,
                "locale": "en-US",
                "viewport": {"width": 1280, "height": 800},
                "user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            if storage is not None:
                context_kwargs["storage_state"] = storage
            context = browser.new_context(**context_kwargs)

            def on_response(response: Any) -> None:
                _capture_pdf_response(response, captured)

            context.on("response", on_response)
            context.on("page", lambda p: p.on("response", on_response))
            page = context.new_page()
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=ms)
            except PlaywrightTimeout:
                _persist_storage_state(context, state_path)
                browser.close()
                return _fail(FAIL_TIMEOUT, "guest browser navigation timed out", url=url)
            try:
                page.wait_for_load_state("networkidle", timeout=min(15_000, ms))
            except Exception:  # noqa: BLE001 - idle wait is best-effort
                pass

            pdf = captured.get("pdf")
            if pdf and pdf[:5] == b"%PDF-":
                _persist_storage_state(context, state_path)
                browser.close()
                return _ok(pdf, url=page.url or url)

            clicked = _guest_click_through(page, context, captured, timeout_ms=min(12_000, ms))
            pdf = captured.get("pdf") or clicked
            if pdf and pdf[:5] == b"%PDF-":
                _persist_storage_state(context, state_path)
                browser.close()
                return _ok(pdf, url=page.url or url)

            title, body_text, current_url = _page_snapshot(page, url)
            wall = classify_browser_page(url=current_url, title=title, text=body_text)
            _persist_storage_state(context, state_path)
            browser.close()
            if wall == FAIL_MFA:
                return _fail(FAIL_MFA, "MFA challenge after guest browser navigation", url=current_url)
            if wall == FAIL_LOGIN:
                return _fail(
                    FAIL_LOGIN,
                    "guest browser landed on a login page; AQPC payment-request "
                    "links do not need an Intuit/QuickBooks sign-in",
                    url=current_url,
                )
            hint = classify_download(
                status_code=200,
                content=None,
                content_type="text/html",
                text=body_text,
            )
            if hint == REASON_PDF_BEHIND_LINK:
                return _fail(
                    FAIL_NO_PDF,
                    "guest browser opened intermediate HTML but no invoice PDF was captured",
                    url=current_url,
                )
            return _fail(
                FAIL_NO_PDF,
                "guest browser opened the link and clicked View/Download invoice but no PDF was captured",
                url=current_url,
            )
    except PlaywrightTimeout:
        return _fail(FAIL_TIMEOUT, "guest browser navigation timed out", url=url)
    except Exception as exc:  # noqa: BLE001 - never raise into inbox
        LOGGER.info("Guest browser PDF download failed (%s)", type(exc).__name__)
        return _fail(FAIL_ERROR, f"browser error ({type(exc).__name__})", url=url)


def _page_snapshot(page: Any, fallback_url: str) -> tuple[str, str, str]:
    title = ""
    body_text = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        title = ""
    try:
        body_text = page.inner_text("body", timeout=2000)[:8000]
    except Exception:  # noqa: BLE001
        try:
            body_text = (page.content() or "")[:4000]
        except Exception:  # noqa: BLE001
            body_text = ""
    try:
        current_url = page.url or fallback_url
    except Exception:  # noqa: BLE001
        current_url = fallback_url
    return title, body_text, current_url


def _launch_browser(playwright: Any) -> Any:
    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:  # noqa: BLE001 - fall back to bundled Chromium
        return playwright.chromium.launch(**launch_kwargs)


def _capture_pdf_response(response: Any, captured: dict[str, bytes | None]) -> None:
    if captured.get("pdf"):
        return
    try:
        headers = {str(k).lower(): str(v) for k, v in (getattr(response, "headers", None) or {}).items()}
        ctype = headers.get("content-type", "")
        url = str(getattr(response, "url", "") or "")
        looks_pdf = "pdf" in ctype.lower() or url.lower().split("?", 1)[0].endswith(".pdf")
        if not looks_pdf:
            return
        body = response.body()
        if body and (body[:5] == b"%PDF-" or "pdf" in ctype.lower()):
            captured["pdf"] = body
    except Exception:  # noqa: BLE001 - response body may be unavailable
        return


def _guest_click_through(
    page: Any,
    context: Any,
    captured: dict[str, bytes | None],
    *,
    timeout_ms: int,
) -> bytes | None:
    """Guest click of View/Download invoice. No login. Never raises."""
    if captured.get("pdf") and str(captured.get("pdf") or b"")[:5] == b"%PDF-":
        return captured["pdf"]
    locators: list[Any] = []
    for selector in DOWNLOAD_SELECTORS:
        try:
            locators.append(page.locator(selector).first)
        except Exception:  # noqa: BLE001
            continue
    for label in GUEST_CLICK_TEXTS:
        try:
            locators.append(page.get_by_role("button", name=re.compile(re.escape(label), re.I)))
        except Exception:  # noqa: BLE001
            pass
        try:
            locators.append(page.get_by_role("link", name=re.compile(re.escape(label), re.I)))
        except Exception:  # noqa: BLE001
            pass
        try:
            locators.append(page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I)))
        except Exception:  # noqa: BLE001
            pass
    for loc in locators:
        got = _click_locator_for_pdf(page, context, loc, captured, timeout_ms=timeout_ms)
        if got and got[:5] == b"%PDF-":
            return got
    return captured.get("pdf") if (captured.get("pdf") or b"")[:5] == b"%PDF-" else None


def _click_locator_for_pdf(
    page: Any,
    context: Any,
    loc: Any,
    captured: dict[str, bytes | None],
    *,
    timeout_ms: int,
) -> bytes | None:
    try:
        if loc.count() == 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    pages_before = list(getattr(context, "pages", None) or [page])
    try:
        with page.expect_download(timeout=timeout_ms) as download_info:
            loc.click(timeout=timeout_ms)
        download = download_info.value
        path = download.path()
        if path:
            data = Path(path).read_bytes()
            if data[:5] == b"%PDF-":
                captured["pdf"] = data
                return data
    except Exception:  # noqa: BLE001 - click may navigate / open a tab instead of download
        if captured.get("pdf") and str(captured.get("pdf") or b"")[:5] == b"%PDF-":
            return captured["pdf"]
        try:
            loc.click(timeout=min(3000, timeout_ms))
        except Exception:  # noqa: BLE001
            pass
    try:
        page.wait_for_timeout(min(1500, timeout_ms))
    except Exception:  # noqa: BLE001
        pass
    if captured.get("pdf") and str(captured.get("pdf") or b"")[:5] == b"%PDF-":
        return captured["pdf"]
    for extra in list(getattr(context, "pages", None) or []):
        if extra in pages_before:
            continue
        try:
            extra.wait_for_load_state("domcontentloaded", timeout=min(8000, timeout_ms))
        except Exception:  # noqa: BLE001
            pass
        if captured.get("pdf") and str(captured.get("pdf") or b"")[:5] == b"%PDF-":
            return captured["pdf"]
    return None


def save_intuit_session(path: Path, *, start_url: str = "https://accounts.intuit.com") -> int:
    """Optional headed helper for other vendor portals. Not required for AQPC."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. pip install playwright && python -m playwright install chrome")
        return 2
    dest = path.expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(
        "AQPC payment-request links do not need this. Optional headed Chrome for "
        "other vendor portals: sign in if that portal requires it, then press Enter. "
        "No password is written — only cookies / storage_state."
    )
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except Exception:  # noqa: BLE001
            browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        try:
            input("Press Enter after the optional portal login succeeds… ")
        except EOFError:
            print("No TTY; closing without waiting for extra input.")
        context.storage_state(path=str(dest))
        browser.close()
    print(
        f"Saved optional storage_state ({dest.stat().st_size} bytes). "
        f"{ENV_STORAGE_STATE} is not required for AQPC guest links."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AP Clerk guest browser PDF helper (AQPC needs no Intuit login)."
    )
    parser.add_argument(
        "--save-session",
        metavar="PATH",
        help="Optional: write Playwright storage_state JSON to PATH (other portals, not AQPC).",
    )
    parser.add_argument(
        "--start-url",
        default="https://accounts.intuit.com",
        help="URL to open for --save-session (default Intuit accounts).",
    )
    args = parser.parse_args(argv)
    print(format_intuit_session_presence(), flush=True)
    if args.save_session:
        return save_intuit_session(Path(args.save_session), start_url=args.start_url)
    print(
        "AQPC Intuit payment-request links are guest: open the https link and click "
        "View/Download invoice. No Intuit login and no storage-state file.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
