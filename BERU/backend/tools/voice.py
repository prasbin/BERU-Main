"""Voice tools for agents: listen (STT) and speak (TTS).

These tools describe the agent-facing voice capability: capturing a spoken
utterance (wake-word aware) and producing a spoken response.

The voice *engine and API* exist, but these agent-facing tools are not wired to
them yet, so every call reports ``unavailable`` rather than fabricating a
successful capture or response.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


def _unavailable(tool_name: str) -> ToolResult:
    return ToolResult.failure(
        f"Tool '{tool_name}' is unavailable: voice tools are not connected to "
        "the voice engine yet."
    )


class VoiceListenTool(Tool):
    """Capture and transcribe a spoken utterance."""

    name = "voice_listen"
    description = "Capture a spoken utterance and transcribe it (wake-word aware)."
    permissions = ["read"]
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The voice session to listen on.",
            },
            "duration_ms": {
                "type": "integer",
                "description": "Optional listen duration hint in milliseconds.",
            },
        },
        "required": [],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return _unavailable(self.name)


class VoiceSpeakTool(Tool):
    """Produce a spoken response from text."""

    name = "voice_speak"
    description = "Synthesize a spoken response and play it to the user."
    permissions = ["read"]
    requires_confirmation = True
    availability = "unavailable"
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to speak aloud.",
            },
            "session_id": {
                "type": "string",
                "description": "The voice session to speak through.",
            },
        },
        "required": ["text"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        return _unavailable(self.name)