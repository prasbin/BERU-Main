"""Tests for the browser automation system: engine, tools, API.

The engine delegates to Playwright, so the invasive browser boundary is mocked
with a fake Playwright page. Engine actions are exercised against the fake page
to verify the real control flow and result mapping without launching a browser.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from backend.agents.registry import get_agent_registry
from backend.engines.browser import BrowserAction, BrowserEngine, BrowserPage, PageStatus
from backend.tools.base import ToolResult
from backend.tools.browser import (
    BrowserBlockTool,
    BrowserClearDownloadsTool,
    BrowserClickTool,
    BrowserCloseTool,
    BrowserConsoleTool,
    BrowserCookiesTool,
    BrowserDownloadsTool,
    BrowserDownloadTool,
    BrowserDragTool,
    BrowserExtractTool,
    BrowserFulfillTool,
    BrowserLaunchTool,
    BrowserNavigateTool,
    BrowserNetworkTool,
    BrowserRedirectTool,
    BrowserRestoreTool,
    BrowserScreenshotTool,
    BrowserSelectTool,
    BrowserSnapshotTool,
    BrowserStorageTool,
    BrowserTypeTool,
    BrowserUploadTool,
)


class FakeMouse:
    """Fake Playwright Mouse sub-object."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def wheel(self, dx: int, dy: int) -> None:
        self._page.wheel_delta = dy


class FakeKeyboard:
    """Fake Playwright Keyboard sub-object."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.pressed_keys.append(key)


class _FakeRequest:
    def __init__(self, method: str, resource_type: str, timing: Any = None) -> None:
        self.method = method
        self.resource_type = resource_type
        self.timing = timing


class _FakeResponse:
    def __init__(
        self, page, url: str, status: int, method: str, resource_type: str,
        headers: dict[str, str] | None = None, timing: Any = None,
    ) -> None:
        self.page = page
        self.url = url
        self.status = status
        self._headers = headers or {}
        self.request = _FakeRequest(method, resource_type, timing=timing)

    def headers(self) -> dict[str, str]:
        return dict(self._headers)


class FakeDownload:
    """Fake Playwright Download object."""

    def __init__(self, filename: str = "report.pdf") -> None:
        self.suggested_filename = filename
        self.saved_to = None

    async def save_as(self, path: str) -> None:
        self.saved_to = path


class FakeContext:
    """Minimal fake Playwright BrowserContext for cookie/route tests."""

    def __init__(self) -> None:
        self.cookies_store: list[dict] = []
        self.added: list[list[dict]] = []
        self.cleared = False
        self.routes: list[tuple[str, Any]] = []

    async def cookies(self) -> list[dict]:
        return list(self.cookies_store)

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.added.append(list(cookies))
        self.cookies_store.extend(cookies)

    async def clear_cookies(self) -> None:
        self.cleared = True
        self.cookies_store.clear()

    async def route(self, glob: str, handler: Any) -> None:
        self.routes.append((glob, handler))

    async def close(self) -> None:
        self.closed = True

    async def storage_state(self) -> dict:
        return {
            "cookies": list(self.cookies_store),
            "origins": [
                {
                    "origin": "https://example.com",
                    "localStorage": [{"name": "k", "value": "v"}],
                }
            ],
        }


class FakeBrowser:
    """Minimal fake Playwright browser that creates fresh fake contexts."""

    def __init__(self) -> None:
        self.context = FakeContext()
        self.new_context_kwargs: list[Any] = []

    async def new_context(self, storage_state=None, **kw):
        self.new_context_kwargs.append(storage_state)
        fc = FakeContext()
        fc.cookies_store = [{"name": "restored", "value": "1"}]
        self.context = fc
        return fc


class FakeRoute:
    """Minimal fake Playwright Route for fulfil/abort handler tests."""

    def __init__(self) -> None:
        self.fulfilled: dict[str, Any] | None = None
        self.aborted = False

    async def fulfill(self, **kw: Any) -> None:
        self.fulfilled = dict(kw)

    async def abort(self, **kw: Any) -> None:
        self.aborted = True


class _FakeDownloadExpect:
    """Async context manager emulating Playwright's ``expect_download()``."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def __aenter__(self):
        return _FakeDownloadInfo(self._page.download)

    async def __aexit__(self, *exc):
        return False


class _FakeDownloadInfo:
    def __init__(self, download: FakeDownload) -> None:
        self.value = self._resolve(download)

    async def _resolve(self, download: FakeDownload) -> FakeDownload:
        return download


class FakePage:
    """Minimal fake Playwright page."""

    def __init__(self, url: str, *, name: str = "") -> None:
        self.url = url
        self.name = name
        self.clicked = []
        self.filled = []
        self.selected = []
        self.pressed_keys = []
        self.pressed = []
        self.dragged: list[tuple[str, str]] = []
        self.uploaded: list[tuple[str, list[str]]] = []
        self.text = "Fake body text"
        self.inspect_result = None
        self.screen_args = {}
        self.frames: list[Any] = []
        self.download = FakeDownload()
        self.child_frames: list[FakePage] = []
        self.handlers: dict[str, Any] = {}

    async def title(self) -> str:
        return "Fake Title"

    async def goto(self, url: str, **kw) -> None:
        self.url = url

    async def click(self, selector: str) -> None:
        self.clicked.append(selector)

    async def fill(self, selector: str, text: str) -> None:
        self.filled.append((selector, text))

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheel_delta = dy

    @property
    def mouse(self):
        return FakeMouse(self)

    @property
    def keyboard(self):
        return FakeKeyboard(self)


    async def screenshot(self, type: str = "png", **kw) -> bytes:
        self.screen_args = {"type": type, **kw}
        return b"\x89PNG fake screenshot bytes"

    async def text_content(self, selector: str) -> str:
        return self.text

    async def query_selector(self, selector: str):
        return self

    async def scroll_into_view_if_needed(self) -> None:
        self.scrolled_into_view = True

    async def evaluate(self, js: str, *args):
        self.evaluated = js
        return self.inspect_result

    async def select_option(self, selector: str, **opts) -> None:
        self.selected.append((selector, opts))

    async def press(self, selector: str, key: str) -> None:
        self.pressed.append((selector, key))

    async def drag_and_drop(self, source: str, target: str) -> None:
        self.dragged.append((source, target))

    async def set_input_files(self, selector: str, paths: list[str]) -> None:
        self.uploaded.append((selector, list(paths)))

    def expect_download(self, **kw):
        """Return an async context manager yielding a fake download info."""
        return _FakeDownloadExpect(self)

    async def inner_html(self) -> str:
        return "<p>fake</p>"

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    async def reload(self, **kw) -> None:
        pass

    async def close(self) -> None:
        pass

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler
        return None


def _ready_engine(url: str = "https://example.com") -> tuple[BrowserEngine, FakePage]:
    engine = BrowserEngine()
    fake = FakePage(url=url)
    engine._available = True
    page = BrowserPage(url=url, title="Fake Title")
    engine._pages[page.id] = page
    engine._pw_pages[page.id] = fake
    engine._history[page.id] = [url]
    engine._history_index[page.id] = 0
    engine._active_page_id = page.id
    return engine, fake


def _available_engine() -> BrowserEngine:
    engine = BrowserEngine()
    engine._available = True
    return engine


# ---- Engine: page management ----


def test_page_serialization():
    page = BrowserPage(url="https://example.com", title="Example")
    data = page.to_dict()
    assert data["url"] == "https://example.com"
    assert data["title"] == "Example"


async def test_engine_unavailable_without_playwright():
    engine = BrowserEngine()

    async def _fail() -> bool:
        engine._available = False
        return False

    engine._ensure_browser = _fail
    assert engine.is_available is False
    page = await engine.create_page("https://example.com")
    assert page.status == PageStatus.ERROR
    result = await engine.execute_action(BrowserAction.NAVIGATE, url="https://x.com")
    assert result.success is False
    assert "not available" in result.error


async def test_engine_create_page():
    engine = _available_engine()

    async def fake_new_page(*args):
        return FakePage("https://example.com")

    engine._context = type("Ctx", (), {"new_page": fake_new_page})()
    page = await engine.create_page("https://example.com")
    assert page.status == PageStatus.READY
    assert len(engine.list_pages()) == 1


async def test_engine_create_page_rejects_unsupported_scheme():
    """javascript:/data:/file: URLs must never reach playwright navigation."""
    engine = _available_engine()
    page = await engine.create_page("javascript:alert(1)//")
    assert page.status == PageStatus.ERROR
    assert page.title == "(unsupported scheme)"
    assert "javascript" in (page.error or "").lower()
    assert len(engine.list_pages()) == 0

    page2 = await engine.create_page("file:///etc/passwd")
    assert page2.status == PageStatus.ERROR


async def test_engine_navigate_rejects_unsupported_scheme():
    engine, _ = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.NAVIGATE, url="javascript:alert(document.cookie)"
    )
    assert result.success is False
    assert "javascript" in (result.error or "").lower()


async def test_engine_create_multiple_pages():
    engine = _available_engine()

    async def fake_new_page(*args):
        return FakePage("https://b.com")

    engine._context = type("Ctx", (), {"new_page": fake_new_page})()
    await engine.create_page("https://a.com")
    await engine.create_page("https://b.com")
    assert len(engine.list_pages()) == 2


async def test_engine_close_page():
    engine, _ = _ready_engine()
    pid = list(engine._pages)[0]
    assert engine.close_page(pid) is True
    assert len(engine.list_pages()) == 0
    assert engine.close_page("nonexistent") is False


def test_engine_set_active_page():
    engine, _ = _ready_engine()
    first = next(iter(engine._pages))
    assert engine.set_active_page(first) is True
    assert engine._active_page_id == first


def test_engine_get_page():
    engine, _ = _ready_engine()
    pid = next(iter(engine._pages))
    retrieved = engine.get_page(pid)
    assert retrieved is not None
    assert engine.get_page("nonexistent") is None


# ---- Engine: actions ----


async def test_engine_navigate():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.NAVIGATE, url="https://new.com")
    assert result.success is True
    assert result.data["url"] == "https://new.com"


async def test_engine_navigate_no_url():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.NAVIGATE)
    assert result.success is False


async def test_engine_click():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.CLICK, selector="#button")
    assert result.success is True
    assert result.data["selector"] == "#button"
    assert fake.clicked == ["#button"]


async def test_engine_click_no_selector():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.CLICK)
    assert result.success is False


async def test_engine_type():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.TYPE, selector="#input", text="hello"
    )
    assert result.success is True
    assert result.data["text"] == "hello"
    assert fake.filled == [("#input", "hello")]


async def test_engine_screenshot():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.SCREENSHOT)
    assert result.success is True
    assert result.data["format"] == "png"
    assert result.data["size_bytes"] > 0
    assert "path" in result.data


async def test_engine_extract_text():
    engine, fake = _ready_engine()
    fake.text = "Hello world"
    result = await engine.execute_action(BrowserAction.EXTRACT_TEXT)
    assert result.success is True
    assert result.data["text"] == "Hello world"
    assert result.data["length"] == 11


async def test_engine_extract_html():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.EXTRACT_HTML)
    assert result.success is True
    assert "<p>fake</p>" in result.data["html"]


async def test_engine_scroll():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SCROLL, direction="down", amount=300
    )
    assert result.success is True
    assert result.data["amount"] == 300
    assert fake.wheel_delta == 300


async def test_engine_wait():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.WAIT, seconds=0)
    assert result.success is True
    assert result.data["waited_seconds"] == 0


async def test_engine_scroll_to():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SCROLL_TO, selector="#section", block="start"
    )
    assert result.success is True
    assert result.data["selector"] == "#section"
    assert result.data["block"] == "start"
    assert getattr(fake, "scrolled_into_view", False) is True


async def test_engine_scroll_to_no_selector():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.SCROLL_TO)
    assert result.success is False
    assert "selector" in result.error


async def test_engine_press_key_global():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.PRESS, key="Enter")
    assert result.success is True
    assert result.data["key"] == "Enter"
    assert fake.pressed_keys == ["Enter"]


async def test_engine_press_key_on_selector():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.PRESS, key="Enter", selector="#search"
    )
    assert result.success is True
    assert fake.pressed == [("#search", "Enter")]


async def test_engine_press_no_key():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.PRESS)
    assert result.success is False
    assert "key" in result.error


async def test_engine_select_by_value():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SELECT, selector="#country", value="CA"
    )
    assert result.success is True
    assert result.data["selected"] is True
    assert result.data["value"] == "CA"
    assert fake.selected == [("#country", {"value": "CA"})]


async def test_engine_select_by_label():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SELECT, selector="#plan", label="Pro"
    )
    assert result.success is True
    assert result.data["label"] == "Pro"
    assert fake.selected == [("#plan", {"label": "Pro"})]


async def test_engine_select_by_index():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SELECT, selector="#region", index=2
    )
    assert result.success is True
    assert result.data["index"] == 2
    assert fake.selected == [("#region", {"index": 2})]


async def test_engine_select_missing_selector():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.SELECT, value="CA")
    assert result.success is False
    assert "selector" in result.error


async def test_engine_select_missing_option():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.SELECT, selector="#country")
    assert result.success is False
    assert "value" in result.error


async def test_engine_select_multi_values():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.SELECT, selector="#tags", values=["a", "b"]
    )
    assert result.success is True
    assert result.data["values"] == ["a", "b"]
    assert fake.selected == [("#tags", {"values": ["a", "b"]})]


async def test_engine_drag():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.DRAG, source="#item", target="#zone"
    )
    assert result.success is True
    assert fake.dragged == [("#item", "#zone")]


async def test_engine_drag_missing_args():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.DRAG, source="#item")
    assert result.success is False
    assert "source" in result.error


async def test_engine_cookies_get():
    engine, fake = _ready_engine()
    ctx = FakeContext()
    ctx.cookies_store = [{"name": "sid", "value": "abc", "domain": "example.com"}]
    engine._context = ctx
    result = await engine.execute_action(BrowserAction.COOKIES)
    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["cookies"][0]["name"] == "sid"


async def test_engine_cookies_set():
    engine, fake = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    req = [{"name": "a", "value": "1", "domain": "example.com"}]
    result = await engine.execute_action(BrowserAction.COOKIES, subcommand="set", cookies=req)
    assert result.success is True
    assert result.data["cookie_count"] == 1
    assert ctx.added == [req]


async def test_engine_cookies_clear():
    engine, fake = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    result = await engine.execute_action(BrowserAction.COOKIES, subcommand="clear")
    assert result.success is True
    assert result.data["cleared"] is True
    assert ctx.cleared is True


async def test_engine_cookies_no_context():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.COOKIES)
    assert result.success is False
    assert "context" in result.error


async def test_engine_cookies_set_missing_payload():
    engine, _ = _ready_engine()
    engine._context = FakeContext()
    result = await engine.execute_action(BrowserAction.COOKIES, subcommand="set")
    assert result.success is False
    assert "cookies" in result.error


async def test_engine_storage_set():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.STORAGE, subcommand="set", key="theme", value="dark"
    )
    assert result.success is True
    assert result.data["key"] == "theme"
    assert "localStorage.setItem" in fake.evaluated


async def test_engine_storage_get_all():
    engine, fake = _ready_engine()
    fake.inspect_result = {"theme": "dark", "lang": "en"}
    result = await engine.execute_action(BrowserAction.STORAGE)
    assert result.success is True
    assert result.data["entries"] == {"theme": "dark", "lang": "en"}
    assert result.data["count"] == 2


async def test_engine_storage_get_key():
    engine, fake = _ready_engine()
    fake.inspect_result = "light"
    result = await engine.execute_action(BrowserAction.STORAGE, subcommand="get", key="theme")
    assert result.success is True
    assert result.data["value"] == "light"


async def test_engine_storage_clear():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.STORAGE, subcommand="clear")
    assert result.success is True
    assert result.data["cleared"] is True
    assert "localStorage.clear" in fake.evaluated


async def test_engine_network_returns_tap():
    engine, _ = _ready_engine()
    pid = next(iter(engine._pages))
    engine._responses[pid] = [
        {"url": "https://a.com", "status": 200, "method": "GET", "resource_type": "document"}
    ]
    result = await engine.execute_action(BrowserAction.NETWORK)
    assert result.success is True
    assert result.data["count"] == 1
    assert result.data["entries"][0]["status"] == 200


async def test_engine_network_limit():
    engine, _ = _ready_engine()
    pid = next(iter(engine._pages))
    engine._responses[pid] = [{"url": f"https://x{i}.com"} for i in range(5)]
    result = await engine.execute_action(BrowserAction.NETWORK, limit=2)
    assert result.success is True
    assert result.data["count"] == 2


async def test_engine_block_url():
    engine, fake = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    result = await engine.execute_action(BrowserAction.BLOCK_URL, glob="*ads*")
    assert result.success is True
    assert result.data["glob"] == "*ads*"
    assert ctx.routes == [("*ads*", engine._abort_request)]


async def test_engine_block_url_no_glob():
    engine, _ = _ready_engine()
    engine._context = FakeContext()
    result = await engine.execute_action(BrowserAction.BLOCK_URL)
    assert result.success is False
    assert "glob" in result.error


async def test_engine_download():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.DOWNLOAD, selector="#dl")
    assert result.success is True
    assert result.data["filename"] == "report.pdf"
    assert fake.clicked == ["#dl"]
    assert fake.download.saved_to is not None


async def test_engine_download_missing_selector():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.DOWNLOAD)
    assert result.success is False
    assert "selector" in result.error


async def test_engine_upload():
    engine, fake = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.UPLOAD, selector="#file", paths=[r"C:\a.txt", r"C:\b.txt"]
    )
    assert result.success is True
    assert fake.uploaded == [("#file", [r"C:\a.txt", r"C:\b.txt"])]


async def test_engine_upload_missing_args():
    engine, _ = _ready_engine()
    no_sel = await engine.execute_action(BrowserAction.UPLOAD, paths=[r"C:\a.txt"])
    assert no_sel.success is False
    no_paths = await engine.execute_action(BrowserAction.UPLOAD, selector="#file")
    assert no_paths.success is False
    assert "paths" in no_paths.error


async def test_engine_console():
    engine, _ = _ready_engine()
    pid = next(iter(engine._pages))
    engine._console_log[pid] = [
        {"type": "log", "text": "hello", "count": 1},
        {"type": "error", "text": "boom", "count": 3},
    ]
    result = await engine.execute_action(BrowserAction.CONSOLE)
    assert result.success is True
    assert result.data["count"] == 2
    assert result.data["entries"][0]["text"] == "hello"
    limited = await engine.execute_action(BrowserAction.CONSOLE, limit=1)
    assert limited.data["count"] == 1
    assert limited.data["entries"][0]["type"] == "error"


async def test_engine_console_empty():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.CONSOLE)
    assert result.success is True
    assert result.data["count"] == 0
    assert result.data["entries"] == []


async def test_engine_console_page_handlers_attached():
    engine, fake = _ready_engine()
    engine._attach_page_handlers(fake)
    assert "console" in fake.handlers
    assert "pageerror" in fake.handlers
    msg_type = type("Msg", (), {})
    msg = msg_type()
    msg.type = "log"
    msg.text = "ping"
    fake.handlers["console"](msg)
    err = msg_type()
    err.message = "uncaught"
    fake.handlers["pageerror"](err)
    pid = next(iter(engine._pages))
    log = engine._console_log[pid]
    assert log[0]["text"] == "ping"
    assert log[1]["type"] == "pageerror"
    assert log[1]["text"] == "uncaught"


async def test_engine_console_dedupe_and_bounds():
    engine, fake = _ready_engine()
    msg_type = type("Msg", (), {})
    msg = msg_type()
    msg.type = "log"
    msg.text = "spam"
    for _ in range(5):
        engine._on_console(fake, msg)
    pid = next(iter(engine._pages))
    log = engine._console_log[pid]
    assert log == [{"type": "log", "text": "spam", "count": 5}]


async def test_engine_downloads(tmp_path):
    engine, _ = _ready_engine()
    engine._download_dir = tmp_path
    (tmp_path / "report.pdf").write_bytes(b"abc")
    (tmp_path / "notes.txt").write_bytes(b"x")
    result = await engine.execute_action(BrowserAction.DOWNLOADS)
    assert result.success is True
    names = {f["name"] for f in result.data["files"]}
    assert names == {"report.pdf", "notes.txt"}
    limited = await engine.execute_action(BrowserAction.DOWNLOADS, limit=1)
    assert limited.data["count"] == 1


async def test_engine_downloads_empty(tmp_path):
    engine, _ = _ready_engine()
    engine._download_dir = tmp_path
    result = await engine.execute_action(BrowserAction.DOWNLOADS)
    assert result.success is True
    assert result.data["count"] == 0
    assert result.data["files"] == []


async def test_engine_clear_downloads(tmp_path):
    engine, _ = _ready_engine()
    engine._download_dir = tmp_path
    (tmp_path / "report.pdf").write_bytes(b"abc")
    (tmp_path / "old.txt").write_bytes(b"x")
    result = await engine.execute_action(BrowserAction.CLEAR_DOWNLOADS)
    assert result.success is True
    assert result.data["removed"] == 2
    assert result.data["failed"] == 0
    remaining = await engine.execute_action(BrowserAction.DOWNLOADS)
    assert remaining.data["count"] == 0


async def test_engine_clear_downloads_empty(tmp_path):
    engine, _ = _ready_engine()
    engine._download_dir = tmp_path
    result = await engine.execute_action(BrowserAction.CLEAR_DOWNLOADS)
    assert result.success is True
    assert result.data["removed"] == 0


async def test_engine_redirect_url():
    engine, _ = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    result = await engine.execute_action(
        BrowserAction.REDIRECT_URL, glob="*old*", redirect="https://new.com"
    )
    assert result.success is True
    assert result.data["glob"] == "*old*"
    assert result.data["redirect"] == "https://new.com"
    assert len(ctx.routes) == 1
    assert ctx.routes[0][0] == "*old*"


async def test_engine_redirect_url_missing_args():
    engine, _ = _ready_engine()
    engine._context = FakeContext()
    no_glob = await engine.execute_action(BrowserAction.REDIRECT_URL, redirect="https://new.com")
    assert no_glob.success is False
    no_target = await engine.execute_action(BrowserAction.REDIRECT_URL, glob="*old*")
    assert no_target.success is False


async def test_engine_redirect_url_no_context():
    engine, _ = _ready_engine()
    result = await engine.execute_action(
        BrowserAction.REDIRECT_URL, glob="*old*", redirect="https://new.com"
    )
    assert result.success is False
    assert "context" in result.error


async def test_engine_snapshot(tmp_path):
    engine, _ = _ready_engine()
    engine._session_dir = tmp_path
    engine._context = FakeContext()
    engine._context.cookies_store = [{"name": "sid", "value": "1"}]
    result = await engine.execute_action(BrowserAction.SNAPSHOT, name="my session")
    assert result.success is True
    assert result.data["name"] == "my_session"
    assert result.data["origins"] == 1
    assert (tmp_path / "my_session.json").is_file()
    saved = (tmp_path / "my_session.json").read_text(encoding="utf-8")
    assert "sid" in saved


async def test_engine_snapshot_no_context():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.SNAPSHOT, name="session")
    assert result.success is False
    assert "context" in result.error


async def test_engine_restore(tmp_path):
    engine, _ = _ready_engine()
    engine._session_dir = tmp_path
    (tmp_path / "session.json").write_text(
        '{"cookies": [{"name": "sid"}], "origins": []}', encoding="utf-8"
    )
    engine._browser = FakeBrowser()
    engine._blocked_globs.append("*ads*")
    pid = next(iter(engine._pages))
    result = await engine.execute_action(BrowserAction.RESTORE, name="session")
    assert result.success is True
    assert result.data["restored"] is True
    assert engine._browser.new_context_kwargs == [str(tmp_path / "session.json")]
    assert engine._pages.get(pid) is None
    assert engine._blocked_globs == []
    assert engine._context is not None


async def test_engine_restore_missing_snapshot(tmp_path):
    engine, _ = _ready_engine()
    engine._session_dir = tmp_path
    engine._browser = FakeBrowser()
    result = await engine.execute_action(BrowserAction.RESTORE, name="nope")
    assert result.success is False
    assert "No snapshot" in result.error


async def test_engine_restore_no_browser():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.RESTORE, name="session")
    assert result.success is False
    assert "session to restore" in result.error


async def test_engine_status_info():
    engine, _ = _ready_engine()
    info = engine.status_info()
    assert info["available"] is True
    assert info["pages"] == 1
    assert info["mode"] == "headless"
    assert info["blocked_globs"] == 0
    assert info["download_dir"]
    assert info["session_dir"]


async def test_engine_status_info_headed():
    engine, _ = _ready_engine()
    engine._launch_opts["headless"] = False
    info = engine.status_info()
    assert info["mode"] == "headed"
    assert info["headless"] is False


async def test_engine_dirs_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("BERU_BROWSER_SESSION_DIR", str(tmp_path / "sess"))
    monkeypatch.setenv("BERU_BROWSER_DOWNLOADS_DIR", str(tmp_path / "dl"))
    monkeypatch.setenv("BERU_BROWSER_SCREENSHOT_DIR", str(tmp_path / "shot"))
    engine = BrowserEngine()
    assert engine._session_dir == tmp_path / "sess"
    assert engine._download_dir == tmp_path / "dl"
    assert engine._screenshot_dir == tmp_path / "shot"


async def test_engine_fulfill():
    engine, _ = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    result = await engine.execute_action(
        BrowserAction.FULFILL, glob="*/api/data*", body='{"ok": true}',
        content_type="application/json", status_code=201,
    )
    assert result.success is True
    assert result.data["glob"] == "*/api/data*"
    assert result.data["status"] == 201
    assert result.data["body_length"] == 12
    assert len(ctx.routes) == 1


async def test_engine_fulfill_with_headers():
    engine, _ = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    result = await engine.execute_action(
        BrowserAction.FULFILL, glob="*/api/data*", body="{}",
        content_type="application/json", headers={"X-Rate-Limit": "100"},
    )
    assert result.success is True
    assert result.data["headers"] == {"X-Rate-Limit": "100"}
    route = FakeRoute()
    await ctx.routes[0][1](route)
    assert route.fulfilled["headers"] == {"X-Rate-Limit": "100"}


async def test_engine_fulfill_handler_responds():
    engine, _ = _ready_engine()
    ctx = FakeContext()
    engine._context = ctx
    await engine.execute_action(
        BrowserAction.FULFILL, glob="*/api/data*", body="x",
        content_type="text/plain", status_code=418,
    )
    route = FakeRoute()
    await ctx.routes[0][1](route)
    assert route.fulfilled == {
        "status": 418, "body": "x", "content_type": "text/plain"
    }


async def test_engine_fulfill_missing_glob():
    engine, _ = _ready_engine()
    engine._context = FakeContext()
    result = await engine.execute_action(BrowserAction.FULFILL)
    assert result.success is False
    assert "glob" in result.error


async def test_engine_fulfill_no_context():
    engine, _ = _ready_engine()
    result = await engine.execute_action(BrowserAction.FULFILL, glob="*")
    assert result.success is False
    assert "context" in result.error


async def test_engine_launch(monkeypatch):
    engine = _available_engine()

    async def fake_ensure() -> bool:
        engine._available = True
        return True

    monkeypatch.setattr(engine, "_ensure_browser", fake_ensure)
    result = await engine.execute_action(
        BrowserAction.LAUNCH, headless=False, width=1024, height=768,
        user_agent="TestUA", locale="en-US",
    )
    assert result.success is True
    assert result.data["headless"] is False
    assert result.data["viewport"] == {"width": 1024, "height": 768}
    assert result.data["user_agent"] == "TestUA"
    assert result.data["locale"] == "en-US"
    assert engine._launch_opts["headless"] is False
    assert engine._context_opts["viewport"] == {"width": 1024, "height": 768}


async def test_engine_launch_only_headless_toggle(monkeypatch):
    engine = _available_engine()

    async def fake_ensure() -> bool:
        engine._available = True
        return True

    monkeypatch.setattr(engine, "_ensure_browser", fake_ensure)
    result = await engine.execute_action(BrowserAction.LAUNCH, headless=True)
    assert result.success is True
    assert result.data["headless"] is True
    assert result.data["viewport"] is None


async def test_engine_launch_device_opts(monkeypatch):
    engine = _available_engine()

    async def fake_ensure() -> bool:
        engine._available = True
        return True

    monkeypatch.setattr(engine, "_ensure_browser", fake_ensure)
    result = await engine.execute_action(
        BrowserAction.LAUNCH, width=390, height=844,
        device_scale_factor=3, is_mobile=True, has_touch=True,
        timezone_id="Asia/Kolkata",
    )
    assert result.success is True
    assert result.data["viewport"] == {"width": 390, "height": 844}
    assert result.data["device_scale_factor"] == 3.0
    assert result.data["is_mobile"] is True
    assert result.data["has_touch"] is True
    assert result.data["timezone_id"] == "Asia/Kolkata"
    assert engine._context_opts["device_scale_factor"] == 3.0
    assert engine._context_opts["is_mobile"] is True


async def test_engine_console_captures_location():
    engine, fake = _ready_engine()
    msg_type = type("Msg", (), {})
    msg = msg_type()
    msg.type = "warning"
    msg.text = "deprecated"
    msg.location = {"url": "https://x/app.js", "line": 5, "column": 2}
    engine._on_console(fake, msg)
    pid = next(iter(engine._pages))
    entry = engine._console_log[pid][0]
    assert entry["location"] == {"url": "https://x/app.js", "line": 5, "column": 2}


async def test_engine_click_into_frame_by_index():
    engine, fake = _ready_engine()
    frame = FakePage(url="https://a.com/frame", name="editor")
    fake.frames = [frame]
    result = await engine.execute_action(
        BrowserAction.CLICK, selector="#btn", frame="0"
    )
    assert result.success is True
    assert frame.clicked == ["#btn"]
    assert fake.clicked == []


async def test_engine_click_into_frame_by_name_and_url():
    engine, fake = _ready_engine()
    f_editor = FakePage(url="https://a.com/edit", name="editor")
    f_help = FakePage(url="https://a.com/help", name="help")
    fake.frames = [f_editor, f_help]
    by_name = await engine.execute_action(
        BrowserAction.CLICK, selector="#b", frame="editor"
    )
    by_url = await engine.execute_action(
        BrowserAction.TYPE, selector="#t", text="x", frame="https://a.com/help"
    )
    assert by_name.success is True
    assert f_editor.clicked == ["#b"]
    assert by_url.success is True
    assert f_help.filled == [("#t", "x")]


async def test_engine_frame_no_match():
    engine, fake = _ready_engine()
    fake.frames = [FakePage(url="https://a.com/edit", name="editor")]
    result = await engine.execute_action(
        BrowserAction.CLICK, selector="#b", frame="nope"
    )
    assert result.success is False
    assert "No frame matches" in result.error


async def test_engine_extract_text_into_frame():
    engine, fake = _ready_engine()
    frame = FakePage(url="https://a.com/frame")
    frame.text = "Frame text"
    fake.frames = [frame]
    result = await engine.execute_action(
        BrowserAction.EXTRACT_TEXT, selector="body", frame="0"
    )
    assert result.success is True
    assert result.data["text"] == "Frame text"


async def test_engine_page_info_lists_frames():
    engine, fake = _ready_engine()
    fake.frames = [
        FakePage(url="https://a.com/main"),
        FakePage(url="https://a.com/edit", name="editor"),
    ]
    result = await engine.execute_action(BrowserAction.PAGE_INFO)
    assert result.success is True
    assert result.data["frame_count"] == 2
    assert result.data["frames"][1]["name"] == "editor"


async def test_engine_screenshot_full_page():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.SCREENSHOT, full_page=True)
    assert result.success is True
    assert result.data["full_page"] is True
    assert fake.screen_args.get("full_page") is True


async def test_engine_screenshot_viewport_default():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.SCREENSHOT)
    assert result.success is True
    assert result.data["full_page"] is False
    assert "full_page" not in fake.screen_args


async def test_engine_page_info_structured_inspection():
    engine, fake = _ready_engine()
    fake.url = "https://info.com"
    fake.inspect_result = {
        "visible_text": "Hello BERU",
        "visible_text_truncated": False,
        "links": [{"text": "Docs", "href": "https://docs.example"}],
        "links_truncated": False,
        "interactive": [
            {
                "tag": "button", "type": "button", "id": "go",
                "name": None, "placeholder": None,
                "text": "Go", "selector": "#go",
            }
        ],
        "interactive_truncated": False,
    }
    result = await engine.execute_action(BrowserAction.PAGE_INFO)
    assert result.success is True
    assert result.data["url"] == "https://info.com"
    assert result.data["title"] == "Fake Title"
    assert result.data["visible_text"] == "Hello BERU"
    assert result.data["links"][0]["href"] == "https://docs.example"
    assert result.data["interactive"][0]["selector"] == "#go"
    assert result.data["interactive_truncated"] is False


async def test_engine_page_info_falls_back_on_eval_error():
    engine, fake = _ready_engine()
    fake.url = "https://info.com"

    async def _boom(js: str, *args):
        raise RuntimeError("boom")

    fake.evaluate = _boom  # type: ignore[assignment]
    result = await engine.execute_action(BrowserAction.PAGE_INFO)
    assert result.success is True
    assert result.data["url"] == "https://info.com"
    assert result.data["title"] == "Fake Title"
    assert "visible_text" not in result.data


async def test_engine_close_browser():
    engine = _available_engine()

    class _Closeable:
        async def close(self) -> None:
            self.closed = True

    class _PwFake:
        async def stop(self) -> None:
            self.stopped = True

    fake_ctx = _Closeable()
    fake_browser = _Closeable()
    engine._context = fake_ctx
    engine._browser = fake_browser
    fake_pw = _PwFake()
    engine._pw = fake_pw
    page = BrowserPage(url="https://example.com")
    engine._pages[page.id] = page

    closed = await engine.close_browser()
    assert closed is True
    assert fake_ctx.closed is True
    assert fake_browser.closed is True
    assert fake_pw.stopped is True
    assert engine._pw is None
    assert engine._available is None
    assert engine.list_pages() == []
    assert engine._browser is None
    assert engine._context is None


async def test_engine_close_browser_idempotent():
    engine = BrowserEngine()
    closed = await engine.close_browser()
    assert closed is False
    assert engine._available is None


async def test_engine_on_new_page_tracks_popup():
    engine = BrowserEngine()
    engine._available = True
    popup = FakePage(url="https://popup.com")
    engine._on_new_page(popup)
    pages = engine.list_pages()
    assert len(pages) == 1
    assert engine._active_page_id == pages[0].id
    assert engine._pw_pages[pages[0].id] is popup


async def test_engine_on_response_taps_log():
    engine, fake = _ready_engine()
    pid = next(iter(engine._pages))
    resp = _FakeResponse(
        fake, url="https://a.com/x.js", status=404, method="GET",
        resource_type="script",
    )
    engine._on_response(resp)
    entries = engine._responses[pid]
    assert entries[-1]["url"] == "https://a.com/x.js"
    assert entries[-1]["status"] == 404
    assert entries[-1]["resource_type"] == "script"
    assert entries[-1]["size"] is None
    assert entries[-1]["timing_ms"] is None


async def test_engine_on_response_captures_size_and_timing():
    engine, fake = _ready_engine()
    pid = next(iter(engine._pages))
    timing = {"requestStart": 100.0, "responseEnd": 352.6}
    resp = _FakeResponse(
        fake, url="https://a.com/data.json", status=200, method="GET",
        resource_type="xhr",
        headers={"content-length": "4128"},
        timing=timing,
    )
    engine._on_response(resp)
    entry = engine._responses[pid][-1]
    assert entry["size"] == 4128
    assert entry["timing_ms"] == 253


async def test_engine_on_response_ignores_unknown_page():
    engine, fake = _ready_engine()
    other = FakePage(url="https://other.com")
    resp = _FakeResponse(
        other, "https://a.com", 200, "GET", "document",
    )
    engine._on_response(resp)
    assert engine._responses == {}


async def test_engine_response_log_bounded():
    from backend.engines.browser import _NETWORK_LOG_MAX

    engine, fake = _ready_engine()
    pid = next(iter(engine._pages))
    for i in range(_NETWORK_LOG_MAX + 5):
        resp = _FakeResponse(
            fake, f"https://a.com/{i}", 200, "GET", "xhr",
        )
        engine._on_response(resp)
    assert len(engine._responses[pid]) == _NETWORK_LOG_MAX


async def test_engine_page_info():
    engine, fake = _ready_engine()
    fake.url = "https://info.com"
    result = await engine.execute_action(BrowserAction.PAGE_INFO)
    assert result.success is True
    assert result.data["url"] == "https://info.com"
    assert result.data["title"] == "Fake Title"


async def test_engine_back_forward():
    engine, fake = _ready_engine()
    pid = next(iter(engine._pages))
    engine._history[pid] = ["https://a.com", "https://b.com", "https://c.com"]
    engine._history_index[pid] = 2

    result = await engine.execute_action(BrowserAction.BACK)
    assert result.data["url"] == "https://b.com"

    result = await engine.execute_action(BrowserAction.FORWARD)
    assert result.data["url"] == "https://c.com"


async def test_engine_refresh():
    engine, fake = _ready_engine()
    result = await engine.execute_action(BrowserAction.REFRESH)
    assert result.success is True
    assert result.data["refreshed"] is True


async def test_engine_no_active_page():
    engine = BrowserEngine()
    engine._available = True
    result = await engine.execute_action(BrowserAction.NAVIGATE, url="https://x.com")
    assert result.success is False
    assert "No active page" in result.error


async def test_engine_unknown_action():
    engine, _ = _ready_engine()
    result = await engine.execute_action("not_an_action")  # type: ignore[arg-type]
    assert result.success is False
    assert "Unknown action" in result.error


# ---- Browser tool tests ----
#
# Tools delegate to the engine. When the engine is not available the tool
# reports failure; here we force availability and drive a real action through
# the fake page.


def _primed_tool_engine() -> tuple[BrowserEngine, FakePage]:
    return _ready_engine()


async def test_navigate_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserNavigateTool()
    result = await tool.run(url="https://new.com")
    assert result.ok is True


async def test_navigate_tool_missing_url(monkeypatch):
    tool = BrowserNavigateTool()
    result = await tool.run()
    assert result.ok is False
    assert "url" in result.error


async def test_navigate_tool_unavailable(monkeypatch):
    from backend.tools import browser as browser_mod

    engine = BrowserEngine()
    engine._available = False
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserNavigateTool()
    result = await tool.run(url="https://x.com")
    assert result.ok is False
    assert "not available" in result.error


async def test_click_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserClickTool()
    result = await tool.run(selector="#btn")
    assert result.ok is True


async def test_click_tool_missing_selector(monkeypatch):
    tool = BrowserClickTool()
    result = await tool.run()
    assert result.ok is False
    assert "selector" in result.error


async def test_type_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserTypeTool()
    result = await tool.run(selector="#input", text="hello")
    assert result.ok is True


async def test_screenshot_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserScreenshotTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["data"]["format"] == "png"


async def test_extract_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    fake.text = "Extracted content"
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserExtractTool()
    result = await tool.run(selector=".content")
    assert result.ok is True
    assert result.output["data"]["text"] == "Extracted content"


async def test_page_info_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import BrowserPageInfoTool

    engine, fake = _primed_tool_engine()
    fake.url = "https://info.com"
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserPageInfoTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["data"]["title"] == "Fake Title"
    assert result.output["data"]["url"] == "https://info.com"


async def test_scroll_tool_wheel(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import BrowserScrollTool

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserScrollTool()
    result = await tool.run(direction="up", amount=200)
    assert result.ok is True
    assert fake.wheel_delta == -200


async def test_scroll_tool_bad_direction(monkeypatch):
    from backend.tools.browser import BrowserScrollTool

    tool = BrowserScrollTool()
    result = await tool.run(direction="sideways")
    assert result.ok is False


async def test_scroll_tool_to_selector(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import BrowserScrollTool

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserScrollTool()
    result = await tool.run(selector="#section")
    assert result.ok is True
    assert result.output["data"]["selector"] == "#section"


async def test_press_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import BrowserPressTool

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserPressTool()
    result = await tool.run(key="Enter")
    assert result.ok is True
    assert fake.pressed_keys == ["Enter"]


async def test_press_tool_missing_key():
    from backend.tools.browser import BrowserPressTool

    tool = BrowserPressTool()
    result = await tool.run()
    assert result.ok is False
    assert "key" in result.error


async def test_select_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserSelectTool()
    result = await tool.run(selector="#country", value="CA")
    assert result.ok is True
    assert result.output["data"]["value"] == "CA"
    assert fake.selected == [("#country", {"value": "CA"})]


async def test_select_tool_label(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserSelectTool()
    result = await tool.run(selector="#plan", label="Pro")
    assert result.ok is True
    assert fake.selected == [("#plan", {"label": "Pro"})]


async def test_select_tool_index(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserSelectTool()
    result = await tool.run(selector="#region", index=1)
    assert result.ok is True
    assert fake.selected == [("#region", {"index": 1})]


async def test_select_tool_missing_selector():
    tool = BrowserSelectTool()
    result = await tool.run(value="CA")
    assert result.ok is False
    assert "selector" in result.error


async def test_select_tool_multi_values(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserSelectTool()
    result = await tool.run(selector="#tags", values=["a", "b"])
    assert result.ok is True
    assert fake.selected == [("#tags", {"values": ["a", "b"]})]


async def test_drag_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserDragTool()
    result = await tool.run(source="#item", target="#zone")
    assert result.ok is True
    assert fake.dragged == [("#item", "#zone")]


async def test_drag_tool_missing_args():
    tool = BrowserDragTool()
    result = await tool.run(source="#item")
    assert result.ok is False


async def test_cookies_tool_get(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    ctx = FakeContext()
    ctx.cookies_store = [{"name": "sid", "value": "1"}]
    engine._context = ctx
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserCookiesTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["data"]["count"] == 1


async def test_cookies_tool_set(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    ctx = FakeContext()
    engine._context = ctx
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserCookiesTool()
    req = [{"name": "a", "value": "1"}]
    result = await tool.run(subcommand="set", cookies=req)
    assert result.ok is True
    assert ctx.added == [req]


async def test_cookies_tool_clear(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    ctx = FakeContext()
    engine._context = ctx
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserCookiesTool()
    result = await tool.run(subcommand="clear")
    assert result.ok is True
    assert ctx.cleared is True


async def test_storage_tool_get(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    fake.inspect_result = {"theme": "dark"}
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserStorageTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["data"]["entries"] == {"theme": "dark"}


async def test_storage_tool_set(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserStorageTool()
    result = await tool.run(subcommand="set", key="theme", value="dark")
    assert result.ok is True
    assert "localStorage.setItem" in fake.evaluated


async def test_network_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    engine._responses[next(iter(engine._pages))] = [
        {"url": "https://a.com", "status": 200}
    ]
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserNetworkTool()
    result = await tool.run(limit=5)
    assert result.ok is True
    assert result.output["data"]["count"] == 1


async def test_block_url_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    ctx = FakeContext()
    engine._context = ctx
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserBlockTool()
    result = await tool.run(glob="*ads*")
    assert result.ok is True
    assert ctx.routes[0][0] == "*ads*"


async def test_block_url_tool_missing_glob():
    tool = BrowserBlockTool()
    result = await tool.run()
    assert result.ok is False


async def test_download_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserDownloadTool()
    result = await tool.run(selector="#dl")
    assert result.ok is True
    assert result.output["data"]["filename"] == "report.pdf"


async def test_download_tool_missing_selector():
    tool = BrowserDownloadTool()
    result = await tool.run()
    assert result.ok is False


async def test_upload_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserUploadTool()
    result = await tool.run(selector="#file", paths=[r"C:\a.txt"])
    assert result.ok is True
    assert fake.uploaded == [("#file", [r"C:\a.txt"])]


async def test_upload_tool_missing_args():
    tool = BrowserUploadTool()
    no_sel = await tool.run(paths=["x"])
    assert no_sel.ok is False
    no_paths = await tool.run(selector="#file")
    assert no_paths.ok is False


async def test_screenshot_tool_full_page(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserScreenshotTool()
    result = await tool.run(full_page=True)
    assert result.ok is True
    assert result.output["data"]["full_page"] is True
    assert fake.screen_args.get("full_page") is True


async def test_close_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine = BrowserEngine()

    class _Closeable:
        async def close(self) -> None:
            pass

    engine._available = True
    engine._context = _Closeable()
    engine._browser = _Closeable()
    monkeypatch.setattr(engine, "ensure_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserCloseTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["closed"] is True
    assert result.output["pages"] == 0
    assert engine.list_pages() == []


async def test_close_tool_idempotent(monkeypatch):
    from backend.tools import browser as browser_mod

    engine = BrowserEngine()
    monkeypatch.setattr(engine, "ensure_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserCloseTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["closed"] is False
    assert engine._available is None


async def test_close_tool_requires_confirmation():
    from backend.tools.browser import BrowserCloseTool

    tool = BrowserCloseTool()
    assert tool.requires_confirmation is True
    cr = ToolResult.confirmation_required("browser_close")
    assert cr.ok is False
    assert cr.output == {"confirmation_required": True}


async def test_select_tool_write_permissions():
    assert BrowserSelectTool().permissions == ["write"]
    assert BrowserCloseTool().permissions == ["write"]


async def test_back_forward_refresh_tools_success(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import (
        BrowserBackTool,
        BrowserForwardTool,
        BrowserRefreshTool,
    )

    engine, fake = _primed_tool_engine()
    pid = next(iter(engine._pages))
    engine._history[pid] = ["https://a.com", "https://b.com"]
    engine._history_index[pid] = 1
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)

    back = await BrowserBackTool().run()
    assert back.ok is True
    forward = await BrowserForwardTool().run()
    assert forward.ok is True
    refresh = await BrowserRefreshTool().run()
    assert refresh.ok is True


async def test_refresh_tool_uses_engine(monkeypatch):
    from backend.tools import browser as browser_mod
    from backend.tools.browser import BrowserRefreshTool

    engine, fake = _primed_tool_engine()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    tool = BrowserRefreshTool()
    result = await tool.run()
    assert result.ok is True
    assert result.output["data"]["refreshed"] is True


async def test_console_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    pid = next(iter(engine._pages))
    engine._console_log[pid] = [
        {"type": "log", "text": "hi", "count": 1},
        {"type": "error", "text": "boom", "count": 1},
    ]
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserConsoleTool().run()
    assert result.ok is True
    assert result.output["data"]["count"] == 2
    limited = await BrowserConsoleTool().run(limit=1)
    assert limited.ok is True
    assert limited.output["data"]["count"] == 1
    assert limited.output["data"]["entries"][0]["type"] == "error"


async def test_console_tool_unavailable(monkeypatch):
    from backend.tools import browser as browser_mod

    engine = BrowserEngine()
    engine._available = False
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserConsoleTool().run()
    assert result.ok is False


async def test_downloads_tool_success(monkeypatch, tmp_path):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._download_dir = tmp_path
    (tmp_path / "report.pdf").write_bytes(b"abc")
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserDownloadsTool().run()
    assert result.ok is True
    assert result.output["data"]["files"][0]["name"] == "report.pdf"
    limited = await BrowserDownloadsTool().run(limit=1)
    assert limited.ok is True
    assert limited.output["data"]["count"] == 1


async def test_redirect_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._context = FakeContext()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserRedirectTool().run(glob="*old*", redirect="https://new.com")
    assert result.ok is True
    assert result.output["data"]["glob"] == "*old*"


async def test_redirect_tool_missing_args():
    no_glob = await BrowserRedirectTool().run(redirect="https://new.com")
    assert no_glob.ok is False
    no_target = await BrowserRedirectTool().run(glob="*old*")
    assert no_target.ok is False


async def test_snapshot_tool_success(monkeypatch, tmp_path):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._session_dir = tmp_path
    ctx = FakeContext()
    ctx.cookies_store = [{"name": "sid", "value": "1"}]
    engine._context = ctx
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserSnapshotTool().run(name="alpha")
    assert result.ok is True
    assert result.output["data"]["name"] == "alpha"
    assert (tmp_path / "alpha.json").is_file()
    default = await BrowserSnapshotTool().run()
    assert default.ok is True
    assert default.output["data"]["name"] == "session"


async def test_restore_tool_success(monkeypatch, tmp_path):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._session_dir = tmp_path
    (tmp_path / "session.json").write_text("{}", encoding="utf-8")
    engine._browser = FakeBrowser()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserRestoreTool().run()
    assert result.ok is True
    assert result.output["data"]["restored"] is True


async def test_restore_tool_missing_snapshot(monkeypatch, tmp_path):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._session_dir = tmp_path
    engine._browser = FakeBrowser()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserRestoreTool().run(name="nope")
    assert result.ok is False
    assert "No snapshot" in result.error


async def test_fulfill_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._context = FakeContext()
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserFulfillTool().run(
        glob="*/api/*", body="{}", content_type="application/json", status_code=201,
        headers={"X-Rate-Limit": "100"},
    )
    assert result.ok is True
    assert result.output["data"]["status"] == 201
    assert result.output["data"]["body_length"] == 2
    assert result.output["data"]["headers"] == {"X-Rate-Limit": "100"}


async def test_fulfill_tool_missing_glob():
    result = await BrowserFulfillTool().run()
    assert result.ok is False
    assert "glob" in result.error


async def test_launch_tool_success(monkeypatch):
    from backend.tools import browser as browser_mod

    engine = _available_engine()

    async def fake_ensure() -> bool:
        engine._available = True
        return True

    monkeypatch.setattr(engine, "_ensure_browser", fake_ensure)
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserLaunchTool().run(headless=True, width=800, height=600)
    assert result.ok is True
    assert result.output["data"]["launched"] is True
    assert result.output["data"]["viewport"] == {"width": 800, "height": 600}


async def test_launch_tool_confirmation_required():
    assert BrowserLaunchTool().requires_confirmation is True


async def test_clear_downloads_tool_success(tmp_path, monkeypatch):
    from backend.tools import browser as browser_mod

    engine, _ = _primed_tool_engine()
    engine._context = FakeContext()
    engine._download_dir = tmp_path
    (tmp_path / "a.pdf").write_bytes(b"x")
    monkeypatch.setattr(browser_mod, "get_browser_engine", lambda: engine)
    result = await BrowserClearDownloadsTool().run()
    assert result.ok is True
    assert result.output["removed"] == 1
    assert result.output["remaining"] == 0


async def test_clear_downloads_tool_confirmation_required():
    assert BrowserClearDownloadsTool().requires_confirmation is True


# ---- Core agent browser tools registration ----


def test_core_agent_has_browser_tools():
    registry = get_agent_registry()
    core = registry.get("beru_core")
    tool_names = [t.name for t in core._tools.values()]

    assert "browser_navigate" in tool_names
    assert "browser_click" in tool_names
    assert "browser_type" in tool_names
    assert "browser_screenshot" in tool_names
    assert "browser_extract" in tool_names
    assert "browser_page_info" in tool_names
    assert "browser_scroll" in tool_names
    assert "browser_press" in tool_names
    assert "browser_select" in tool_names
    assert "browser_drag" in tool_names
    assert "browser_cookies" in tool_names
    assert "browser_storage" in tool_names
    assert "browser_network" in tool_names
    assert "browser_block_url" in tool_names
    assert "browser_download" in tool_names
    assert "browser_upload" in tool_names
    assert "browser_console" in tool_names
    assert "browser_downloads" in tool_names
    assert "browser_redirect_url" in tool_names
    assert "browser_snapshot" in tool_names
    assert "browser_restore" in tool_names
    assert "browser_fulfill" in tool_names
    assert "browser_launch" in tool_names
    assert "browser_clear_downloads" in tool_names
    assert "browser_back" in tool_names
    assert "browser_forward" in tool_names
    assert "browser_refresh" in tool_names
    assert "browser_close" in tool_names
    total_browser = len([n for n in tool_names if n.startswith("browser_")])
    assert total_browser == 28
