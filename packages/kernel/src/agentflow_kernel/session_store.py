from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
import uuid

from .command_executor import TERMINATION_GRACE_SEC
from .event_kinds import STATUS_PROVIDER_EVENTS
from .outputs import (
    ExecutionSnapshot,
    missing_completion_output_names,
    publish_output_files,
    session_status_after_execution,
    runnable_violation,
    select_source_execution,
    should_publish_outputs,
)
from .process_ownership import (
    REAP_ALREADY_EXITED,
    REAP_BOOT_MISMATCH,
    REAP_REAPED,
    REAP_STILL_RUNNING,
    REAP_UNVERIFIED,
    ProcessInspector,
    ProcessStartInfo,
    PosixProcessInspector,
    ReapDecision,
    classify_orphaned_execution,
    process_metadata_payload,
    raise_reap_decision,
    stop_verified_process_group,
)
from .runtime import (
    AGENTFLOW_DIRNAME,
    CONVERSATION_FILENAME,
    EXECUTION_SCHEMA_VERSION,
    EXECUTIONS_DIRNAME,
    LEGACY_CONVERSATION_FILENAME,
    SESSIONS_DIRNAME,
    safe_component,
    utc_now,
)
from .stage_outcome import StageOutcome
from .workflow import WorkflowDocument, load_workflow_document


@dataclass
class AgentSessionRecord:
    provider: str
    model: str
    workspace: str
    agent_id: Optional[str] = None
    thinking: Optional[str] = None
    status: str = "new"
    history: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_json(
        cls, raw: Any, provider: str, model: str, workspace: str
    ) -> "AgentSessionRecord":
        if isinstance(raw, list):
            return cls(provider, model, workspace, history=raw)
        if not isinstance(raw, dict):
            return cls(provider, model, workspace)
        history = raw.get("history")
        return cls(
            provider=raw.get("provider") or provider,
            model=raw.get("model") or model,
            workspace=raw.get("workspace") or workspace,
            agent_id=raw.get("agent_id")
            or raw.get("native_session_id"),
            thinking=raw.get("thinking"),
            status=raw.get("status") or "unknown",
            history=history if isinstance(history, list) else [],
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "workspace": self.workspace,
            "agent_id": self.agent_id,
            "thinking": self.thinking,
            "status": self.status,
            "history": self.history,
        }


@dataclass(frozen=True)
class ExecutionContext:
    session_id: str
    execution_id: str
    directory: Path

    @property
    def conversation_path(self) -> Path:
        return self.directory / CONVERSATION_FILENAME

    @property
    def conversation_read_path(self) -> Path:
        path = self.conversation_path
        if path.exists():
            return path
        return self.directory / LEGACY_CONVERSATION_FILENAME

    @property
    def artifact_directory(self) -> Path:
        return self.directory / "artifacts"


class SessionStore:
    def __init__(
        self,
        workspace: Path,
        process_inspector: Optional[ProcessInspector] = None,
    ) -> None:
        self.workspace = workspace
        self.sessions_directory = workspace / AGENTFLOW_DIRNAME / SESSIONS_DIRNAME
        self.process_inspector = process_inspector or PosixProcessInspector()

    def load_or_create(
        self,
        provider: str,
        model: str,
        thinking: Optional[str],
        workspace: Path,
        session_id: Optional[str],
        stage_id: str,
        stage_attempt: Optional[int],
        execution_order: Optional[int],
        prompt: str,
        resume_execution_id: Optional[str] = None,
    ) -> Tuple[AgentSessionRecord, bool, ExecutionContext]:
        if resume_execution_id is not None and session_id is None:
            raise ValueError("Resuming an execution requires a session.")
        if session_id is not None:
            self.reap_orphaned_executions(session_id, on_live_owner="raise")
        self._ensure_session_is_runnable(session_id)
        previous = None
        if resume_execution_id is not None:
            assert session_id is not None
            previous = self.execution_context(session_id, resume_execution_id)
            if previous is None:
                raise ValueError(
                    f"Execution {resume_execution_id} was not found in session {session_id}."
                )
        if previous is None:
            context = self._create_execution(
                provider,
                model,
                thinking,
                session_id,
                stage_id,
                stage_attempt,
                execution_order,
                prompt,
            )
            return (
                AgentSessionRecord(provider, model, str(workspace), thinking=thinking),
                True,
                context,
            )
        raw = json.loads(previous.conversation_read_path.read_text(encoding="utf-8"))
        context = self._create_execution(
            provider,
            model,
            thinking,
            session_id,
            stage_id,
            stage_attempt,
            execution_order,
            prompt,
            resumes_execution_id=previous.execution_id,
        )
        return (
            AgentSessionRecord.from_json(raw, provider, model, str(workspace)),
            False,
            context,
        )

    def save(self, record: AgentSessionRecord, context: ExecutionContext) -> None:
        context.conversation_path.write_text(
            json.dumps(record.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_execution(
        self,
        context: ExecutionContext,
        record: AgentSessionRecord,
        status: str,
        error: Optional[str] = None,
        outcome: Optional[StageOutcome] = None,
        *,
        _locked: bool = False,
    ) -> None:
        path = context.directory / "execution.json"
        if _locked:
            self._save_execution_unlocked(context, record, status, error, outcome)
            return
        with self._lock_execution(path):
            self._save_execution_unlocked(context, record, status, error, outcome)

    def save_process_metadata(
        self, context: ExecutionContext, info: ProcessStartInfo
    ) -> None:
        path = context.directory / "execution.json"
        with self._lock_execution(path):
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata.update(process_metadata_payload(info))
            path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def reap_orphaned_executions(
        self, session_id: str, *, on_live_owner: str
    ) -> None:
        if os.name != "posix":
            return
        for context in self._execution_contexts(session_id):
            self._reap_execution(context, on_live_owner=on_live_owner)

    def record_provider_event(
        self, context: ExecutionContext, event: Dict[str, Any]
    ) -> None:
        session_directory = context.directory.parents[1]
        self._append_event(session_directory, event)
        if event.get("event") not in STATUS_PROVIDER_EVENTS:
            return
        status = self._read_session_status(session_directory)
        status.update(
            {
                "latest_provider_event": event,
                "current_stage": {
                    "execution_id": context.execution_id,
                    "stage_id": event["stage_id"],
                    "provider": event["provider"],
                    "event": event["event"],
                    "summary": event.get("summary"),
                    "updated_at": event["timestamp"],
                },
                "updated_at": event["timestamp"],
            }
        )
        (session_directory / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def execution_context(
        self, session_id: str, execution_id: str
    ) -> Optional[ExecutionContext]:
        directory = (
            self.sessions_directory / session_id / EXECUTIONS_DIRNAME / execution_id
        )
        if not directory.is_dir():
            return None
        return ExecutionContext(session_id, execution_id, directory)

    def latest_execution(self, session_id: str) -> Optional[ExecutionContext]:
        contexts = self._execution_contexts(session_id)
        if not contexts:
            return None
        return max(contexts, key=self._execution_sort_key)

    def stage_id(self, context: ExecutionContext) -> str:
        return self._stage_id(context)

    def read_session_status(self, session_id: str) -> Dict[str, Any]:
        return self._read_session_status(self.sessions_directory / session_id)

    def execution_snapshots(self, session_id: str) -> list[ExecutionSnapshot]:
        return self._execution_snapshots(session_id)

    def missing_completion_outputs(
        self, context: ExecutionContext, outcome: Optional[StageOutcome]
    ) -> list[str]:
        previous_status = self._read_session_status(context.directory.parents[1])
        workflow = self._load_workflow(previous_status)
        if workflow is None:
            return []
        workflow_id = previous_status.get("workflow_id")
        if not should_publish_outputs(
            workflow_id=workflow_id if isinstance(workflow_id, str) else None,
            execution_status="completed",
            outcome=outcome,
            workflow=workflow,
            stage_id=self._stage_id(context),
        ):
            return []
        metadata = json.loads(
            (context.directory / "execution.json").read_text(encoding="utf-8")
        )
        return missing_completion_output_names(
            workflow=workflow,
            snapshots=self._execution_snapshots(context.session_id),
            before_order=int(metadata.get("execution_order") or 0),
        )

    def save_prompt(self, context: ExecutionContext, prompt: str) -> None:
        (context.directory / "prompt.md").write_text(prompt, encoding="utf-8")

    def save_response(self, context: ExecutionContext, response_text: str) -> None:
        (context.directory / "response.md").write_text(response_text, encoding="utf-8")

    def _save_execution_unlocked(
        self,
        context: ExecutionContext,
        record: AgentSessionRecord,
        status: str,
        error: Optional[str] = None,
        outcome: Optional[StageOutcome] = None,
    ) -> None:
        path = context.directory / "execution.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        existing_status = metadata.get("status")
        if existing_status in {"completed", "failed", "cancelled"}:
            raise ValueError(
                f"Execution {context.execution_id} is immutable after it ended."
            )
        session_directory = context.directory.parents[1]
        previous_status = self._read_session_status(session_directory)
        workflow = self._load_workflow(previous_status)
        if status == "running" and previous_status.get("status") == "completed":
            self._rewrite_manifest_status(session_directory, "running")
        published = (
            {}
            if status == "running"
            else self._published_outputs(
                context, status, outcome, previous_status, workflow, metadata
            )
        )
        metadata["status"] = status
        metadata["agent_id"] = record.agent_id
        metadata["completed_at"] = utc_now() if status != "running" else None
        if error is not None:
            metadata["error"] = error
        if outcome is not None:
            metadata["outcome"] = outcome.to_json()
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        session_status = self._session_status(
            status,
            outcome,
            previous_status=previous_status,
            workflow=workflow,
            stage_id=str(metadata.get("stage_id") or ""),
        )
        outputs = {}
        previous_outputs = previous_status.get("outputs")
        if isinstance(previous_outputs, dict):
            outputs.update(previous_outputs)
        outputs.update(published)
        payload = {
            **self._provider_status_fields(previous_status, context, status),
            "session_id": context.session_id,
            "status": session_status,
            "latest_execution_id": context.execution_id,
            "latest_execution_status": status,
            "latest_stage_id": str(metadata.get("stage_id") or ""),
            "updated_at": utc_now(),
            **self._terminal_session_details(context, outcome, session_status),
        }
        decision = None
        if outcome and outcome.decision:
            decision = outcome.decision
        else:
            previous_decision = previous_status.get("latest_decision")
            if isinstance(previous_decision, str) and previous_decision:
                decision = previous_decision
        if decision:
            payload["latest_decision"] = decision
        if outputs:
            payload["outputs"] = outputs
        (session_directory / "status.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_event(
            session_directory,
            {
                "event": f"execution.{status}",
                "execution_id": context.execution_id,
                "stage_id": metadata["stage_id"],
                "timestamp": utc_now(),
            },
        )
        if outcome is not None:
            self._append_event(
                session_directory,
                {
                    "event": f"stage.{outcome.status}",
                    "execution_id": context.execution_id,
                    "stage_id": metadata["stage_id"],
                    "timestamp": utc_now(),
                    **outcome.to_json(),
                },
            )
        if session_status != "active":
            self._mark_session_terminal(context, session_status, outcome)

    def _reap_execution(self, context: ExecutionContext, *, on_live_owner: str) -> None:
        path = context.directory / "execution.json"
        with self._lock_execution(path):
            metadata = json.loads(path.read_text(encoding="utf-8"))
            decision = classify_orphaned_execution(
                metadata, context.directory, self.process_inspector
            )
            if decision.action in {REAP_ALREADY_EXITED, REAP_BOOT_MISMATCH}:
                self._fail_orphaned_execution(
                    context, metadata, decision.error or "", _locked=True
                )
                return
            if decision.action == REAP_REAPED:
                if not stop_verified_process_group(
                    metadata,
                    context.directory,
                    self.process_inspector,
                    TERMINATION_GRACE_SEC,
                ):
                    if on_live_owner == "raise":
                        raise_reap_decision(
                            ReapDecision(
                                REAP_UNVERIFIED,
                                decision.session_id,
                                decision.execution_id,
                                decision.owner_pid,
                            )
                        )
                    return
                self._fail_orphaned_execution(
                    context, metadata, decision.error or "", _locked=True
                )
                return
            if decision.action in {REAP_STILL_RUNNING, REAP_UNVERIFIED}:
                if on_live_owner == "raise":
                    raise_reap_decision(decision)

    def _fail_orphaned_execution(
        self,
        context: ExecutionContext,
        metadata: Dict[str, Any],
        error: str,
        *,
        _locked: bool,
    ) -> None:
        record = AgentSessionRecord(
            provider=str(metadata.get("provider") or "unknown"),
            model=str(metadata.get("model") or "unknown"),
            workspace=str(self.workspace),
            agent_id=metadata.get("agent_id")
            if isinstance(metadata.get("agent_id"), str)
            else None,
            thinking=metadata.get("thinking")
            if isinstance(metadata.get("thinking"), str)
            else None,
            status="failed",
        )
        conversation_path = context.conversation_read_path
        if conversation_path.exists():
            raw = json.loads(conversation_path.read_text(encoding="utf-8"))
            record = AgentSessionRecord.from_json(
                raw,
                record.provider,
                record.model,
                record.workspace,
            )
            record.status = "failed"
            self.save(record, context)
        self.save_execution(
            context, record, "failed", error=error, _locked=_locked
        )

    @contextmanager
    def _lock_execution(self, path: Path) -> Iterator[None]:
        if os.name != "posix" or not path.exists():
            yield
            return
        import fcntl

        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _ensure_session_is_runnable(self, session_id: Optional[str]) -> None:
        if session_id is None:
            return
        status_path = self.sessions_directory / session_id / "status.json"
        if not status_path.exists():
            return
        status = json.loads(status_path.read_text(encoding="utf-8"))
        message = runnable_violation(session_id, status)
        if message:
            raise ValueError(message)

    def _session_status(
        self,
        execution_status: str,
        outcome: Optional[StageOutcome],
        *,
        previous_status: Dict[str, Any],
        workflow: Optional[WorkflowDocument],
        stage_id: str,
    ) -> str:
        return session_status_after_execution(
            execution_status,
            outcome,
            previous_status,
            workflow,
            stage_id,
        )

    def _terminal_session_details(
        self,
        context: ExecutionContext,
        outcome: Optional[StageOutcome],
        session_status: str,
    ) -> Dict[str, str]:
        if session_status == "blocked":
            details = {
                "terminal_execution_id": context.execution_id,
                "blocked_by_execution_id": context.execution_id,
                "blocked_by_stage_id": self._stage_id(context),
            }
            if outcome and outcome.blocker:
                details["blocker"] = outcome.blocker
            return details
        if session_status in {"cancelled", "completed"}:
            return {"terminal_execution_id": context.execution_id}
        return {}

    def _rewrite_manifest_status(self, session_directory: Path, status: str) -> None:
        manifest_path = session_directory / "manifest.yaml"
        if not manifest_path.is_file():
            return
        text = manifest_path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^status: \S+\s*$", f"status: {status}", text, count=1
        )
        if count:
            manifest_path.write_text(updated, encoding="utf-8")

    def _load_workflow(
        self, previous_status: Dict[str, Any]
    ) -> Optional[WorkflowDocument]:
        if previous_status.get("workflow_id") == "agent":
            return None
        workflow_path = previous_status.get("workflow_path")
        if not isinstance(workflow_path, str) or not workflow_path:
            return None
        path = self.workspace / workflow_path
        if not path.is_file():
            return None
        try:
            return load_workflow_document(path)
        except ValueError:
            return None

    def _mark_session_terminal(
        self,
        context: ExecutionContext,
        status: str,
        outcome: Optional[StageOutcome],
    ) -> None:
        session_directory = context.directory.parents[1]
        self._rewrite_manifest_status(session_directory, status)
        self._append_event(
            session_directory,
            {
                "event": f"session.{status}",
                "execution_id": context.execution_id,
                "stage_id": self._stage_id(context),
                "timestamp": utc_now(),
                **(outcome.to_json() if outcome else {}),
            },
        )

    def _stage_id(self, context: ExecutionContext) -> str:
        metadata = json.loads(
            (context.directory / "execution.json").read_text(encoding="utf-8")
        )
        return metadata["stage_id"]

    def _read_session_status(self, session_directory: Path) -> Dict[str, Any]:
        status_path = session_directory / "status.json"
        if not status_path.exists():
            return {}
        return json.loads(status_path.read_text(encoding="utf-8"))

    def _provider_status_fields(
        self,
        previous_status: Dict[str, Any],
        context: ExecutionContext,
        status: str,
    ) -> Dict[str, Any]:
        if status == "running":
            return {
                key: value
                for key, value in previous_status.items()
                if key
                in {
                    "workflow_id",
                    "workflow_path",
                    "role",
                    "task",
                    "created_at",
                    "prior_session_id",
                    "latest_provider_event",
                    "latest_decision",
                    "latest_stage_id",
                    "current_stage",
                    "outputs",
                    "workspace_id",
                }
            }
        current_stage = previous_status.get("current_stage")
        if isinstance(current_stage, dict) and current_stage.get("execution_id") != context.execution_id:
            preserved = {"current_stage": current_stage}
            outputs = previous_status.get("outputs")
            if isinstance(outputs, dict):
                preserved["outputs"] = outputs
            if "workspace_id" in previous_status:
                preserved["workspace_id"] = previous_status["workspace_id"]
            return preserved
        return {
            key: value
            for key, value in previous_status.items()
            if key
            in {
                "workflow_id",
                "workflow_path",
                "role",
                "task",
                "created_at",
                "prior_session_id",
                "latest_provider_event",
                "latest_decision",
                "latest_stage_id",
                "outputs",
                "workspace_id",
            }
        }

    def _create_execution(
        self,
        provider: str,
        model: str,
        thinking: Optional[str],
        session_id: Optional[str],
        stage_id: str,
        stage_attempt: Optional[int],
        execution_order: Optional[int],
        prompt: str,
        resumes_execution_id: Optional[str] = None,
    ) -> ExecutionContext:
        session_id = session_id or f"session_{uuid.uuid4().hex}"
        execution_order = execution_order or self._next_execution_order(session_id)
        stage_attempt = stage_attempt or self._next_stage_attempt(session_id, stage_id)
        execution_id = (
            f"{execution_order:04d}-{safe_component(stage_id)}"
            f"--attempt-{stage_attempt:02d}"
        )
        directory = self.sessions_directory / session_id / EXECUTIONS_DIRNAME / execution_id
        if directory.exists():
            raise ValueError(f"Execution already exists: {directory}")
        directory.mkdir(parents=True)
        context = ExecutionContext(session_id, execution_id, directory)
        context.artifact_directory.mkdir()
        self._create_session_files(context)
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")
        (directory / "execution.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "execution_order": execution_order,
                    "stage_id": stage_id,
                    "stage_attempt": stage_attempt,
                    "provider": provider,
                    "model": model,
                    "thinking": thinking,
                    "agent_id": None,
                    "resumes_execution_id": resumes_execution_id,
                    "status": "running",
                    "schema_version": EXECUTION_SCHEMA_VERSION,
                    "created_at": utc_now(),
                    "artifact_directory": "artifacts",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._append_event(
            directory.parents[1],
            {
                "event": "execution.started",
                "execution_id": execution_id,
                "stage_id": stage_id,
                "timestamp": utc_now(),
            },
        )
        return context

    def _create_session_files(self, context: ExecutionContext) -> None:
        session_directory = context.directory.parents[1]
        manifest_path = session_directory / "manifest.yaml"
        if manifest_path.exists():
            return
        manifest_path.write_text(
            "\n".join(
                [
                    f"session_id: {context.session_id}",
                    f"created_at: {utc_now()}",
                    "status: running",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (session_directory / "status.json").write_text(
            json.dumps(
                {
                    "session_id": context.session_id,
                    "status": "active",
                    "latest_execution_id": context.execution_id,
                    "latest_execution_status": "running",
                    "updated_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _next_execution_order(self, session_id: str) -> int:
        contexts = self._execution_contexts(session_id)
        return max((self._execution_sort_key(context)[0] for context in contexts), default=0) + 1

    def _next_stage_attempt(self, session_id: str, stage_id: str) -> int:
        attempts = []
        for context in self._execution_contexts(session_id):
            metadata = json.loads(
                (context.directory / "execution.json").read_text(encoding="utf-8")
            )
            if metadata.get("stage_id") == stage_id:
                attempts.append(metadata.get("stage_attempt", 0))
        return max(attempts, default=0) + 1

    def _execution_contexts(self, session_id: str) -> List[ExecutionContext]:
        directory = self.sessions_directory / session_id / EXECUTIONS_DIRNAME
        if not directory.exists():
            return []
        return [
            ExecutionContext(session_id, path.name, path)
            for path in directory.iterdir()
            if path.is_dir() and (path / "execution.json").exists()
        ]

    def _execution_sort_key(self, context: ExecutionContext) -> Tuple[int, str]:
        metadata = json.loads(
            (context.directory / "execution.json").read_text(encoding="utf-8")
        )
        return metadata.get("execution_order", 0), context.execution_id

    def _published_outputs(
        self,
        context: ExecutionContext,
        status: str,
        outcome: Optional[StageOutcome],
        previous_status: Dict[str, Any],
        workflow: Optional[WorkflowDocument],
        metadata: Dict[str, Any],
    ) -> Dict[str, Dict[str, str]]:
        if workflow is None:
            return {}
        session_directory = context.directory.parents[1]
        stage_id = str(metadata.get("stage_id") or "")
        workflow_id = previous_status.get("workflow_id")
        if not should_publish_outputs(
            workflow_id=workflow_id if isinstance(workflow_id, str) else None,
            execution_status=status,
            outcome=outcome,
            workflow=workflow,
            stage_id=stage_id,
        ):
            return {}
        completion = workflow.completion
        if completion is None:
            return {}
        before_order = metadata.get("execution_order", 0)
        snapshots = self._execution_snapshots(context.session_id)
        sources = {}
        for output in completion.outputs:
            source = select_source_execution(snapshots, output.from_stage, before_order)
            if source is None:
                continue
            sources[output.name] = (
                source.artifact_directory / output.artifact,
                source.execution_id,
            )
        return publish_output_files(session_directory, sources, context.execution_id)

    def _execution_snapshots(self, session_id: str) -> list[ExecutionSnapshot]:
        snapshots: list[ExecutionSnapshot] = []
        for context in self._execution_contexts(session_id):
            metadata = json.loads(
                (context.directory / "execution.json").read_text(encoding="utf-8")
            )
            outcome = metadata.get("outcome")
            outcome_status = None
            outcome_decision = None
            if isinstance(outcome, dict):
                raw_status = outcome.get("status")
                if isinstance(raw_status, str):
                    outcome_status = raw_status
                raw_decision = outcome.get("decision")
                if isinstance(raw_decision, str) and raw_decision:
                    outcome_decision = raw_decision
            snapshots.append(
                ExecutionSnapshot(
                    execution_id=context.execution_id,
                    stage_id=str(metadata.get("stage_id") or ""),
                    execution_order=int(metadata.get("execution_order") or 0),
                    status=str(metadata.get("status") or ""),
                    outcome_status=outcome_status,
                    artifact_directory=context.artifact_directory,
                    outcome_decision=outcome_decision,
                )
            )
        return snapshots

    def _append_event(self, session_directory: Path, event: Dict[str, Any]) -> None:
        with (session_directory / "events.jsonl").open("a", encoding="utf-8") as events_file:
            events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
