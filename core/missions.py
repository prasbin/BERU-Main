"""
Mission registry — tracks active, pending, and completed objectives for BERU.
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR      = get_base_dir()
MISSIONS_PATH = BASE_DIR / "memory" / "missions.json"
_lock         = Lock()


class MissionStatus(str, Enum):
    PENDING   = "pending"
    ACTIVE    = "active"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class Mission:
    mission_id:  str
    title:       str
    goal:        str
    agent:       str = "BERU"
    status:      MissionStatus = MissionStatus.PENDING
    created_at:  str = field(default_factory=lambda: _now())
    updated_at:  str = field(default_factory=lambda: _now())
    result:      str = ""
    scheduled:   bool = False
    schedule_id: str = ""
    task_id:     str = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_raw() -> list[dict]:
    if not MISSIONS_PATH.exists():
        return []
    try:
        data = json.loads(MISSIONS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[Missions] ⚠️ Load error: {e}")
        return []


def _save_raw(items: list[dict]) -> None:
    MISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MISSIONS_PATH.write_text(
            json.dumps(items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def list_missions(status: MissionStatus | None = None) -> list[Mission]:
    items = []
    for raw in _load_raw():
        try:
            m = Mission(
                mission_id=raw["mission_id"],
                title=raw.get("title", ""),
                goal=raw.get("goal", ""),
                agent=raw.get("agent", "BERU"),
                status=MissionStatus(raw.get("status", "pending")),
                created_at=raw.get("created_at", _now()),
                updated_at=raw.get("updated_at", _now()),
                result=raw.get("result", ""),
                scheduled=bool(raw.get("scheduled", False)),
                schedule_id=raw.get("schedule_id", ""),
                task_id=raw.get("task_id", ""),
            )
            if status is None or m.status == status:
                items.append(m)
        except Exception:
            continue
    return items


def get_active_missions() -> list[Mission]:
    return [
        m for m in list_missions()
        if m.status in (MissionStatus.ACTIVE, MissionStatus.PENDING)
    ]


def create_mission(
    title: str,
    goal: str,
    agent: str = "BERU",
    *,
    scheduled: bool = False,
    schedule_id: str = "",
    activate: bool = True,
) -> Mission:
    mission = Mission(
        mission_id=str(uuid.uuid4())[:8],
        title=title[:80],
        goal=goal,
        agent=agent,
        status=MissionStatus.ACTIVE if activate else MissionStatus.PENDING,
        scheduled=scheduled,
        schedule_id=schedule_id,
    )
    items = _load_raw()
    items.append(asdict(mission) | {"status": mission.status.value})
    _save_raw(items)
    print(f"[Missions] Created [{mission.mission_id}] {mission.title}")
    return mission


def update_mission(
    mission_id: str,
    *,
    status: MissionStatus | None = None,
    result: str | None = None,
    task_id: str | None = None,
) -> Mission | None:
    items = _load_raw()
    updated = None
    for raw in items:
        if raw.get("mission_id") != mission_id:
            continue
        if status is not None:
            raw["status"] = status.value
        if result is not None:
            raw["result"] = result
        if task_id is not None:
            raw["task_id"] = task_id
        raw["updated_at"] = _now()
        updated = raw
        break
    if updated:
        _save_raw(items)
    return _dict_to_mission(updated) if updated else None


def complete_mission(mission_id: str, result: str = "") -> None:
    update_mission(mission_id, status=MissionStatus.COMPLETED, result=result)


def fail_mission(mission_id: str, error: str = "") -> None:
    update_mission(mission_id, status=MissionStatus.FAILED, result=error)


def status_summary() -> dict:
    missions = list_missions()
    by_status: dict[str, int] = {}
    for m in missions:
        by_status[m.status.value] = by_status.get(m.status.value, 0) + 1
    active = get_active_missions()
    return {
        "commander": "BERU",
        "agents": ["IGRIS", "TANK", "DHANUS"],
        "active_missions": [
            {"id": m.mission_id, "title": m.title, "agent": m.agent, "status": m.status.value}
            for m in active[:20]
        ],
        "counts": by_status,
    }


def _dict_to_mission(raw: dict | None) -> Mission | None:
    if not raw:
        return None
    return Mission(
        mission_id=raw["mission_id"],
        title=raw.get("title", ""),
        goal=raw.get("goal", ""),
        agent=raw.get("agent", "BERU"),
        status=MissionStatus(raw.get("status", "pending")),
        created_at=raw.get("created_at", _now()),
        updated_at=raw.get("updated_at", _now()),
        result=raw.get("result", ""),
        scheduled=bool(raw.get("scheduled", False)),
        schedule_id=raw.get("schedule_id", ""),
        task_id=raw.get("task_id", ""),
    )
