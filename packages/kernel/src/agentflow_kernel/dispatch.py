from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import ConfigurationError, resolve_stage_target
from .agent_runner import AgentToolRunner
from .run_records import bind_prior_run_id, create_workflow_run
from .selection import stage_dispatch_error
from .session_store import SessionStore
from .workflow import WorkflowDocument, load_workflow_document


@dataclass(frozen=True)
class StageDispatchRequest:
    workflow_id: str
    stage_id: str
    prompt: str
    task: Optional[str] = None
    run_id: Optional[str] = None
    prior_run_id: Optional[str] = None
    runner_session_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    thinking: Optional[str] = None
    stage_attempt: Optional[int] = None
    execution_order: Optional[int] = None


@dataclass(frozen=True)
class StageDispatchResult:
    run_id: str
    runner_session_id: str
    execution_id: str
    outcome_status: str
    outcome_decision: str
    run_status: str
    response: str
    provider_session_id: str = ""
    thinking: str = ""
    artifact_directory: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "runner_session_id": self.runner_session_id,
            "provider_session_id": self.provider_session_id,
            "thinking": self.thinking,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "artifact_directory": self.artifact_directory,
            "outcome_status": self.outcome_status,
            "outcome_decision": self.outcome_decision,
            "run_status": self.run_status,
            "response": self.response,
        }


def dispatch_named_stage(
    workspace: Path,
    request: StageDispatchRequest,
    runner: Optional[AgentToolRunner] = None,
) -> StageDispatchResult:
    target = resolve_stage_target(
        workspace,
        request.workflow_id,
        request.stage_id,
        provider=request.provider,
        model=request.model,
        thinking=request.thinking,
    )
    workflow = load_named_workflow(workspace, request.workflow_id)
    run_id = request.run_id
    if run_id is None:
        if not request.task:
            raise ConfigurationError("The first workflow stage requires --task.")
        run_id = create_workflow_run(
            workspace,
            request.workflow_id,
            request.task,
            prior_run_id=request.prior_run_id,
        )
    else:
        bind_prior_run_id(workspace, run_id, request.prior_run_id)
    assert_stage_dispatchable(workspace, workflow, run_id, request.stage_id)
    runner_session_id = resolve_resume_session(
        workspace,
        workflow,
        run_id,
        request.stage_id,
        request.runner_session_id,
    )
    result = (runner or AgentToolRunner()).run(
        provider=target.provider,
        model=target.model,
        thinking=target.thinking,
        prompt=request.prompt,
        workspace=str(workspace),
        runner_session_id=runner_session_id,
        run_id=run_id,
        stage_id=request.stage_id,
        stage_attempt=request.stage_attempt,
        execution_order=request.execution_order,
    )
    return StageDispatchResult(
        run_id=str(result.get("run_id") or run_id),
        runner_session_id=str(result.get("runner_session_id") or ""),
        execution_id=str(result.get("execution_id") or ""),
        outcome_status=str(result.get("outcome_status") or ""),
        outcome_decision=str(result.get("outcome_decision") or ""),
        run_status=str(result.get("run_status") or ""),
        response=str(result.get("response") or ""),
        provider_session_id=str(result.get("provider_session_id") or ""),
        thinking=str(result.get("thinking") or ""),
        artifact_directory=str(result.get("artifact_directory") or ""),
    )


def load_named_workflow(workspace: Path, workflow_id: str) -> WorkflowDocument:
    path = workspace / ".agentflow" / "workflows" / f"{workflow_id}.yaml"
    if not path.is_file():
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    try:
        return load_workflow_document(path)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def assert_stage_dispatchable(
    workspace: Path,
    workflow: WorkflowDocument,
    run_id: str,
    stage_id: str,
) -> None:
    snapshots = SessionStore(workspace).execution_snapshots(run_id)
    error = stage_dispatch_error(workflow, stage_id, snapshots)
    if error:
        raise ConfigurationError(error)


def resolve_resume_session(
    workspace: Path,
    workflow: WorkflowDocument,
    run_id: str,
    stage_id: str,
    requested: Optional[str],
) -> Optional[str]:
    stage = workflow.stages.get(stage_id)
    if stage is None or not stage.resume_from:
        return requested
    snapshots = SessionStore(workspace).execution_snapshots(run_id)
    latest = None
    for snapshot in snapshots:
        if snapshot.stage_id != stage.resume_from:
            continue
        if snapshot.status != "completed" or snapshot.outcome_status != "complete":
            continue
        if latest is None or snapshot.execution_order > latest.execution_order:
            latest = snapshot
    if latest is None or not latest.runner_session_id:
        raise ConfigurationError(
            f"Stage {stage_id} resumes {stage.resume_from}, which has no completed session."
        )
    if requested and requested != latest.runner_session_id:
        raise ConfigurationError(
            f"--runner-session-id {requested} does not match "
            f"{stage.resume_from} session {latest.runner_session_id}."
        )
    return latest.runner_session_id
