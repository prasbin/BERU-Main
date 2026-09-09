"""Plan engine — orchestrates task decomposition and step execution.

Uses an LLM to decompose a goal into ordered steps, then executes each step
using the appropriate agent and tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.engines.llm.base import LLMMessage, LLMProvider
from backend.engines.planning import Plan, PlanStatus, PlanStep, StepStatus

logger = logging.getLogger(__name__)

DECOMPOSITION_PROMPT = """\
You are a task decomposition assistant. Given a user goal, break it down into
a sequence of concrete, actionable steps.

For each step, provide:
- A clear description of what needs to be done
- The agent that should handle it (e.g. "beru_core", "igris", "dhanus", "tank"),
  or null if any agent can do it
- Any tools needed (e.g. "web_search", "file_analyser", "calendar")
- Dependencies on other step IDs (steps that must complete before this one)

Return ONLY a JSON array of steps, no explanation. Each step object:
[
  {
    "id": "step1",
    "description": "...",
    "agent": null,
    "tools": [],
    "dependencies": []
  }
]

Goal: {goal}
"""


class PlanEngine:
    """Orchestrates task decomposition and step execution."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def decompose(self, goal: str) -> Plan:
        """Use an LLM to decompose a goal into ordered steps."""
        prompt = DECOMPOSITION_PROMPT.format(goal=goal)
        messages = [LLMMessage(role="user", content=prompt)]

        response = await self._provider.chat(
            messages, temperature=0.3, max_tokens=2000
        )

        try:
            steps_data = json.loads(response.content)
            if not isinstance(steps_data, list):
                steps_data = [steps_data]
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse decomposition response, creating single-step plan")
            steps_data = [
                {
                    "id": "step1",
                    "description": goal,
                    "agent": None,
                    "tools": [],
                    "dependencies": [],
                }
            ]

        steps = [PlanStep.from_dict(s) for s in steps_data]
        return Plan(goal=goal, steps=steps, status=PlanStatus.DRAFT)

    def execute_step(self, plan: Plan, step_id: str) -> dict[str, Any]:
        """Execute a single step and update the plan.

        Returns a dict with the step execution result.
        """
        step = plan.get_step(step_id)
        if step is None:
            return {"error": f"Step '{step_id}' not found in plan"}

        # Check dependencies.
        completed_ids = {s.id for s in plan.completed_steps}
        unmet = [dep for dep in step.dependencies if dep not in completed_ids]
        if unmet:
            return {"error": f"Unmet dependencies: {', '.join(unmet)}"}

        # Mark as in progress.
        step.status = StepStatus.IN_PROGRESS
        plan.status = PlanStatus.IN_PROGRESS

        return {
            "step_id": step.id,
            "description": step.description,
            "agent": step.agent,
            "tools": step.tools,
            "status": "ready",
        }

    def complete_step(
        self, plan: Plan, step_id: str, result: str = "Done."
    ) -> None:
        """Mark a step as completed."""
        step = plan.get_step(step_id)
        if step is None:
            return
        step.status = StepStatus.COMPLETED
        step.result = result

        # Check if plan is complete.
        if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in plan.steps):
            plan.status = PlanStatus.COMPLETED
        else:
            plan.current_step_index = min(
                plan.current_step_index + 1, len(plan.steps) - 1
            )

    def fail_step(self, plan: Plan, step_id: str, error: str) -> None:
        """Mark a step as failed."""
        step = plan.get_step(step_id)
        if step is None:
            return
        step.status = StepStatus.FAILED
        step.error = error
        plan.status = PlanStatus.FAILED

    def skip_step(self, plan: Plan, step_id: str) -> None:
        """Skip a step (mark as skipped)."""
        step = plan.get_step(step_id)
        if step is None:
            return
        step.status = StepStatus.SKIPPED

        if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED) for s in plan.steps):
            plan.status = PlanStatus.COMPLETED

    def cancel_plan(self, plan: Plan) -> None:
        """Cancel a plan, marking all pending steps as skipped."""
        plan.status = PlanStatus.CANCELLED
        for step in plan.steps:
            if step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED
