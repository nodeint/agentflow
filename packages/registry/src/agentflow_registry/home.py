from __future__ import annotations

import os
from pathlib import Path


def agentflow_home_path() -> Path:
    override = os.environ.get("AGENTFLOW_HOME")
    if override:
        return Path(override)
    return Path.home() / ".agentflow"


def registry_home_path() -> Path:
    return agentflow_home_path() / "registry"


def ensure_registry_home(path: Path | None = None) -> Path:
    directory = registry_home_path() if path is None else Path(path)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory
