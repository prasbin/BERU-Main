"""
Mission helpers for BERU delegation (shadow_command + agent_task).
"""
from __future__ import annotations

from core.missions import Mission, MissionStatus, create_mission, update_mission


def mission_title(goal: str, max_len: int = 60) -> str:
    text = " ".join((goal or "").split())
    if len(text) <= max_len:
        return text or "Mission"
    return text[: max_len - 3] + "..."


def create_agent_task_mission(goal: str) -> Mission:
    return create_mission(
        title=mission_title(goal),
        goal=goal,
        agent="BERU",
        activate=True,
    )


def create_shadow_mission(goal: str, agent: str) -> Mission:
    agent_label = "BERU" if agent == "GENERAL" else agent
    return create_mission(
        title=mission_title(goal),
        goal=goal,
        agent=agent_label,
        activate=True,
    )


def link_task_to_mission(mission_id: str, task_id: str) -> None:
    update_mission(mission_id, task_id=task_id)


def finalize_mission(mission_id: str, *, success: bool, result: str) -> None:
    status = MissionStatus.COMPLETED if success else MissionStatus.FAILED
    update_mission(mission_id, status=status, result=result[:4000])
