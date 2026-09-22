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
    run_status_after_execution,
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
    EXECUTION_SCHEMA_VERSION,
    EXECUTIONS_DIRNAME,
    RUNS_DIRNAME,
    SESSION_FILENAME,
    safe_component,
    utc_now,
)
from .stage_outcome import StageOutcome
from .workflow import WorkflowDocument, load_workflow_document


@dataclass
class SessionRecord:
    provider: str
    model: str
    workspace: str
    provider_session_id: Optional[str] = None
    thinking: Optional[str] = None
    status: str = "new"
    history: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_json(
        cls, raw: Any, provider: str, model: str, workspace: str
    ) -> "SessionRecord":
        if isinstance(raw, list):
            return cls(provider, model, workspace, history=raw)
        if not isinstance(raw, dict):
            return cls(provider, model, workspace)
        history = raw.get("history")
        return cls(
            provider=raw.get("provider") or provider,
            model=raw.get("model") or model,
            workspace=raw.get("workspace") or workspace,
            provider_session_id=raw.get("provider_session_id")
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
            "provider_session_id": self.provider_session_id,
            "thinking": self.thinking,
            "status": self.status,
            "history": self.history,
        }


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    execution_id: str
    directory: Path

    @property
    def session_path(self) -> Path:
        return self.directory / SESSION_FILENAME

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
        self.runs_directory = workspace / AGENTFLOW_DIRNAME / RUNS_DIRNAME
        self.process_inspector = process_inspector or PosixProcessInspector()

    def load_or_create(
        self,
        runner_session_id: Optional[str],
        provider: str,
        model: str,
        thinking: Optional[str],
        workspace: Path,
        run_id: Optional[str],
        stage_id: str,
        stage_attempt: Optional[int],
        execution_order: Optional[int],
        prompt: str,
    ) -> Tuple[str, SessionRecord, bool, ExecutionContext]:
        runner_session_id = runner_session_id or str(uuid.uuid4())
        previous_context = self._find_latest_execution(runner_session_id)
        if (
            previous_context is not None
            and run_id is not None
            and previous_context.run_id != run_id
        ):
            raise ValueError(
                f"Run {run_id} does not match runner session run {previous_context.run_id}."
            )
        resolved_run_id = run_id or (
            previous_context.run_id if previous_context is not None else None
        )
        if resolved_run_id is not None:
            self.reap_orphaned_executions(resolved_run_id, on_live_owner="raise")
        self._ensure_run_is_runnable(resolved_run_id)
        if previous_context is None:
            legacy_path = self.workspace / ".sessions" / f"{runner_session_id}.json"
            if legacy_path.exists():
                raw = json.loads(legacy_path.read_text(encoding="utf-8"))
                context = self._create_execution(
                    runner_session_id,
                    provider,
                    model,
                    thinking,
                    resolved_run_id,
                    stage_id,
                    stage_attempt,
                    execution_order,
                    prompt,
                )
                return (
                    runner_session_id,
                    SessionRecord.from_json(raw, provider, model, str(workspace)),
                    False,
                    context,
                )
        if previous_context is None:
            context = self._create_execution(
                runner_session_id,
                provider,
                model,
                thinking,
                resolved_run_id,
                stage_id,
                stage_attempt,
                execution_order,
                prompt,
            )
            return (
                runner_session_id,
                SessionRecord(provider, model, str(workspace), thinking=thinking),
                True,
                context,
            )

        raw = json.loads(previous_context.session_path.read_text(encoding="utf-8"))
        context = self._create_execution(
            runner_session_id,
            provider,
            model,
            thinking,
            resolved_run_id,
            stage_id,
            stage_attempt,
            execution_order,
            prompt,
            resumes_execution_id=previous_context.execution_id,
        )
        return (
            runner_session_id,
            SessionRecord.from_json(raw, provider, model, str(workspace)),
            False,
            context,
        )

    def save(
        self, runner_session_id: str, record: SessionRecord, context: ExecutionContext
    ) -> None:
        payload = record.to_json()
        payload["runner_session_id"] = runner_session_id
        context.session_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_execution(
        self,
        context: ExecutionContext,
        record: SessionRecord,
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
        self, run_id: str, *, on_live_owner: str
    ) -> None:
        if os.name != "posix":
            return
        for context in self._execution_contexts(run_id):
            self._reap_execution(context, on_live_owner=on_live_owner)

    def record_provider_event(
        self, context: ExecutionContext, event: Dict[str, Any]
    ) -> None:
        run_directory = context.directory.parents[1]
        self._append_event(run_directory, event)
        if event.get("event") not in STATUS_PROVIDER_EVENTS:
            return
        status = self._read_run_status(run_directory)
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
        (run_directory / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def latest_execution(self, runner_session_id: str) -> Optional[ExecutionContext]:
        return self._find_latest_execution(runner_session_id)

    def stage_id(self, context: ExecutionContext) -> str:
        return self._stage_id(context)

    def read_run_status(self, run_id: str) -> Dict[str, Any]:
        return self._read_run_status(self.runs_directory / run_id)

    def execution_snapshots(self, run_id: str) -> list[ExecutionSnapshot]:
        return self._execution_snapshots(run_id)

    def missing_completion_outputs(
        self, context: ExecutionContext, outcome: Optional[StageOutcome]
    ) -> list[str]:
        previous_status = self._read_run_status(context.directory.parents[1])
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
            snapshots=self._execution_snapshots(context.run_id),
            before_order=int(metadata.get("execution_order") or 0),
        )

    def save_prompt(self, context: ExecutionContext, prompt: str) -> None:
        (context.directory / "prompt.md").write_text(prompt, encoding="utf-8")

    def save_response(self, context: ExecutionContext, response_text: str) -> None:
        (context.directory / "response.md").write_text(response_text, encoding="utf-8")

    def _save_execution_unlocked(
        self,
        context: ExecutionContext,
        record: SessionRecord,
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
        run_directory = context.directory.parents[1]
        previous_status = self._read_run_status(run_directory)
        workflow = self._load_workflow(previous_status)
        if status == "running" and previous_status.get("status") == "completed":
            self._rewrite_manifest_status(run_directory, "running")
        published = (
            {}
            if status == "running"
            else self._published_outputs(
                context, status, outcome, previous_status, workflow, metadata
            )
        )
        metadata["status"] = status
        metadata["provider_session_id"] = record.provider_session_id
        metadata["completed_at"] = utc_now() if status != "running" else None
        if error is not None:
            metadata["error"] = error
        if outcome is not None:
            metadata["outcome"] = outcome.to_json()
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        run_status = self._run_status(
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
            "run_id": context.run_id,
            "status": run_status,
            "latest_execution_id": context.execution_id,
            "latest_execution_status": status,
            "latest_stage_id": str(metadata.get("stage_id") or ""),
            "updated_at": utc_now(),
            **self._terminal_run_details(context, outcome, run_status),
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
        (run_directory / "status.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_event(
            run_directory,
            {
                "event": f"execution.{status}",
                "execution_id": context.execution_id,
                "stage_id": metadata["stage_id"],
                "timestamp": utc_now(),
            },
        )
        if outcome is not None:
            self._append_event(
                run_directory,
                {
                    "event": f"stage.{outcome.status}",
                    "execution_id": context.execution_id,
                    "stage_id": metadata["stage_id"],
                    "timestamp": utc_now(),
                    **outcome.to_json(),
                },
            )
        if run_status != "active":
            self._mark_run_terminal(context, run_status, outcome)

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
                                decision.run_id,
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
        record = SessionRecord(
            provider=str(metadata.get("provider") or "unknown"),
            model=str(metadata.get("model") or "unknown"),
            workspace=str(self.workspace),
            provider_session_id=metadata.get("provider_session_id")
            if isinstance(metadata.get("provider_session_id"), str)
            else None,
            thinking=metadata.get("thinking")
            if isinstance(metadata.get("thinking"), str)
            else None,
            status="failed",
        )
        if context.session_path.exists():
            raw = json.loads(context.session_path.read_text(encoding="utf-8"))
            record = SessionRecord.from_json(
                raw,
                record.provider,
                record.model,
                record.workspace,
            )
            record.status = "failed"
            runner_session_id = raw.get("runner_session_id") or metadata.get(
                "runner_session_id"
            )
            if isinstance(runner_session_id, str) and runner_session_id:
                self.save(runner_session_id, record, context)
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

    def _ensure_run_is_runnable(self, run_id: Optional[str]) -> None:
        if run_id is None:
            return
        status_path = self.runs_directory / run_id / "status.json"
        if not status_path.exists():
            return
        status = json.loads(status_path.read_text(encoding="utf-8"))
        message = runnable_violation(run_id, status)
        if message:
            raise ValueError(message)

    def _run_status(
        self,
        execution_status: str,
        outcome: Optional[StageOutcome],
        *,
        previous_status: Dict[str, Any],
        workflow: Optional[WorkflowDocument],
        stage_id: str,
    ) -> str:
        return run_status_after_execution(
            execution_status,
            outcome,
            previous_status,
            workflow,
            stage_id,
        )

    def _terminal_run_details(
        self,
        context: ExecutionContext,
        outcome: Optional[StageOutcome],
        run_status: str,
    ) -> Dict[str, str]:
        if run_status == "blocked":
            details = {
                "terminal_execution_id": context.execution_id,
                "blocked_by_execution_id": context.execution_id,
                "blocked_by_stage_id": self._stage_id(context),
            }
            if outcome and outcome.blocker:
                details["blocker"] = outcome.blocker
            return details
        if run_status in {"cancelled", "completed"}:
            return {"terminal_execution_id": context.execution_id}
        return {}

    def _rewrite_manifest_status(self, run_directory: Path, status: str) -> None:
        manifest_path = run_directory / "manifest.yaml"
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

    def _mark_run_terminal(
        self,
        context: ExecutionContext,
        status: str,
        outcome: Optional[StageOutcome],
    ) -> None:
        run_directory = context.directory.parents[1]
        self._rewrite_manifest_status(run_directory, status)
        self._append_event(
            run_directory,
            {
                "event": f"run.{status}",
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

    def _read_run_status(self, run_directory: Path) -> Dict[str, Any]:
        status_path = run_directory / "status.json"
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
                    "prior_run_id",
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
                "prior_run_id",
                "latest_provider_event",
                "latest_decision",
                "latest_stage_id",
                "outputs",
                "workspace_id",
            }
        }

    def _find_latest_execution(self, runner_session_id: str) -> Optional[ExecutionContext]:
        if not self.runs_directory.exists():
            return None
        matches: List[ExecutionContext] = []
        for session_path in self.runs_directory.glob(
            f"*/{EXECUTIONS_DIRNAME}/*/{SESSION_FILENAME}"
        ):
            raw = json.loads(session_path.read_text(encoding="utf-8"))
            if raw.get("runner_session_id") == runner_session_id:
                matches.append(
                    ExecutionContext(
                        run_id=session_path.parents[2].name,
                        execution_id=session_path.parent.name,
                        directory=session_path.parent,
                    )
                )
        return max(matches, key=self._execution_sort_key) if matches else None

    def _create_execution(
        self,
        runner_session_id: str,
        provider: str,
        model: str,
        thinking: Optional[str],
        run_id: Optional[str],
        stage_id: str,
        stage_attempt: Optional[int],
        execution_order: Optional[int],
        prompt: str,
        resumes_execution_id: Optional[str] = None,
    ) -> ExecutionContext:
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        execution_order = execution_order or self._next_execution_order(run_id)
        stage_attempt = stage_attempt or self._next_stage_attempt(run_id, stage_id)
        execution_id = (
            f"{execution_order:04d}-{safe_component(stage_id)}"
            f"--attempt-{stage_attempt:02d}"
        )
        directory = self.runs_directory / run_id / EXECUTIONS_DIRNAME / execution_id
        if directory.exists():
            raise ValueError(f"Execution already exists: {directory}")
        directory.mkdir(parents=True)
        context = ExecutionContext(run_id, execution_id, directory)
        context.artifact_directory.mkdir()
        self._create_run_files(context)
        (directory / "prompt.md").write_text(prompt, encoding="utf-8")
        (directory / "execution.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "execution_id": execution_id,
                    "execution_order": execution_order,
                    "stage_id": stage_id,
                    "stage_attempt": stage_attempt,
                    "provider": provider,
                    "model": model,
                    "thinking": thinking,
                    "runner_session_id": runner_session_id,
                    "provider_session_id": None,
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

    def _create_run_files(self, context: ExecutionContext) -> None:
        run_directory = context.directory.parents[1]
        manifest_path = run_directory / "manifest.yaml"
        if manifest_path.exists():
            return
        manifest_path.write_text(
            "\n".join(
                [
                    f"run_id: {context.run_id}",
                    f"created_at: {utc_now()}",
                    "status: running",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (run_directory / "status.json").write_text(
            json.dumps(
                {
                    "run_id": context.run_id,
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

    def _next_execution_order(self, run_id: str) -> int:
        contexts = self._execution_contexts(run_id)
        return max((self._execution_sort_key(context)[0] for context in contexts), default=0) + 1

    def _next_stage_attempt(self, run_id: str, stage_id: str) -> int:
        attempts = []
        for context in self._execution_contexts(run_id):
            metadata = json.loads(
                (context.directory / "execution.json").read_text(encoding="utf-8")
            )
            if metadata.get("stage_id") == stage_id:
                attempts.append(metadata.get("stage_attempt", 0))
        return max(attempts, default=0) + 1

    def _execution_contexts(self, run_id: str) -> List[ExecutionContext]:
        directory = self.runs_directory / run_id / EXECUTIONS_DIRNAME
        if not directory.exists():
            return []
        return [
            ExecutionContext(run_id, path.name, path)
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
        run_directory = context.directory.parents[1]
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
        snapshots = self._execution_snapshots(context.run_id)
        sources = {}
        for output in completion.outputs:
            source = select_source_execution(snapshots, output.from_stage, before_order)
            if source is None:
                continue
            sources[output.name] = (
                source.artifact_directory / output.artifact,
                source.execution_id,
            )
        return publish_output_files(run_directory, sources, context.execution_id)

    def _execution_snapshots(self, run_id: str) -> list[ExecutionSnapshot]:
        snapshots: list[ExecutionSnapshot] = []
        for context in self._execution_contexts(run_id):
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
            runner_session_id = metadata.get("runner_session_id")
            snapshots.append(
                ExecutionSnapshot(
                    execution_id=context.execution_id,
                    stage_id=str(metadata.get("stage_id") or ""),
                    execution_order=int(metadata.get("execution_order") or 0),
                    status=str(metadata.get("status") or ""),
                    outcome_status=outcome_status,
                    artifact_directory=context.artifact_directory,
                    outcome_decision=outcome_decision,
                    runner_session_id=runner_session_id
                    if isinstance(runner_session_id, str)
                    else None,
                )
            )
        return snapshots

    def _append_event(self, run_directory: Path, event: Dict[str, Any]) -> None:
        with (run_directory / "events.jsonl").open("a", encoding="utf-8") as events_file:
            events_file.write(json.dumps(event, ensure_ascii=False) + "\n")
