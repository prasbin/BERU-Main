"""TANK — coding and software development agent.

Focused on programming, debugging, code review, architecture, and
software engineering best practices.
"""

from __future__ import annotations

from backend.agents.base import BaseAgent

TANK_SYSTEM_PROMPT = (
    "You are TANK, BERU's coding and software development specialist. You are "
    "an expert programmer who writes clean, efficient, and well-documented code. "
    "You help with debugging, code review, architecture decisions, algorithm "
    "design, and learning new technologies. You follow best practices: write "
    "tests, handle errors gracefully, and explain your reasoning. When reviewing "
    "code, you focus on correctness, readability, performance, and security."
)


class TankAgent(BaseAgent):
    name = "tank"
    description = "Coding specialist — programming, debugging, code review, architecture."
    capabilities = [
        "programming",
        "debugging",
        "code_review",
        "architecture",
        "software_engineering",
    ]
    default_system_prompt = TANK_SYSTEM_PROMPT
