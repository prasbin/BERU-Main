"""Document generation tool.

Allows agents to generate documents (markdown, text, code). Stub implementation
returns structured results ready to be connected to real generation backends.
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
        doc_format = kwargs.get("format", "markdown")
        title = kwargs.get("title", "Untitled Document")
        return ToolResult.success(
            {
                "title": title,
                "format": doc_format,
                "content": (
                    f"# {title}\n\n"
                    f"*Generated from prompt: {prompt}*\n\n"
                    "This is a stub document — connect to real generation backend."
                ),
                "word_count": 12,
            }
        )
