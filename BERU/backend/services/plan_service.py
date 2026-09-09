"""Plan management service.

Stores and manages plans in memory (can be extended to use database persistence).
Provides CRUD operations and plan execution orchestration.
"""

from __future__ import annotations

from typing import Any

from backend.core.errors import NotFoundError
from backend.engines.plan_engine import PlanEngine
from backend.engines.planning import Plan, PlanStatus


class PlanService:
    """Manages plans and coordinates their execution."""

    def __init__(self, plan_engine: PlanEngine | None = None) -> None:
        self._engine = plan_engine or PlanEngine(provider=None)  # type: ignore[arg-type]
        self._plans: dict[str, Plan] = {}

    async def create_plan(self, goal: str) -> Plan:
        """Decompose a goal into a plan using the LLM."""
        plan = await self._engine.decompose(goal)
        self._plans[plan.id] = plan
        return plan

    def create_manual_plan(self, goal: str, steps: list[dict[str, Any]]) -> Plan:
        """Create a plan manually from provided steps."""
        from backend.engines.planning import PlanStep

        plan_steps = [PlanStep.from_dict(s) for s in steps]
        plan = Plan(goal=goal, steps=plan_steps, status=PlanStatus.DRAFT)
        self._plans[plan.id] = plan
        return plan

    def get_plan(self, plan_id: str) -> Plan:
        """Get a plan by ID."""
        plan = self._plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"Plan '{plan_id}' not found.")
        return plan

    def list_plans(self) -> list[Plan]:
        """List all plans."""
        return list(self._plans.values())

    def list_active_plans(self) -> list[Plan]:
        """List plans that are in progress or draft."""
        return [
            p
            for p in self._plans.values()
            if p.status in (PlanStatus.DRAFT, PlanStatus.IN_PROGRESS)
        ]

    def start_step(self, plan_id: str, step_id: str) -> dict[str, Any]:
        """Start execution of a step."""
        plan = self.get_plan(plan_id)
        result = self._engine.execute_step(plan, step_id)
        if "error" in result:
            return result
        return {**result, "plan_status": plan.status.value}

    def complete_step(
        self, plan_id: str, step_id: str, result_text: str = "Done."
    ) -> Plan:
        """Mark a step as completed."""
        plan = self.get_plan(plan_id)
        self._engine.complete_step(plan, step_id, result_text)
        return plan

    def fail_step(self, plan_id: str, step_id: str, error: str) -> Plan:
        """Mark a step as failed."""
        plan = self.get_plan(plan_id)
        self._engine.fail_step(plan, step_id, error)
        return plan

    def skip_step(self, plan_id: str, step_id: str) -> Plan:
        """Skip a step."""
        plan = self.get_plan(plan_id)
        self._engine.skip_step(plan, step_id)
        return plan

    def cancel_plan(self, plan_id: str) -> Plan:
        """Cancel a plan."""
        plan = self.get_plan(plan_id)
        self._engine.cancel_plan(plan)
        return plan

    def delete_plan(self, plan_id: str) -> None:
        """Delete a plan."""
        if plan_id not in self._plans:
            raise NotFoundError(f"Plan '{plan_id}' not found.")
        del self._plans[plan_id]
