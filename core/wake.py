"""
Wake-word detection and runtime IPC flags for BERU dormant/active modes.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


RUNTIME_DIR    = get_base_dir() / "memory" / "runtime"
WAKE_FLAG      = RUNTIME_DIR / "wake.flag"
DORMANT_FLAG   = RUNTIME_DIR / "dormant.flag"
COMMAND_INBOX  = RUNTIME_DIR / "command_inbox.jsonl"
SERVICE_STATE  = RUNTIME_DIR / "service_state.json"

_WAKE_RE = re.compile(
    r"^\s*(hey\s+)?beru[\s!.?]*$|^\s*beru[\s,!.?]*(wake|activate|online|listen)\b",
    re.IGNORECASE,
)


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def is_wake_phrase(text: str) -> bool:
    return bool(_WAKE_RE.match(text.strip()))


def request_wake() -> None:
    ensure_runtime_dir()
    WAKE_FLAG.write_text(str(time.time()), encoding="utf-8")


def consume_wake_request() -> bool:
    if not WAKE_FLAG.exists():
        return False
    try:
        WAKE_FLAG.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def request_dormant() -> None:
    ensure_runtime_dir()
    DORMANT_FLAG.write_text(str(time.time()), encoding="utf-8")


def consume_dormant_request() -> bool:
    if not DORMANT_FLAG.exists():
        return False
    try:
        DORMANT_FLAG.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def enqueue_command(text: str, *, wake: bool = True) -> None:
    ensure_runtime_dir()
    entry = {"text": text.strip(), "wake": wake, "ts": time.time()}
    with COMMAND_INBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def drain_commands() -> list[dict]:
    if not COMMAND_INBOX.exists():
        return []
    try:
        lines = COMMAND_INBOX.read_text(encoding="utf-8").splitlines()
        COMMAND_INBOX.unlink(missing_ok=True)
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"text": line, "wake": True, "ts": time.time()})
        return out
    except Exception:
        return []


def write_service_state(state: dict) -> None:
    ensure_runtime_dir()
    SERVICE_STATE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def read_service_state() -> dict:
    if not SERVICE_STATE.exists():
        return {}
    try:
        return json.loads(SERVICE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}
