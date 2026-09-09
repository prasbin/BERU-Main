"""Browser automation engine — manages real browser sessions via Playwright.

Provides a provider-agnostic interface for browser automation backed by
Playwright's async API. Each page maps 1:1 to a real Playwright ``Page``
inside a shared ``BrowserContext``. Screenshots are written to a temp directory
and returned with metadata. If Playwright is not installed or cannot launch a
browser, the engine reports ``unavailable`` instead of crashing.

The public interface (``BrowserAction``, ``BrowserPage``, ``ActionResult``,
``BrowserEngine.execute_action``, page-management methods) is preserved so
existing tools and the API router need only minimal changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)


def _valid_page_url(url: str) -> bool:
    """Only ``http(s)`` targets may be loaded in the browser session.

    Blocking ``file:``/``data:``/``blob:``/``javascript:`` at the engine keeps
    an LLM-generated URL from being able to read local files or run in-page
    script with the page's origin.
    """
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return False
    return scheme in ("http", "https")


def _dig(obj: Any, key: str, default: Any = None) -> Any:
    """Read a dict-or-attribute field from a Playwright-ish object."""
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return getter(key)
    return getattr(obj, key, default)


def _timing_ms(timing: Any) -> int | None:
    """Response latency (ms) from a Playwright request-timing object."""
    if not timing:
        return None
    try:
        start = _dig(timing, "requestStart")
        end = _dig(timing, "responseEnd")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
            return int(round(float(end) - float(start)))
    except Exception:
        return None
    return None

# Bounded page-inspection sizes: the LLM gets a useful, structured summary, not
# a raw dump of an enormous page.
_INSPECT_TEXT_MAX = 1500
_INSPECT_LINKS_MAX = 15
_INSPECT_ELEMENTS_MAX = 20

# Bounded per-page roll of captured network responses.
_NETWORK_LOG_MAX = 100
# Bounded list of URL globs blocked in the current session.
_BLOCKED_GLOBS_MAX = 50
# Bounded per-page roll of captured console messages / page errors.
_CONSOLE_LOG_MAX = 150
# Bounded list of URL-glob redirect rewrites in the current session.
_REDIRECTS_MAX = 50
# Bounded list of URL-glob fulfil (mock-response) rewrites in the session.
_FULFILLS_MAX = 50

# In-page inspection: visible text (clamped), top links, and interactive
# elements with practical identifiers. Runs in a single ``evaluate`` call.
# Placeholders ({links_max}, {elements_max}, {text_max}) are filled below.
_INSPECTION_JS = """() => {
  const clamp = (s, n) => {
    s = (s || "").replace(/\\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n) + "..." : s;
  };
  const attr = (el, a) => el.getAttribute(a);
  const bodyText = (document.body && document.body.innerText) || "";
  const allLinks = Array.from(document.querySelectorAll("a[href]"));
  const links = allLinks.slice(0, LINKS_MAX).map(a => ({
    text: clamp(a.innerText, 60) || a.textContent.trim().slice(0, 40),
    href: a.href
  }));
  const allEls = Array.from(document.querySelectorAll(
    "a, button, input, select, textarea, [role='button'], [role='link']"
  ));
  const interactive = allEls.slice(0, ELEMENTS_MAX).map(el => {
    const tag = el.tagName.toLowerCase();
    let selector = "";
    if (el.id) {
      selector = "#" + el.id;
    } else if (attr(el, "name")) {
      selector = tag + "[name=\\"" + attr(el, "name") + "\\"]";
    } else if (attr(el, "placeholder")) {
      selector = tag + "[placeholder=\\"" + attr(el, "placeholder") + "\\"]";
    } else if (attr(el, "data-testid")) {
      selector = "[data-testid=\\"" + attr(el, "data-testid") + "\\"]";
    }
    let type = attr(el, "type");
    if (!type && tag === "a") type = "link";
    if (!type && tag === "button") type = "button";
    return {
      tag: tag, type: type,
      id: el.id || undefined, name: attr(el, "name") || undefined,
      placeholder: attr(el, "placeholder") || undefined,
      text: clamp(el.innerText || el.value || el.textContent || "", 50),
      selector: selector
    };
  });
  return {
    visible_text: clamp(bodyText, TEXT_MAX),
    visible_text_truncated: bodyText.length > TEXT_MAX,
    links: links, links_truncated: allLinks.length > links.length,
    interactive: interactive,
    interactive_truncated: allEls.length > interactive.length
  };
}
""".replace("LINKS_MAX", str(_INSPECT_LINKS_MAX)).replace(
    "ELEMENTS_MAX", str(_INSPECT_ELEMENTS_MAX)
).replace("TEXT_MAX", str(_INSPECT_TEXT_MAX))


class BrowserAction(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    SCROLL_TO = "scroll_to"
    PRESS = "press"
    SELECT = "select"
    DRAG = "drag"
    COOKIES = "cookies"
    STORAGE = "storage"
    NETWORK = "network"
    BLOCK_URL = "block_url"
    DOWNLOAD = "download"
    UPLOAD = "upload"
    SCREENSHOT = "screenshot"
    EXTRACT_TEXT = "extract_text"
    EXTRACT_HTML = "extract_html"
    PAGE_INFO = "page_info"
    WAIT = "wait"
    BACK = "back"
    FORWARD = "forward"
    REFRESH = "refresh"
    CONSOLE = "console"
    DOWNLOADS = "downloads"
    REDIRECT_URL = "redirect_url"
    SNAPSHOT = "snapshot"
    RESTORE = "restore"
    FULFILL = "fulfill"
    LAUNCH = "launch"
    CLEAR_DOWNLOADS = "clear_downloads"


# Actions that operate on the whole session and don't need an active page.
_SESSION_ACTIONS = frozenset({BrowserAction.LAUNCH, BrowserAction.RESTORE})


class PageStatus(str, Enum):
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


@dataclass
class BrowserPage:
    """Represents a browser page/tab."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    url: str = ""
    title: str = ""
    status: PageStatus = PageStatus.READY
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class ActionResult:
    """Result of a browser action."""
    success: bool = True
    action: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0

    def to_dict(self) -> dict[str, Any]:
        result = {
            "success": self.success,
            "action": self.action,
            "data": self.data,
            "duration_ms": self.duration_ms,
        }
        if self.error:
            result["error"] = self.error
        return result


class BrowserEngine:
    """Browser automation engine backed by Playwright.

    Each ``BrowserPage`` maps 1:1 to a real Playwright ``Page`` inside a
    shared ``BrowserContext``. The engine launches headless Chromium on
    first use; if that fails, all actions return structured errors and the
    engine reports ``is_available = False``.
    """

    def __init__(self, screenshot_dir: str | Path | None = None) -> None:
        self._pages: dict[str, BrowserPage] = {}
        self._pw_pages: dict[str, Any] = {}  # page_id → playwright Page
        self._active_page_id: str | None = None
        self._history: dict[str, list[str]] = {}
        self._history_index: dict[str, int] = {}
        self._screenshot_dir = Path(
            screenshot_dir
            or os.environ.get("BERU_BROWSER_SCREENSHOT_DIR")
            or os.path.join(tempfile.gettempdir(), "beru_browser_screenshots")
        )
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir = Path(
            os.environ.get("BERU_BROWSER_DOWNLOADS_DIR")
            or os.path.join(tempfile.gettempdir(), "beru_browser_downloads")
        )
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._session_dir = Path(
            os.environ.get("BERU_BROWSER_SESSION_DIR")
            or os.path.join(tempfile.gettempdir(), "beru_browser_sessions")
        )
        self._session_dir.mkdir(parents=True, exist_ok=True)
        # Playwright state — lazily initialised on first use.
        self._pw = None
        self._browser = None
        self._context = None
        self._available: bool | None = None  # None = not yet probed
        self._started_at: str | None = None
        # Launch/context options for the next (re)launch (headless toggle,
        # viewport, user agent, locale).
        self._launch_opts: dict[str, Any] = {}
        self._context_opts: dict[str, Any] = {}
        # Per-page bounded roll of captured network responses; session-wide
        # URL-glob blocking list; per-page console/page-error roll; session-wide
        # URL-glob redirect rewrites and fulfil (mock-response) rewrites.
        self._responses: dict[str, list[dict[str, Any]]] = {}
        self._blocked_globs: list[str] = []
        self._console_log: dict[str, list[dict[str, Any]]] = {}
        self._redirects: list[dict[str, str]] = []
        self._fulfills: list[dict[str, Any]] = []

    # ---- Playwright lifecycle ----

    async def _open_context(
        self, storage_state: str | dict | None = None, **context_opts: Any
    ) -> bool:
        """Create a fresh BrowserContext (optionally from a storage-state file)."""
        if self._browser is None:
            return False
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                logger.warning("Error closing prior browser context", exc_info=True)
        self._context = await self._browser.new_context(
            storage_state=storage_state, **context_opts
        )
        self._attach_context_handlers()
        return True

    async def _ensure_browser(self) -> bool:
        """Lazily launch Playwright Chromium. Returns True if ready."""
        if self._available is not None:
            return self._available
        try:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=bool(self._launch_opts.get("headless", True))
            )
            if not await self._open_context(**self._context_opts):
                raise RuntimeError("Browser context could not be created")
            self._available = True
            self._started_at = datetime.now(timezone.utc).isoformat()
            logger.info("Playwright Chromium launched successfully")
        except Exception:
            self._available = False
            logger.warning("Playwright unavailable — browser engine disabled")
        return self._available

    @property
    def is_available(self) -> bool:
        """Whether a real browser is reachable (probed synchronously)."""
        if self._available is not None:
            return self._available
        # Trigger the async probe via a helper that can be awaited from the
        # engine methods.  For a sync check (e.g. tool registration), the
        # first call will be False until an async action triggers the probe.
        return False

    def status_info(self) -> dict[str, Any]:
        """Structured engine status for ``/status`` and tool output."""
        headless = bool(self._launch_opts.get("headless", True))
        return {
            "available": self._available is True,
            "mode": "headless" if (self._available is True and headless) else (
                "headed" if self._available is True else None
            ),
            "pages": len(self._pages),
            "active_page": self._active_page_id,
            "blocked_globs": len(self._blocked_globs),
            "redirects": len(self._redirects),
            "fulfills": len(self._fulfills),
            "headless": headless,
            "viewport": self._context_opts.get("viewport"),
            "user_agent": self._context_opts.get("user_agent"),
            "locale": self._context_opts.get("locale"),
            "device_scale_factor": self._context_opts.get("device_scale_factor"),
            "is_mobile": self._context_opts.get("is_mobile"),
            "has_touch": self._context_opts.get("has_touch"),
            "timezone_id": self._context_opts.get("timezone_id"),
            "started_at": self._started_at,
            "download_dir": str(self._download_dir),
            "session_dir": str(self._session_dir),
        }

    async def ensure_ready(self) -> bool:
        """Public async probe — call from any async engine method."""
        return await self._ensure_browser()

    # ---- Context event wiring (popups + network tap) ----

    def _attach_context_handlers(self) -> None:
        """Hook popup/page tracking and the response tap onto the context."""
        if self._context is None:
            return
        try:
            self._context.on("page", self._on_new_page)
            self._context.on("response", self._on_response)
        except Exception:
            logger.warning("Could not attach browser-context event handlers")

    def _on_new_page(self, pw_page: Any) -> None:
        """Register a page created outside ``create_page`` (popups, windows)."""
        page = BrowserPage(
            url=pw_page.url or "about:blank",
            title=f"Page {len(self._pages) + 1}",
        )
        self._pages[page.id] = page
        self._pw_pages[page.id] = pw_page
        self._history[page.id] = [page.url]
        self._history_index[page.id] = 0
        if not self._active_page_id:
            self._active_page_id = page.id
        self._attach_page_handlers(pw_page)
        logger.info("Tracked new/popup browser page %s", page.id)

    def _page_id_for(self, pw_page: Any) -> str | None:
        """Map a Playwright page object back to our tracked page id."""
        return next(
            (pid for pid, pp in self._pw_pages.items() if pp is pw_page),
            None,
        )

    def _attach_page_handlers(self, pw_page: Any) -> None:
        """Hook console-message and page-error taps onto a tracked page."""
        try:
            pw_page.on("console", lambda msg: self._on_console(pw_page, msg))
            pw_page.on("pageerror", lambda err: self._on_pageerror(pw_page, err))
        except Exception:
            logger.warning("Could not attach browser page event handlers")

    def _on_console(self, pw_page: Any, message: Any) -> None:
        """Append a bounded console entry for the page that produced it."""
        page_id = self._page_id_for(pw_page)
        if page_id is None:
            return
        try:
            entry: dict[str, Any] = {
                "type": getattr(message, "type", None) or "log",
                "text": getattr(message, "text", None) or "",
                "count": 1,
            }
            location = getattr(message, "location", None)
            if location:
                if isinstance(location, dict):
                    entry["location"] = {
                        "url": location.get("url"),
                        "line": location.get("line"),
                        "column": location.get("column"),
                    }
                else:
                    entry["location"] = {
                        "url": getattr(location, "url", None),
                        "line": getattr(location, "line", None),
                        "column": getattr(location, "column", None),
                    }
        except Exception:
            return
        log = self._console_log.setdefault(page_id, [])
        if log and log[-1].get("text") == entry["text"] and log[-1].get("type") == entry["type"]:
            log[-1]["count"] += 1
        else:
            log.append(entry)
        if len(log) > _CONSOLE_LOG_MAX:
            del log[: len(log) - _CONSOLE_LOG_MAX]

    def _on_pageerror(self, pw_page: Any, error: Any) -> None:
        """Append a bounded uncaught page-error entry for the page."""
        page_id = self._page_id_for(pw_page)
        if page_id is None:
            return
        try:
            text = str(getattr(error, "message", "") or error or "")
            entry: dict[str, Any] = {"type": "pageerror", "text": text, "count": 1}
        except Exception:
            return
        log = self._console_log.setdefault(page_id, [])
        if log and log[-1].get("text") == entry["text"] and log[-1].get("type") == entry["type"]:
            log[-1]["count"] += 1
        else:
            log.append(entry)
        if len(log) > _CONSOLE_LOG_MAX:
            del log[: len(log) - _CONSOLE_LOG_MAX]

    def _on_response(self, response: Any) -> None:
        """Append a bounded response log entry to the page that produced it."""
        try:
            pw_page = response.page
        except Exception:
            return
        page_id = self._page_id_for(pw_page)
        if page_id is None:
            return
        size: int | None = None
        try:
            headers_obj = response.headers()
            raw = _dig(headers_obj, "content-length")
            if isinstance(headers_obj, dict):
                raw = headers_obj.get("content-length")
            if raw is not None:
                size = int(raw)
        except Exception:
            size = None
        try:
            entry: dict[str, Any] = {
                "url": response.url,
                "status": getattr(response, "status", None),
                "method": getattr(response.request, "method", None),
                "resource_type": getattr(response.request, "resource_type", None),
                "size": size,
                "timing_ms": _timing_ms(getattr(response.request, "timing", None)),
            }
        except Exception:
            return
        log = self._responses.setdefault(page_id, [])
        log.append(entry)
        if len(log) > _NETWORK_LOG_MAX:
            del log[: len(log) - _NETWORK_LOG_MAX]

    async def _abort_request(self, route: Any) -> None:
        """Route handler that aborts blocked requests."""
        try:
            await route.abort()
        except Exception:
            logger.debug("Could not abort blocked request", exc_info=True)

    # ---- Page management ----

    def _get_active_page(self) -> BrowserPage | None:
        if self._active_page_id:
            return self._pages.get(self._active_page_id)
        return None

    async def create_page(self, url: str = "about:blank") -> BrowserPage:
        """Create a new browser page/tab."""
        # Reject dangerous schemes before touching Playwright at all: a
        # javascript:/file:/data: target must never reach page creation.
        if url and url != "about:blank" and not _valid_page_url(url):
            page = BrowserPage(url=url, title="(unsupported scheme)")
            page.status = PageStatus.ERROR
            page.error = (
                "Only http/https URLs may be loaded in the browser session "
                f"(rejected scheme '{url.split(':', 1)[0]}')."
            )
            logger.warning("Refused to open unsupported URL scheme: %s", url[:60])
            return page

        if not await self._ensure_browser():
            page = BrowserPage(url=url, title="(unavailable)")
            page.status = PageStatus.ERROR
            self._pages[page.id] = page
            return page

        try:
            pw_page = await self._context.new_page()
        except Exception:
            page = BrowserPage(url=url, title="(error)")
            page.status = PageStatus.ERROR
            self._pages[page.id] = page
            logger.exception("Failed to create Playwright page")
            return page

        page = BrowserPage(url=url, title="New Page")
        self._pages[page.id] = page
        self._pw_pages[page.id] = pw_page
        self._history[page.id] = [url]
        self._history_index[page.id] = 0
        self._active_page_id = page.id
        self._attach_page_handlers(pw_page)

        # Navigate if a real URL was given (http/https only).
        if url and url != "about:blank":
            if not _valid_page_url(url):
                page.status = PageStatus.ERROR
                page.title = "(unsupported scheme)"
                logger.warning("Refused to open unsupported URL scheme on page %s", page.id)
                return page
            try:
                await pw_page.goto(url, wait_until="domcontentloaded")
                page.title = await pw_page.title()
                page.url = pw_page.url
                page.status = PageStatus.READY
            except Exception:
                page.status = PageStatus.ERROR
                logger.warning("Failed to navigate to %s on page creation", url)

        logger.info("Created page %s: %s", page.id, url)
        return page

    def close_page(self, page_id: str) -> bool:
        """Close a browser page/tab."""
        if page_id not in self._pages:
            return False

        pw_page = self._pw_pages.pop(page_id, None)
        if pw_page:
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(pw_page.close())
            except RuntimeError:
                pass  # no running loop — page will be GC'd

        del self._pages[page_id]
        self._history.pop(page_id, None)
        self._history_index.pop(page_id, None)
        self._responses.pop(page_id, None)
        self._console_log.pop(page_id, None)
        if self._active_page_id == page_id:
            self._active_page_id = next(iter(self._pages), None)
        return True

    def list_pages(self) -> list[BrowserPage]:
        return list(self._pages.values())

    def get_page(self, page_id: str) -> BrowserPage | None:
        return self._pages.get(page_id)

    def set_active_page(self, page_id: str) -> bool:
        if page_id in self._pages:
            self._active_page_id = page_id
            return True
        return False

    async def close_browser(self) -> bool:
        """Close the entire browser session and release resources.

        All pages are discarded; the next action lazily relaunches a fresh
        browser. Returns True when a launched browser was actually closed.
        """
        closed = False
        try:
            if self._context and self._browser:
                closed = True
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
        except Exception:
            logger.exception("Error while closing the browser session")
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            logger.warning("Error while stopping Playwright")
        self._pw = None
        self._browser = None
        self._context = None
        self._available = None  # relaunch on next use
        self._pages.clear()
        self._pw_pages.clear()
        self._history.clear()
        self._history_index.clear()
        self._responses.clear()
        self._blocked_globs.clear()
        self._console_log.clear()
        self._redirects.clear()
        self._fulfills.clear()
        self._active_page_id = None
        self._started_at = None
        return closed

    # ---- Action dispatch ----

    async def execute_action(
        self, action: BrowserAction, **kwargs: Any
    ) -> ActionResult:
        """Execute a browser action on the active page."""
        if not await self._ensure_browser():
            return ActionResult(
                success=False,
                action=action.value,
                error="Browser backend not available.",
            )

        # Session-scoped actions (relaunch, restore) don't require an active page.
        page = self._get_active_page()
        pw_page = None
        if action not in _SESSION_ACTIONS:
            if not page:
                return ActionResult(
                    success=False,
                    action=action.value,
                    error="No active page. Create a page first.",
                )
            pw_page = self._pw_pages.get(page.id)
            if not pw_page:
                return ActionResult(
                    success=False,
                    action=action.value,
                    error="No real browser page backing this tab.",
                )

        started = time.perf_counter()

        handler_map = {
            BrowserAction.NAVIGATE: self._navigate,
            BrowserAction.CLICK: self._click,
            BrowserAction.TYPE: self._type,
            BrowserAction.SCROLL: self._scroll,
            BrowserAction.SCROLL_TO: self._scroll_to,
            BrowserAction.PRESS: self._press,
            BrowserAction.SELECT: self._select,
            BrowserAction.DRAG: self._drag,
            BrowserAction.COOKIES: self._cookies,
            BrowserAction.STORAGE: self._storage,
            BrowserAction.NETWORK: self._network,
            BrowserAction.BLOCK_URL: self._block_url,
            BrowserAction.DOWNLOAD: self._download,
            BrowserAction.UPLOAD: self._upload,
            BrowserAction.PAGE_INFO: self._page_info,
            BrowserAction.SCREENSHOT: self._screenshot,
            BrowserAction.EXTRACT_TEXT: self._extract_text,
            BrowserAction.EXTRACT_HTML: self._extract_html,
            BrowserAction.WAIT: self._wait,
            BrowserAction.BACK: self._back,
            BrowserAction.FORWARD: self._forward,
            BrowserAction.REFRESH: self._refresh,
            BrowserAction.CONSOLE: self._console,
            BrowserAction.DOWNLOADS: self._downloads,
            BrowserAction.REDIRECT_URL: self._redirect_url,
            BrowserAction.SNAPSHOT: self._snapshot,
            BrowserAction.RESTORE: self._restore,
            BrowserAction.FULFILL: self._fulfill,
            BrowserAction.LAUNCH: self._launch,
            BrowserAction.CLEAR_DOWNLOADS: self._clear_downloads,
        }
        handler = handler_map.get(action)
        if not handler:
            return ActionResult(
                success=False,
                action=getattr(action, "value", str(action)),
                error=f"Unknown action: {action}",
            )

        try:
            result = await handler(page, pw_page, **kwargs)
        except Exception as exc:
            result = ActionResult(
                success=False, action=action.value, error=str(exc),
            )
            logger.exception("Browser action '%s' failed", action.value)

        result.duration_ms = round(
            (time.perf_counter() - started) * 1000, 1
        )
        return result

    # ---- Per-action handlers (real Playwright calls) ----

    async def _resolve_frame(self, pw_page: Any, frame: str | None = None) -> Any:
        """Resolve a frame descriptor to a target page/frame.

        Accepts ``None`` (main page), a frame *name*, a URL substring, or a
        zero-based frame index. Raises :class:`ValueError` when no frame matches.
        """
        if not frame:
            return pw_page
        frames = getattr(pw_page, "frames", None)
        if frames is None:
            raise ValueError(
                f"Cannot target frame '{frame}' — page has no frames."
            )
        if str(frame).isdigit():
            idx = int(frame)
            if 0 <= idx < len(frames):
                return frames[idx]
            raise ValueError(
                f"Frame index {idx} out of range ({len(frames)} frames)."
            )
        for candidate in frames:
            name = getattr(candidate, "name", "") or ""
            url = getattr(candidate, "url", "") or ""
            if name == frame or frame in url:
                return candidate
        raise ValueError(f"No frame matches '{frame}'.")

    async def _navigate(
        self, page: BrowserPage, pw_page: Any, url: str = "", **kw: Any
    ) -> ActionResult:
        if not url:
            return ActionResult(success=False, action="navigate", error="Missing URL")
        if not _valid_page_url(url):
            page.status = PageStatus.ERROR
            return ActionResult(
                success=False,
                action="navigate",
                error=(
                    "Only http/https URLs may be loaded in the browser session "
                    f"(rejected scheme '{url.split(':', 1)[0]}')."
                ),
            )

        page.status = PageStatus.LOADING
        try:
            await pw_page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            page.status = PageStatus.ERROR
            return ActionResult(
                success=False, action="navigate",
                error=f"Navigation failed: {exc}",
            )

        page.url = pw_page.url
        page.title = await pw_page.title()
        page.status = PageStatus.READY

        # Update history.
        history = self._history.get(page.id, [])
        idx = self._history_index.get(page.id, 0)
        history = history[: idx + 1]
        history.append(pw_page.url)
        self._history[page.id] = history
        self._history_index[page.id] = len(history) - 1

        return ActionResult(
            action="navigate",
            data={"url": page.url, "title": page.title},
        )

    async def _click(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(success=False, action="click", error="Missing selector")
        target = await self._resolve_frame(pw_page, frame)
        await target.click(selector)
        return ActionResult(
            action="click", data={"selector": selector, "clicked": True},
        )

    async def _type(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", text: str = "", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(success=False, action="type", error="Missing selector")
        target = await self._resolve_frame(pw_page, frame)
        await target.fill(selector, text)
        return ActionResult(
            action="type",
            data={"selector": selector, "text": text, "typed": True},
        )

    async def _scroll(
        self, page: BrowserPage, pw_page: Any,
        direction: str = "down", amount: int = 500, **kw: Any
    ) -> ActionResult:
        delta_y = amount if direction == "down" else -amount
        await pw_page.mouse.wheel(0, delta_y)
        return ActionResult(
            action="scroll",
            data={"direction": direction, "amount": amount},
        )

    async def _scroll_to(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", block: str = "center", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(success=False, action="scroll_to", error="Missing selector")
        target = await self._resolve_frame(pw_page, frame)
        el = await target.query_selector(selector)
        if not el:
            return ActionResult(
                success=False, action="scroll_to",
                error=f"No element matches selector: {selector}",
            )
        await el.scroll_into_view_if_needed()
        await target.evaluate(
            "({ sel, block }) => { const el = document.querySelector(sel); "
            "if (el) el.scrollIntoView({ block, behavior: 'instant' }); }",
            {"sel": selector, "block": block},
        )
        return ActionResult(
            action="scroll_to",
            data={"selector": selector, "block": block, "scrolled": True},
        )

    async def _press(
        self, page: BrowserPage, pw_page: Any,
        key: str = "", selector: str = "", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        if not key:
            return ActionResult(success=False, action="press", error="Missing key")
        if selector:
            target = await self._resolve_frame(pw_page, frame)
            await target.press(selector, key)
        elif frame:
            return ActionResult(
                success=False, action="press",
                error="press on a frame requires a 'selector'.",
            )
        else:
            await pw_page.keyboard.press(key)
        return ActionResult(
            action="press",
            data={"key": key, "selector": selector},
        )

    async def _select(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", value: str | None = None,
        label: str | None = None, index: int | None = None,
        values: list[str] | None = None, frame: str | None = None, **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(success=False, action="select", error="Missing selector")
        options: dict[str, Any] = {}
        if values is not None:
            options["values"] = [str(v) for v in values]
        elif value is not None:
            options["value"] = str(value)
        elif label is not None:
            options["label"] = str(label)
        elif index is not None:
            options["index"] = int(index)
        else:
            return ActionResult(
                success=False, action="select",
                error="browser_select requires 'value', 'label', 'index', or 'values'.",
            )
        target = await self._resolve_frame(pw_page, frame)
        await target.select_option(selector, **options)
        return ActionResult(
            action="select",
            data={"selector": selector, **options, "selected": True},
        )

    async def _drag(
        self, page: BrowserPage, pw_page: Any,
        source: str = "", target: str = "", **kw: Any
    ) -> ActionResult:
        if not source or not target:
            return ActionResult(
                success=False, action="drag",
                error="browser_drag requires 'source' and 'target' selectors.",
            )
        await pw_page.drag_and_drop(source, target)
        return ActionResult(
            action="drag",
            data={"source": source, "target": target, "dragged": True},
        )

    async def _cookies(
        self, page: BrowserPage, pw_page: Any,
        subcommand: str = "get", cookies: list[dict[str, Any]] | None = None, **kw: Any
    ) -> ActionResult:
        ctx = self._context
        if ctx is None:
            return ActionResult(
                success=False, action="cookies",
                error="No browser context — the session is closed.",
            )
        if subcommand in ("set", "add"):
            if not cookies:
                return ActionResult(
                    success=False, action="cookies",
                    error="cookies subcommand 'set' requires a list of cookie objects.",
                )
            await ctx.add_cookies(cookies)
            return ActionResult(
                action="cookies",
                data={"subcommand": "set", "cookie_count": len(cookies), "set": True},
            )
        if subcommand == "clear":
            await ctx.clear_cookies()
            return ActionResult(
                action="cookies", data={"subcommand": "clear", "cleared": True},
            )
        current = await ctx.cookies()
        return ActionResult(
            action="cookies",
            data={"subcommand": "get", "cookies": current, "count": len(current)},
        )

    async def _storage(
        self, page: BrowserPage, pw_page: Any,
        subcommand: str = "get", key: str = "", value: str = "", **kw: Any
    ) -> ActionResult:
        if subcommand == "set":
            if not key:
                return ActionResult(
                    success=False, action="storage",
                    error="storage subcommand 'set' requires a 'key'.",
                )
            await pw_page.evaluate(
                "(k, v) => { localStorage.setItem(k, v); return true; }",
                key, str(value),
            )
            return ActionResult(
                action="storage",
                data={"subcommand": "set", "key": key, "set": True},
            )
        if subcommand == "clear":
            await pw_page.evaluate(
                "() => { localStorage.clear(); return true; }"
            )
            return ActionResult(
                action="storage", data={"subcommand": "clear", "cleared": True},
            )
        if key:
            got = await pw_page.evaluate(
                "k => localStorage.getItem(k)", key
            )
            return ActionResult(
                action="storage",
                data={"subcommand": "get", "key": key, "value": got},
            )
        entries = await pw_page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"
        )
        return ActionResult(
            action="storage",
            data={"subcommand": "get", "entries": entries, "count": len(entries)},
        )

    async def _network(
        self, page: BrowserPage, pw_page: Any, limit: int = 50, **kw: Any
    ) -> ActionResult:
        entries = self._responses.get(page.id, [])
        if limit and limit > 0:
            entries = entries[-limit:]
        return ActionResult(
            action="network",
            data={
                "entries": entries,
                "count": len(entries),
                "blocked_globs": list(self._blocked_globs),
            },
        )

    async def _block_url(
        self, page: BrowserPage, pw_page: Any, glob: str = "", **kw: Any
    ) -> ActionResult:
        if not glob:
            return ActionResult(
                success=False, action="block_url", error="browser_block_url requires a 'glob'.",
            )
        if self._context is None:
            return ActionResult(
                success=False, action="block_url",
                error="No browser context — the session is closed.",
            )
        self._blocked_globs.append(glob)
        if len(self._blocked_globs) > _BLOCKED_GLOBS_MAX:
            del self._blocked_globs[0]
        await self._context.route(glob, self._abort_request)
        return ActionResult(
            action="block_url",
            data={"glob": glob, "blocked_globs": list(self._blocked_globs)},
        )

    async def _download(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(
                success=False, action="download",
                error="browser_download requires a 'selector' for the download trigger.",
            )
        stamp = time.strftime("%Y%m%d_%H%M%S")
        async with pw_page.expect_download() as info:
            await pw_page.click(selector)
        download = await info.value
        suggested = getattr(download, "suggested_filename", "") or ""
        filepath = self._resolve_download_path(suggested, stamp=stamp, page_id=page.id)
        await download.save_as(str(filepath))
        return ActionResult(
            action="download",
            data={
                "selector": selector,
                "path": str(filepath),
                "filename": filepath.name,
                "downloaded": True,
            },
        )

    def _resolve_download_path(self, suggested: str, *, stamp: str, page_id: str) -> Path:
        """Resolve a download destination that stays inside the downloads dir.

        The suggested filename is attacker-controlled (a malicious site can send
        ``../..\\<name>`` via Content-Disposition), so only the final basename is
        used — with either path separator — and the result is verified to live
        within the configured download directory.
        """
        base = self._download_dir.resolve()
        fallback = f"download_{stamp}_{page_id}"
        name = ""
        if suggested:
            name = PurePosixPath(suggested.replace("\\", "/")).name.strip().strip(".")
        if not name or name in (".", ".."):
            name = fallback
        target = (base / name)
        if target.resolve() != base / name or not target.resolve().is_relative_to(base):
            name = fallback
            target = base / name
        return target

    async def _upload(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "", paths: list[str] | None = None, **kw: Any
    ) -> ActionResult:
        if not selector:
            return ActionResult(
                success=False, action="upload",
                error="browser_upload requires a 'selector'.",
            )
        if not paths:
            return ActionResult(
                success=False, action="upload",
                error="browser_upload requires at least one 'paths' entry.",
            )
        await pw_page.set_input_files(selector, list(paths))
        return ActionResult(
            action="upload",
            data={"selector": selector, "files": list(paths), "uploaded": True},
        )

    def _safe_session_name(self, name: str) -> str:
        """Sanitise a snapshot name into a safe filename stem."""
        cleaned = re.sub(r"[^0-9A-Za-z_.-]", "_", name or "session").strip("._")
        return cleaned or "session"

    async def _console(
        self, page: BrowserPage, pw_page: Any, limit: int | None = None, **kw: Any
    ) -> ActionResult:
        entries = list(self._console_log.get(page.id, []))
        if limit:
            entries = entries[-int(limit):]
        return ActionResult(
            action="console",
            data={
                "count": len(entries),
                "entries": entries,
                "total_captured": len(self._console_log.get(page.id, [])),
            },
        )

    async def _downloads(
        self, page: BrowserPage, pw_page: Any, limit: int | None = None, **kw: Any
    ) -> ActionResult:
        files: list[dict[str, Any]] = []
        if self._download_dir.exists():
            for item in self._download_dir.iterdir():
                if item.is_file():
                    try:
                        files.append({
                            "name": item.name,
                            "size_bytes": item.stat().st_size,
                            "path": str(item),
                        })
                    except OSError:
                        continue
        files.sort(key=lambda f: f["name"], reverse=True)
        if limit:
            files = files[: int(limit)]
        return ActionResult(
            action="downloads",
            data={"count": len(files), "files": files},
        )

    async def _clear_downloads(
        self, page: BrowserPage, pw_page: Any, **kw: Any
    ) -> ActionResult:
        removed = 0
        failed = 0
        if self._download_dir.exists():
            for item in list(self._download_dir.iterdir()):
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        failed += 1
        return ActionResult(
            action="clear_downloads",
            data={"removed": removed, "failed": failed},
        )

    def _make_redirect_handler(self, target: str) -> Any:
        """Build a route handler that 302-redirects a matched request."""
        async def handler(route: Any) -> None:
            try:
                await route.fulfill(
                    status=302,
                    headers={"location": target},
                    redirect_url=target,
                )
            except Exception:
                logger.debug("Could not fulfil redirect route", exc_info=True)

        return handler

    async def _redirect_url(
        self, page: BrowserPage, pw_page: Any,
        glob: str = "", redirect: str = "", **kw: Any
    ) -> ActionResult:
        if not glob:
            return ActionResult(
                success=False, action="redirect_url",
                error="browser_redirect_url requires a 'glob'.",
            )
        if not redirect:
            return ActionResult(
                success=False, action="redirect_url",
                error="browser_redirect_url requires a 'redirect' target URL.",
            )
        if self._context is None:
            return ActionResult(
                success=False, action="redirect_url",
                error="No browser context — the session is closed.",
            )
        self._redirects.append({"glob": glob, "redirect": redirect})
        if len(self._redirects) > _REDIRECTS_MAX:
            del self._redirects[0]
        await self._context.route(glob, self._make_redirect_handler(redirect))
        return ActionResult(
            action="redirect_url",
            data={
                "glob": glob,
                "redirect": redirect,
                "redirects": list(self._redirects),
            },
        )

    async def _snapshot(
        self, page: BrowserPage, pw_page: Any, name: str = "session", **kw: Any
    ) -> ActionResult:
        if self._context is None:
            return ActionResult(
                success=False, action="snapshot",
                error="No browser context — the session is closed.",
            )
        safe = self._safe_session_name(name)
        state = await self._context.storage_state()
        filepath = self._session_dir / f"{safe}.json"
        filepath.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return ActionResult(
            action="snapshot",
            data={
                "name": safe,
                "path": str(filepath),
                "cookies": len(state.get("cookies", [])),
                "origins": len(state.get("origins", [])),
            },
        )

    async def _restore(
        self, page: BrowserPage, pw_page: Any, name: str = "session", **kw: Any
    ) -> ActionResult:
        if self._browser is None:
            return ActionResult(
                success=False, action="restore",
                error="No browser session to restore into.",
            )
        safe = self._safe_session_name(name)
        filepath = self._session_dir / f"{safe}.json"
        if not filepath.is_file():
            return ActionResult(
                success=False, action="restore",
                error=f"No snapshot named '{safe}' in {self._session_dir}.",
            )
        if not await self._open_context(storage_state=str(filepath)):
            return ActionResult(
                success=False, action="restore",
                error="Browser context could not be recreated.",
            )
        self._pages.clear()
        self._pw_pages.clear()
        self._history.clear()
        self._history_index.clear()
        self._responses.clear()
        self._console_log.clear()
        self._blocked_globs.clear()
        self._redirects.clear()
        self._active_page_id = None
        return ActionResult(
            action="restore",
            data={"name": safe, "path": str(filepath), "restored": True},
        )

    def _make_fulfill_handler(
        self, status: int, body: str, content_type: str | None, headers: dict[str, str] | None,
    ) -> Any:
        """Build a route handler that answers a matched request with fixed content."""
        async def handler(route: Any) -> None:
            try:
                kw: dict[str, Any] = {"status": int(status) if status else 200}
                if body:
                    kw["body"] = body
                if content_type:
                    kw["content_type"] = content_type
                if headers:
                    kw["headers"] = dict(headers)
                await route.fulfill(**kw)
            except Exception:
                logger.debug("Could not fulfil mock route", exc_info=True)

        return handler

    async def _fulfill(
        self, page: BrowserPage, pw_page: Any,
        glob: str = "", body: str = "", content_type: str | None = None,
        status_code: int | None = None, headers: dict[str, str] | None = None,
        **kw: Any
    ) -> ActionResult:
        if not glob:
            return ActionResult(
                success=False, action="fulfill",
                error="browser_fulfill requires a 'glob'.",
            )
        if self._context is None:
            return ActionResult(
                success=False, action="fulfill",
                error="No browser context — the session is closed.",
            )
        entry: dict[str, Any] = {
            "glob": glob,
            "status": int(status_code) if status_code else 200,
            "content_type": content_type,
            "body_length": len(body or ""),
            "headers": dict(headers) if headers else None,
        }
        self._fulfills.append(entry)
        if len(self._fulfills) > _FULFILLS_MAX:
            del self._fulfills[0]
        await self._context.route(
            glob, self._make_fulfill_handler(entry["status"], body, content_type, headers)
        )
        return ActionResult(
            action="fulfill",
            data={**entry, "fulfills": list(self._fulfills)},
        )

    async def _launch(
        self, page: BrowserPage, pw_page: Any,
        headless: bool | None = None, width: int | None = None,
        height: int | None = None, user_agent: str | None = None,
        locale: str | None = None, device_scale_factor: int | None = None,
        is_mobile: bool | None = None, has_touch: bool | None = None,
        timezone_id: str | None = None, **kw: Any
    ) -> ActionResult:
        close_result = await self.close_browser()
        if headless is not None:
            self._launch_opts["headless"] = bool(headless)
        new_context_opts: dict[str, Any] = {}
        if width and height:
            new_context_opts["viewport"] = {
                "width": int(width), "height": int(height),
            }
        if user_agent:
            new_context_opts["user_agent"] = user_agent
        if locale:
            new_context_opts["locale"] = locale
        if device_scale_factor is not None:
            new_context_opts["device_scale_factor"] = float(device_scale_factor)
        if is_mobile is not None:
            new_context_opts["is_mobile"] = bool(is_mobile)
        if has_touch is not None:
            new_context_opts["has_touch"] = bool(has_touch)
        if timezone_id:
            new_context_opts["timezone_id"] = timezone_id
        if new_context_opts:
            self._context_opts = new_context_opts
        launched = await self._ensure_browser()
        if not launched:
            return ActionResult(
                success=False, action="launch",
                error="Browser could not be relaunched.",
            )
        return ActionResult(
            action="launch",
            data={
                "launched": True,
                "closed_previous": close_result,
                "headless": bool(self._launch_opts.get("headless", True)),
                "viewport": self._context_opts.get("viewport"),
                "user_agent": self._context_opts.get("user_agent"),
                "locale": self._context_opts.get("locale"),
                "device_scale_factor": self._context_opts.get("device_scale_factor"),
                "is_mobile": self._context_opts.get("is_mobile"),
                "has_touch": self._context_opts.get("has_touch"),
                "timezone_id": self._context_opts.get("timezone_id"),
            },
        )

    async def _page_info(
        self, page: BrowserPage, pw_page: Any, **kw: Any
    ) -> ActionResult:
        url = pw_page.url or page.url
        title = await pw_page.title()
        page.url = url
        page.title = title
        data: dict[str, Any] = {"url": url, "title": title}
        # Structured, bounded inspection — not a raw page dump.
        try:
            inspection = await pw_page.evaluate(_INSPECTION_JS)
        except Exception:
            inspection = None
        if isinstance(inspection, dict):
            for key in (
                "visible_text", "visible_text_truncated",
                "links", "links_truncated",
                "interactive", "interactive_truncated",
            ):
                if key in inspection:
                    data[key] = inspection[key]
        # Frame map (names, urls) for frame-scoped follow-up actions.
        try:
            frames = [
                {
                    "index": i,
                    "name": getattr(f, "name", "") or "",
                    "url": getattr(f, "url", "") or "",
                    "children": len(getattr(f, "child_frames", []) or []),
                }
                for i, f in enumerate(getattr(pw_page, "frames", []) or [])
            ]
            data["frames"] = frames
            data["frame_count"] = len(frames)
        except Exception:
            pass
        return ActionResult(action="page_info", data=data)

    async def _screenshot(
        self, page: BrowserPage, pw_page: Any, full_page: bool = False, **kw: Any
    ) -> ActionResult:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"browser_{stamp}_{page.id}.png"
        filepath = self._screenshot_dir / filename
        kwargs: dict[str, Any] = {"type": "png"}
        if full_page:
            kwargs["full_page"] = True
        png_bytes = await pw_page.screenshot(**kwargs)
        filepath.write_bytes(png_bytes)
        return ActionResult(
            action="screenshot",
            data={
                "url": page.url,
                "title": page.title,
                "path": str(filepath),
                "format": "png",
                "size_bytes": len(png_bytes),
                "full_page": bool(full_page),
            },
        )

    async def _extract_text(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "body", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        target = await self._resolve_frame(pw_page, frame)
        text = await target.text_content(selector) or ""
        return ActionResult(
            action="extract_text",
            data={"selector": selector, "text": text, "length": len(text)},
        )

    async def _extract_html(
        self, page: BrowserPage, pw_page: Any,
        selector: str = "body", frame: str | None = None, **kw: Any
    ) -> ActionResult:
        target = await self._resolve_frame(pw_page, frame)
        el = await target.query_selector(selector)
        html = await el.inner_html() if el else ""
        return ActionResult(
            action="extract_html",
            data={"selector": selector, "html": html, "length": len(html)},
        )

    async def _wait(
        self, page: BrowserPage, pw_page: Any,
        seconds: float = 1.0, **kw: Any
    ) -> ActionResult:
        await pw_page.wait_for_timeout(int(min(seconds, 10.0) * 1000))
        return ActionResult(action="wait", data={"waited_seconds": seconds})

    async def _back(self, page: BrowserPage, pw_page: Any) -> ActionResult:
        history = self._history.get(page.id, [])
        idx = self._history_index.get(page.id, 0)
        if idx > 0:
            idx -= 1
            self._history_index[page.id] = idx
            target = history[idx]
            try:
                await pw_page.goto(target, wait_until="domcontentloaded")
                page.url = pw_page.url
                page.title = await pw_page.title()
            except Exception:
                page.url = target
            return ActionResult(action="back", data={"url": page.url})
        return ActionResult(
            action="back", data={"url": page.url, "note": "Already at start"},
        )

    async def _forward(self, page: BrowserPage, pw_page: Any) -> ActionResult:
        history = self._history.get(page.id, [])
        idx = self._history_index.get(page.id, 0)
        if idx < len(history) - 1:
            idx += 1
            self._history_index[page.id] = idx
            target = history[idx]
            try:
                await pw_page.goto(target, wait_until="domcontentloaded")
                page.url = pw_page.url
                page.title = await pw_page.title()
            except Exception:
                page.url = target
            return ActionResult(action="forward", data={"url": page.url})
        return ActionResult(
            action="forward",
            data={"url": page.url, "note": "Already at end"},
        )

    async def _refresh(self, page: BrowserPage, pw_page: Any) -> ActionResult:
        await pw_page.reload(wait_until="domcontentloaded")
        page.url = pw_page.url
        page.title = await pw_page.title()
        return ActionResult(
            action="refresh",
            data={"url": page.url, "refreshed": True},
        )


@lru_cache
def get_browser_engine() -> BrowserEngine:
    """Return the process-wide browser engine (shared by API + agent tools)."""
    return BrowserEngine()
