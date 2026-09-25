from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from agentflow_kernel.base_adapter import BaseCLIAdapter, CommandSpec


class FakeAdapter(BaseCLIAdapter):
    command = "fake"

    def validate_options(self, options: Optional[dict[str, str]] = None) -> None:
        del options

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
        options: Optional[dict[str, str]] = None,
    ) -> CommandSpec:
        self.calls.append(
            {
                "model": model,
                "workspace": workspace,
                "prompt": prompt,
                "agent_id": agent_id,
                "new_agent_id": new_agent_id,
                "prompt_file": prompt_file,
                "options": dict(options or {}),
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


FAKE_FLOW_CONFIG = """\
models:
  fake-model:
    provider: fake
    model: m1
    options:
      effort: medium
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
