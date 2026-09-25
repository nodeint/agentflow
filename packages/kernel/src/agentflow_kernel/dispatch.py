from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import ConfigurationError, resolve_stage_target
from .agent_runner import AgentToolRunner
from .session_records import bind_prior_session_id, create_workflow_session
from .selection import stage_dispatch_error
from .session_store import SessionStore
from .workflow import WorkflowDocument, load_named_workflow


@dataclass(frozen=True)
class StageDispatchRequest:
    workflow_id: str
    stage_id: str
    prompt: str
    task: Optional[str] = None
    session_id: Optional[str] = None
    prior_session_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    options: Optional[dict[str, str]] = None
    stage_attempt: Optional[int] = None
    execution_order: Optional[int] = None


@dataclass(frozen=True)
class StageDispatchResult:
    session_id: str
    execution_id: str
    outcome_status: str
    outcome_decision: str
    session_status: str
    response: str
    agent_id: str = ""
    options: dict[str, str] | None = None
    artifact_directory: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "options": dict(self.options or {}),
            "session_id": self.session_id,
            "execution_id": self.execution_id,
            "artifact_directory": self.artifact_directory,
            "outcome_status": self.outcome_status,
            "outcome_decision": self.outcome_decision,
            "session_status": self.session_status,
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
        options=request.options,
    )
    workflow = load_named_workflow(workspace, request.workflow_id)
    session_id = request.session_id
    if session_id is None:
        if not request.task:
            raise ConfigurationError("The first workflow stage requires --task.")
        session_id = create_workflow_session(
            workspace,
            request.workflow_id,
            request.task,
            prior_session_id=request.prior_session_id,
        )
    else:
        bind_prior_session_id(workspace, session_id, request.prior_session_id)
    assert_stage_dispatchable(workspace, workflow, session_id, request.stage_id)
    resume_execution_id = resolve_resume_execution(
        workspace,
        workflow,
        session_id,
        request.stage_id,
    )
    result = (runner or AgentToolRunner()).run(
        provider=target.provider,
        model=target.model,
        options=dict(target.options),
        prompt=request.prompt,
        workspace=str(workspace),
        session_id=session_id,
        resume_execution_id=resume_execution_id,
        stage_id=request.stage_id,
        stage_attempt=request.stage_attempt,
        execution_order=request.execution_order,
    )
    return StageDispatchResult(
        session_id=str(result.get("session_id") or session_id),
        execution_id=str(result.get("execution_id") or ""),
        outcome_status=str(result.get("outcome_status") or ""),
        outcome_decision=str(result.get("outcome_decision") or ""),
        session_status=str(result.get("session_status") or ""),
        response=str(result.get("response") or ""),
        agent_id=str(result.get("agent_id") or ""),
        options=dict(result.get("options") or {}),
        artifact_directory=str(result.get("artifact_directory") or ""),
    )


def assert_stage_dispatchable(
    workspace: Path,
    workflow: WorkflowDocument,
    session_id: str,
    stage_id: str,
) -> None:
    snapshots = SessionStore(workspace).execution_snapshots(session_id)
    error = stage_dispatch_error(workflow, stage_id, snapshots)
    if error:
        raise ConfigurationError(error)


def resolve_resume_execution(
    workspace: Path,
    workflow: WorkflowDocument,
    session_id: str,
    stage_id: str,
) -> Optional[str]:
    stage = workflow.stages.get(stage_id)
    if stage is None or not stage.resume_from:
        return None
    snapshots = SessionStore(workspace).execution_snapshots(session_id)
    latest = None
    for snapshot in snapshots:
        if snapshot.stage_id != stage.resume_from:
            continue
        if snapshot.status != "completed" or snapshot.outcome_status != "complete":
            continue
        if latest is None or snapshot.execution_order > latest.execution_order:
            latest = snapshot
    if latest is None:
        raise ConfigurationError(
            f"Stage {stage_id} resumes {stage.resume_from}, which has no completed execution."
        )
    return latest.execution_id
