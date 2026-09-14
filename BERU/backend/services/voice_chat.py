"""Full voice conversation chain: utterance to spoken reply.

Composes the voice session engine (STT/TTS providers) with the chat
orchestration service so one turn flows continuously:

    utterance (speech audio or text)
        -> wake-word aware transcription (VoiceEngine)
        -> ChatService.process (conversation memory + agent tool loop + LLM)
        -> spoken reply (VoiceEngine.respond)

The chain is honest end to end: with the hermetic mock LLM / mock STT / mock
TTS everything is simulated and :attr:`VoiceChatOutcome.simulated` is ``True``;
with real providers configured the same code path runs real speech
recognition, real reasoning, real tool calls and real synthesis. No step
fabricates a result — an empty transcript, no configured speech, or a reply
the providers could not produce surfaces as an explicit failure.

A voice session remembers the conversation it created, so consecutive
utterances on the same session continue the same chat thread (multi-turn voice
conversation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings
from backend.core.errors import BadRequestError
from backend.engines.llm.base import LLMUsage
from backend.engines.speech import AudioClip
from backend.engines.voice import UtteranceRecord, VoiceEngine
from backend.schemas.chat import ChatRequest
from backend.services.chat_service import ChatService


@dataclass
class VoiceChatOutcome:
    """The result of one voice conversation turn."""

    utterance: UtteranceRecord
    conversation_id: str
    reply: str
    agent: str
    model: str | None
    provider: str
    tool_calls_made: int
    usage: LLMUsage | None
    clip: AudioClip
    pending_confirmations: list[dict] = field(default_factory=list)

    @property
    def simulated(self) -> bool:
        """True when the reply came from the deterministic mock LLM provider."""
        return self.provider.lower() == "mock"


class VoiceChatService:
    """One voice turn: transcribe -> reason (with the agent tool loop) -> speak."""

    def __init__(self, *, voice: VoiceEngine, chat: ChatService) -> None:
        self._voice = voice
        self._chat = chat

    async def converse(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        session_id: str,
        audio: bytes | None = None,
        text: str | None = None,
        conversation_id: str | None = None,
        agent: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> VoiceChatOutcome:
        """Run one full voice conversation turn on ``session_id``.

        Provide exactly one of ``audio`` (speech bytes) or ``text``. The
        utterance is transcribed wake-word aware, the instruction goes through
        :meth:`ChatService.process` (conversation memory + agent tool loop +
        LLM), and the LLM's reply is synthesized into a spoken ``AudioClip``.
        """
        if (audio is None) == (text is None):
            raise BadRequestError("Provide exactly one of 'audio' or 'text' for a voice turn.")

        if audio is not None:
            await self._voice.ingest_audio(session_id, audio)
            record = await self._voice.finalize_utterance(session_id)
            if record is None:
                raise BadRequestError("No audio was buffered for the voice session to transcribe.")
        else:
            record = await self._voice.transcribe_text(session_id, text or "")

        message = (record.remainder if record.was_wake else record.text).strip()
        if not message:
            raise BadRequestError("The voice utterance contained no speech to act on.")

        voice_session = self._voice.get_session(session_id)
        bound = voice_session.conversation_id if voice_session is not None else None
        chat_outcome = await self._chat.process(
            session,
            settings,
            ChatRequest(
                message=message,
                conversation_id=conversation_id or bound,
                agent=agent,
                title=title,
                project_id=project_id,
            ),
        )
        if voice_session is not None:
            voice_session.conversation_id = chat_outcome.conversation.id

        reply = chat_outcome.result.content or ""
        clip = await self._voice.respond(session_id, reply)

        return VoiceChatOutcome(
            utterance=record,
            conversation_id=chat_outcome.conversation.id,
            reply=reply,
            agent=chat_outcome.result.agent,
            model=chat_outcome.result.model,
            provider=self._chat.provider_name,
            tool_calls_made=chat_outcome.result.tool_calls_made,
            usage=chat_outcome.result.usage,
            clip=clip,
            pending_confirmations=chat_outcome.pending_confirmations or [],
        )