"""Agent registry.

Holds the set of available agents and resolves them by name. The default
registry is created lazily and seeded with the core agent and specialist agents.
Additional agents register via :meth:`AgentRegistry.register`.
"""

from __future__ import annotations

from functools import lru_cache

from backend.agents.base import BaseAgent
from backend.agents.core_agent import CoreAgent
from backend.agents.dhanus_agent import DhanusAgent
from backend.agents.igris_agent import IgrisAgent
from backend.agents.tank_agent import TankAgent
from backend.core.errors import NotFoundError
from backend.tools.browser import (
    BrowserBackTool,
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
    BrowserForwardTool,
    BrowserFulfillTool,
    BrowserLaunchTool,
    BrowserNavigateTool,
    BrowserNetworkTool,
    BrowserPageInfoTool,
    BrowserPressTool,
    BrowserRedirectTool,
    BrowserRefreshTool,
    BrowserRestoreTool,
    BrowserScreenshotTool,
    BrowserScrollTool,
    BrowserSelectTool,
    BrowserSnapshotTool,
    BrowserStorageTool,
    BrowserTypeTool,
    BrowserUploadTool,
)
from backend.tools.calendar import CalendarTool
from backend.tools.clock import ClockTool
from backend.tools.coding import CodeAnalyzerTool, CodeFormatterTool
from backend.tools.desktop import (
    ClipboardReadTool,
    ClipboardWriteTool,
    CloseWindowTool,
    FindProcessTool,
    FocusWindowTool,
    KeyboardHotkeyTool,
    KeyboardPressTool,
    KeyboardTypeTool,
    ListHotkeysTool,
    ListMonitorsTool,
    ListProcessesTool,
    ListWindowsTool,
    MouseClickTool,
    MouseDoubleClickTool,
    MouseMoveTool,
    MousePositionTool,
    MouseScrollTool,
    RegisterHotkeyTool,
    ScreenshotTool,
    ScreenSizeTool,
    UnregisterHotkeyTool,
)
from backend.tools.document import FileAnalyserTool
from backend.tools.generation import DocumentGeneratorTool
from backend.tools.spiritual import LookupScriptureTool, MeditationTimerTool
from backend.tools.study import CreateFlashcardTool, SearchKnowledgeTool
from backend.tools.system import (
    GetSystemInfoTool,
    LaunchAppTool,
    RunCommandTool,
    SendNotificationTool,
)
from backend.tools.voice import VoiceListenTool, VoiceSpeakTool
from backend.tools.web import WebSearchTool

DEFAULT_AGENT_NAME = "beru_core"


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent, *, replace: bool = False) -> None:
        if agent.name in self._agents and not replace:
            raise ValueError(f"Agent '{agent.name}' is already registered.")
        self._agents[agent.name] = agent

    def get(self, name: str | None) -> BaseAgent:
        resolved = name or DEFAULT_AGENT_NAME
        agent = self._agents.get(resolved)
        if agent is None:
            raise NotFoundError(
                f"Unknown agent '{resolved}'.",
                detail={"available": sorted(self._agents)},
            )
        return agent

    def list(self) -> list[BaseAgent]:
        return list(self._agents.values())


def _seed_registry(registry: AgentRegistry) -> None:
    """Register all built-in agents and their scoped tools."""
    # Core agent — general purpose, gets the clock, core tools, and browser tools.
    core = CoreAgent()
    core.register_tool(ClockTool())
    core.register_tool(WebSearchTool())
    core.register_tool(FileAnalyserTool())
    core.register_tool(DocumentGeneratorTool())
    core.register_tool(CalendarTool())
    core.register_tool(BrowserNavigateTool())
    core.register_tool(BrowserClickTool())
    core.register_tool(BrowserTypeTool())
    core.register_tool(BrowserScreenshotTool())
    core.register_tool(BrowserExtractTool())
    core.register_tool(BrowserPageInfoTool())
    core.register_tool(BrowserScrollTool())
    core.register_tool(BrowserPressTool())
    core.register_tool(BrowserSelectTool())
    core.register_tool(BrowserDragTool())
    core.register_tool(BrowserCookiesTool())
    core.register_tool(BrowserStorageTool())
    core.register_tool(BrowserNetworkTool())
    core.register_tool(BrowserBlockTool())
    core.register_tool(BrowserClearDownloadsTool())
    core.register_tool(BrowserDownloadTool())
    core.register_tool(BrowserUploadTool())
    core.register_tool(BrowserBackTool())
    core.register_tool(BrowserForwardTool())
    core.register_tool(BrowserFulfillTool())
    core.register_tool(BrowserLaunchTool())
    core.register_tool(BrowserRefreshTool())
    core.register_tool(BrowserConsoleTool())
    core.register_tool(BrowserDownloadsTool())
    core.register_tool(BrowserRedirectTool())
    core.register_tool(BrowserSnapshotTool())
    core.register_tool(BrowserRestoreTool())
    core.register_tool(BrowserCloseTool())
    core.register_tool(RunCommandTool())
    core.register_tool(LaunchAppTool())
    core.register_tool(SendNotificationTool())
    core.register_tool(GetSystemInfoTool())
    core.register_tool(ScreenshotTool())
    core.register_tool(ScreenSizeTool())
    core.register_tool(MousePositionTool())
    core.register_tool(MouseMoveTool())
    core.register_tool(MouseClickTool())
    core.register_tool(MouseDoubleClickTool())
    core.register_tool(MouseScrollTool())
    core.register_tool(KeyboardTypeTool())
    core.register_tool(KeyboardPressTool())
    core.register_tool(KeyboardHotkeyTool())
    core.register_tool(ClipboardReadTool())
    core.register_tool(ClipboardWriteTool())
    core.register_tool(ListProcessesTool())
    core.register_tool(FindProcessTool())
    core.register_tool(ListWindowsTool())
    core.register_tool(FocusWindowTool())
    core.register_tool(CloseWindowTool())
    core.register_tool(ListMonitorsTool())
    core.register_tool(ListHotkeysTool())
    core.register_tool(RegisterHotkeyTool())
    core.register_tool(UnregisterHotkeyTool())
    core.register_tool(VoiceListenTool())
    core.register_tool(VoiceSpeakTool())
    registry.register(core)

    # IGRIS — study/academic agent.
    igris = IgrisAgent()
    igris.register_tool(SearchKnowledgeTool())
    igris.register_tool(CreateFlashcardTool())
    registry.register(igris)

    # DHANUS — spiritual knowledge agent.
    dhanus = DhanusAgent()
    dhanus.register_tool(LookupScriptureTool())
    dhanus.register_tool(MeditationTimerTool())
    registry.register(dhanus)

    # TANK — coding specialist agent.
    tank = TankAgent()
    tank.register_tool(CodeAnalyzerTool())
    tank.register_tool(CodeFormatterTool())
    registry.register(tank)


@lru_cache
def get_agent_registry() -> AgentRegistry:
    """Return the process-wide agent registry, seeded with built-in agents."""
    registry = AgentRegistry()
    _seed_registry(registry)
    return registry
