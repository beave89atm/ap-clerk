"""Browser click-through PDF download for Intuit / QuickBooks payment-request links.

Unauthenticated GET is tried first (`pdf_links`). This module is the escalate
path: open the https invoice link in Playwright (system Chrome when present),
reuse a persisted Intuit session, follow redirects, and return PDF bytes.

Session files are paths from the environment only. Never invent credentials,
never log cookie values or storage_state contents, never commit those files.

Env (names only — values are secrets):

- ``AP_CLERK_INTUIT_STORAGE_STATE`` — Playwright ``storage_state`` JSON path
  (cookies + localStorage). Preferred. Kyle exports this once after login/MFA.
- ``AP_CLERK_INTUIT_COOKIE_JAR`` — optional cookie JSON (storage_state or a
  cookie list). Used when a full storage_state is not available.
- ``AP_CLERK_BROWSER_PDF`` — set ``0`` / ``false`` / ``no`` to skip the
  browser escalate (unauth GET only). Default: enabled.
- ``AP_CLERK_BROWSER_PDF_TIMEOUT`` — seconds (default 45).

One-time Kyle setup (headed machine, MFA allowed)::

    python -m ap_clerk.browser_pdf --save-session /secure/path/intuit-storage-state.json

Log in to Intuit (complete MFA), then press Enter. Point
``AP_CLERK_INTUIT_STORAGE_STATE`` at that file. The file is a cookie/session
export, not a password.
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
    FAIL_LOGIN: "login required",
    FAIL_MFA: "MFA required",
    FAIL_TIMEOUT: "timeout",
    FAIL_NO_SESSION: "no Intuit session (set AP_CLERK_INTUIT_STORAGE_STATE)",
    FAIL_NO_PLAYWRIGHT: "Playwright not installed",
    FAIL_NO_PDF: "no PDF after browser navigation",
    FAIL_ERROR: "browser error",
    FAIL_DISABLED: "browser download disabled",
}

LOGIN_RE = re.compile(
    r"(sign[\s-]?in|log[\s-]?in|enter your password|intuit account|"
    r"accounts\.intuit|please\s+sign|create an account)",
    flags=re.I,
)
MFA_RE = re.compile(
    r"(verif(?:y|ication) code|two[\s-]?factor|two[\s-]?step|authenticator|"
    r"one[\s-]?time code|enter the code|\bmfa\b|text message|approve this sign)",
    flags=re.I,
)
DOWNLOAD_SELECTORS = (
    "a[href*='.pdf' i]",
    "a[download]",
    "button:has-text('Download PDF')",
    "a:has-text('Download PDF')",
    "button:has-text('Download invoice')",
    "a:has-text('Download invoice')",
    "button:has-text('Download')",
    "a:has-text('Download')",
    "button:has-text('View invoice')",
    "a:has-text('View invoice')",
    "[data-testid*='download' i]",
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
    lines = ["Intuit/QuickBooks session presence (names only, values never printed):"]
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


def classify_browser_page(*, url: str = "", title: str = "", text: str = "") -> str | None:
    """Return login-required / mfa when the rendered page is an auth wall."""
    blob = f"{url}\n{title}\n{text or ''}"[:8000]
    host = (urlparse(url).netloc or "").lower()
    if MFA_RE.search(blob):
        return FAIL_MFA
    if LOGIN_RE.search(blob) or "accounts.intuit.com" in host:
        if b"%PDF" in (text or "").encode("utf-8", "ignore")[:8]:
            return None
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
    """Load Playwright storage_state or a cookie-list JSON. Never logs values."""
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
        LOGGER.info("Intuit session file is not readable")
        return None
    text = raw.strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.info("Intuit session file is not valid JSON")
            return None
        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            return payload
        if isinstance(payload, list):
            return {"cookies": payload, "origins": []}
        LOGGER.info("Intuit session JSON has no cookies list")
        return None
    cookies = _parse_netscape_cookies(text)
    if cookies:
        return {"cookies": cookies, "origins": []}
    LOGGER.info("Intuit session file is not a recognized cookie format")
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
        LOGGER.info("Could not persist Intuit storage state (path not writable)")
    except Exception:  # noqa: BLE001 - persist must not raise into inbox
        LOGGER.info("Could not persist Intuit storage state")


def try_browser_download(
    url: str,
    *,
    downloader: Callable[..., Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Open ``url`` in a browser session and return PDF bytes or a HOLD reason.

    ``downloader(url, timeout=...)`` injects tests. Production uses Playwright.
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
    has_session = storage is not None
    state_path = storage_state_path()
    captured: dict[str, bytes | None] = {"pdf": None}
    ms = max(1000, int(timeout * 1000))

    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context_kwargs: dict[str, Any] = {"accept_downloads": True}
            if storage is not None:
                context_kwargs["storage_state"] = storage
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            def on_response(response: Any) -> None:
                _capture_pdf_response(response, captured)

            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=ms)
            except PlaywrightTimeout:
                _persist_storage_state(context, state_path)
                browser.close()
                return _fail(FAIL_TIMEOUT, "browser navigation timed out", url=url)
            try:
                page.wait_for_load_state("networkidle", timeout=min(15_000, ms))
            except Exception:  # noqa: BLE001 - idle wait is best-effort
                pass

            pdf = captured.get("pdf")
            if pdf and pdf[:5] == b"%PDF-":
                _persist_storage_state(context, state_path)
                browser.close()
                return _ok(pdf, url=page.url or url)

            clicked = _click_download_buttons(page, captured, timeout_ms=min(8_000, ms))
            pdf = captured.get("pdf") or clicked
            if pdf and pdf[:5] == b"%PDF-":
                _persist_storage_state(context, state_path)
                browser.close()
                return _ok(pdf, url=page.url or url)

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
            current_url = page.url or url
            wall = classify_browser_page(url=current_url, title=title, text=body_text)
            _persist_storage_state(context, state_path)
            browser.close()
            if wall == FAIL_MFA:
                return _fail(FAIL_MFA, "Intuit MFA challenge after browser navigation", url=current_url)
            if wall == FAIL_LOGIN:
                failure = FAIL_NO_SESSION if not has_session else FAIL_LOGIN
                detail = (
                    "login required; no Intuit session file (set AP_CLERK_INTUIT_STORAGE_STATE)"
                    if failure == FAIL_NO_SESSION
                    else "login required (session expired or rejected)"
                )
                return _fail(failure, detail, url=current_url)
            hint = classify_download(
                status_code=200,
                content=None,
                content_type="text/html",
                text=body_text,
            )
            if hint == REASON_PDF_BEHIND_LINK:
                failure = FAIL_NO_SESSION if not has_session else FAIL_LOGIN
                return _fail(failure, "auth wall HTML after browser navigation", url=current_url)
            return _fail(FAIL_NO_PDF, "browser opened the link but no invoice PDF was captured", url=current_url)
    except PlaywrightTimeout:
        return _fail(FAIL_TIMEOUT, "browser navigation timed out", url=url)
    except Exception as exc:  # noqa: BLE001 - never raise into inbox
        LOGGER.info("Browser PDF download failed (%s)", type(exc).__name__)
        return _fail(FAIL_ERROR, f"browser error ({type(exc).__name__})", url=url)


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except Exception:  # noqa: BLE001 - fall back to bundled Chromium
        return playwright.chromium.launch(headless=True)


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


def _click_download_buttons(page: Any, captured: dict[str, bytes | None], *, timeout_ms: int) -> bytes | None:
    """Best-effort click of Download / View invoice. Never raises."""
    if captured.get("pdf"):
        return captured["pdf"]
    for selector in DOWNLOAD_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
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
            except Exception:  # noqa: BLE001 - click may navigate instead of download
                if captured.get("pdf"):
                    return captured["pdf"]
                continue
        except Exception:  # noqa: BLE001
            continue
    return captured.get("pdf")


def save_intuit_session(path: Path, *, start_url: str = "https://accounts.intuit.com") -> int:
    """Headed browser: Kyle logs in (MFA ok), then we write storage_state.

    Does not read or write passwords. The saved file is a session export.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. pip install playwright && python -m playwright install chrome")
        return 2
    dest = path.expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(
        "A headed Chrome window will open. Sign in to Intuit (complete MFA), "
        "then return here and press Enter to save the session. "
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
            input("Press Enter after Intuit login/MFA succeeds… ")
        except EOFError:
            print("No TTY; closing without waiting for extra input.")
        context.storage_state(path=str(dest))
        browser.close()
    print(f"Saved Intuit storage_state ({dest.stat().st_size} bytes). Set {ENV_STORAGE_STATE} to this path.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AP Clerk Intuit/QuickBooks session helper")
    parser.add_argument(
        "--save-session",
        metavar="PATH",
        help="Headed login: write Playwright storage_state JSON to PATH (not a password).",
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
        f"Pass --save-session PATH to export cookies after a one-time Kyle login. "
        f"Then set {ENV_STORAGE_STATE}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
