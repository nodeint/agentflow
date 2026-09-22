from __future__ import annotations

import os
from pathlib import Path


def response_text(prompt: str) -> str:
    if os.environ.get("AGENTFLOW_FAKE_OUTCOME") == "invalid":
        return "not a status header"
    if "decision: approved|revise" in prompt:
        return "status: complete\ndecision: approved\n\nok"
    return "status: complete\n\nready"


def write_artifact(prompt: str) -> None:
    marker = "Agentflow artifact directory:\n"
    if marker not in prompt:
        return
    lines = prompt.split(marker, 1)[1].splitlines()
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
    (directory / name).write_text("# ready\n", encoding="utf-8")
