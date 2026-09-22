from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import json
import tempfile
from typing import Optional, Tuple

from agentflow_kernel.base_adapter import BaseCLIAdapter, CommandSpec
from agentflow_kernel.session_records import create_workflow_session


class FakeAdapter(BaseCLIAdapter):
    def __init__(self) -> None:
        self.calls = []

    def build_command(
        self,
        model: str,
        workspace: str,
        prompt: str,
        agent_id: Optional[str],
        new_agent_id: Optional[str],
        prompt_file: Path,
        last_message_file: Path,
        thinking: Optional[str] = None,
    ) -> CommandSpec:
        self.calls.append(
            {
                "model": model,
                "workspace": workspace,
                "prompt": prompt,
                "agent_id": agent_id,
                "new_agent_id": new_agent_id,
                "prompt_file": prompt_file,
                "thinking": thinking,
            }
        )
        response_input = prompt.splitlines()[0].removeprefix("user: ")
        payload = json.dumps(
            {
                "text": f"status: complete\necho:{response_input}",
                "sessionId": "native-session-1",
            }
        )
        return CommandSpec(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.argv[1])",
                payload,
            ]
        )

    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        data = json.loads(stdout)
        return data["text"], data.get("sessionId")


class BlockedAdapter(FakeAdapter):
    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        del stdout, last_message_file
        return "status: blocked\nblocker: missing approved design", "native-session-1"


class MissingOutcomeAdapter(FakeAdapter):
    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        del stdout, last_message_file
        return "implementation report without a status header", "native-session-1"


class ScriptedAdapter(FakeAdapter):
    def __init__(
        self,
        responses: list[str],
        artifacts: Optional[list[Optional[str]]] = None,
    ) -> None:
        super().__init__()
        self._responses = list(responses)
        self._artifacts = list(artifacts) if artifacts is not None else []

    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        del stdout, last_message_file
        if self._artifacts:
            body = self._artifacts.pop(0)
            if body is not None and self.calls:
                _write_prompt_artifact(str(self.calls[-1]["prompt"]), body)
        return self._responses.pop(0), "native-session-1"


def _write_prompt_artifact(prompt: str, body: str) -> None:
    marker = "Agentflow artifact directory:\n"
    if marker not in prompt:
        return
    rest = prompt.split(marker, 1)[1]
    lines = rest.splitlines()
    if not lines:
        return
    directory = Path(lines[0].strip())
    name = None
    for line in lines[1:6]:
        if "as `" in line:
            name = line.split("as `", 1)[1].split("`", 1)[0]
            break
    if not name:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


PLAN_REVIEW_WORKFLOW = """\
id: plan-review
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

CONFIG = """\
models:
  gpt-terra:
    provider: codex
    model: gpt-5.6-terra
    thinking:
      allowed: [medium, high]
      default: medium
runtime:
  coordinator:
    model: gpt-terra
    thinking: high
roles:
  developer:
    default_model: gpt-terra
"""

STAGE_CONFIG = """\
models:
  grok:
    provider: grok
    model: grok-4.6
    thinking:
      allowed: [low, medium, high, xhigh]
      default: high
  gpt-terra:
    provider: codex
    model: gpt-5.6-terra
    thinking:
      allowed: [medium, high]
      default: medium
roles:
  developer:
    default_model: grok
  reviewer:
    default_model: gpt-terra
    thinking: medium
"""

STAGE_WORKFLOW = """\
id: plan-implement
stages:
  - id: implement
    role: developer
    depends_on: []
    produces:
      artifact: implementation-result.md
  - id: review-work
    role: reviewer
    model: gpt-terra
    thinking: high
    depends_on:
      - implement
"""


def write_plan_review_session() -> tuple[Path, str]:
    workspace = Path(tempfile.mkdtemp())
    workflows = workspace / ".agentflow" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "plan-review.yaml").write_text(PLAN_REVIEW_WORKFLOW, encoding="utf-8")
    return workspace, create_workflow_session(workspace, "plan-review", "Write a plan")


def write_config(contents: str) -> Path:
    directory = Path(tempfile.mkdtemp())
    path = directory / "config.yaml"
    path.write_text(contents, encoding="utf-8")
    return path


def write_stage_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp())
    agentflow = workspace / ".agentflow"
    workflows = agentflow / "workflows"
    workflows.mkdir(parents=True)
    (agentflow / "config.yaml").write_text(STAGE_CONFIG, encoding="utf-8")
    (workflows / "plan-implement.yaml").write_text(STAGE_WORKFLOW, encoding="utf-8")
    return workspace


def write_role_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp())
    agentflow = workspace / ".agentflow"
    agentflow.mkdir()
    (agentflow / "config.yaml").write_text(STAGE_CONFIG, encoding="utf-8")
    return workspace


FAKE_FLOW_CONFIG = """\
models:
  fake-model:
    provider: fake
    model: m1
    thinking:
      allowed: [low, medium, high]
      default: medium
roles:
  planner:
    default_model: fake-model
  reviewer:
    default_model: fake-model
  developer:
    default_model: fake-model
"""


def repo_workflow_text(workflow_id: str) -> str:
    return (
        PACKAGE_ROOT / "tests" / "fixtures" / "workflows" / f"{workflow_id}.yaml"
    ).read_text(encoding="utf-8")


def write_fake_workflow_workspace(documents: dict[str, str]) -> Path:
    workspace = Path(tempfile.mkdtemp())
    agentflow = workspace / ".agentflow"
    workflows = agentflow / "workflows"
    workflows.mkdir(parents=True)
    (agentflow / "config.yaml").write_text(FAKE_FLOW_CONFIG, encoding="utf-8")
    for workflow_id, document in documents.items():
        (workflows / f"{workflow_id}.yaml").write_text(document, encoding="utf-8")
    return workspace
