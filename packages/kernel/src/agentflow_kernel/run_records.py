from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional

from .config import ConfigurationError
from .query import read_status_file
from .session_store import SessionStore
from .workflow import WorkflowDocument, load_workflow_document


def allocate_run_id(runs_directory: Path, run_id_base: str) -> str:
    run_id = run_id_base
    suffix = 2
    while (runs_directory / run_id).exists():
        run_id = f"{run_id_base}--{suffix:02d}"
        suffix += 1
    return run_id


def run_slug(value: str, limit: int) -> str:
    slug = "".join(
        character.lower() if character.isalnum() else "-" for character in value
    )
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise ConfigurationError(
            "Workflow ID and task must contain a letter or number."
        )
    return slug[:limit].rstrip("-")


def create_workflow_run(
    workspace: Path,
    workflow_id: str,
    task: str,
    prior_run_id: Optional[str] = None,
) -> str:
    workflow_path = workspace / ".agentflow" / "workflows" / f"{workflow_id}.yaml"
    if not workflow_path.is_file():
        raise ConfigurationError(f"Workflow not found: {workflow_id}")
    try:
        workflow = load_workflow_document(workflow_path)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    resolved_prior = resolve_prior_run_id(workspace, workflow, prior_run_id)
    task_summary = task.strip()
    if not task_summary:
        raise ConfigurationError("Task summary must not be empty.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id_base = f"{timestamp}--{run_slug(workflow_id, 36)}--{run_slug(task_summary, 48)}"
    runs_directory = workspace / ".agentflow" / "runs"
    run_id = allocate_run_id(runs_directory, run_id_base)
    run_directory = runs_directory / run_id
    run_directory.mkdir(parents=True)
    created_at = datetime.now(timezone.utc).isoformat()
    workflow_relative_path = f".agentflow/workflows/{workflow_id}.yaml"
    manifest_lines = [
        f"run_id: {run_id}",
        f"workflow_id: {workflow_id}",
        f"workflow_path: {workflow_relative_path}",
        f"task: {json.dumps(task_summary, ensure_ascii=False)}",
        f"created_at: {created_at}",
        "status: running",
    ]
    if resolved_prior:
        manifest_lines.insert(-1, f"prior_run_id: {resolved_prior}")
    (run_directory / "manifest.yaml").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    status_payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "workflow_path": workflow_relative_path,
        "task": task_summary,
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if resolved_prior:
        status_payload["prior_run_id"] = resolved_prior
    (run_directory / "status.json").write_text(
        json.dumps(status_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_id


def create_agent_run(workspace: Path, role: str, task: str) -> str:
    task_summary = task.strip()
    if not task_summary:
        raise ConfigurationError("Task summary must not be empty.")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id_base = f"{timestamp}--agent--{run_slug(task_summary, 48)}"
    runs_directory = workspace / ".agentflow" / "runs"
    run_id = allocate_run_id(runs_directory, run_id_base)
    run_directory = runs_directory / run_id
    run_directory.mkdir(parents=True)
    created_at = datetime.now(timezone.utc).isoformat()
    (run_directory / "manifest.yaml").write_text(
        "\n".join(
            [
                f"run_id: {run_id}",
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
    (run_directory / "status.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
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
    return run_id


def require_agent_run(workspace: Path, run_id: str) -> None:
    run_directory = workspace / ".agentflow" / "runs" / run_id
    if not run_directory.is_dir():
        raise ConfigurationError(f"Run not found: {run_id}.")
    status_path = run_directory / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Run {run_id} is not an agent run.") from exc
    if not isinstance(status, dict) or status.get("workflow_id") != "agent":
        raise ConfigurationError(f"Run {run_id} is not an agent run.")


def resolve_prior_run_id(
    workspace: Path,
    workflow: WorkflowDocument,
    prior_run_id: Optional[str],
) -> Optional[str]:
    if workflow.requires is None:
        if prior_run_id:
            raise ConfigurationError(
                "Workflow does not declare requires; omit --prior-run-id."
            )
        return None
    if not prior_run_id:
        raise ConfigurationError(
            f"Workflow {workflow.id} requires completed {workflow.requires.workflow} "
            f"({workflow.requires.decision}); pass --prior-run-id."
        )
    status = read_status_file(
        workspace / ".agentflow" / "runs" / prior_run_id / "status.json"
    )
    if not status:
        raise ConfigurationError(f"Required run not found: {prior_run_id}.")
    if status.get("workflow_id") != workflow.requires.workflow:
        raise ConfigurationError(
            f"--prior-run-id {prior_run_id} is workflow {status.get('workflow_id')!r}, "
            f"not {workflow.requires.workflow!r}."
        )
    if status.get("status") != "completed":
        raise ConfigurationError(
            f"--prior-run-id {prior_run_id} is {status.get('status')}, not completed."
        )
    decision = status.get("latest_decision")
    if decision != workflow.requires.decision:
        raise ConfigurationError(
            f"--prior-run-id {prior_run_id} decision is {decision!r}, "
            f"not {workflow.requires.decision!r}."
        )
    return prior_run_id


def bind_prior_run_id(
    workspace: Path,
    run_id: str,
    prior_run_id: Optional[str],
) -> None:
    if not prior_run_id:
        return
    status = SessionStore(workspace).read_run_status(run_id)
    stored = status.get("prior_run_id")
    if stored != prior_run_id:
        raise ConfigurationError(
            f"--prior-run-id {prior_run_id} does not match run {stored}."
        )


