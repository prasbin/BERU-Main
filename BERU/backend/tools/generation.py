"""Document generation tool.

Allows agents to generate documents (markdown, text, code). The generation
backend is not implemented yet: the tool declares it needs the ``llm_api_key``
credential for real generation, and otherwise reports itself as unavailable
rather than returning fabricated documents.
"""

from __future__ import annotations

from typing import Any

from backend.tools.base import Tool, ToolResult


class DocumentGeneratorTool(Tool):
    """Generate a document from a prompt or template."""

    name = "document_generator"
    description = "Generate a document (markdown, text, or code) from a prompt."
    permissions = ["write"]
    availability = "unavailable"
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
        creds = self.credentials
        if not creds or not creds.get("llm_api_key"):
            return ToolResult.failure(
                "document_generator needs the llm_api_key credential, which is "
                "not configured. Provide an LLM_API_KEY to enable generation."
            )
        return ToolResult.failure(
            "document_generator is unavailable: the generation backend is not "
            "implemented, so no document can be produced yet."
        )
