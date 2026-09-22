from __future__ import annotations

from pathlib import Path

from agentflow_kernel.config import (
    SUPPORTED_COORDINATOR_PROVIDERS,
    ConfigurationError,
    CoordinatorConfig,
)


def build_coordinator_prompt(workspace: Path) -> str:
    skill = _read_coordinator_skill(workspace)
    config_path = workspace / ".agentflow/config.yaml"
    workflows_path = workspace / ".agentflow/workflows"
    return (
        f"{skill.rstrip()}\n\n"
        "## This session\n\n"
        f"Config: `{config_path}`\n"
        f"Workflows: `{workflows_path}`\n"
        "Do not create a session until the user supplies a task and selects a workflow.\n"
        "Start by asking what they want to accomplish and which workflow to run, "
        "if that is not already clear.\n"
    )


def _read_coordinator_skill(workspace: Path) -> str:
    candidates = [workspace / ".agents/skills/agentflow/SKILL.md"]
    for parent in Path(__file__).resolve().parents:
        candidates.append(parent / ".agents/skills/agentflow/SKILL.md")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return _strip_frontmatter(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(f"Unable to read coordinator skill: {path} ({exc})") from exc
    raise ConfigurationError("Coordinator skill not found: .agents/skills/agentflow/SKILL.md")


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return text
    return parts[2].lstrip()


def build_coordinator_command(workspace: Path, coordinator: CoordinatorConfig) -> list[str]:
    prompt = build_coordinator_prompt(workspace)
    if coordinator.provider == "codex":
        return [
            "codex",
            "-C",
            str(workspace),
            "-m",
            coordinator.model,
            "-c",
            f'model_reasoning_effort="{coordinator.thinking}"',
            "--approve-for-me",
            prompt,
        ]
    if coordinator.provider == "grok":
        return [
            "grok",
            "--cwd",
            str(workspace),
            "-m",
            coordinator.model,
            "--reasoning-effort",
            coordinator.thinking,
            "--always-approve",
            "--verbatim",
            prompt,
        ]
    supported = ", ".join(sorted(SUPPORTED_COORDINATOR_PROVIDERS))
    raise ConfigurationError(
        f"runtime.coordinator.model resolved to unsupported provider {coordinator.provider!r}. "
        f"Supported: {supported}."
    )
