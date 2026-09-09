"""Planning engine — task decomposition and multi-step execution.

Allows agents to break down complex goals into ordered steps, track progress,
and execute each step using the appropriate tools or agents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    """Status of an individual plan step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    """Status of an entire plan."""

    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class PlanStep:
    """A single step in a decomposed plan."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    description: str = ""
    agent: str | None = None
    tools: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "agent": self.agent,
            "tools": self.tools,
            "dependencies": self.dependencies,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStep:
        return cls(
            id=data.get("id", uuid.uuid4().hex[:8]),
            description=data.get("description", ""),
            agent=data.get("agent"),
            tools=data.get("tools", []),
            dependencies=data.get("dependencies", []),
            status=StepStatus(data.get("status", "pending")),
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass
class Plan:
    """A multi-step plan for achieving a complex goal."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    current_step_index: int = 0

    @property
    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    @property
    def completed_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]

    @property
    def failed_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.FAILED]

    @property
    def progress_pct(self) -> float:
        if not self.steps:
            return 0.0
        return len(self.completed_steps) / len(self.steps) * 100

    def get_step(self, step_id: str) -> PlanStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def steps_ready(self) -> list[PlanStep]:
        """Return pending steps whose dependencies are all completed."""
        completed_ids = {s.id for s in self.completed_steps}
        return [
            s
            for s in self.pending_steps
            if all(dep in completed_ids for dep in s.dependencies)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "status": self.status.value,
            "current_step_index": self.current_step_index,
            "progress_pct": self.progress_pct,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            goal=data.get("goal", ""),
            steps=[PlanStep.from_dict(s) for s in data.get("steps", [])],
            status=PlanStatus(data.get("status", "draft")),
            current_step_index=data.get("current_step_index", 0),
        )
