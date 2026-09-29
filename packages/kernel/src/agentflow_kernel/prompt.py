from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .selection import latest_complete_by_stage
from .outputs import ExecutionSnapshot
from .workflow import WorkflowDocument


def build_stage_prompt(
    *,
    workflow: WorkflowDocument,
    stage_id: str,
    task: str,
    snapshots: Sequence[ExecutionSnapshot],
    workspace: Path,
    prior_session_id: Optional[str] = None,
    prior_outputs: Optional[Mapping[str, Any]] = None,
) -> str:
    stage = workflow.stages.get(stage_id)
    if stage is None:
        raise ValueError(f"Stage is not declared: {stage_id}.")
    sections = [
        f"Task: {task}",
        f"Stage: {stage.id} (role: {stage.role})",
    ]
    if stage.instructions:
        lines = ["Instructions:"]
        lines.extend(f"- {item}" for item in stage.instructions)
        sections.append("\n".join(lines))
    if workflow.constraints:
        lines = ["Constraints:"]
        lines.extend(f"- {item}" for item in workflow.constraints)
        sections.append("\n".join(lines))
    upstream = _upstream_artifact_lines(workflow, stage.depends_on, snapshots)
    if upstream:
        sections.append("Upstream artifacts on this session:\n" + "\n".join(upstream))
    routed = _routed_result_lines(workflow, stage_id, snapshots)
    if routed:
        sections.append("Decision that routed to this stage:\n" + "\n".join(routed))
    prior = _prior_result_lines(workspace, prior_session_id, prior_outputs)
    if prior:
        sections.append("Required prior workflow result:\n" + "\n".join(prior))
    if stage.artifact:
        sections.append(
            f"Write the file artifact as `{stage.artifact}` in the execution artifacts directory."
        )
    return "\n\n".join(sections)


def _upstream_artifact_lines(
    workflow: WorkflowDocument,
    depends_on: Sequence[str],
    snapshots: Sequence[ExecutionSnapshot],
) -> list[str]:
    complete = latest_complete_by_stage(snapshots)
    lines: list[str] = []
    for dep_id in depends_on:
        dep = workflow.stages.get(dep_id)
        if dep is None or not dep.artifact:
            continue
        snapshot = complete.get(dep_id)
        if snapshot is None:
            continue
        path = (snapshot.artifact_directory / dep.artifact).resolve()
        lines.append(f"- {dep_id}: {path}")
    return lines


def _routed_result_lines(
    workflow: WorkflowDocument,
    stage_id: str,
    snapshots: Sequence[ExecutionSnapshot],
) -> list[str]:
    complete = latest_complete_by_stage(snapshots)
    previous = complete.get(stage_id)
    previous_order = previous.execution_order if previous else 0
    routed = [
        snapshot
        for snapshot in snapshots
        if snapshot.status == "completed"
        and snapshot.outcome_status == "complete"
        and snapshot.execution_order > previous_order
        and (source := workflow.stages.get(snapshot.stage_id)) is not None
        and source.routes.get((snapshot.outcome_decision or "").lower()) == stage_id
    ]
    if not routed:
        return []
    decision = max(routed, key=lambda snapshot: snapshot.execution_order)
    response = (decision.artifact_directory.parent / "response.md").resolve()
    if not response.is_file():
        raise ValueError(f"Routed stage response is missing: {response}")
    lines = [
        f"- {decision.stage_id} ({decision.outcome_decision}): {response}",
        "Read this response and use its decision and feedback when working on this stage.",
    ]
    stage = workflow.stages[stage_id]
    if previous and stage.artifact:
        artifact = (previous.artifact_directory / stage.artifact).resolve()
        if artifact.is_file():
            lines.append(f"- Previous {stage_id} artifact: {artifact}")
    return lines


def _prior_result_lines(
    workspace: Path,
    prior_session_id: Optional[str],
    prior_outputs: Optional[Mapping[str, Any]],
) -> list[str]:
    if not prior_session_id:
        return []
    lines = [f"- session: {prior_session_id}"]
    paths = _prior_output_paths(workspace, prior_session_id, prior_outputs)
    if paths:
        lines.append("- outputs: " + ", ".join(paths))
    return lines


def _prior_output_paths(
    workspace: Path,
    prior_session_id: str,
    prior_outputs: Optional[Mapping[str, Any]],
) -> list[str]:
    if not prior_outputs:
        return []
    paths: list[str] = []
    for entry in prior_outputs.values():
        path = _absolute_output_path(workspace, prior_session_id, entry)
        if path is not None:
            paths.append(str(path))
    return paths


def _absolute_output_path(
    workspace: Path, prior_session_id: str, entry: Any
) -> Optional[Path]:
    if not isinstance(entry, dict):
        return None
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip())
    if path.is_absolute():
        return path
    return (workspace / ".agentflow" / "sessions" / prior_session_id / path).resolve()
