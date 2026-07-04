"""
Background job scheduler for BERU — runs recurring missions (e.g. morning MST check).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


def get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR       = get_base_dir()
SCHEDULES_PATH = BASE_DIR / "config" / "beru_schedules.json"
LAST_RUN_PATH  = BASE_DIR / "memory" / "runtime" / "schedule_last_run.json"


@dataclass
class ScheduledJob:
    job_id:    str
    title:     str
    goal:      str
    agent:     str
    hour:      int
    minute:    int
    weekdays:  list[int]  # 0=Mon .. 6=Sun; empty = every day
    enabled:   bool = True


def _default_schedules() -> list[dict]:
    return [
        {
            "job_id": "mst_morning_check",
            "title": "Assignment Monitoring",
            "goal": (
                "Check mySecondTeacher for new assignments, notices, deadlines, "
                "announcements and updates. Summarize anything urgent."
            ),
            "agent": "IGRIS",
            "hour": 7,
            "minute": 0,
            "weekdays": [0, 1, 2, 3, 4],
            "enabled": True,
        }
    ]


def _read_schedule_file() -> list[dict]:
    if not SCHEDULES_PATH.exists():
        SCHEDULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"jobs": _default_schedules()}
        SCHEDULES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        raw = json.loads(SCHEDULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {"jobs": _default_schedules()}
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        jobs = raw.get("jobs", [])
        return jobs if isinstance(jobs, list) else _default_schedules()
    return _default_schedules()


def load_schedules() -> list[ScheduledJob]:
    raw = _read_schedule_file()
    jobs = []
    for item in raw:
        try:
            jobs.append(ScheduledJob(
                job_id=item["job_id"],
                title=item.get("title", item["job_id"]),
                goal=item["goal"],
                agent=item.get("agent", "IGRIS"),
                hour=int(item.get("hour", 7)),
                minute=int(item.get("minute", 0)),
                weekdays=[int(d) for d in item.get("weekdays", [])],
                enabled=bool(item.get("enabled", True)),
            ))
        except Exception as e:
            print(f"[Scheduler] Skipping bad job: {e}")
    return jobs


def _load_last_runs() -> dict[str, str]:
    if not LAST_RUN_PATH.exists():
        return {}
    try:
        return json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_run(job_id: str) -> None:
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    runs = _load_last_runs()
    runs[job_id] = datetime.now().isoformat(timespec="seconds")
    LAST_RUN_PATH.write_text(json.dumps(runs, indent=2), encoding="utf-8")


def _already_ran_today(job_id: str) -> bool:
    ts = _load_last_runs().get(job_id, "")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts).date()
        return last == datetime.now().date()
    except Exception:
        return False


def _job_due(job: ScheduledJob, now: datetime) -> bool:
    if not job.enabled:
        return False
    if now.hour != job.hour or now.minute != job.minute:
        return False
    if job.weekdays and now.weekday() not in job.weekdays:
        return False
    if _already_ran_today(job.job_id):
        return False
    return True


class MissionScheduler:
    """Polls schedule config and fires due jobs through a callback."""

    def __init__(self, on_job: Callable[[ScheduledJob], None]):
        self._on_job   = on_job
        self._running  = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="BeruScheduler"
        )
        self._thread.start()
        print("[Scheduler] Started")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                now = datetime.now()
                for job in load_schedules():
                    if _job_due(job, now):
                        print(f"[Scheduler] Firing: {job.title}")
                        _save_last_run(job.job_id)
                        try:
                            self._on_job(job)
                        except Exception as e:
                            print(f"[Scheduler] Job failed: {e}")
            except Exception as e:
                print(f"[Scheduler] Loop error: {e}")
            time.sleep(30)
