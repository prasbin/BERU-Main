"""WebSocket endpoint for live streaming chat responses.

Provides real-time bidirectional communication for streaming chat responses,
agent status updates, and system notifications.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.api.deps import get_chat_service
from backend.api.security import websocket_authorized, websocket_origin_ok
from backend.core.config import get_settings
from backend.schemas.chat import ChatRequest
from backend.services.chat_service import ChatStreamEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

#: Hard cap on simultaneous WebSocket connections (one per open tab).
_MAX_WS_CONNECTIONS = 32
#: client_id is used as a dict key and appears in logs; keep it bounded.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class WebSocketManager:
    """Manages active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str) -> None:
        """Accept and register a new WebSocket connection.

        A reconnect from the same ``client_id`` (e.g. a tab that refreshed or a
        dropped link) replaces the stale registration: the old socket is closed
        best-effort so dead entries can never block reconnects, while a single
        live link per id is guaranteed.
        """
        await websocket.accept()
        previous = self._connections.get(client_id)
        if previous is not None and not previous.client_state.DISCONNECTED:
            try:
                await previous.close(code=1000)
            except Exception:  # noqa: BLE001 - best-effort cleanup of a stale socket
                pass
        self._connections[client_id] = websocket
        logger.info("WebSocket client connected: %s", client_id)

    def disconnect(self, client_id: str) -> None:
        """Remove a WebSocket connection."""
        self._connections.pop(client_id, None)
        logger.info("WebSocket client disconnected: %s", client_id)

    async def send_message(self, client_id: str, message: dict[str, Any]) -> None:
        """Send a JSON message to a specific client."""
        ws = self._connections.get(client_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                logger.exception("Failed to send message to %s", client_id)
                self.disconnect(client_id)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all connected clients."""
        disconnected = []
        for client_id, ws in self._connections.items():
            try:
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            except Exception:
                logger.exception("Failed to broadcast to %s", client_id)
                disconnected.append(client_id)
        for cid in disconnected:
            self.disconnect(cid)

    @property
    def active_connections(self) -> int:
        return len(self._connections)


_manager = WebSocketManager()


def get_ws_manager() -> WebSocketManager:
    """Return the global WebSocket manager."""
    return _manager


@router.websocket("/ws/{client_id}")
async def websocket_chat(websocket: WebSocket, client_id: str) -> None:
    """WebSocket endpoint for live chat streaming.

    Protocol:
    1. Client connects to /ws/{client_id}
    2. Client sends JSON messages with the chat request
    3. Server streams back JSON events (start, delta, end, error)

    Message format from client:
    {
        "message": "user message",
        "conversation_id": "optional",
        "agent": "optional agent name",
        "project_id": "optional"
    }
    """
    settings = get_settings()
    if not websocket_origin_ok(websocket, settings):
        await websocket.close(code=1008)  # policy violation — cross-origin handshake
        return
    if not websocket_authorized(websocket, settings):
        await websocket.close(code=1008)  # policy violation — no credentials
        return
    if not _CLIENT_ID_RE.match(client_id):
        await websocket.close(code=1008)  # policy violation — malformed client id
        return
    if _manager.active_connections >= _MAX_WS_CONNECTIONS:
        await websocket.close(code=1013)  # try again later — too many connections
        return
    await _manager.connect(websocket, client_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await _manager.send_message(
                    client_id, {"type": "error", "error": "Invalid JSON"}
                )
                continue

            message = data.get("message", "")
            if not message:
                await _manager.send_message(
                    client_id, {"type": "error", "error": "Missing 'message' field"}
                )
                continue

            # Build a ChatRequest from the WebSocket payload.
            request = ChatRequest(
                message=message,
                conversation_id=data.get("conversation_id"),
                agent=data.get("agent"),
                project_id=data.get("project_id"),
            )

            settings = get_settings()
            chat_service = get_chat_service()
            # stream_process opens its own short-lived DB sessions per stage, so
            # no WebSocket-scoped session is held while the LLM streams tokens.
            stream = chat_service.stream_process(settings, request)

            try:
                first_event = await stream.__anext__()
                await _send_ws_event(client_id, first_event)

                async for event in stream:
                    await _send_ws_event(client_id, event)

            except StopAsyncIteration:
                pass
            except Exception:
                logger.exception("WebSocket chat stream failed")
                await _manager.send_message(
                    client_id,
                    {"type": "error", "error": "Generation failed."},
                )

    except WebSocketDisconnect:
        _manager.disconnect(client_id)


async def _send_ws_event(client_id: str, event: ChatStreamEvent) -> None:
    """Convert a ChatStreamEvent to a WebSocket message and send it."""
    if event.kind == "start":
        payload = {
            "type": "start",
            "conversation_id": event.conversation_id,
            "agent": event.agent,
        }
    elif event.kind == "delta":
        payload = {"type": "delta", "text": event.text or ""}
    elif event.kind == "end":
        usage = None
        if event.usage:
            usage = {
                "prompt_tokens": event.usage.prompt_tokens,
                "completion_tokens": event.usage.completion_tokens,
                "total_tokens": event.usage.total_tokens,
            }
        payload = {
            "type": "end",
            "conversation_id": event.conversation_id,
            "agent": event.agent,
            "message_id": event.message_id,
            "model": event.model,
            "usage": usage,
            "pending_confirmations": event.pending_confirmations or [],
        }
    else:
        payload = {"type": "error", "error": event.error or "Generation failed."}

    await _manager.send_message(client_id, payload)
