from __future__ import annotations

import sys
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Optional

from agentflow_kernel.config import ConfigurationError

_invocation_cwd: ContextVar[Optional[Path]] = ContextVar("agentflow_cwd", default=None)


def set_invocation_cwd(cwd: Optional[Path]) -> Token[Optional[Path]]:
    return _invocation_cwd.set(cwd)


def reset_invocation_cwd(token: Token[Optional[Path]]) -> None:
    _invocation_cwd.reset(token)


def invocation_cwd() -> Path:
    return (_invocation_cwd.get() or Path.cwd()).resolve()


def find_workspace(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".agentflow/config.yaml").is_file():
            return candidate
    raise ConfigurationError(
        "Could not find .agentflow/config.yaml in this directory or its parents."
    )


def resolve_prompt(
    *,
    prompt: Optional[str],
    prompt_file: Optional[str],
    prompt_stdin: bool,
) -> str:
    if prompt_stdin:
        text = sys.stdin.read()
        if text == "":
            raise ValueError("Prompt from stdin is empty.")
        return text
    if prompt_file is None:
        if prompt is None:
            raise ValueError("Pass one of --prompt, --file, or --stdin.")
        return prompt
    try:
        return Path(prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"Unable to read prompt file: {prompt_file} ({exc})"
        ) from exc
