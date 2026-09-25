from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def take_step() -> dict:
    script = os.environ.get("AGENTFLOW_FAKE_SCRIPT")
    if not script:
        return {}
    steps = json.loads(Path(script).read_text(encoding="utf-8"))
    if not isinstance(steps, list):
        return {"text": "script must be a list", "exit": 9}
    cursor = Path(os.environ["AGENTFLOW_FAKE_CURSOR"])
    cursor.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(cursor, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("ascii") or "0"
        index = int(raw.strip() or "0")
        if index < len(steps) and isinstance(steps[index], dict):
            step = steps[index]
        else:
            step = {"text": "script exhausted", "exit": 9}
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(index + 1).encode("ascii"))
        return step
    finally:
        os.close(fd)


def record_invocation(provider: str) -> None:
    log = os.environ.get("AGENTFLOW_FAKE_LOG")
    if not log:
        return
    path = Path(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"provider": provider, "argv": sys.argv}, ensure_ascii=False)
            + "\n"
        )


def response_text(prompt: str, step: dict | None = None) -> str:
    step = step or {}
    if "text" in step:
        return str(step["text"])
    if os.environ.get("AGENTFLOW_FAKE_OUTCOME") == "invalid":
        return "not a status header"
    if "decision: approved|revise" in prompt:
        return "status: complete\ndecision: approved\n\nok"
    return "status: complete\n\nready"


def write_artifact(prompt: str, step: dict | None = None) -> None:
    step = step or {}
    if step.get("skip_artifact") is True:
        return
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
    content = step.get("artifact")
    if not isinstance(content, str):
        content = "# ready\n"
    (directory / name).write_text(content, encoding="utf-8")
