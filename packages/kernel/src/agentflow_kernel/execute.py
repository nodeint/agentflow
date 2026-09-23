from __future__ import annotations

from dataclasses import dataclass
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .agent_runner import AgentToolRunner
from .command_executor import CancellationRequested, raise_cancellation
from .config import ConfigurationError, resolve_stage_target
from .dispatch import (
    StageDispatchRequest,
    assert_stage_dispatchable,
    dispatch_named_stage,
)
from .workflow import load_named_workflow
from .selection import (
    Error as NextStageError,
    NextStage,
    Stop,
    failed_stage_count,
    next_stage,
    revision_stop,
)
from .prompt import build_stage_prompt
from .session_records import bind_prior_session_id, create_workflow_session, resolve_prior_session_id
from .runtime import log
from .session_store import SessionStore
from .workflow import WorkflowDocument


DEFAULT_MAX_DISPATCHES = 20


@dataclass(frozen=True)
class ExecuteRequest:
    workflow_id: Optional[str] = None
    task: Optional[str] = None
    session_id: Optional[str] = None
    stage_id: Optional[str] = None
    prior_session_id: Optional[str] = None
    max_attempts: Optional[int] = 3
    max_dispatches: Optional[int] = DEFAULT_MAX_DISPATCHES
    once: bool = False

_EXECUTE_EXIT_CODES = {
    "completed": 0,
    "blocked": 1,
    "cancelled": 130,
    "retry_exhausted": 3,
    "revisions_exhausted": 4,
    "dispatch_limit": 5,
    "stage": 0,
}
_TERMINAL_SESSION_STATUSES = frozenset({"completed", "blocked", "cancelled"})


def execute_named_workflow(
    workspace: Path,
    request: ExecuteRequest,
    *,
    runner: Optional[AgentToolRunner] = None,
) -> int:
    stage_id = request.stage_id
    max_attempts = request.max_attempts
    once = request.once
    max_dispatches = request.max_dispatches
    if not once and (max_attempts is None or max_attempts < 1):
        raise ConfigurationError("--attempts must be at least 1.")
    if not once and max_dispatches is not None and max_dispatches < 1:
        raise ConfigurationError("--max-dispatches must be at least 1.")
    store = SessionStore(workspace)
    session_id = request.session_id
    workflow: Optional[WorkflowDocument] = None
    if session_id:
        workflow_id, status = _bind_execute_run(
            workspace, session_id, request.workflow_id, request.prior_session_id
        )
        session_status = str(status.get("status") or "")
        if session_status in _TERMINAL_SESSION_STATUSES:
            _print_execute_final(status, session_status, [])
            return _EXECUTE_EXIT_CODES[session_status]
    else:
        if not request.workflow_id:
            raise ConfigurationError("Creating a session requires a workflow.")
        if not request.task:
            raise ConfigurationError("Creating a session requires --task.")
        workflow_id = request.workflow_id
        workflow = load_named_workflow(workspace, workflow_id)
        resolve_prior_session_id(workspace, workflow, request.prior_session_id)
        if stage_id is not None:
            _require_eligible_stage(workflow, [], "active", stage_id)
        session_id = create_workflow_session(
            workspace,
            workflow_id,
            request.task,
            prior_session_id=request.prior_session_id,
        )
        print(f"[agentflow] session_id: {session_id}", file=sys.stderr, flush=True)
    if workflow is None:
        workflow = load_named_workflow(workspace, workflow_id)
    previous_handlers = {
        signal.SIGINT: signal.signal(signal.SIGINT, raise_cancellation),
        signal.SIGTERM: signal.signal(signal.SIGTERM, raise_cancellation),
    }
    try:
        if once or stage_id is not None:
            return _run_execute_once(
                workspace,
                store,
                workflow,
                workflow_id,
                session_id,
                stage_id,
                runner or AgentToolRunner(),
            )
        return _run_execute_loop(
            workspace,
            store,
            workflow,
            workflow_id,
            session_id,
            max_attempts,
            max_dispatches,
            runner or AgentToolRunner(),
        )
    except CancellationRequested as exc:
        log(str(exc))
        status = store.read_session_status(session_id)
        _print_execute_final(status, "cancelled", [])
        return 130
    except ConfigurationError:
        raise
    except ValueError as exc:
        print(f"agentflow: {exc}", file=sys.stderr)
        return 2
    finally:
        for signal_number, previous_handler in previous_handlers.items():
            signal.signal(signal_number, previous_handler)


def _require_eligible_stage(
    workflow: WorkflowDocument,
    snapshots: Sequence[Any],
    session_status: str,
    stage_id: str,
) -> str:
    selected = _selected_stage(workflow, snapshots, session_status)
    if isinstance(selected, Stop):
        raise ConfigurationError(f"Session is {selected.reason}.")
    if selected.stage_id != stage_id:
        raise ConfigurationError(
            f"Stage {stage_id} is not the eligible stage {selected.stage_id}."
        )
    return selected.stage_id


def _selected_stage(
    workflow: WorkflowDocument,
    snapshots: Sequence[Any],
    session_status: str,
) -> Any:
    selected = next_stage(workflow, snapshots, session_status)
    if isinstance(selected, NextStageError):
        raise ConfigurationError(selected.message)
    if isinstance(selected, (NextStage, Stop)):
        return selected
    raise ConfigurationError("no eligible stage")


def _run_execute_once(
    workspace: Path,
    store: SessionStore,
    workflow: WorkflowDocument,
    workflow_id: str,
    session_id: str,
    stage_id: Optional[str],
    runner: Optional[AgentToolRunner],
) -> int:
    store.reap_orphaned_executions(session_id, on_live_owner="raise")
    snapshots = store.execution_snapshots(session_id)
    status = store.read_session_status(session_id)
    session_status = str(status.get("status") or "")
    if session_status in _TERMINAL_SESSION_STATUSES:
        _print_execute_final(status, session_status, [])
        return _EXECUTE_EXIT_CODES[session_status]
    selected = _selected_stage(workflow, snapshots, session_status)
    if isinstance(selected, Stop):
        _print_execute_final(status, selected.reason, [])
        return _EXECUTE_EXIT_CODES.get(selected.reason, 2)
    if stage_id is not None and selected.stage_id != stage_id:
        raise ConfigurationError(
            f"Stage {stage_id} is not the eligible stage {selected.stage_id}."
        )
    if revision_stop(workflow, snapshots, selected.stage_id):
        _print_execute_final(status, "revisions_exhausted", [])
        return _EXECUTE_EXIT_CODES["revisions_exhausted"]
    assert_stage_dispatchable(workspace, workflow, session_id, selected.stage_id)
    stage = _dispatch_execute_stage(
        workspace,
        workflow,
        workflow_id,
        session_id,
        selected.stage_id,
        status,
        snapshots,
        runner,
    )
    status = store.read_session_status(session_id)
    session_status = str(status.get("status") or "")
    stop_reason = session_status if session_status in _TERMINAL_SESSION_STATUSES else "stage"
    _print_execute_final(status, stop_reason, [stage])
    if stop_reason == "stage" and stage.get("outcome_status") != "complete":
        return 1
    return _EXECUTE_EXIT_CODES[stop_reason]


def _run_execute_loop(
    workspace: Path,
    store: SessionStore,
    workflow: WorkflowDocument,
    workflow_id: str,
    session_id: str,
    max_attempts: Optional[int],
    max_dispatches: Optional[int],
    runner: Optional[AgentToolRunner],
) -> int:
    stages: list[dict[str, Any]] = []
    dispatched = 0
    while True:
        store.reap_orphaned_executions(session_id, on_live_owner="raise")
        snapshots = store.execution_snapshots(session_id)
        status = store.read_session_status(session_id)
        selected = _selected_stage(
            workflow, snapshots, str(status.get("status") or "")
        )
        if isinstance(selected, Stop):
            _print_execute_final(status, selected.reason, stages)
            return _EXECUTE_EXIT_CODES.get(selected.reason, 2)
        if failed_stage_count(snapshots, selected.stage_id) >= max_attempts:
            _print_execute_final(status, "retry_exhausted", stages)
            return _EXECUTE_EXIT_CODES["retry_exhausted"]
        if revision_stop(workflow, snapshots, selected.stage_id):
            _print_execute_final(status, "revisions_exhausted", stages)
            return _EXECUTE_EXIT_CODES["revisions_exhausted"]
        if max_dispatches is not None and dispatched >= max_dispatches:
            _print_execute_final(status, "dispatch_limit", stages)
            return _EXECUTE_EXIT_CODES["dispatch_limit"]
        dispatched += 1
        stages.append(
            _dispatch_execute_stage(
                workspace,
                workflow,
                workflow_id,
                session_id,
                selected.stage_id,
                status,
                snapshots,
                runner,
            )
        )


def _dispatch_execute_stage(
    workspace: Path,
    workflow: WorkflowDocument,
    workflow_id: str,
    session_id: str,
    stage_id: str,
    status: Dict[str, Any],
    snapshots: Sequence[Any],
    runner: Optional[AgentToolRunner],
) -> dict[str, Any]:
    target = resolve_stage_target(workspace, workflow_id, stage_id)
    thinking_label = target.thinking or "default"
    print(
        f"[agentflow] stage {stage_id} · {target.provider}/{target.model} "
        f"(thinking: {thinking_label}).",
        file=sys.stderr,
        flush=True,
    )
    prior_session_id = _stored_prior_session_id(status)
    prompt = build_stage_prompt(
        workflow=workflow,
        stage_id=stage_id,
        task=str(status.get("task") or ""),
        snapshots=snapshots,
        workspace=workspace,
        prior_session_id=prior_session_id,
        prior_outputs=_prior_session_outputs(workspace, prior_session_id),
    )
    result = dispatch_named_stage(
        workspace,
        StageDispatchRequest(
            workflow_id=workflow_id,
            stage_id=stage_id,
            prompt=prompt,
            session_id=session_id,
        ),
        runner=runner,
    )
    return result.to_json()


def _bind_execute_run(
    workspace: Path,
    session_id: str,
    workflow_id: Optional[str],
    prior_session_id: Optional[str],
) -> tuple[str, Dict[str, Any]]:
    status = SessionStore(workspace).read_session_status(session_id)
    stored_workflow = status.get("workflow_id")
    if not status or not isinstance(stored_workflow, str) or not stored_workflow:
        raise ConfigurationError(f"Session not found: {session_id}.")
    if stored_workflow == "agent":
        raise ConfigurationError(f"Session {session_id} is not a named workflow session.")
    if workflow_id and workflow_id != stored_workflow:
        raise ConfigurationError(
            f"Workflow {workflow_id} does not match session {stored_workflow}."
        )
    bind_prior_session_id(workspace, session_id, prior_session_id)
    return stored_workflow, status


def _stored_prior_session_id(status: Dict[str, Any]) -> Optional[str]:
    prior_session_id = status.get("prior_session_id")
    return prior_session_id if isinstance(prior_session_id, str) and prior_session_id else None


def _prior_session_outputs(
    workspace: Path, prior_session_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not prior_session_id:
        return None
    outputs = SessionStore(workspace).read_session_status(prior_session_id).get("outputs")
    return outputs if isinstance(outputs, dict) else {}


def _print_execute_final(
    status: Dict[str, Any], stop_reason: str, stages: list[dict[str, Any]]
) -> None:
    outputs = status.get("outputs") if isinstance(status.get("outputs"), dict) else {}
    print(
        json.dumps(
            {
                "session_id": status.get("session_id") or "",
                "session_status": status.get("status") or "",
                "stop_reason": stop_reason,
                "outputs": outputs,
                "stages": stages,
            },
            ensure_ascii=False,
        )
    )
