"""Browser automation tools for agents.

Tools that allow agents to interact with web pages: navigate, click, type,
take screenshots, and extract content. These tools delegate to the
:class:`BrowserEngine` backed by Playwright. If the browser backend is
unavailable, the tool reports ``availability = "unavailable"`` and is filtered
from the LLM tool list.
"""

from __future__ import annotations

from typing import Any

from backend.engines.browser import BrowserAction, get_browser_engine
from backend.tools.base import Tool, ToolResult


class BrowserNavigateTool(Tool):
    """Navigate to a URL in the browser."""

    name = "browser_navigate"
    description = "Navigate the browser to a specified URL."
    permissions = ["read", "write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to.",
            }
        },
        "required": ["url"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url", "")
        if not url:
            return ToolResult.failure("browser_navigate requires a 'url' argument.")
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        # Ensure there is an active page.
        if not engine._active_page_id:
            await engine.create_page(url)
        result = await engine.execute_action(BrowserAction.NAVIGATE, url=url)
        if not result.success:
            return ToolResult.failure(result.error or "Navigation failed.")
        return ToolResult.success(result.to_dict())


class BrowserClickTool(Tool):
    """Click an element on the page."""

    name = "browser_click"
    description = "Click an element on the page using a CSS selector."
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the element to click.",
            }
        },
        "required": ["selector"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector", "")
        if not selector:
            return ToolResult.failure("browser_click requires a 'selector' argument.")
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        result = await engine.execute_action(BrowserAction.CLICK, selector=selector)
        if not result.success:
            return ToolResult.failure(result.error or "Click failed.")
        return ToolResult.success(result.to_dict())


class BrowserTypeTool(Tool):
    """Type text into an input field."""

    name = "browser_type"
    description = "Type text into an input field identified by a CSS selector."
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the input field.",
            },
            "text": {
                "type": "string",
                "description": "Text to type into the field.",
            },
        },
        "required": ["selector", "text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector", "")
        text = kwargs.get("text", "")
        if not selector:
            return ToolResult.failure("browser_type requires a 'selector' argument.")
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        result = await engine.execute_action(
            BrowserAction.TYPE, selector=selector, text=text,
        )
        if not result.success:
            return ToolResult.failure(result.error or "Type failed.")
        return ToolResult.success(result.to_dict())


class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current page."""

    name = "browser_screenshot"
    description = (
        "Take a screenshot of the current browser page and save it. Pass "
        "'full_page' to capture the entire scrollable page instead of the viewport."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "full_page": {
                "type": "boolean",
                "description": "Capture the whole scrollable page (default false).",
            }
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        result = await engine.execute_action(
            BrowserAction.SCREENSHOT, full_page=bool(kwargs.get("full_page"))
        )
        if not result.success:
            return ToolResult.failure(result.error or "Screenshot failed.")
        return ToolResult.success(result.to_dict())


class BrowserExtractTool(Tool):
    """Extract text content from the page."""

    name = "browser_extract"
    description = "Extract text content from a page element."
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the element to extract text from.",
            }
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        selector = kwargs.get("selector") or "body"
        result = await engine.execute_action(
            BrowserAction.EXTRACT_TEXT, selector=selector,
        )
        if not result.success:
            return ToolResult.failure(result.error or "Extraction failed.")
        return ToolResult.success(result.to_dict())


async def _run_action(action: BrowserAction, **params: Any) -> ToolResult:
    engine = get_browser_engine()
    if not await engine.ensure_ready():
        return ToolResult.failure(
            "Browser backend not available on this platform."
        )
    result = await engine.execute_action(action, **params)
    if not result.success:
        return ToolResult.failure(result.error or f"{action.value} failed.")
    return ToolResult.success(result.to_dict())


class BrowserPageInfoTool(Tool):
    """Read the current page title and URL."""

    name = "browser_page_info"
    description = "Read the current browser page's title and URL."
    permissions = ["read"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        return await _run_action(BrowserAction.PAGE_INFO)


class BrowserScrollTool(Tool):
    """Scroll the page or bring an element into view."""

    name = "browser_scroll"
    description = (
        "Scroll the page. Pass 'direction' ('up'/'down') and 'amount' for the "
        "mouse wheel, or 'selector' to scroll an element into view."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "description": "Scroll direction: 'up' or 'down'.",
            },
            "amount": {
                "type": "integer",
                "description": "Wheel scroll amount in pixels.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector of the element to scroll into view.",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector") or ""
        if selector:
            return await _run_action(
                BrowserAction.SCROLL_TO, selector=selector,
                block=kwargs.get("block") or "center",
            )
        direction = kwargs.get("direction") or "down"
        if direction not in ("up", "down"):
            return ToolResult.failure("browser_scroll direction must be 'up' or 'down'.")
        amount = int(kwargs.get("amount") or 500)
        return await _run_action(
            BrowserAction.SCROLL, direction=direction, amount=amount,
        )


class BrowserPressTool(Tool):
    """Press a keyboard key, optionally focused on a selector."""

    name = "browser_press"
    description = (
        "Press a keyboard key on the focused element (e.g. 'Enter' to submit "
        "a form). Optionally pass a 'selector' to focus first."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Key to press, e.g. 'Enter', 'Tab', 'Escape'.",
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to press the key on.",
            },
        },
        "required": ["key"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        key = kwargs.get("key", "")
        if not key:
            return ToolResult.failure("browser_press requires a 'key' argument.")
        if kwargs.get("selector"):
            return await _run_action(
                BrowserAction.PRESS, key=key, selector=kwargs["selector"],
            )
        return await _run_action(BrowserAction.PRESS, key=key)


class BrowserSelectTool(Tool):
    """Select an option in a dropdown/select element."""

    name = "browser_select"
    description = (
        "Select an option in a dropdown. Pass a 'selector' and exactly one of "
        "'value', 'label', 'index', or a 'values' list to choose the option(s)."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the <select> element.",
            },
            "value": {
                "type": "string",
                "description": "The option's value attribute.",
            },
            "label": {
                "type": "string",
                "description": "The option's visible label text.",
            },
            "index": {
                "type": "integer",
                "description": "Zero-based option index.",
            },
            "values": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Value list for multi-select <select> elements.",
            },
            "frame": {
                "type": "string",
                "description": "Optional frame name, URL substring, or index.",
            },
        },
        "required": ["selector"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector", "")
        if not selector:
            return ToolResult.failure("browser_select requires a 'selector' argument.")
        params: dict[str, Any] = {"selector": selector}
        if kwargs.get("values"):
            params["values"] = list(kwargs["values"])
        elif kwargs.get("value") is not None:
            params["value"] = kwargs.get("value")
        elif kwargs.get("label") is not None:
            params["label"] = kwargs.get("label")
        elif kwargs.get("index") is not None:
            params["index"] = int(kwargs.get("index"))
        if kwargs.get("frame"):
            params["frame"] = kwargs["frame"]
        return await _run_action(BrowserAction.SELECT, **params)


class BrowserDragTool(Tool):
    """Drag and drop an element onto another."""

    name = "browser_drag"
    description = (
        "Drag the element at 'source' and drop it on the element at 'target' "
        "(both CSS selectors)."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "CSS selector of the draggable element.",
            },
            "target": {
                "type": "string",
                "description": "CSS selector of the drop target.",
            },
        },
        "required": ["source", "target"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        source = kwargs.get("source", "")
        target = kwargs.get("target", "")
        if not source or not target:
            return ToolResult.failure(
                "browser_drag requires 'source' and 'target' arguments."
            )
        return await _run_action(
            BrowserAction.DRAG, source=source, target=target
        )


class BrowserCookiesTool(Tool):
    """Read, write, or clear browser cookies."""

    name = "browser_cookies"
    description = (
        "Manage session cookies. 'get' lists them, 'set' adds the cookies in "
        "'cookies' (each a {name, value, domain, path, ...} dict), 'clear' "
        "removes all cookies."
    )
    permissions = ["read", "write"]
    parameters = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "enum": ["get", "set", "clear"],
                "description": "Operation to perform.",
            },
            "cookies": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Cookie objects for 'set'.",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        sub = kwargs.get("subcommand") or "get"
        params: dict[str, Any] = {"subcommand": sub}
        if kwargs.get("cookies"):
            params["cookies"] = list(kwargs["cookies"])
        return await _run_action(BrowserAction.COOKIES, **params)


class BrowserStorageTool(Tool):
    """Read, write, or clear localStorage."""

    name = "browser_storage"
    description = (
        "Manage page localStorage. 'get' lists entries (or one entry when "
        "'key' is given), 'set' writes 'key'='value', 'clear' empties storage."
    )
    permissions = ["read", "write"]
    parameters = {
        "type": "object",
        "properties": {
            "subcommand": {
                "type": "string",
                "enum": ["get", "set", "clear"],
                "description": "Operation to perform.",
            },
            "key": {"type": "string", "description": "Storage key for get/set."},
            "value": {"type": "string", "description": "Value to store."},
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        sub = kwargs.get("subcommand") or "get"
        params: dict[str, Any] = {"subcommand": sub}
        if kwargs.get("key"):
            params["key"] = kwargs["key"]
        if kwargs.get("value"):
            params["value"] = kwargs["value"]
        return await _run_action(BrowserAction.STORAGE, **params)


class BrowserNetworkTool(Tool):
    """Inspect recent responses captured by the network tap."""

    name = "browser_network"
    description = (
        "Return the most recent network responses (URL, status, method, "
        "resource type, response size, timing) for the active page, plus the "
        "session's blocked-URL globs."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default 50).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        limit = int(kwargs.get("limit") or 50)
        return await _run_action(BrowserAction.NETWORK, limit=limit)


class BrowserBlockTool(Tool):
    """Block network requests matching a URL glob."""

    name = "browser_block_url"
    description = (
        "Block network requests whose URL matches the glob pattern (e.g. "
        "'*analytics*', 'https://ads.example/*'). Applies for the session."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "glob": {
                "type": "string",
                "description": "URL glob pattern to block.",
            },
        },
        "required": ["glob"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        glob = kwargs.get("glob", "")
        if not glob:
            return ToolResult.failure("browser_block_url requires a 'glob' argument.")
        return await _run_action(BrowserAction.BLOCK_URL, glob=glob)


class BrowserDownloadTool(Tool):
    """Trigger and save a file download."""

    name = "browser_download"
    description = (
        "Click the element at 'selector' to trigger a download and save it "
        "locally, returning the saved path."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector that triggers the download.",
            },
        },
        "required": ["selector"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector", "")
        if not selector:
            return ToolResult.failure(
                "browser_download requires a 'selector' argument."
            )
        return await _run_action(BrowserAction.DOWNLOAD, selector=selector)


class BrowserUploadTool(Tool):
    """Upload file(s) to a file input."""

    name = "browser_upload"
    description = (
        "Set the files of the input at 'selector' (type=file) to the paths in "
        "'paths' (one or more)."
    )
    permissions = ["write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector for the <input type=file> element.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute file paths to upload.",
            },
        },
        "required": ["selector", "paths"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        selector = kwargs.get("selector", "")
        paths = kwargs.get("paths") or []
        if not selector:
            return ToolResult.failure(
                "browser_upload requires a 'selector' argument."
            )
        if not paths:
            return ToolResult.failure(
                "browser_upload requires at least one path in 'paths'."
            )
        return await _run_action(
            BrowserAction.UPLOAD, selector=selector, paths=list(paths)
        )


class BrowserConsoleTool(Tool):
    """Read recent console messages and uncaught page errors."""

    name = "browser_console"
    description = (
        "Return recent console messages (log/warn/error/...) and uncaught page "
        "errors for the active page, captured since the page started."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of entries to return (default all).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        params: dict[str, Any] = {}
        if kwargs.get("limit") is not None:
            params["limit"] = int(kwargs["limit"])
        return await _run_action(BrowserAction.CONSOLE, **params)


class BrowserDownloadsTool(Tool):
    """List files saved by the download facility."""

    name = "browser_downloads"
    description = (
        "List files previously downloaded via browser_download (name, size, "
        "local path)."
    )
    permissions = ["read"]
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of files to return (default all).",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        params: dict[str, Any] = {}
        if kwargs.get("limit") is not None:
            params["limit"] = int(kwargs["limit"])
        return await _run_action(BrowserAction.DOWNLOADS, **params)


class BrowserRedirectTool(Tool):
    """Rewrite matching requests to a different URL."""

    name = "browser_redirect_url"
    description = (
        "Redirect every request whose URL matches the 'glob' pattern to the "
        "'redirect' target URL (302). Applies for the session and follows the "
        "same glob syntax as browser_block_url."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "glob": {
                "type": "string",
                "description": "URL glob pattern whose requests are redirected.",
            },
            "redirect": {
                "type": "string",
                "description": "Target URL to redirect to.",
            },
        },
        "required": ["glob", "redirect"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        glob = kwargs.get("glob", "")
        redirect = kwargs.get("redirect", "")
        if not glob:
            return ToolResult.failure(
                "browser_redirect_url requires a 'glob' argument."
            )
        if not redirect:
            return ToolResult.failure(
                "browser_redirect_url requires a 'redirect' argument."
            )
        return await _run_action(
            BrowserAction.REDIRECT_URL, glob=glob, redirect=redirect
        )


class BrowserSnapshotTool(Tool):
    """Save the session's cookies + localStorage to a named snapshot file."""

    name = "browser_snapshot"
    description = (
        "Save the current session state (cookies and localStorage) into a "
        "named snapshot file that browser_restore can load later."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Snapshot name (letters, digits, _-.; default 'session').",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name") or "session"
        return await _run_action(BrowserAction.SNAPSHOT, name=name)


class BrowserRestoreTool(Tool):
    """Restore a snapshot's cookies + localStorage into a fresh context."""

    name = "browser_restore"
    description = (
        "Restore cookies and localStorage from a snapshot created by "
        "browser_snapshot. Closes the current session's tabs and starts a "
        "fresh context carrying that state."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Snapshot name to restore from.",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name") or "session"
        return await _run_action(BrowserAction.RESTORE, name=name)


class BrowserFulfillTool(Tool):
    """Answer matching requests with fixed content (API mocking)."""

    name = "browser_fulfill"
    description = (
        "Intercept every request whose URL matches the 'glob' pattern and "
        "respond with fixed 'status'/'body'/'content_type'/'headers' content "
        "instead of the network — useful for mocking APIs. Applies for the "
        "session."
    )
    permissions = ["write"]
    parameters = {
        "type": "object",
        "properties": {
            "glob": {
                "type": "string",
                "description": "URL glob pattern whose requests are fulfilled.",
            },
            "body": {
                "type": "string",
                "description": "Response body to serve.",
            },
            "content_type": {
                "type": "string",
                "description": "Response Content-Type, e.g. 'application/json'.",
            },
            "status_code": {
                "type": "integer",
                "description": "HTTP status to return (default 200).",
            },
            "headers": {
                "type": "object",
                "description": "Extra response headers as {'name': 'value'}.",
            },
        },
        "required": ["glob"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        glob = kwargs.get("glob", "")
        if not glob:
            return ToolResult.failure("browser_fulfill requires a 'glob' argument.")
        params: dict[str, Any] = {"glob": glob}
        if kwargs.get("body") is not None:
            params["body"] = kwargs["body"]
        if kwargs.get("content_type") is not None:
            params["content_type"] = kwargs["content_type"]
        if kwargs.get("status_code") is not None:
            params["status_code"] = int(kwargs["status_code"])
        if kwargs.get("headers") is not None:
            params["headers"] = dict(kwargs["headers"])
        return await _run_action(BrowserAction.FULFILL, **params)


class BrowserLaunchTool(Tool):
    """Relaunch the browser with new launch options."""

    name = "browser_launch"
    description = (
        "Relaunch the browser, toggling headless mode and optionally setting "
        "the viewport (width/height), user agent, locale, device-scale factor, "
        "mobile/touch emulation, or timezone. Closes the current session first. "
        "Requires confirmation."
    )
    permissions = ["write"]
    requires_confirmation = True
    parameters = {
        "type": "object",
        "properties": {
            "headless": {
                "type": "boolean",
                "description": "Run without a visible window (default true).",
            },
            "width": {
                "type": "integer",
                "description": "Viewport width in pixels.",
            },
            "height": {
                "type": "integer",
                "description": "Viewport height in pixels.",
            },
            "user_agent": {
                "type": "string",
                "description": "User-Agent header to use.",
            },
            "locale": {
                "type": "string",
                "description": "Browser locale, e.g. 'en-US'.",
            },
            "device_scale_factor": {
                "type": "number",
                "description": "Device scale factor, e.g. 2 for Retina.",
            },
            "is_mobile": {
                "type": "boolean",
                "description": "Emulate a mobile device (meta viewport).",
            },
            "has_touch": {
                "type": "boolean",
                "description": "Emulate touch input.",
            },
            "timezone_id": {
                "type": "string",
                "description": "Emulated timezone, e.g. 'Europe/Paris'.",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        params: dict[str, Any] = {}
        for name in (
            "headless", "width", "height", "user_agent", "locale",
            "device_scale_factor", "is_mobile", "has_touch", "timezone_id",
        ):
            if kwargs.get(name) is not None:
                params[name] = kwargs[name]
        return await _run_action(BrowserAction.LAUNCH, **params)


class BrowserClearDownloadsTool(Tool):
    """Empty the session's download directory."""

    name = "browser_clear_downloads"
    description = (
        "Delete every downloaded file in the session's download directory. "
        "Requires confirmation because it permanently removes files."
    )
    permissions = ["write"]
    requires_confirmation = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        if not engine._context:
            return ToolResult.failure(
                "No browser context — the session is closed."
            )
        cleared = await _run_action(BrowserAction.CLEAR_DOWNLOADS)
        if not cleared.ok:
            return cleared
        files = await _run_action(BrowserAction.DOWNLOADS)
        data: dict[str, Any] = dict(cleared.output["data"])
        if files.ok:
            data["remaining"] = files.output["data"]["count"]
            data["files"] = files.output["data"]["files"]
        return ToolResult.success(data)


class BrowserCloseTool(Tool):
    """Close the whole browser session (all tabs)."""

    name = "browser_close"
    description = (
        "Close the browser session: closes every tab and shuts down the browser. "
        "Requires confirmation because it discards all in-session state."
    )
    permissions = ["write"]
    requires_confirmation = True
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        engine = get_browser_engine()
        if not await engine.ensure_ready():
            return ToolResult.failure(
                "Browser backend not available on this platform."
            )
        closed = await engine.close_browser()
        return ToolResult.success(
            {"closed": closed, "pages": 0, "message": "Browser session closed."}
        )


class BrowserBackTool(Tool):
    """Go back to the previous page."""

    name = "browser_back"
    description = "Navigate back to the previously visited page."
    permissions = ["write"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        return await _run_action(BrowserAction.BACK)


class BrowserForwardTool(Tool):
    """Go forward to the next page."""

    name = "browser_forward"
    description = "Navigate forward to the next page in history."
    permissions = ["write"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        return await _run_action(BrowserAction.FORWARD)


class BrowserRefreshTool(Tool):
    """Reload the current page."""

    name = "browser_refresh"
    description = "Reload the current browser page."
    permissions = ["read", "write"]
    parameters = {"type": "object", "properties": {}, "required": []}

    async def run(self, **kwargs: Any) -> ToolResult:
        return await _run_action(BrowserAction.REFRESH)
