"""
IGRIS — Study Strategist & Task Monitor

Responsibilities:
  - Build study plans / strategies on request.
  - Track deadlines (assignments, vivas, submissions) in a small JSON store.
  - Report on deadline status (overdue / urgent / upcoming).
  - Report on the live agent task queue (agent/task_queue.py).
"""
import json
import sys
from datetime import datetime, date
from pathlib import Path

from agents.base_agent import ShadowAgent, AgentReport
from agents._llm import generate, generate_json


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
DEADLINES_PATH  = BASE_DIR / "memory" / "deadlines.json"


STUDY_SYSTEM_PROMPT = """You are IGRIS, BERU's study strategist — disciplined, methodical, and direct.
Produce a clear, actionable study strategy for the request given.

Rules:
- Structure the plan (e.g. by day/week, or by topic priority) — never just a wall of prose.
- Be concrete: name topics, suggested time blocks, and a priority order.
- If the request references specific modules, courses, or deadlines, work them in.
- Keep it practical for a student who wants to actually follow it, not a generic essay on study skills.
- Do not pad with motivational filler. Address the user as "Master" only in a brief opening line, then go straight into the plan.
"""

INTENT_SYSTEM_PROMPT = """Classify the request into exactly one category. Reply with ONLY the category word.

MST_SYNC         — the user wants assignments/deadlines/dashboard data checked or synced from
                    mySecondTeacher (MST) specifically.
ADD_DEADLINE     — the user is reporting/registering a new deadline, due date, submission date, exam date, or viva date to track.
DEADLINE_REPORT  — the user wants a status report on deadlines already being tracked (what's due, what's overdue, what's coming up).
TASK_STATUS      — the user wants a status report on background/running agent tasks or the task queue.
STUDY_PLAN       — anything else study/strategy related: study plans, prioritization, how to approach a subject, revision strategy.
"""

_MST_KEYWORDS = ("mysecondteacher", "my second teacher", "mst dashboard", "mst assignment", "mst sync", "sync mst")


def _load_deadlines() -> list[dict]:
    if not DEADLINES_PATH.exists():
        return []
    try:
        return json.loads(DEADLINES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_deadlines(items: list[dict]) -> None:
    DEADLINES_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEADLINES_PATH.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


class Igris(ShadowAgent):
    NAME  = "IGRIS"
    TITLE = "Study Strategist & Task Monitor"

    def handle(self, goal: str, speak=None) -> AgentReport:
        intent = self._classify(goal)
        print(f"[IGRIS] 📋 Intent: {intent}")

        try:
            if intent == "MST_SYNC":
                return self._mst_sync()
            elif intent == "ADD_DEADLINE":
                return self._add_deadline(goal)
            elif intent == "DEADLINE_REPORT":
                return self._deadline_report()
            elif intent == "TASK_STATUS":
                return self._task_status()
            else:
                return self._study_plan(goal)
        except Exception as e:
            return self._report(f"IGRIS hit an error: {e}", success=False)

    def _classify(self, goal: str) -> str:
        low = goal.lower()
        if any(k in low for k in _MST_KEYWORDS):
            return "MST_SYNC"
        try:
            text = generate(
                f"Request: {goal}",
                model_name="gemini-2.5-flash-lite",
                system_instruction=INTENT_SYSTEM_PROMPT,
            ).strip().upper()
            for opt in ("MST_SYNC", "ADD_DEADLINE", "DEADLINE_REPORT", "TASK_STATUS", "STUDY_PLAN"):
                if opt in text:
                    return opt
        except Exception as e:
            print(f"[IGRIS] ⚠️ Intent classification failed: {e}")
        return "STUDY_PLAN"

    def _add_deadline(self, goal: str) -> AgentReport:
        data = generate_json(
            f"Extract the deadline from this request.\nRequest: {goal}\n"
            f"Today's date: {date.today().isoformat()}",
            system_instruction=(
                'Return ONLY valid JSON: {"title": str, "due_date": "YYYY-MM-DD", "notes": str}. '
                "If no explicit date is given, make a reasonable estimate and explain the "
                "assumption in notes. Title should be short (max ~8 words)."
            ),
        )
        title    = data.get("title") or goal[:60]
        due_date = data.get("due_date", "")
        notes    = data.get("notes", "")

        entry = {
            "title": title,
            "due_date": due_date,
            "notes": notes,
            "added": datetime.now().isoformat(timespec="seconds"),
            "done": False,
        }
        items = _load_deadlines()
        items.append(entry)
        _save_deadlines(items)

        when = f"due {due_date}" if due_date else "with no clear date — please confirm one"
        return self._report(
            summary=f"Deadline tracked: '{title}', {when}.",
            details=notes,
        )

    def _deadline_report(self) -> AgentReport:
        items = [i for i in _load_deadlines() if not i.get("done")]
        if not items:
            return self._report("No active deadlines are being tracked, Master.")

        today = date.today()

        def sort_key(it):
            return it.get("due_date") or "9999-99-99"

        lines = []
        for it in sorted(items, key=sort_key):
            days_left = None
            try:
                d = datetime.strptime(it["due_date"], "%Y-%m-%d").date()
                days_left = (d - today).days
            except Exception:
                pass

            if days_left is None:
                tag, left_str = "🗓️", "date unclear"
            elif days_left < 0:
                tag, left_str = "⚠️ OVERDUE", f"{abs(days_left)}d ago"
            elif days_left <= 2:
                tag, left_str = "🔥 URGENT", f"{days_left}d left"
            else:
                tag, left_str = "🗓️", f"{days_left}d left"

            lines.append(f"{tag}  {it['title']} — {it.get('due_date', '?')} ({left_str})")

        details = "\n".join(lines)
        return self._report(f"{len(items)} active deadline(s) tracked.", details)

    def _task_status(self) -> AgentReport:
        from agent.task_queue import get_queue

        statuses = get_queue().get_all_statuses()
        if not statuses:
            return self._report("No tasks in the queue, Master.")

        counts: dict[str, int] = {}
        for s in statuses:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        summary_line = ", ".join(f"{v} {k}" for k, v in counts.items())

        details = "\n".join(
            f"[{s['task_id']}] {s['goal']} — {s['status']}" for s in statuses[-10:]
        )
        return self._report(f"Task queue status: {summary_line}.", details)

    def _mst_sync(self) -> AgentReport:
        from actions.mst_connector import sync_mst

        result = sync_mst()
        if not result.get("ok"):
            return self._report(result.get("message", "MST sync failed, Master."), success=False)

        assignments = result.get("assignments", [])
        if not assignments:
            return self._report("Synced with MST, Master — no assignments found.")

        items = _load_deadlines()
        existing_keys = {
            (i.get("title", "").strip().lower(), i.get("due_date", "")) for i in items
        }

        added = 0
        for a in assignments:
            key = (a.get("title", "").strip().lower(), a.get("due_date", ""))
            if not a.get("title") or key in existing_keys:
                continue
            items.append({
                "title": a.get("title"),
                "due_date": a.get("due_date", ""),
                "notes": f"Subject: {a.get('subject', 'Unknown')}. Status: {a.get('status', 'unknown')}.",
                "added": datetime.now().isoformat(timespec="seconds"),
                "done": a.get("status") == "submitted",
                "source": "mst",
            })
            existing_keys.add(key)
            added += 1
        _save_deadlines(items)

        return self._build_mst_summary(assignments, added)

    def _build_mst_summary(self, assignments: list[dict], added: int) -> AgentReport:
        pending = [a for a in assignments if a.get("status") != "submitted"]
        count   = len(pending)
        noun    = "assignment" if count == 1 else "assignments"

        nearest_line = ""
        dated = [a for a in pending if a.get("due_date")]
        if dated:
            dated.sort(key=lambda a: a["due_date"])
            nearest = dated[0]
            try:
                d = datetime.strptime(nearest["due_date"], "%Y-%m-%d").date()
                days_left = (d - date.today()).days
                if days_left < 0:
                    when = f"was due {abs(days_left)} day(s) ago"
                elif days_left == 0:
                    when = "due today"
                elif days_left == 1:
                    when = "due tomorrow"
                else:
                    when = f"due in {days_left} days"
                nearest_line = f" The nearest deadline is {when} — '{nearest['title']}'."
            except Exception:
                nearest_line = f" The nearest deadline is '{nearest['title']}' ({nearest['due_date']})."

        summary = f"Master, you have {count} pending {noun}.{nearest_line}"

        details_lines = [
            f"• {a.get('title', '(untitled)')} — {a.get('subject', '')} — "
            f"due {a.get('due_date') or 'unknown'} — {a.get('status', 'unknown')}"
            for a in assignments
        ]
        details = "\n".join(details_lines)
        if added:
            details += f"\n\n({added} new item(s) added to deadline tracking.)"

        return self._report(summary, details)

    def _study_plan(self, goal: str) -> AgentReport:
        plan = generate(goal, model_name="gemini-2.5-flash", system_instruction=STUDY_SYSTEM_PROMPT)
        return self._report("Study strategy prepared.", plan)
