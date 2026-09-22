from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional

from .config import ConfigurationError
from .query import read_status_file
from .session_store import SessionStore
from .workflow import WorkflowDocument, load_workflow_document


def allocate_session_id(sessions_directory: Path, session_id_base: str) -> str:
    session_id = session_id_base
    suffix = 2
    while (sessions_directory / session_id).exists():
        session_id = f"{session_id_base}--{suffix:02d}"
        suffix += 1
    return session_id


def session_slug(value: str, limit: int) -> str:
    slug = "".join(
        character.lower() if character.isalnum() else "-" for character in value
    )
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise ConfigurationError(
            "Workflow ID and task must contain a letter or number."
        )
    return slug[:limit].rstrip("-")


def create_workflow_session(
    workspace: Path,
    workflow_id: str,
    task: str,
    prior_session_id: Optional[str] = None,
) -> str:
    workflow_path = workspace / ".agentflow" / "workflows" / f"{workflow_id}.yaml"
    if not workflow_path.is_file():
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    try:
        workflow = load_workflow_document(workflow_path)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    resolved_prior = resolve_prior_session_id(workspace, workflow, prior_session_id)
    task_summary = task.strip()
    if not task_summary:
        raise ConfigurationError("Task summary must not be empty.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    session_id_base = f"{timestamp}--{session_slug(workflow_id, 36)}--{session_slug(task_summary, 48)}"
    sessions_directory = workspace / ".agentflow" / "sessions"
    session_id = allocate_session_id(sessions_directory, session_id_base)
    session_directory = sessions_directory / session_id
    session_directory.mkdir(parents=True)
    created_at = datetime.now(timezone.utc).isoformat()
    workflow_relative_path = f".agentflow/workflows/{workflow_id}.yaml"
    manifest_lines = [
        f"session_id: {session_id}",
        f"workflow_id: {workflow_id}",
        f"workflow_path: {workflow_relative_path}",
        f"task: {json.dumps(task_summary, ensure_ascii=False)}",
        f"created_at: {created_at}",
        "status: running",
    ]
    if resolved_prior:
        manifest_lines.insert(-1, f"prior_session_id: {resolved_prior}")
    (session_directory / "manifest.yaml").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    status_payload: dict[str, Any] = {
        "session_id": session_id,
        "workflow_id": workflow_id,
        "workflow_path": workflow_relative_path,
        "task": task_summary,
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if resolved_prior:
        status_payload["prior_session_id"] = resolved_prior
    (session_directory / "status.json").write_text(
        json.dumps(status_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return session_id


def create_agent_session(workspace: Path, role: str, task: str) -> str:
    task_summary = task.strip()
    if not task_summary:
        raise ConfigurationError("Task summary must not be empty.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    session_id_base = f"{timestamp}--agent--{session_slug(task_summary, 48)}"
    sessions_directory = workspace / ".agentflow" / "sessions"
    session_id = allocate_session_id(sessions_directory, session_id_base)
    session_directory = sessions_directory / session_id
    session_directory.mkdir(parents=True)
    created_at = datetime.now(timezone.utc).isoformat()
    (session_directory / "manifest.yaml").write_text(
        "\n".join(
            [
                f"session_id: {session_id}",
                "workflow_id: agent",
                f"role: {role}",
                f"task: {json.dumps(task_summary, ensure_ascii=False)}",
                f"created_at: {created_at}",
                "status: running",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (session_directory / "status.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "workflow_id": "agent",
                "role": role,
                "task": task_summary,
                "status": "active",
                "created_at": created_at,
                "updated_at": created_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return session_id


def require_agent_session(workspace: Path, session_id: str) -> None:
    session_directory = workspace / ".agentflow" / "sessions" / session_id
    if not session_directory.is_dir():
        raise ConfigurationError(f"Session not found: {session_id}.")
    status_path = session_directory / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Session {session_id} was not created by agent.") from exc
    if not isinstance(status, dict) or status.get("workflow_id") != "agent":
        raise ConfigurationError(f"Session {session_id} was not created by agent.")


def resolve_prior_session_id(
    workspace: Path,
    workflow: WorkflowDocument,
    prior_session_id: Optional[str],
) -> Optional[str]:
    if workflow.requires is None:
        if prior_session_id:
            raise ConfigurationError(
                "Workflow does not declare requires; omit --prior."
            )
        return None
    if not prior_session_id:
        raise ConfigurationError(
            f"Workflow {workflow.id} requires completed {workflow.requires.workflow} "
            f"({workflow.requires.decision}); pass --prior."
        )
    status = read_status_file(
        workspace / ".agentflow" / "sessions" / prior_session_id / "status.json"
    )
    if not status:
        raise ConfigurationError(f"Required session not found: {prior_session_id}.")
    if status.get("workflow_id") != workflow.requires.workflow:
        raise ConfigurationError(
            f"--prior {prior_session_id} is workflow {status.get('workflow_id')!r}, "
            f"not {workflow.requires.workflow!r}."
        )
    if status.get("status") != "completed":
        raise ConfigurationError(
            f"--prior {prior_session_id} is {status.get('status')}, not completed."
        )
    decision = status.get("latest_decision")
    if decision != workflow.requires.decision:
        raise ConfigurationError(
            f"--prior {prior_session_id} decision is {decision!r}, "
            f"not {workflow.requires.decision!r}."
        )
    return prior_session_id


def bind_prior_session_id(
    workspace: Path,
    session_id: str,
    prior_session_id: Optional[str],
) -> None:
    if not prior_session_id:
        return
    status = SessionStore(workspace).read_session_status(session_id)
    stored = status.get("prior_session_id")
    if stored != prior_session_id:
        raise ConfigurationError(
            f"--prior {prior_session_id} does not match session {stored}."
        )


