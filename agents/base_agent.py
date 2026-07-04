"""
Common contract every shadow agent (IGRIS, DHANUS, TANK) implements,
so BERU's coordinator can treat them uniformly.
"""
from dataclasses import dataclass


@dataclass
class AgentReport:
    agent:   str            # short name, e.g. "IGRIS"
    title:   str            # role title, e.g. "Study Strategist & Task Monitor"
    summary: str            # one-line result, suitable for TTS / quick confirmation
    details: str = ""       # full body — the actual content to show the user
    success: bool = True


class ShadowAgent:
    NAME  = "AGENT"
    TITLE = "Shadow Soldier"

    def handle(self, goal: str, speak=None) -> AgentReport:
        """Every shadow agent must implement this. `goal` is the (sub)task assigned
        to this agent by the coordinator. `speak` is an optional callable for
        live voice feedback, same convention as agent/executor.py."""
        raise NotImplementedError

    def _report(self, summary: str, details: str = "", success: bool = True) -> AgentReport:
        return AgentReport(
            agent=self.NAME, title=self.TITLE,
            summary=summary, details=details, success=success,
        )
