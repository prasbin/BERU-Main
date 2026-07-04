"""
BERU — Supreme Coordinator

Classifies an incoming goal, delegates it (in whole or split into sub-goals)
to the relevant shadow agent(s) — IGRIS, DHANUS, TANK — or to the existing
general-purpose AgentExecutor for anything outside their domains, then
combines all resulting reports into a single response.
"""
from agents.base_agent import AgentReport
from agents._llm import generate_json
from core.delegation import create_shadow_mission, finalize_mission
from core import registry


class BERUCoordinator:
    def __init__(self):
        self._executor = None

    def _get_executor(self):
        if self._executor is None:
            from agent.executor import AgentExecutor
            self._executor = AgentExecutor()
        return self._executor

    def _get_classify_prompt(self) -> str:
        agents_info = []
        fallbacks = {
            "IGRIS": "studying, study plans/strategy, deadlines/due dates, assignment status (including mySecondTeacher / MST dashboard checks), status reports on the running agent task queue.",
            "DHANUS": "Hindu/Vedic scripture and philosophy, specifically: Mahabharata, Bhagavad Gita, Ramayana, Chanakya Niti, the Puranas, and Sanskrit verses generally — explaining philosophy/doctrine, translating, explaining narrative/historical context, or reciting/reading a verse aloud.",
            "TANK": "programming: writing/building code, explaining an algorithm or its complexity, a line-by-line breakdown of existing code, debugging an error, or optimizing code."
        }
        for name, inst in registry.get_agents().items():
            desc = getattr(inst, "CLASSIFICATION_INFO", None)
            if not desc:
                desc = fallbacks.get(name.upper(), inst.TITLE)
            agents_info.append(f"  {name.upper()}   — {desc}")
            
        agents_str = "\n".join(agents_info)
        
        return f"""You are BERU's delegation module. Split the user's request into one or
more assignments for your shadow soldiers.

Agents available:
{agents_str}
  GENERAL — everything else (opening apps, web search, weather, reminders unrelated to academic
            deadlines, messaging, file operations, etc.)

Return ONLY valid JSON:
{{"assignments": [{{"agent": "AGENT_NAME", "goal": "focused sub-goal text"}}]}}

Rules:
- If the request fits one domain, return exactly one assignment with the full request as the goal.
- If it clearly spans multiple domains (e.g. "make me a study plan and also write the code for a
  flashcard app"), split it into one assignment per agent, each with a focused, self-contained
  sub-goal — do not leave a sub-goal that depends on another assignment's output.
- For GENERAL assignments, use the full original request, unmodified.
- Always return at least one assignment.
"""

    def _classify(self, goal: str) -> list[dict]:
        system_prompt = self._get_classify_prompt()
        data = generate_json(
            f"Request: {goal}",
            model_name="gemini-2.5-flash-lite",
            system_instruction=system_prompt,
        )
        assignments = data.get("assignments", []) if isinstance(data, dict) else []
        
        valid_agents = list(registry.get_agents().keys()) + ["GENERAL"]
        valid = [
            a for a in assignments
            if isinstance(a, dict) and a.get("agent") in valid_agents and a.get("goal")
        ]
        if valid:
            return valid

        print("[BERU] ⚠️ Classification failed or empty — defaulting to GENERAL.")
        return [{"agent": "GENERAL", "goal": goal}]

    def handle(self, goal: str, speak=None) -> str:
        goal = (goal or "").strip()
        if not goal:
            return "I need a clear directive, Master."

        assignments = self._classify(goal)
        print(f"[BERU] 🗡️ Delegating: {assignments}")

        reports: list[AgentReport] = []

        for a in assignments:
            agent_name = a["agent"].upper()
            sub_goal   = a["goal"]
            mission    = create_shadow_mission(sub_goal, agent_name)
            try:
                agent_instance = registry.get_agent(agent_name)
                if agent_instance:
                    report = agent_instance.handle(sub_goal, speak=speak)
                elif agent_name == "GENERAL":
                    result = self._get_executor().execute(sub_goal, speak=None)
                    report = AgentReport(
                        agent="BERU", title="General Operations",
                        summary=result, details="", success=True,
                    )
                else:
                    raise ValueError(f"Unknown agent: {agent_name}")
                
                body = report.details.strip() if report.details else report.summary
                finalize_mission(
                    mission.mission_id,
                    success=report.success,
                    result=body or report.summary,
                )
                reports.append(report)
            except Exception as e:
                finalize_mission(mission.mission_id, success=False, result=str(e))
                reports.append(AgentReport(
                    agent=agent_name, title="", summary=f"Assignment failed: {e}",
                    details="", success=False,
                ))

        return self._combine(reports, speak=speak)

    def _combine(self, reports: list[AgentReport], speak=None) -> str:
        if len(reports) == 1:
            r = reports[0]
            final = r.details.strip() if r.details else r.summary
            if speak:
                speak(r.summary or "Task complete, Master.")
            return final

        sections = []
        for r in reports:
            label = "【BERU — General Ops】" if r.agent == "BERU" else f"【{r.agent}】"
            body  = r.details.strip() if r.details else r.summary
            sections.append(f"{label}\n{body}")

        combined = "\n\n".join(sections)

        if speak:
            speak(f"All shadows report in, Master. {len(reports)} fronts handled.")

        return f"Master, your shadow soldiers report:\n\n{combined}"


_coordinator: BERUCoordinator | None = None


def get_coordinator() -> BERUCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = BERUCoordinator()
    return _coordinator
