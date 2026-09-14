"""Voice system API: sessions, transcription, synthesis, interruption.

REST endpoints cover discrete operations; a WebSocket endpoint provides
continuous-listening style streamed frames for clients that pump audio chunks.
Audio is passed as base64 within JSON (REST) or JSON frames (WebSocket); the
synthesized audio is served as raw WAV from ``GET /voice/audio/{audio_id}``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_chat_service
from backend.api.security import require_api_key, websocket_authorized
from backend.core.config import Settings, get_settings
from backend.database.base import get_session
from backend.engines.speech import (
    build_stt_provider,
    build_tts_provider,
)
from backend.engines.voice import VoiceEngine
from backend.services.chat_service import ChatService
from backend.services.voice_chat import VoiceChatOutcome, VoiceChatService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/voice", tags=["voice"], dependencies=[Depends(require_api_key)]
)

# WebSocket routes cannot use the header-based HTTP dependency (the security
# scheme needs a Request object), so the WS endpoint lives on its own router and
# authenticates the handshake via websocket_authorized() instead.
ws_router = APIRouter(prefix="/voice", tags=["voice"])


def _build_engine() -> VoiceEngine:
    """Build the shared voice engine from current settings."""
    settings = get_settings()
    wake_words = [w.strip() for w in settings.voice_wake_words.split(",") if w.strip()]
    return VoiceEngine(
        stt=build_stt_provider(settings.voice_stt_provider),
        tts=build_tts_provider(settings.voice_tts_provider),
        wake_words=wake_words,
        session_timeout=settings.voice_session_timeout,
    )


_engine: VoiceEngine | None = None


def get_voice_engine() -> VoiceEngine:
    """Return the process-wide voice engine (built lazily from settings)."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


# ---- Request/response models ----


class CreateSessionRequest(BaseModel):
    wake_words: list[str] | None = None


class ListenRequest(BaseModel):
    audio: str = Field(..., description="Base64-encoded audio payload.")
    final: bool = True


class TranscribeTextRequest(BaseModel):
    session_id: str
    text: str
    wake_words: list[str] | None = None


class RespondRequest(BaseModel):
    session_id: str
    text: str


class ConverseRequest(BaseModel):
    """A single voice conversation turn through the full chain.

    Exactly one of ``audio`` (base64 speech) or ``text`` must be provided.
    """

    session_id: str = Field(..., description="The voice session to converse on.")
    audio: str | None = Field(
        None,
        description="Base64-encoded speech audio (exactly one of 'audio'/'text').",
    )
    text: str | None = Field(
        None,
        description="Spoken text recorded directly (exactly one of 'audio'/'text').",
    )
    conversation_id: str | None = Field(
        None,
        description="Continue an existing conversation; omitted continues the session's own.",
    )
    agent: str | None = Field(
        None, description="Agent to handle the turn; defaults to the core agent."
    )
    title: str | None = Field(
        None, description="Optional title when starting a new conversation."
    )
    project_id: str | None = Field(
        None, description="Optional project to scope a new conversation to."
    )


class InterruptResponse(BaseModel):
    interrupted: bool


def _session_or_404(engine: VoiceEngine, session_id: str):
    session = engine.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown voice session '{session_id}'.")
    return session


# ---- REST endpoints ----


@router.post("/sessions", status_code=201, summary="Create a voice session")
async def create_session(body: CreateSessionRequest) -> dict:
    engine = get_voice_engine()
    session = await engine.create_session(wake_words=body.wake_words)
    return session.to_dict()


@router.get("/sessions", summary="List active voice sessions")
async def list_sessions() -> list[dict]:
    return get_voice_engine().list_sessions()


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    summary="End a voice session",
)
async def end_session(session_id: str) -> Response:
    engine = get_voice_engine()
    if not engine.end_session(session_id):
        raise HTTPException(status_code=404, detail=f"Unknown voice session '{session_id}'.")
    return Response(status_code=204)


@router.post("/sessions/{session_id}/interrupt", summary="Interrupt active speech")
async def interrupt(session_id: str) -> InterruptResponse:
    engine = get_voice_engine()
    _session_or_404(engine, session_id)
    return InterruptResponse(interrupted=engine.interrupt(session_id))


@router.post("/listen", summary="Transcribe audio for a session")
async def listen(session_id: str, body: ListenRequest) -> dict:
    settings = get_settings()
    engine = get_voice_engine()
    _session_or_404(engine, session_id)

    try:
        audio = base64.b64decode(body.audio)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}") from exc

    if len(audio) > settings.voice_max_audio_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Audio exceeds {settings.voice_max_audio_bytes} bytes.",
        )

    await engine.ingest_audio(session_id, audio)
    record = await engine.finalize_utterance(session_id)
    if record is None:
        return {"transcribed": False, "session": engine.get_session(session_id).to_dict()}
    return {
        "transcribed": True,
        "utterance": record.to_dict(),
        "session": engine.get_session(session_id).to_dict(),
    }


@router.post("/transcribe-text", summary="Record spoken text directly")
async def transcribe_text(body: TranscribeTextRequest) -> dict:
    engine = get_voice_engine()
    _session_or_404(engine, body.session_id)
    record = await engine.transcribe_text(body.session_id, body.text)
    return {"utterance": record.to_dict()}


@router.post("/respond", status_code=201, summary="Synthesize a spoken response")
async def respond(body: RespondRequest) -> dict:
    engine = get_voice_engine()
    _session_or_404(engine, body.session_id)
    clip = await engine.respond(body.session_id, body.text)
    return clip.to_dict()


def _serialize_converse(outcome: VoiceChatOutcome) -> dict:
    """Render a :class:`VoiceChatOutcome` as the API response payload."""
    usage = None
    if outcome.usage is not None:
        usage = {
            "prompt_tokens": outcome.usage.prompt_tokens,
            "completion_tokens": outcome.usage.completion_tokens,
            "total_tokens": outcome.usage.total_tokens,
        }
    return {
        "utterance": outcome.utterance.to_dict(),
        "conversation_id": outcome.conversation_id,
        "reply": outcome.reply,
        "agent": outcome.agent,
        "model": outcome.model,
        "provider": outcome.provider,
        "simulated": outcome.simulated,
        "tool_calls_made": outcome.tool_calls_made,
        "usage": usage,
        "clip": outcome.clip.to_dict(),
        "pending_confirmations": outcome.pending_confirmations,
    }


@router.post("/converse", status_code=201, summary="Full voice conversation turn")
async def converse(
    body: ConverseRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    """Run one voice turn end to end: speech -> STT -> agent loop -> LLM -> TTS.

    The utterance is transcribed wake-word aware, sent through the configured
    conversation/agent/LLM chain (including real tool calling when a real
    provider is configured), and the reply is synthesized into a spoken clip.
    With the hermetic mock providers the whole chain is simulated and
    ``simulated`` reports ``True`` honestly — nothing is fabricated.
    """
    engine = get_voice_engine()
    _session_or_404(engine, body.session_id)

    if (body.audio is None) == (body.text is None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of 'audio' or 'text' for a voice turn.",
        )

    audio: bytes | None = None
    if body.audio is not None:
        try:
            audio = base64.b64decode(body.audio)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}") from exc
        if len(audio) > settings.voice_max_audio_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Audio exceeds {settings.voice_max_audio_bytes} bytes.",
            )

    service = VoiceChatService(voice=engine, chat=chat_service)
    outcome = await service.converse(
        session,
        settings,
        session_id=body.session_id,
        audio=audio,
        text=body.text,
        conversation_id=body.conversation_id,
        agent=body.agent,
        title=body.title,
        project_id=body.project_id,
    )
    return _serialize_converse(outcome)


@router.get("/audio/{audio_id}", summary="Fetch synthesized audio (WAV)")
async def get_audio(audio_id: str) -> Response:
    engine = get_voice_engine()
    for meta in engine.list_sessions():
        session = engine.get_session(meta["session_id"])
        if session is None:
            continue
        for clip in session.clips:
            if clip.id == audio_id:
                extension = clip.format if clip.format != "wav" else "wav"
                return Response(
                    content=clip.audio,
                    media_type=f"audio/{clip.format}",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{audio_id}.{extension}"'
                        )
                    },
                )
    raise HTTPException(status_code=404, detail=f"Unknown audio '{audio_id}'.")


@router.get("/status", summary="Voice engine status")
async def status() -> dict:
    return get_voice_engine().status()


# ---- WebSocket endpoint ----


@ws_router.websocket("/ws/{client_id}")
async def voice_ws(websocket: WebSocket, client_id: str) -> None:
    """Continuous-listening WebSocket for a voice session.

    The connection owns a private voice session (created on connect, destroyed
    on disconnect). Client frames (JSON):

    * ``{"type":"audio", "audio": "<base64>"}`` — buffer an audio chunk.
    * ``{"type":"final"}`` — transcribe the buffered utterance.
    * ``{"type":"text", "text": "..."}`` — record text directly.
    * ``{"type":"interrupt"}`` — interrupt active speech.
    * ``{"type":"respond", "text": "..."}`` — synthesize a spoken response.

    Server events:

    * ``{"type":"state", "state": ...}``
    * ``{"type":"wake", "session_id": ..., "wake_word": ..., "remainder": ...}``
    * ``{"type":"transcript", "utterance": {...}}``
    * ``{"type":"audio", "audio_id": ..., "audio": {...}}``
    * ``{"type":"interrupted", "interrupted": true}``
    * ``{"type":"error", "error": "..."}``
    """
    settings = get_settings()
    if not websocket_authorized(websocket, settings):
        await websocket.close(code=1008)  # policy violation — no credentials
        return
    await websocket.accept()
    engine = get_voice_engine()
    session = await engine.create_session()
    logger.info("Voice WebSocket connected: %s", client_id)

    async def emit(event: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(event, ensure_ascii=False))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await emit({"type": "error", "error": "Invalid JSON"})
                continue

            frame_type = data.get("type")
            if frame_type == "audio":
                try:
                    audio = base64.b64decode(data.get("audio", ""))
                except Exception:
                    await emit({"type": "error", "error": "Invalid audio base64"})
                    continue
                await engine.ingest_audio(session.session_id, audio)
                await emit({"type": "state", "state": session.state.value})
            elif frame_type == "final":
                record = await engine.finalize_utterance(session.session_id)
                if record is None:
                    await emit({"type": "state", "state": session.state.value})
                    continue
                await emit({"type": "transcript", "utterance": record.to_dict()})
                if record.was_wake:
                    await emit(
                        {
                            "type": "wake",
                            "wake_word": record.wake_word,
                            "remainder": record.remainder,
                        }
                    )
                await emit({"type": "state", "state": session.state.value})
            elif frame_type == "text":
                record = await engine.transcribe_text(session.session_id, data.get("text", ""))
                await emit({"type": "transcript", "utterance": record.to_dict()})
                if record.was_wake:
                    await emit(
                        {
                            "type": "wake",
                            "wake_word": record.wake_word,
                            "remainder": record.remainder,
                        }
                    )
            elif frame_type == "interrupt":
                interrupted = engine.interrupt(session.session_id)
                await emit({"type": "interrupted", "interrupted": interrupted})
            elif frame_type == "respond":
                clip = await engine.respond(session.session_id, data.get("text", ""))
                await emit(
                    {
                        "type": "audio",
                        "audio_id": clip.id,
                        "audio": clip.to_dict(),
                    }
                )
            else:
                await emit({"type": "error", "error": f"Unknown frame type '{frame_type}'."})
    except WebSocketDisconnect:
        engine.end_session(session.session_id)
        logger.info("Voice WebSocket disconnected: %s", client_id)