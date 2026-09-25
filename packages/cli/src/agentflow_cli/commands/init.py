from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Mapping, Optional

import typer
from InquirerPy import inquirer

from agentflow_adapters import default_adapters
from agentflow_kernel.base_adapter import BaseCLIAdapter, ModelCatalog
from agentflow_kernel.doctor import diagnose

from ..display import (
    print_already_initialized,
    print_created,
    print_error,
    print_init_conflicts,
)
from ..workspace import invocation_cwd
from .doctor import run_doctor

PRESETS = ("implement", "plan")

init_app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    subcommand_metavar="",
    rich_markup_mode="rich",
)


@init_app.callback(invoke_without_command=True)
def init_command(
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="Provider for the default model. Pass with --model."),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", help="Model id for the default model. Pass with --provider."),
    ] = None,
    thinking: Annotated[
        Optional[str],
        typer.Option(
            "--thinking",
            help="options.thinking for the default model. Omit it to keep the provider default.",
        ),
    ] = None,
    preset: Annotated[
        str,
        typer.Option(
            "--preset",
            help="Workflow to create: implement or plan. Default: implement.",
        ),
    ] = "implement",
) -> None:
    """Create a minimal config, one workflow, and a sessions gitignore.

    In a terminal, choose the provider and model from the lists reported by
    the provider CLIs. Thinking values are the ones that provider lists for
    the model you picked. Pass --provider and --model together when input is
    not a terminal.
    --preset implement writes one developer stage. plan writes a
    planner and a reviewer. A separate reviewer model is asked only in the
    interactive plan flow.
    Exit 0 when the project is created or already initialized.
    Exit 1 when existing files conflict or doctor finds a problem.
    Exit 2 when the flags or answers are rejected.
    """
    interactive = provider is None and sys.stdin.isatty()
    raise typer.Exit(
        run_init(
            invocation_cwd(),
            provider=provider,
            model=model,
            thinking=thinking,
            preset=preset,
            interactive=interactive,
        )
    )


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str
    thinking: Optional[str] = None


def run_init(
    workspace: Path,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    thinking: Optional[str] = None,
    preset: str = "implement",
    interactive: bool = False,
    adapters: Optional[Mapping[str, BaseCLIAdapter]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    prompter: Optional[Prompter] = None,
    run_command: Optional[Callable[[list[str]], str]] = None,
) -> int:
    chosen = default_adapters() if adapters is None else adapters
    resolve = shutil.which if which is None else which
    prompt = InquirerPrompter() if prompter is None else prompter
    execute = _run_command if run_command is None else run_command
    if preset not in PRESETS:
        print_error("preset must be implement or plan.")
        return 2
    if (provider is None) != (model is None):
        print_error("Pass both --provider and --model, or omit both.")
        return 2
    if thinking is not None and provider is None:
        print_error("Pass --provider and --model with --thinking.")
        return 2
    if provider is None and not interactive:
        print_error("Pass --provider and --model when init is not interactive.")
        return 2

    state, conflicts = _classify(workspace, chosen, resolve)
    if state == "ready":
        print_already_initialized()
        return 0
    if state == "conflict":
        print_init_conflicts(conflicts)
        return 1

    try:
        primary, reviewer = _choices(
            chosen,
            resolve,
            prompt,
            provider=provider,
            model=model,
            thinking=thinking,
            preset=preset,
            interactive=interactive,
            run_command=execute,
        )
    except ValueError as exc:
        print_error(str(exc))
        return 2

    paths = _write_project(workspace, preset, primary, reviewer)
    print_created(paths, f'agentflow start {preset} --task "..."')
    return run_doctor(workspace, which=resolve)


class Prompter:
    def select_provider(
        self,
        rows: tuple[tuple[str, Optional[str], BaseCLIAdapter], ...],
        message: str,
    ) -> str:
        raise NotImplementedError

    def select_model(self, provider: str, catalog: ModelCatalog, message: str) -> str:
        raise NotImplementedError

    def select_thinking(self, values: tuple[str, ...], message: str) -> str:
        raise NotImplementedError

    def confirm(self, message: str) -> bool:
        raise NotImplementedError


class InquirerPrompter(Prompter):
    def select_provider(
        self,
        rows: tuple[tuple[str, Optional[str], BaseCLIAdapter], ...],
        message: str,
    ) -> str:
        if not rows:
            raise ValueError("No providers are available.")
        result = inquirer.select(
            message=message,
            choices=[
                {"name": f"{name}  {path or 'not found'}", "value": name}
                for name, path, _adapter in rows
            ],
            default=rows[0][0],
        ).execute()
        if not isinstance(result, str) or result not in {name for name, _path, _adapter in rows}:
            raise ValueError("Provider is required.")
        return result

    def select_model(self, provider: str, catalog: ModelCatalog, message: str) -> str:
        del provider
        if not catalog.ids:
            raise ValueError("Model is required.")
        default = catalog.default_id if catalog.default_id in catalog.ids else catalog.ids[0]
        result = inquirer.select(
            message=message,
            choices=list(catalog.ids),
            default=default,
        ).execute()
        if result not in catalog.ids:
            raise ValueError(f"Unknown model: {result}.")
        return str(result)

    def select_thinking(self, values: tuple[str, ...], message: str) -> str:
        if not values:
            return ""
        result = inquirer.select(
            message=message,
            choices=[
                {"name": "provider default", "value": ""},
                *({"name": value, "value": value} for value in values),
            ],
            default="",
        ).execute()
        if result not in {"", *values}:
            raise ValueError(f"Unknown thinking value: {result}.")
        return "" if result is None else str(result)

    def confirm(self, message: str) -> bool:
        result = inquirer.confirm(message=message, default=True).execute()
        if not isinstance(result, bool):
            raise ValueError("Answer yes or no.")
        return result


def _choices(
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompt: Prompter,
    *,
    provider: Optional[str],
    model: Optional[str],
    thinking: Optional[str],
    preset: str,
    interactive: bool,
    run_command: Callable[[list[str]], str],
) -> tuple[ModelChoice, Optional[ModelChoice]]:
    if provider is None:
        rows = _detected(adapters, which)
        provider = prompt.select_provider(rows, "Default provider")
        adapter = adapters[provider]
        catalog = _load_catalog(adapter, which, run_command)
        model = prompt.select_model(provider, catalog, "Model")
        thinking_values = _load_thinking(adapter, model, catalog, which, run_command)
        thinking_value = prompt.select_thinking(thinking_values, "Thinking")
    else:
        adapter = _require_adapter(adapters, provider)
        assert model is not None
        catalog = _load_catalog(adapter, which, run_command)
        model = _known_model(model, catalog)
        if thinking is not None and thinking.strip():
            thinking_values = _load_thinking(adapter, model, catalog, which, run_command)
            thinking_value = _accepted_thinking(thinking, thinking_values, model)
        else:
            thinking_value = None
    if not model or not model.strip():
        raise ValueError("Model is required.")
    primary = ModelChoice(
        provider=provider,
        model=model.strip(),
        thinking=_accepted_thinking(
            thinking_value,
            thinking_values if thinking_value else (),
            model,
        ),
    )
    if preset != "plan" or not interactive:
        return primary, None
    if prompt.confirm("Use the default model for both planner and reviewer?"):
        return primary, None
    rows = _detected(adapters, which)
    reviewer_provider = prompt.select_provider(rows, "Reviewer provider")
    reviewer_adapter = adapters[reviewer_provider]
    reviewer_catalog = _load_catalog(reviewer_adapter, which, run_command)
    reviewer_model = prompt.select_model(
        reviewer_provider, reviewer_catalog, "Reviewer model"
    )
    reviewer_values = _load_thinking(
        reviewer_adapter, reviewer_model, reviewer_catalog, which, run_command
    )
    reviewer_thinking = prompt.select_thinking(reviewer_values, "Reviewer thinking")
    return primary, ModelChoice(
        provider=reviewer_provider,
        model=reviewer_model.strip(),
        thinking=_accepted_thinking(reviewer_thinking, reviewer_values, reviewer_model),
    )


def _classify(
    workspace: Path,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
) -> tuple[str, tuple[str, ...]]:
    root = workspace / ".agentflow"
    config = root / "config.yaml"
    workflows = root / "workflows"
    gitignore = root / ".gitignore"
    owned: list[str] = []
    if config.exists():
        owned.append(".agentflow/config.yaml")
    if workflows.exists():
        owned.append(".agentflow/workflows")
        if workflows.is_dir():
            for path in sorted(workflows.glob("*.yaml")):
                if path.is_file():
                    owned.append(f".agentflow/workflows/{path.name}")
    if gitignore.exists():
        owned.append(".agentflow/.gitignore")
    if not owned:
        return "create", ()
    yaml_files = (
        [path for path in workflows.glob("*.yaml") if path.is_file()]
        if workflows.is_dir()
        else []
    )
    if config.is_file() and yaml_files:
        checks = diagnose(workspace, adapters=adapters, which=which)
        by_name = {check.name: check for check in checks}
        if by_name["config"].ok and by_name["workflows"].ok:
            return "ready", ()
    return "conflict", tuple(owned)


def _write_project(
    workspace: Path,
    preset: str,
    primary: ModelChoice,
    reviewer: Optional[ModelChoice],
) -> tuple[str, ...]:
    root = workspace / ".agentflow"
    workflows = root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        _render_config(preset, primary, reviewer), encoding="utf-8"
    )
    workflow_name = f"{preset}.yaml"
    (workflows / workflow_name).write_text(_render_workflow(preset), encoding="utf-8")
    (root / ".gitignore").write_text("sessions/\n", encoding="utf-8")
    return (
        ".agentflow/config.yaml",
        f".agentflow/workflows/{workflow_name}",
        ".agentflow/.gitignore",
    )


def _render_config(
    preset: str, primary: ModelChoice, reviewer: Optional[ModelChoice]
) -> str:
    models = [("default", primary)]
    if preset == "implement":
        roles = (("developer", "default"),)
    elif reviewer is None:
        roles = (("planner", "default"), ("reviewer", "default"))
    else:
        models.append(("review", reviewer))
        roles = (("planner", "default"), ("reviewer", "review"))
    lines = ["models:"]
    for name, choice in models:
        lines.extend(
            [
                f"  {name}:",
                f"    provider: {_yaml_scalar(choice.provider)}",
                f"    model: {_yaml_scalar(choice.model)}",
            ]
        )
        if choice.thinking is not None:
            lines.extend(
                [
                    "    options:",
                    f"      thinking: {_yaml_scalar(choice.thinking)}",
                ]
            )
    lines.append("roles:")
    for role, model_name in roles:
        lines.extend([f"  {role}:", f"    default_model: {model_name}"])
    return "\n".join(lines) + "\n"


def _render_workflow(preset: str) -> str:
    if preset == "implement":
        return (
            "id: implement\n"
            "stages:\n"
            "  - id: implement\n"
            "    role: developer\n"
            "    depends_on: []\n"
            "    instructions:\n"
            "      - Implement the requested change.\n"
            "      - Verify the result with relevant checks or tests.\n"
            "completion:\n"
            "  stage: implement\n"
        )
    return (
        "id: plan\n"
        "stages:\n"
        "  - id: plan\n"
        "    role: planner\n"
        "    depends_on: []\n"
        "    max_revisions: 3\n"
        "    instructions:\n"
        "      - Analyze the task and write an implementation plan.\n"
        "    produces:\n"
        "      artifact: plan.md\n"
        "  - id: review-plan\n"
        "    role: reviewer\n"
        "    depends_on:\n"
        "      - plan\n"
        "    instructions:\n"
        "      - Review the plan for correctness, missing risks, and unnecessary scope.\n"
        "    decision:\n"
        "      values:\n"
        "        - approved\n"
        "        - revise\n"
        "      routes:\n"
        "        approved: complete\n"
        "        revise: plan\n"
        "completion:\n"
        "  stage: review-plan\n"
        "  decision: approved\n"
        "  outputs:\n"
        "    - name: plan\n"
        "      from_stage: plan\n"
        "      artifact: plan.md\n"
    )


def _load_catalog(
    adapter: BaseCLIAdapter,
    which: Callable[[str], Optional[str]],
    run_command: Callable[[list[str]], str],
) -> ModelCatalog:
    if which(adapter.command) is None:
        raise ValueError(
            f"{adapter.command} is not on PATH, so its models could not be listed."
        )
    try:
        stdout = run_command(adapter.model_catalog_command())
        catalog = adapter.parse_model_catalog(stdout)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"Could not list {adapter.command} models: {exc}") from exc
    if not catalog.ids:
        raise ValueError(f"{adapter.command} returned no models.")
    return catalog


def _known_model(model: str, catalog: ModelCatalog) -> str:
    value = model.strip()
    if value not in catalog.ids:
        raise ValueError(f"Unknown model: {value}.")
    return value


def _load_thinking(
    adapter: BaseCLIAdapter,
    model: str,
    catalog: ModelCatalog,
    which: Callable[[str], Optional[str]],
    run_command: Callable[[list[str]], str],
) -> tuple[str, ...]:
    found = catalog.thinking_for(model)
    if found is not None:
        return found
    if which(adapter.command) is None:
        raise ValueError(
            f"{adapter.command} is not on PATH, so its thinking values could not be listed."
        )
    try:
        text = run_command(adapter.thinking_command(model))
    except ValueError as exc:
        text = str(exc)
    except OSError as exc:
        raise ValueError(
            f"Could not list {adapter.command} thinking values for {model}: {exc}"
        ) from exc
    return adapter.parse_thinking_values(text, model=model)


def _run_command(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ValueError(detail or f"{argv[0]} exited {completed.returncode}.")
    return completed.stdout


def _detected(
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
) -> tuple[tuple[str, Optional[str], BaseCLIAdapter], ...]:
    rows = tuple(
        (name, which(adapter.command), adapter) for name, adapter in adapters.items()
    )
    return tuple(sorted(rows, key=lambda row: (row[1] is None, row[0])))


def _require_adapter(
    adapters: Mapping[str, BaseCLIAdapter], provider: str
) -> BaseCLIAdapter:
    adapter = adapters.get(provider)
    if adapter is None:
        supported = ", ".join(sorted(adapters))
        raise ValueError(f"Unknown provider: {provider}. Supported: {supported}.")
    return adapter


def _accepted_thinking(
    thinking: Optional[str], values: tuple[str, ...], model: str
) -> Optional[str]:
    if thinking is None or not thinking.strip():
        return None
    value = thinking.strip()
    if value not in values:
        if not values:
            raise ValueError(f"{model} does not accept thinking.")
        raise ValueError(f"thinking must be one of: {', '.join(values)}.")
    return value


_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def _yaml_scalar(value: str) -> str:
    if _PLAIN_SCALAR.fullmatch(value) and value not in {"true", "false", "null"}:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
