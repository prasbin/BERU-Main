"""Tests for the planning system: decomposition, execution, tracking."""

from __future__ import annotations

from backend.engines.plan_engine import PlanEngine
from backend.engines.planning import Plan, PlanStatus, PlanStep, StepStatus
from backend.services.plan_service import PlanService

# ---- Plan data structure tests ----


def test_plan_step_creation():
    step = PlanStep(description="Test step")
    assert step.status == StepStatus.PENDING
    assert step.id
    assert step.tools == []
    assert step.dependencies == []


def test_plan_creation():
    plan = Plan(goal="Test goal")
    assert plan.status == PlanStatus.DRAFT
    assert plan.steps == []
    assert plan.progress_pct == 0.0


def test_plan_progress():
    plan = Plan(goal="Test", steps=[
        PlanStep(description="Step 1"),
        PlanStep(description="Step 2"),
        PlanStep(description="Step 3"),
    ])
    assert plan.progress_pct == 0.0

    plan.steps[0].status = StepStatus.COMPLETED
    assert plan.progress_pct == 33.33333333333333

    plan.steps[1].status = StepStatus.COMPLETED
    assert abs(plan.progress_pct - 66.66666666666667) < 0.0001


def test_plan_pending_steps():
    plan = Plan(goal="Test", steps=[
        PlanStep(description="Step 1", status=StepStatus.COMPLETED),
        PlanStep(description="Step 2", status=StepStatus.PENDING),
        PlanStep(description="Step 3", status=StepStatus.IN_PROGRESS),
    ])
    assert len(plan.pending_steps) == 1
    assert plan.pending_steps[0].description == "Step 2"


def test_plan_steps_ready_with_dependencies():
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2", dependencies=["s1"]),
        PlanStep(id="s3", description="Step 3", dependencies=["s2"]),
    ])

    ready = plan.steps_ready()
    assert len(ready) == 1
    assert ready[0].id == "s1"

    plan.steps[0].status = StepStatus.COMPLETED
    ready = plan.steps_ready()
    assert len(ready) == 1
    assert ready[0].id == "s2"


def test_plan_get_step():
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2"),
    ])
    assert plan.get_step("s1") is not None
    assert plan.get_step("s1").description == "Step 1"
    assert plan.get_step("nonexistent") is None


def test_plan_serialization():
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1", tools=["web_search"]),
    ])
    data = plan.to_dict()
    assert data["goal"] == "Test"
    assert len(data["steps"]) == 1

    restored = Plan.from_dict(data)
    assert restored.goal == "Test"
    assert restored.steps[0].id == "s1"
    assert restored.steps[0].tools == ["web_search"]


# ---- PlanEngine tests ----


def test_engine_complete_step():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2"),
    ])

    engine.execute_step(plan, "s1")
    assert plan.steps[0].status == StepStatus.IN_PROGRESS
    assert plan.status == PlanStatus.IN_PROGRESS

    engine.complete_step(plan, "s1", "Finished step 1")
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[0].result == "Finished step 1"
    assert plan.status == PlanStatus.IN_PROGRESS  # Not all done yet

    engine.complete_step(plan, "s2")
    assert plan.status == PlanStatus.COMPLETED  # All done


def test_engine_fail_step():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
    ])

    engine.execute_step(plan, "s1")
    engine.fail_step(plan, "s1", "Something went wrong")
    assert plan.steps[0].status == StepStatus.FAILED
    assert plan.steps[0].error == "Something went wrong"
    assert plan.status == PlanStatus.FAILED


def test_engine_skip_step():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2"),
    ])

    engine.skip_step(plan, "s1")
    engine.skip_step(plan, "s2")
    assert plan.status == PlanStatus.COMPLETED  # All skipped counts as complete


def test_engine_cancel_plan():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2"),
    ])

    engine.cancel_plan(plan)
    assert plan.status == PlanStatus.CANCELLED
    assert all(s.status == StepStatus.SKIPPED for s in plan.steps)


def test_engine_dependency_check():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test", steps=[
        PlanStep(id="s1", description="Step 1"),
        PlanStep(id="s2", description="Step 2", dependencies=["s1"]),
    ])

    result = engine.execute_step(plan, "s2")
    assert "error" in result
    assert "Unmet dependencies" in result["error"]


def test_engine_execute_missing_step():
    engine = PlanEngine(provider=None)  # type: ignore[arg-type]
    plan = Plan(goal="Test")
    result = engine.execute_step(plan, "nonexistent")
    assert "error" in result
    assert "not found" in result["error"]


# ---- PlanService tests ----


async def test_service_create_manual_plan():
    service = PlanService()
    plan = service.create_manual_plan(
        goal="Test goal",
        steps=[
            {"id": "s1", "description": "Step 1"},
            {"id": "s2", "description": "Step 2"},
        ],
    )
    assert plan.goal == "Test goal"
    assert len(plan.steps) == 2
    assert plan.status == PlanStatus.DRAFT


async def test_service_get_plan():
    service = PlanService()
    plan = service.create_manual_plan(goal="Test", steps=[])
    retrieved = service.get_plan(plan.id)
    assert retrieved.id == plan.id


async def test_service_list_plans():
    service = PlanService()
    service.create_manual_plan(goal="Plan 1", steps=[])
    service.create_manual_plan(goal="Plan 2", steps=[])
    assert len(service.list_plans()) == 2


async def test_service_list_active_plans():
    service = PlanService()
    p1 = service.create_manual_plan(
        goal="Active", steps=[{"id": "s1", "description": "Step 1"}]
    )
    p2 = service.create_manual_plan(goal="Done", steps=[{"id": "s1", "description": "Step 1"}])
    service.start_step(p1.id, "s1")
    service.complete_step(p1.id, "s1")
    service.start_step(p2.id, "s1")
    service.complete_step(p2.id, "s1")
    active = service.list_active_plans()
    assert len(active) == 0  # Both plans are completed


async def test_service_start_and_complete_step():
    service = PlanService()
    plan = service.create_manual_plan(
        goal="Test", steps=[{"id": "s1", "description": "Step 1"}]
    )
    result = service.start_step(plan.id, "s1")
    assert result["status"] == "ready"

    plan = service.complete_step(plan.id, "s1", "Done!")
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[0].result == "Done!"


async def test_service_fail_step():
    service = PlanService()
    plan = service.create_manual_plan(
        goal="Test", steps=[{"id": "s1", "description": "Step 1"}]
    )
    service.start_step(plan.id, "s1")
    plan = service.fail_step(plan.id, "s1", "Error occurred")
    assert plan.steps[0].status == StepStatus.FAILED
    assert plan.steps[0].error == "Error occurred"


async def test_service_skip_step():
    service = PlanService()
    plan = service.create_manual_plan(
        goal="Test",
        steps=[
            {"id": "s1", "description": "Step 1"},
            {"id": "s2", "description": "Step 2"},
        ],
    )
    plan = service.skip_step(plan.id, "s1")
    assert plan.steps[0].status == StepStatus.SKIPPED


async def test_service_cancel_plan():
    service = PlanService()
    plan = service.create_manual_plan(
        goal="Test", steps=[{"id": "s1", "description": "Step 1"}]
    )
    plan = service.cancel_plan(plan.id)
    assert plan.status == PlanStatus.CANCELLED


async def test_service_delete_plan():
    service = PlanService()
    plan = service.create_manual_plan(goal="Test", steps=[])
    service.delete_plan(plan.id)
    assert len(service.list_plans()) == 0
