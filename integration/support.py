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
    thinking:
      allowed: [low]
      default: low
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
    thinking:
      allowed: [low]
      default: low
roles:
  writer:
    default_model: codex-model
"""

PLAN_REVIEW = """\
id: plan-review
description: Write and approve a plan before implementation.
stages:
  - id: plan
    role: planner
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
"""


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


def run_main(
    workspace: Path, argv: list[str], outcome: str = "complete"
) -> tuple[int, list[dict], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with provider_env(outcome), redirect_stdout(stdout), redirect_stderr(stderr):
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
    runs = workspace / ".agentflow" / "runs"
    statuses = []
    for path in sorted(runs.glob("*/executions/*/execution.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        statuses.append(str(payload.get("status") or ""))
    return statuses
