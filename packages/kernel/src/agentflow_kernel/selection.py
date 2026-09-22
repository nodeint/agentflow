from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Union

from .outputs import ExecutionSnapshot
from .workflow import StageSpec, WorkflowDocument

_TERMINAL_SESSION_STATUSES = frozenset({"blocked", "cancelled", "completed"})


@dataclass(frozen=True)
class NextStage:
    stage_id: str


@dataclass(frozen=True)
class Stop:
    reason: str


@dataclass(frozen=True)
class Error:
    message: str


NextStageResult = Union[NextStage, Stop, Error]


def next_stage(
    workflow: WorkflowDocument,
    snapshots: Sequence[ExecutionSnapshot],
    session_status: str,
) -> NextStageResult:
    if any(snapshot.status == "running" for snapshot in snapshots):
        return Error("an execution is still running")
    if session_status in _TERMINAL_SESSION_STATUSES:
        return Stop(session_status)
    latest = _latest_snapshot(snapshots)
    if latest is not None and latest.status == "failed":
        return NextStage(latest.stage_id)
    if (
        latest is not None
        and latest.status == "completed"
        and latest.outcome_status == "complete"
    ):
        stage = workflow.stages.get(latest.stage_id)
        if stage is not None and stage.routes:
            return _routed_next_stage(workflow, snapshots, stage, latest)
    candidates = _candidate_stage_ids(workflow, snapshots)
    if not candidates:
        return Error("no eligible stage")
    if len(candidates) > 1:
        return Error("ambiguous next stage: " + ", ".join(candidates))
    return NextStage(candidates[0])


def stage_dispatch_error(
    workflow: WorkflowDocument,
    stage_id: str,
    snapshots: Sequence[ExecutionSnapshot],
) -> Optional[str]:
    if not workflow.stages:
        return None
    stage = workflow.stages.get(stage_id)
    if stage is None:
        return f"Stage is not declared: {stage_id}."
    latest_complete = latest_complete_by_stage(snapshots)
    for dep in stage.depends_on:
        if dep not in latest_complete:
            return f"Stage {stage_id} depends on {dep}, which has not completed."
    incoming = _incoming_routers(workflow, stage_id)
    if not incoming:
        return None
    completed_incoming = [
        router for router in incoming if router.id in latest_complete
    ]
    if not completed_incoming:
        if stage.depends_on:
            return f"Stage {stage_id} is waiting for a routing decision."
        return None
    router = max(
        completed_incoming,
        key=lambda item: latest_complete[item.id].execution_order,
    )
    decision = (latest_complete[router.id].outcome_decision or "").lower()
    target = router.routes.get(decision)
    if target != stage_id:
        return (
            f"Stage {stage_id} is not routed by {router.id} decision {decision!r}."
        )
    return None


def latest_complete_by_stage(
    snapshots: Sequence[ExecutionSnapshot],
) -> dict[str, ExecutionSnapshot]:
    latest: dict[str, ExecutionSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.status != "completed" or snapshot.outcome_status != "complete":
            continue
        current = latest.get(snapshot.stage_id)
        if current is None or snapshot.execution_order > current.execution_order:
            latest[snapshot.stage_id] = snapshot
    return latest


def failed_stage_count(
    snapshots: Sequence[ExecutionSnapshot], stage_id: str
) -> int:
    return sum(
        1
        for snapshot in snapshots
        if snapshot.stage_id == stage_id and snapshot.status == "failed"
    )


def revision_stop(
    workflow: WorkflowDocument,
    snapshots: Sequence[ExecutionSnapshot],
    stage_id: str,
) -> bool:
    """True when another routed visit would pass the stage's max_revisions.

    The first successful visit is free. Each later successful visit is one
    revision. A failed retry of the visit already started does not count.
    """
    stage = workflow.stages.get(stage_id)
    if stage is None or stage.max_revisions is None:
        return False
    latest = _latest_snapshot(snapshots)
    if (
        latest is not None
        and latest.stage_id == stage_id
        and latest.status == "failed"
    ):
        return False
    completed = sum(
        1
        for snapshot in snapshots
        if snapshot.stage_id == stage_id
        and snapshot.status == "completed"
        and snapshot.outcome_status == "complete"
    )
    if completed == 0:
        return False
    return completed - 1 >= stage.max_revisions


def _routed_next_stage(
    workflow: WorkflowDocument,
    snapshots: Sequence[ExecutionSnapshot],
    stage: StageSpec,
    latest: ExecutionSnapshot,
) -> NextStageResult:
    decision = (latest.outcome_decision or "").lower()
    target = stage.routes.get(decision)
    if target is None:
        return Error(
            f"stage {stage.id} decision {decision!r} is not a valid route."
        )
    if target == "complete":
        return Stop("completed")
    error = stage_dispatch_error(workflow, target, snapshots)
    if error is None:
        return NextStage(target)
    return Error(error)


def _candidate_stage_ids(
    workflow: WorkflowDocument,
    snapshots: Sequence[ExecutionSnapshot],
) -> list[str]:
    complete = latest_complete_by_stage(snapshots)
    candidates: list[str] = []
    for stage_id, stage in workflow.stages.items():
        if stage_dispatch_error(workflow, stage_id, snapshots) is not None:
            continue
        snapshot = complete.get(stage_id)
        if snapshot is None:
            candidates.append(stage_id)
            continue
        if _has_later_route_to(workflow, complete, stage_id, snapshot.execution_order):
            candidates.append(stage_id)
            continue
        # Re-open S when a dependency completed after S, e.g. plan v2 after revise.
        if _has_newer_dependency(complete, stage, snapshot.execution_order):
            candidates.append(stage_id)
    return candidates


def _has_newer_dependency(
    complete: dict[str, ExecutionSnapshot],
    stage: StageSpec,
    complete_order: int,
) -> bool:
    for dep_id in stage.depends_on:
        snapshot = complete.get(dep_id)
        if snapshot is not None and snapshot.execution_order > complete_order:
            return True
    return False


def _has_later_route_to(
    workflow: WorkflowDocument,
    complete: dict[str, ExecutionSnapshot],
    stage_id: str,
    complete_order: int,
) -> bool:
    for router in _incoming_routers(workflow, stage_id):
        snapshot = complete.get(router.id)
        if snapshot is None or snapshot.execution_order <= complete_order:
            continue
        decision = (snapshot.outcome_decision or "").lower()
        if router.routes.get(decision) == stage_id:
            return True
    return False


def _latest_snapshot(
    snapshots: Sequence[ExecutionSnapshot],
) -> Optional[ExecutionSnapshot]:
    if not snapshots:
        return None
    return max(snapshots, key=lambda snapshot: snapshot.execution_order)


def _incoming_routers(workflow: WorkflowDocument, stage_id: str) -> list[StageSpec]:
    return [
        stage
        for stage in workflow.stages.values()
        if stage_id in stage.routes.values()
    ]
