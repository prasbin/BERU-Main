"""Document generation tool.

Drives the configured LLM provider (see :func:`backend.engines.llm.registry
.build_provider`) to produce a real document: the prompt, title, and format are
sent to the actual provider and the body is the provider's genuine output.

The tool declares it needs the ``llm_api_key`` credential (the agent injects a
scoped view — never the global owner key) and a *real* provider. With
``LLM_PROVIDER=mock`` it fails honestly rather than shipping a simulated
placeholder document that would look authoritative but be generated offline.
"""

from __future__ import annotations

from typing import Any

from backend.core.config import get_settings
from backend.core.errors import ConfigurationError, LLMProviderError
from backend.engines.llm.base import LLMMessage
from backend.engines.llm.mock import MockProvider
from backend.engines.llm.registry import build_provider
from backend.tools.base import Tool, ToolResult

_FORMATS = ("markdown", "text", "code")

_GENERATION_SYSTEM_PROMPT = (
    "You are BERU's document generator. Write the complete document requested "
    "in the message as the final response — do not describe it, do not ask "
    "follow-up questions. Use only the requested output format."
)


class DocumentGeneratorTool(Tool):
    """Generate a document from a prompt or template."""

    name = "document_generator"
    description = "Generate a document (markdown, text, or code) from a prompt."
    permissions = ["write"]
    availability = "limited"  # works whenever a real (non-mock) LLM provider is configured
    # Real generation needs an LLM provider; the agent injects exactly this
    # key (scoped — never the global owner key) before running the tool.
    required_credentials = ["llm_api_key"]
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The prompt or instructions for document generation.",
            },
            "format": {
                "type": "string",
                "description": "Output format: 'markdown', 'text', 'code'.",
            },
            "title": {
                "type": "string",
                "description": "Optional title for the document.",
            },
        },
        "required": ["prompt"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        prompt = kwargs.get("prompt", "")
        document_format = kwargs.get("format") or "markdown"
        title = kwargs.get("title") or ""

        if not prompt or not prompt.strip():
            return ToolResult.failure("document_generator requires a non-empty 'prompt'.")
        if document_format not in _FORMATS:
            return ToolResult.failure(
                f"Unsupported format '{document_format}'. Use one of {', '.join(_FORMATS)}."
            )

        creds = self.credentials
        if not creds or not creds.get("llm_api_key"):
            return ToolResult.failure(
                "document_generator needs the llm_api_key credential, which is "
                "not configured. Provide an LLM_API_KEY to enable generation."
            )

        try:
            provider_settings = getattr(self, "settings", None) or get_settings()
            provider = build_provider(provider_settings)
        except ConfigurationError as exc:
            return ToolResult.failure(
                f"document_generator cannot build an LLM provider: {exc}"
            )

        if isinstance(provider, MockProvider):
            return ToolResult.failure(
                "document_generator requires a real LLM provider, but "
                "LLM_PROVIDER=mock is a deterministic offline simulation. Set "
                "LLM_PROVIDER=openai_compatible (with LLM_BASE_URL, LLM_API_KEY, "
                "LLM_MODEL) to enable real document generation."
            )

        instruction = _build_generation_prompt(prompt, document_format, title)
        try:
            response = await provider.chat(
                [
                    LLMMessage(role="system", content=_GENERATION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=instruction),
                ]
            )
        except LLMProviderError as exc:
            return ToolResult.failure(f"document_generator failed: {exc}")

        content = (response.content or "").strip()
        if not content:
            return ToolResult.failure(
                "document_generator failed: the LLM provider returned an empty document."
            )

        return ToolResult.success(
            {
                "title": title,
                "format": document_format,
                "content": content,
                "word_count": len(content.split()),
                "model": response.model or "unknown",
            }
        )


def _build_generation_prompt(prompt: str, document_format: str, title: str) -> str:
    """Compose the user-facing generation instruction for the provider."""
    parts: list[str] = [f"Output format: {document_format}."]
    if title:
        parts.append(f"Document title: {title}.")
    parts.append("")
    parts.append(prompt.strip())
    return "\n".join(parts)