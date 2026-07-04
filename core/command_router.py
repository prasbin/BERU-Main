"""
Route terminal / inbox commands to shadow_command or agent_task with mission tracking.
"""
from __future__ import annotations

from core.delegation import create_agent_task_mission, link_task_to_mission
from core.notifier import notify


_COMPLEX_HINTS = (
    " and then ",
    " step by step",
    "multi-step",
    "analyze all",
    "first ",
    "second ",
    "finally ",
)


def is_agent_task(text: str) -> bool:
    stripped = (text or "").strip()
    low = stripped.lower()
    if low.startswith("task:"):
        return True
    if len(stripped) < 90:
        return False
    if " and then " in low:
        return True
    hits = sum(1 for h in _COMPLEX_HINTS if h in low)
    return hits >= 2


def normalize_command(text: str) -> str:
    stripped = (text or "").strip()
    low = stripped.lower()
    if low.startswith("task:"):
        return stripped[5:].strip()
    if low.startswith("shadow:"):
        return stripped[7:].strip()
    return stripped


def execute_command(text: str, *, speak=None, notify_user: bool = True) -> str:
    text = normalize_command(text)
    if not text:
        return "Empty command."

    if is_agent_task(text):
        return _run_agent_task(text, speak=speak, notify_user=notify_user)
    return _run_shadow_command(text, speak=speak, notify_user=notify_user)


def _run_shadow_command(text: str, *, speak=None, notify_user: bool) -> str:
    from agents.coordinator import get_coordinator
    result = get_coordinator().handle(text, speak=speak)
    if notify_user:
        notify("BERU Report", result[:200])
    return result


def _run_agent_task(text: str, *, speak=None, notify_user: bool) -> str:
    from agent.task_queue import TaskPriority, get_queue

    mission = create_agent_task_mission(text)
    task_id = get_queue().submit(
        goal=text,
        priority=TaskPriority.NORMAL,
        speak=speak,
        mission_id=mission.mission_id,
    )
    link_task_to_mission(mission.mission_id, task_id)
    message = f"Mission [{mission.mission_id}] queued — task {task_id}."
    if notify_user:
        notify("BERU Mission", message[:120])
    return message
