from __future__ import annotations

from pathlib import Path
import os
import tempfile
from typing import Dict, Mapping, Optional, Tuple

from .base_adapter import BaseCLIAdapter, NewSession
from .command_executor import (
    DEFAULT_TIMEOUT_SEC,
    CancellationRequested,
    CommandExecutor,
)
from .process_ownership import ProcessStartInfo
from .runtime import (
    add_artifact_context,
    add_stage_outcome_contract,
    flatten_history,
    log,
)
from .provider_events import ProviderEventReporter
from .session_store import ExecutionContext, SessionStore
from .outputs import nonempty_artifact
from .stage_outcome import parse_stage_outcome
from .workflow import load_stage


class AgentToolRunner:
    def __init__(
        self,
        adapters: Optional[Dict[str, BaseCLIAdapter]] = None,
        timeout_sec: Optional[float] = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if adapters is None:
            from agentflow_adapters import default_adapters

            adapters = default_adapters()
        self.adapters = adapters
        self.executor = CommandExecutor(timeout_sec=timeout_sec)

    def run(
        self,
        provider: str,
        model: str,
        prompt: str,
        workspace: str,
        session_id: Optional[str] = None,
        resume_execution_id: Optional[str] = None,
        stage_id: str = "agent",
        stage_attempt: Optional[int] = None,
        execution_order: Optional[int] = None,
        options: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace}")
        if stage_attempt is not None and stage_attempt < 1:
            raise ValueError("Stage attempt must be at least 1.")
        if execution_order is not None and execution_order < 1:
            raise ValueError("Execution order must be at least 1.")

        provider_key = provider.lower()
        adapter = self.adapters.get(provider_key)
        if adapter is None:
            supported = ", ".join(sorted(self.adapters))
            raise ValueError(f"Unsupported CLI provider: {provider}. Supported: {supported}")

        resolved_options = dict(options or {})
        thinking = resolved_options.get("thinking") or None
        store = SessionStore(workspace_path)
        record, is_new, context = store.load_or_create(
            provider_key,
            model,
            thinking,
            workspace_path,
            session_id,
            stage_id,
            stage_attempt,
            execution_order,
            prompt,
            resume_execution_id=resume_execution_id,
        )
        if not is_new:
            self._validate_session_profile(record.provider, record.model, record.thinking, provider_key, model, thinking)
        starts_new_session = is_new

        new_session = adapter.create_new_session() if starts_new_session else None
        if new_session and new_session.agent_id:
            record.agent_id = new_session.agent_id
        record.history.append({"role": "user", "content": prompt})
        record.status = "running"
        store.save(record, context)
        store.save_execution(context, record, "running")
        event_reporter = ProviderEventReporter(store, context, provider_key, model)

        agent_id = None if starts_new_session else record.agent_id
        turn_prompt = (
            prompt
            if starts_new_session or agent_id
            else flatten_history(record.history)
        )
        stage = None
        if session_id is not None:
            session_status = store.read_session_status(context.session_id)
            workflow_path = session_status.get("workflow_path")
            if (
                isinstance(workflow_path, str)
                and session_status.get("workflow_id") != "agent"
            ):
                stage = load_stage(store.workspace, workflow_path, stage_id)
        decision_values = stage.decision_values if stage is not None else ()
        if session_id is not None:
            turn_prompt = add_stage_outcome_contract(turn_prompt, decision_values)
        artifact_name = stage.artifact if stage is not None else None
        turn_prompt = add_artifact_context(
            turn_prompt, context.artifact_directory, artifact_name
        )
        store.save_prompt(context, turn_prompt)
        event_reporter.emit(
            "provider.stage_started",
            f"{provider_key}/{model}",
            {"thinking": thinking} if thinking else None,
        )

        try:
            response_text, parsed_provider_id, invoke_error = self._run_turn(
                adapter,
                model,
                workspace_path,
                turn_prompt,
                agent_id,
                new_session,
                resolved_options,
                event_reporter,
                store,
                context,
            )
        except CancellationRequested:
            event_reporter.emit("provider.cancelled", "cancellation requested")
            record.status = "cancelled"
            store.save(record, context)
            store.save_execution(context, record, "cancelled")
            log(f"Execution {context.execution_id} marked cancelled.")
            raise

        event_reporter.emit(
            "provider.turn_completed" if invoke_error is None else "provider.turn_failed",
            "completed" if invoke_error is None else invoke_error,
        )

        record.agent_id = parsed_provider_id or record.agent_id
        record.model = model
        record.thinking = thinking
        record.workspace = str(workspace_path)
        outcome = None
        if invoke_error is None:
            try:
                outcome = parse_stage_outcome(
                    response_text,
                    required=session_id is not None,
                    decision_values=decision_values,
                )
            except ValueError as exc:
                invoke_error = str(exc)
        if (
            invoke_error is None
            and outcome is not None
            and outcome.status == "complete"
            and artifact_name
        ):
            artifact_path = context.artifact_directory / artifact_name
            if not nonempty_artifact(artifact_path):
                invoke_error = f"Stage artifact missing or empty: {artifact_name}."
        if invoke_error is None and outcome is not None:
            missing = store.missing_completion_outputs(context, outcome)
            if missing:
                invoke_error = (
                    "Completion outputs missing or empty: " + ", ".join(missing) + "."
                )
        record.status = "completed" if invoke_error is None else "failed"
        record.history.append({"role": "assistant", "content": response_text})
        store.save(record, context)
        store.save_response(context, response_text)
        store.save_execution(
            context,
            record,
            record.status,
            error=invoke_error,
            outcome=outcome,
        )
        session_status = store.read_session_status(context.session_id)
        return {
            "agent_id": record.agent_id or "",
            "thinking": record.thinking or "",
            "session_id": context.session_id,
            "execution_id": context.execution_id,
            "artifact_directory": str(context.artifact_directory),
            "outcome_status": outcome.status if outcome else "",
            "outcome_decision": outcome.decision if outcome and outcome.decision else "",
            "session_status": str(session_status.get("status") or ""),
            "response": response_text,
        }

    def _run_turn(
        self,
        adapter: BaseCLIAdapter,
        model: str,
        workspace: Path,
        prompt: str,
        agent_id: Optional[str],
        new_session: Optional[NewSession],
        options: Mapping[str, str],
        event_reporter: ProviderEventReporter,
        store: SessionStore,
        context: ExecutionContext,
    ) -> Tuple[str, Optional[str], Optional[str]]:
        with tempfile.TemporaryDirectory(prefix="agentflow_") as tmp:
            tmp_path = Path(tmp)
            prompt_file = tmp_path / "prompt.txt"
            last_message_file = tmp_path / "last_message.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            spec = adapter.build_command(
                model=model,
                workspace=str(workspace),
                prompt=prompt,
                agent_id=agent_id,
                new_agent_id=(
                    new_session.agent_id if new_session else None
                ),
                prompt_file=prompt_file,
                last_message_file=last_message_file,
                options=options,
            )
            def on_output(line: str) -> None:
                event = adapter.parse_progress_event(line)
                if event is not None:
                    event_reporter.emit(
                        event["event"], event.get("summary"), event.get("details")
                    )

            identity_token = os.urandom(16).hex()
            identity_path = context.directory / "process.identity"

            def on_start(info: ProcessStartInfo) -> None:
                store.save_process_metadata(context, info)

            stdout, invoke_error = self.executor.run(
                spec,
                workspace,
                on_output=on_output,
                on_heartbeat=lambda elapsed: event_reporter.emit(
                    "provider.heartbeat", f"running for {elapsed}s", {"elapsed_seconds": elapsed}
                ),
                on_start=on_start,
                identity_path=identity_path,
                identity_token=identity_token,
            )
            response_text, parsed_provider_id = adapter.parse_response(
                stdout, last_message_file
            )
            return response_text or invoke_error or "", parsed_provider_id, invoke_error

    def _validate_session_profile(
        self,
        previous_provider: str,
        previous_model: str,
        previous_thinking: Optional[str],
        provider: str,
        model: str,
        thinking: Optional[str],
    ) -> None:
        previous_profile = (previous_provider, previous_model, previous_thinking)
        requested_profile = (provider, model, thinking)
        if previous_profile != requested_profile:
            raise ValueError(
                "Cannot resume an agent session with a different provider, model, or thinking level."
            )
