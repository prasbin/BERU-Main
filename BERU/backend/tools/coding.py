"""Coding tools for the TANK agent.

Tools to assist with programming, code analysis, and software development.
``code_formatter`` piped Python through a real formatter (ruff, or black when
only that is installed). It never pretends formatting happened: when no
formatter exists for the requested language the tool says so and returns the
code unmodified.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from typing import Any

from backend.tools.base import Tool, ToolResult


@lru_cache(maxsize=1)
def _python_formatter() -> list[str] | None:
    """Return the argv prefix of an installed Python formatter, or ``None``.

    Checks ruff first (module or binary), then black. The result is cached per
    process so the tool does not fork a probe on every call.
    """
    candidates: list[list[str]] = [["python", "-m", "ruff"], ["ruff"], ["black"]]
    for command in candidates:
        try:
            result = subprocess.run(
                [*command, "--version"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return command
    return None


class CodeAnalyzerTool(Tool):
    """Analyse code for quality, complexity, and potential issues.

    The analysis is a local, purely heuristic pass (line counts and a crude
    size-based complexity estimate) computed directly from the supplied code.
    It never claims to perform semantic or AST-level analysis it cannot do.
    """

    name = "code_analyser"
    description = "Analyse code for complexity, potential bugs, and improvement opportunities."
    permissions = ["read"]
    availability = "limited"
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The source code to analyse.",
            },
            "language": {
                "type": "string",
                "description": "Programming language (e.g. 'python', 'javascript').",
            },
        },
        "required": ["code"],
    }

    async def run(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        language = kwargs.get("language", "unknown")
        lines = code.splitlines()
        nonblank = [ln for ln in lines if ln.strip()]
        return ToolResult.success(
            {
                "language": language,
                "line_count": len(lines),
                "nonblank_line_count": len(nonblank),
                "character_count": len(code),
                "complexity_estimate": "low" if len(lines) < 50 else "medium",
                "note": (
                    "Heuristic analysis only: metrics are computed from raw "
                    "line/character counts. No semantic or AST analysis is "
                    "performed."
                ),
            }
        )


#: Language -> extension used for the formatter's ``--stdin-filename``.
_FORMAT_EXTENSIONS = {
    "python": ".py",
    "py": ".py",
    "pyi": ".pyi",
}


class CodeFormatterTool(Tool):
    """Format code according to language conventions."""

    name = "code_formatter"
    description = "Format code according to language-specific style conventions."
    permissions = ["read"]
    availability = "limited"  # works for languages with an installed formatter
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The source code to format.",
            },
            "language": {
                "type": "string",
                "description": "Programming language (e.g. 'python', 'javascript').",
            },
        },
        "required": ["code"],
    }

    def __init__(self, formatter: list[str] | None = None) -> None:
        #: Injectable argv prefix (tests), defaulting to the installed probe.
        self._formatter = formatter

    async def run(self, **kwargs: Any) -> ToolResult:
        code = kwargs.get("code", "")
        language = kwargs.get("language", "unknown").lower().strip()

        extension = _FORMAT_EXTENSIONS.get(language)
        if extension is None:
            return ToolResult.failure(
                f"code_formatter has no formatter backend for language "
                f"'{language}'. Supported: {', '.join(sorted(_FORMAT_EXTENSIONS))}."
            )

        formatter = self._formatter if self._formatter is not None else _python_formatter()
        if formatter is None:
            return ToolResult.failure(
                "code_formatter is unavailable for Python: no formatter (ruff or "
                "black) is installed on this host. The code is returned unmodified."
            )

        try:
            result = subprocess.run(
                [*formatter, "format", f"--stdin-filename=stdin{extension}", "-"],
                input=code.encode("utf-8"),
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult.failure(f"code_formatter could not run {formatter[0]}: {exc}")

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            return ToolResult.failure(
                "code_formatter failed (the code may not be valid "
                f"{language}): {stderr or 'unknown formatter error'}"
            )

        formatted = result.stdout.decode("utf-8", errors="replace")
        return ToolResult.success(
            {
                "language": language,
                "formatter": formatter[-1],
                "changed": formatted != code,
                "formatted_code": formatted,
            }
        )
