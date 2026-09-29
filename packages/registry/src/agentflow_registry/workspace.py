from __future__ import annotations

from pathlib import PurePosixPath
from pathlib import Path


class WorkspaceLocationError(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def locate_workspace(path: str) -> Path:
    """Return the workspace root for an absolute path.

    A relative path is rejected before any filesystem walk and before the
    path is joined to the process cwd.
    """
    if not isinstance(path, str) or path == "" or "\x00" in path:
        raise WorkspaceLocationError("relative", "Workspace path is not absolute.")
    if not PurePosixPath(path).is_absolute():
        raise WorkspaceLocationError("relative", "Workspace path is not absolute.")
    start = Path(path).resolve()
    if start.is_file():
        raise WorkspaceLocationError("file", "Workspace path is a file.")
    if not start.is_dir():
        raise WorkspaceLocationError(
            "not_directory", "Workspace path is not a directory."
        )
    for candidate in (start, *start.parents):
        if (candidate / ".agentflow" / "config.yaml").is_file():
            return candidate.resolve()
    raise WorkspaceLocationError(
        "not_workspace",
        "Could not find .agentflow/config.yaml in this directory or its parents.",
    )
