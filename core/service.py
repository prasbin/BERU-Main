"""
BERU background service — headless daemon for scheduled missions and command inbox.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from core.command_router import execute_command
from core.missions import complete_mission, create_mission, fail_mission, status_summary
from core.notifier import notify
from core.scheduler import MissionScheduler, ScheduledJob, load_schedules
from core.wake import drain_commands, write_service_state


def get_runtime_dir() -> Path:
    from core.wake import RUNTIME_DIR
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def write_daemon_pid() -> None:
    pid_file = get_runtime_dir() / "daemon.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


def clear_daemon_pid() -> None:
    pid_file = get_runtime_dir() / "daemon.pid"
    pid_file.unlink(missing_ok=True)


def read_daemon_pid() -> int | None:
    pid_file = get_runtime_dir() / "daemon.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def is_daemon_running() -> bool:
    pid = read_daemon_pid()
    if pid is None:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run_scheduled_job(job: ScheduledJob, *, ui=None) -> None:
    mission = create_mission(
        title=job.title,
        goal=job.goal,
        agent=job.agent,
        scheduled=True,
        schedule_id=job.job_id,
    )
    notify("BERU Mission", f"{job.agent}: {job.title}")
    if ui:
        ui.write_log(f"SYS: Scheduled mission — {job.title}")
        if hasattr(ui, "refresh_missions"):
            ui.refresh_missions()

    try:
        if job.agent.upper() == "IGRIS":
            from agents.igris import Igris
            report = Igris().handle(job.goal)
            summary = report.summary
            if report.details:
                summary = f"{summary}\n{report.details}"
            complete_mission(mission.mission_id, summary[:2000])
            notify("BERU Report", report.summary[:200])
            if ui:
                ui.write_log(f"IGRIS: {report.summary[:200]}")
        else:
            from agents.coordinator import get_coordinator
            result = get_coordinator().handle(job.goal)
            complete_mission(mission.mission_id, result[:2000])
            notify("BERU Report", result[:200])
            if ui:
                ui.write_log(f"BERU: {result[:200]}")
    except Exception as e:
        fail_mission(mission.mission_id, str(e))
        notify("BERU Error", f"{job.title}: {e}")
        if ui:
            ui.write_log(f"ERR: {job.title} — {e}")
    finally:
        if ui and hasattr(ui, "refresh_missions"):
            ui.refresh_missions()


def _process_inbox_command(entry: dict) -> None:
    text = (entry.get("text") or "").strip()
    if not text:
        return
    notify("BERU Command", text[:120])
    try:
        execute_command(text, notify_user=True)
    except Exception as e:
        notify("BERU Error", str(e)[:200])


class BeruBackgroundService:
    def __init__(self):
        self._running = False
        self._scheduler = MissionScheduler(on_job=run_scheduled_job)

    def start(self) -> None:
        self._running = True
        write_daemon_pid()
        self._scheduler.start()
        threading.Thread(
            target=self._inbox_loop, daemon=True, name="BeruInbox"
        ).start()
        threading.Thread(
            target=self._heartbeat, daemon=True, name="BeruHeartbeat"
        ).start()
        print("[BERU Service] Background service online.")
        notify("BERU", "Shadow Army background service is online.")

    def stop(self) -> None:
        self._running = False
        self._scheduler.stop()
        clear_daemon_pid()

    def _inbox_loop(self) -> None:
        while self._running:
            for entry in drain_commands():
                _process_inbox_command(entry)
            time.sleep(1)

    def _heartbeat(self) -> None:
        while self._running:
            jobs = load_schedules()
            write_service_state({
                "mode": "daemon",
                "online": True,
                "pid": os.getpid(),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "schedules": [
                    {"id": j.job_id, "title": j.title, "enabled": j.enabled,
                     "time": f"{j.hour:02d}:{j.minute:02d}"}
                    for j in jobs
                ],
                **status_summary(),
            })
            time.sleep(10)


def run_daemon() -> None:
    if is_daemon_running():
        pid = read_daemon_pid()
        print(f"[BERU Service] Daemon already running (PID {pid}).")
        return

    print("[BERU Service] Starting headless background service...")
    print("[BERU Service] Press Ctrl+C to stop.")
    svc = BeruBackgroundService()
    svc.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        svc.stop()
        print("\n[BERU Service] Standing down.")
