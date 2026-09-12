"""Durable JSON stores shared by tool backends.

Calendar events and tasks, study knowledge notes, flashcards, and meditation
sessions have no natural home in BERU's relational store (which owns
conversations, facts, projects, notifications, scheduler/monitor state), so
they persist to compact JSON files under an injectable data directory.

Writes are atomic (temp file + :func:`os.replace`): a crash mid-write never
leaves a half-written store behind. The store path is supplied in each tool's
constructor — the agent registry uses the project's ``data/`` directory while
tests pass a ``tmp_path`` so the suite stays hermetic.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# .../BERU/backend/tools/stores.py -> parents[3] == project root (BERU/).
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class StoreError(Exception):
    """Raised when a tool store cannot be read or written."""


def default_data_dir() -> Path:
    """Return the project ``data/`` directory (created on first write)."""
    return PROJECT_ROOT / "data"


def load_json(path: Path) -> Any:
    """Parse a JSON file, raising :class:`StoreError` on corruption.

    ``None`` is returned when the file does not exist — callers supply the
    default document they expect.
    """
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise StoreError(f"Could not read store '{path}': {exc}") from exc


def save_json(path: Path, data: Any) -> None:
    """Atomically write ``data`` to ``path`` (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class JsonStore:
    """A minimal JSON document store with atomic writes.

    ``load`` returns the caller-supplied default when no file exists yet and
    raises :class:`StoreError` when the file is unreadable or malformed.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self, default: Any) -> Any:
        return load_json(self.path) if self.path.exists() else default

    def save(self, data: Any) -> None:
        save_json(self.path, data)