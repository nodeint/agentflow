from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Optional, Sequence, Tuple

from .stage_outcome import StageOutcome
from .workflow import WorkflowDocument


@dataclass(frozen=True)
class ExecutionSnapshot:
    execution_id: str
    stage_id: str
    execution_order: int
    status: str
    outcome_status: Optional[str]
    artifact_directory: Path
    outcome_decision: Optional[str] = None
    runner_session_id: Optional[str] = None


def run_status_after_execution(
    execution_status: str,
    outcome: Optional[StageOutcome],
    previous_status: Dict[str, Any],
    workflow: Optional[WorkflowDocument],
    stage_id: str,
) -> str:
    if outcome and outcome.status == "blocked":
        return "blocked"
    if execution_status == "cancelled":
        return "cancelled"
    if previous_status.get("workflow_id") == "agent":
        if (
            execution_status == "completed"
            and outcome is not None
            and outcome.status == "complete"
        ):
            return "completed"
        return "active"
    workflow_id = previous_status.get("workflow_id")
    if is_completion_decision(
        workflow_id=workflow_id if isinstance(workflow_id, str) else None,
        execution_status=execution_status,
        outcome=outcome,
        workflow=workflow,
        stage_id=stage_id,
    ):
        return "completed"
    return "active"


def runnable_violation(run_id: str, status: Dict[str, Any]) -> Optional[str]:
    run_status = status.get("status")
    if run_status == "active":
        return None
    if run_status == "completed" and status.get("workflow_id") == "agent":
        return None
    terminal_by = status.get(
        "blocked_by_execution_id",
        status.get("terminal_execution_id", "an earlier execution"),
    )
    return f"Run {run_id} is {run_status} at {terminal_by}; start a new run to continue."


def is_completion_decision(
    *,
    workflow_id: Optional[str],
    execution_status: str,
    outcome: Optional[StageOutcome],
    workflow: Optional[WorkflowDocument],
    stage_id: str,
) -> bool:
    if workflow_id == "agent":
        return False
    if execution_status != "completed":
        return False
    if outcome is None or outcome.status != "complete":
        return False
    if workflow is None or workflow.completion is None:
        return False
    completion = workflow.completion
    if completion.stage != stage_id:
        return False
    if completion.decision is not None:
        expected = completion.decision.lower()
        actual = outcome.decision.lower() if outcome.decision else None
        if actual != expected:
            return False
    return True


def should_publish_outputs(
    *,
    workflow_id: Optional[str],
    execution_status: str,
    outcome: Optional[StageOutcome],
    workflow: WorkflowDocument,
    stage_id: str,
) -> bool:
    if not is_completion_decision(
        workflow_id=workflow_id,
        execution_status=execution_status,
        outcome=outcome,
        workflow=workflow,
        stage_id=stage_id,
    ):
        return False
    completion = workflow.completion
    return completion is not None and bool(completion.outputs)


def select_source_execution(
    snapshots: Sequence[ExecutionSnapshot],
    from_stage: str,
    before_order: int,
) -> Optional[ExecutionSnapshot]:
    candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot.stage_id == from_stage
        and snapshot.execution_order < before_order
        and snapshot.status == "completed"
        and snapshot.outcome_status == "complete"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda snapshot: snapshot.execution_order)


def missing_completion_output_names(
    *,
    workflow: WorkflowDocument,
    snapshots: Sequence[ExecutionSnapshot],
    before_order: int,
) -> list[str]:
    completion = workflow.completion
    if completion is None or not completion.outputs:
        return []
    missing: list[str] = []
    for output in completion.outputs:
        source = select_source_execution(snapshots, output.from_stage, before_order)
        if source is None:
            missing.append(output.name)
            continue
        if not nonempty_artifact(source.artifact_directory / output.artifact):
            missing.append(output.name)
    return missing


def nonempty_artifact(path: Path) -> bool:
    if not path.is_file():
        return False
    return bool(path.read_bytes().strip())


def publish_output_files(
    run_directory: Path,
    sources: Dict[str, Tuple[Path, str]],
    published_by_execution_id: str,
) -> Dict[str, Dict[str, str]]:
    recorded: Dict[str, Dict[str, str]] = {}
    for name, (source_path, source_execution_id) in sources.items():
        if not nonempty_artifact(source_path):
            continue
        destination = run_directory / "outputs" / name
        atomic_copy(source_path, destination)
        recorded[name] = {
            "path": f"outputs/{name}",
            "source_execution_id": source_execution_id,
            "published_by_execution_id": published_by_execution_id,
        }
    return recorded


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(handle)
        shutil.copyfile(source, tmp_path)
        os.replace(tmp_path, destination)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
