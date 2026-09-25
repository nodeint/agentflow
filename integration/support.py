from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator

from agentflow_cli.run import main

INTEGRATION_ROOT = Path(__file__).resolve().parent
PROVIDER_BIN = INTEGRATION_ROOT / "fixtures" / "bin"
GROK_SESSION = "fake-grok-session"
CODEX_THREAD = "fake-codex-thread"

GROK_CONFIG = """\
models:
  grok-model:
    provider: grok
    model: grok-test
    options:
      thinking: low
roles:
  planner:
    default_model: grok-model
  reviewer:
    default_model: grok-model
"""

CODEX_CONFIG = """\
models:
  codex-model:
    provider: codex
    model: codex-test
    options:
      thinking: low
roles:
  writer:
    default_model: codex-model
"""

PLAN_REVIEW = """\
id: plan
description: Write and approve a plan before implementation.
stages:
  - id: plan
    role: planner
    max_revisions: 3
    depends_on: []
    produces:
      artifact: plan.md
  - id: review-plan
    role: reviewer
    depends_on:
      - plan
    decision:
      values:
        - approved
        - revise
      routes:
        approved: complete
        revise: plan
completion:
  stage: review-plan
  decision: approved
  outputs:
    - name: plan
      from_stage: plan
      artifact: plan.md
"""

DRAFT_NOTE = """\
id: draft-note
stages:
  - id: draft
    role: writer
    depends_on: []
    produces:
      artifact: note.md
"""

SOLO = """\
id: solo
stages:
  - id: write
    role: planner
    depends_on: []
    produces:
      artifact: note.md
completion:
  stage: write
"""


FAKE_SCRIPT = ".agentflow-fake.json"
FAKE_CURSOR = ".agentflow-fake.cursor"
FAKE_LOG = ".agentflow-fake.log"

COMPLETE = "status: complete\n\nready"
APPROVED = "status: complete\ndecision: approved\n\nok"
REVISE = "status: complete\ndecision: revise\n\nagain"
BLOCKED = "status: blocked\nblocker: missing schema\n\nnope"


def resumed_notes(role: str) -> str:
    return f"""\
id: notes
stages:
  - id: draft
    role: {role}
    depends_on: []
    produces:
      artifact: draft.md
  - id: follow
    role: {role}
    session:
      resume_from: draft
    depends_on:
      - draft
    produces:
      artifact: follow.md
completion:
  stage: follow
  outputs:
    - name: draft
      from_stage: draft
      artifact: draft.md
"""


def install_script(root: Path, steps: list[dict]) -> None:
    (root / FAKE_SCRIPT).write_text(json.dumps(steps), encoding="utf-8")
    (root / FAKE_CURSOR).write_text("0", encoding="utf-8")
    log = root / FAKE_LOG
    if log.exists():
        log.unlink()


def provider_invocations(root: Path) -> list[dict]:
    log = root / FAKE_LOG
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def has_args(argv: list[str], *parts: str) -> bool:
    width = len(parts)
    return any(tuple(argv[index : index + width]) == parts for index in range(len(argv) - width + 1))


def write_workspace(root: Path, config: str, workflows: dict[str, str]) -> None:
    agentflow = root / ".agentflow"
    workflow_dir = agentflow / "workflows"
    workflow_dir.mkdir(parents=True)
    (agentflow / "config.yaml").write_text(config, encoding="utf-8")
    for workflow_id, document in workflows.items():
        (workflow_dir / f"{workflow_id}.yaml").write_text(document, encoding="utf-8")


@contextmanager
def provider_env(outcome: str = "complete") -> Iterator[None]:
    previous_path = os.environ.get("PATH", "")
    previous_outcome = os.environ.get("AGENTFLOW_FAKE_OUTCOME")
    previous_script = os.environ.get("AGENTFLOW_FAKE_SCRIPT")
    previous_cursor = os.environ.get("AGENTFLOW_FAKE_CURSOR")
    previous_log = os.environ.get("AGENTFLOW_FAKE_LOG")
    os.environ["PATH"] = str(PROVIDER_BIN) + os.pathsep + previous_path
    os.environ["AGENTFLOW_FAKE_OUTCOME"] = outcome
    try:
        yield
    finally:
        os.environ["PATH"] = previous_path
        if previous_outcome is None:
            os.environ.pop("AGENTFLOW_FAKE_OUTCOME", None)
        else:
            os.environ["AGENTFLOW_FAKE_OUTCOME"] = previous_outcome
        _restore_env("AGENTFLOW_FAKE_SCRIPT", previous_script)
        _restore_env("AGENTFLOW_FAKE_CURSOR", previous_cursor)
        _restore_env("AGENTFLOW_FAKE_LOG", previous_log)


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def run_main(
    workspace: Path, argv: list[str], outcome: str = "complete"
) -> tuple[int, list[dict], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    script = workspace / FAKE_SCRIPT
    with provider_env(outcome), redirect_stdout(stdout), redirect_stderr(stderr):
        if script.is_file():
            os.environ["AGENTFLOW_FAKE_SCRIPT"] = str(script)
            os.environ["AGENTFLOW_FAKE_CURSOR"] = str(workspace / FAKE_CURSOR)
            os.environ["AGENTFLOW_FAKE_LOG"] = str(workspace / FAKE_LOG)
        code = main(argv, cwd=workspace)
    return code, json_objects(stdout.getvalue()), stderr.getvalue()


def json_objects(text: str) -> list[dict]:
    objects = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        objects.append(json.loads(stripped))
    return objects


def execution_statuses(workspace: Path) -> list[str]:
    return [str(record.get("status") or "") for record in execution_records(workspace)]


def execution_records(workspace: Path, session_id: str | None = None) -> list[dict]:
    runs = workspace / ".agentflow" / "sessions"
    pattern = (
        f"{session_id}/executions/*/execution.json"
        if session_id
        else "*/executions/*/execution.json"
    )
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in runs.glob(pattern)
    ]
    records.sort(key=lambda item: (item.get("session_id") or "", item.get("execution_order") or 0))
    return records


def session_status(workspace: Path, session_id: str) -> dict:
    path = workspace / ".agentflow" / "sessions" / session_id / "status.json"
    return json.loads(path.read_text(encoding="utf-8"))
