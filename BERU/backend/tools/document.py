"""File and document analysis tool.

Reads and analyses real files from disk: the ``summary``/``stats``/``structure``
views are all computed from the actual file bytes — never fabricated. Reading is
capped so one oversized file cannot balloon the agent context; when a file
exceeds the cap the tool says so rather than pretending it read everything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.tools.base import Tool, ToolResult

#: Maximum number of bytes read from a file in one analysis pass.
_MAX_ANALYSIS_BYTES = 256 * 1024

#: Extension -> language label used by the (real, heuristic) language guess.
_LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
    ".txt": "text",
    ".json": "json",
    ".html": "html",
    ".css": "css",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sql": "sql",
    ".sh": "shell",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "c++",
    ".cxx": "c++",
}

#: Prefixes that indicate a structural heading/marker line in the source.
_STRUCTURE_MARKERS = (
    "# ",
    "## ",
    "### ",
    "def ",
    "class ",
    "function ",
    "func ",
    "const ",
)


def guess_language(path: str | Path) -> str:
    """Guess a language label from a file's extension (best-effort)."""
    return _LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower(), "unknown")


class FileAnalyserTool(Tool):
    """Read and analyse the content of a file at a given path."""

    name = "file_analyser"
    description = "Read and analyse the content of a file at a given path."
    permissions = ["read"]
    availability = "available"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The file path to analyse.",
            },
            "analysis_type": {
                "type": "string",
                "description": "Type of analysis: 'summary', 'stats', 'structure'.",
            },
        },
        "required": ["path"],
    }
    #: Cap on the number of lines surfaced for 'summary' / 'structure' views.
    summary_line_cap: int = 40

    async def run(self, **kwargs: Any) -> ToolResult:
        path_arg = kwargs.get("path", "")
        analysis_type = kwargs.get("analysis_type", "stats")
        if not path_arg:
            return ToolResult.failure("file_analyser requires a 'path' parameter.")

        analysis_types = ("summary", "stats", "structure")
        if analysis_type not in analysis_types:
            return ToolResult.failure(
                f"Unknown analysis_type '{analysis_type}'. "
                f"Use one of {', '.join(analysis_types)}."
            )

        try:
            file_path = Path(path_arg).expanduser()
        except (OSError, ValueError) as exc:
            return ToolResult.failure(f"Invalid file path: {exc}")

        if not file_path.is_file():
            return ToolResult.failure(f"No such file: {file_path}")

        try:
            stat = file_path.stat()
        except OSError as exc:
            return ToolResult.failure(f"Could not stat '{file_path}': {exc}")

        try:
            with open(file_path, "rb") as handle:
                raw = handle.read(_MAX_ANALYSIS_BYTES)
        except OSError as exc:
            return ToolResult.failure(f"Could not read '{file_path}': {exc}")

        truncated = stat.st_size > _MAX_ANALYSIS_BYTES
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        nonblank = [ln for ln in lines if ln.strip()]

        base = {
            "path": str(file_path),
            "analysis_type": analysis_type,
            "exists": True,
            "size_bytes": stat.st_size,
            "truncated": truncated,
            "language": guess_language(file_path),
        }

        if analysis_type == "stats":
            base.update(
                {
                    "line_count": len(lines),
                    "nonblank_line_count": len(nonblank),
                    "character_count": len(text),
                    "bytes_analysed": len(raw),
                }
            )
            return ToolResult.success(base)

        if analysis_type == "summary":
            visible = lines[: self.summary_line_cap]
            base.update(
                {
                    "line_count": len(lines),
                    "nonblank_line_count": len(nonblank),
                    "lines_shown": len(visible),
                    "content": "\n".join(visible),
                }
            )
            return ToolResult.success(base)

        # structure: real scan for heading/marker lines (numbering included).
        markers = [
            {"line": index + 1, "text": ln.strip()}
            for index, ln in enumerate(lines)
            if ln.lstrip().startswith(_STRUCTURE_MARKERS)
        ]
        base.update({"markers": markers[: self.summary_line_cap], "marker_count": len(markers)})
        return ToolResult.success(base)