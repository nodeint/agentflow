from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Mapping, Optional, Sequence

import typer
from InquirerPy import inquirer

from agentflow_adapters import default_adapters
from agentflow_kernel.base_adapter import BaseCLIAdapter, ModelCatalog
from agentflow_kernel.config import ConfigurationError, load_model_target, load_yaml_mapping, resolve_stage_spec
from agentflow_kernel.config_edit import (
    UNSET,
    ConfigChange,
    ConfigEditError,
    ConfigView,
    ModelView,
    RoleView,
    assign_roles,
    describe_change,
    inspect_config,
    matching_model_name,
    patch_config_text,
    place_role_model,
    set_role_options,
    upsert_model,
)
from agentflow_kernel.doctor import diagnose
from agentflow_kernel.workflow import WorkflowDocument, load_workflow_catalog

from ..display import print_error
from ..workspace import find_workspace, invocation_cwd
from .doctor import run_doctor
from .init import (
    _accepted_options,
    _detected,
    _known_model,
    _load_catalog,
    _load_option_values,
    _parse_options,
    _require_adapter,
    _run_command,
)

NEW = "__new__"

config_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Update models and roles in .agentflow/config.yaml.",
)


class ConfigPrompter:
    def select(
        self,
        message: str,
        choices: Sequence[tuple[str, str]],
        default: Optional[str] = None,
    ) -> str:
        raise NotImplementedError

    def confirm(self, message: str) -> bool:
        raise NotImplementedError

    def text(self, message: str, default: str = "") -> str:
        raise NotImplementedError


class InquirerConfigPrompter(ConfigPrompter):
    def select(
        self,
        message: str,
        choices: Sequence[tuple[str, str]],
        default: Optional[str] = None,
    ) -> str:
        if not choices:
            raise ValueError("Choose a value.")
        values = {value for _label, value in choices}
        selected = default if default in values else choices[0][1]
        result = inquirer.select(
            message=message,
            choices=[{"name": label, "value": value} for label, value in choices],
            default=selected,
        ).execute()
        if result not in values:
            raise ValueError("Choose a value.")
        return str(result)

    def confirm(self, message: str) -> bool:
        result = inquirer.confirm(message=message, default=True).execute()
        if not isinstance(result, bool):
            raise ValueError("Answer yes or no.")
        return result

    def text(self, message: str, default: str = "") -> str:
        result = inquirer.text(message=message, default=default).execute()
        if not isinstance(result, str):
            raise ValueError("Enter a value.")
        return result


@config_app.command("show")
def show_command() -> None:
    """Print .agentflow/config.yaml."""
    raise typer.Exit(run_config_show(find_workspace(invocation_cwd())))


@config_app.command("model")
def model_command(
    key: Annotated[
        Optional[str],
        typer.Argument(metavar="NAME", help="Name to show or update.", show_default=False),
    ] = None,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="Provider for this name. Pass with --model."),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option("--model", help="Model id for this name. Pass with --provider."),
    ] = None,
    option: Annotated[
        Optional[list[str]],
        typer.Option("--option", help="KEY=VALUE stored under this model's options."),
    ] = None,
    clear_option: Annotated[
        Optional[list[str]],
        typer.Option("--clear-option", help="Remove one key from this model's options."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON object."),
    ] = False,
) -> None:
    """List models, or create and update one model name.

    With no name in a terminal, choose a model or add one. Pass a name with
    --provider and --model to write without prompts. A new name is created.
    An existing name is updated, including every role and stage that uses it.
    Exit 0 when the list is shown or the write passes doctor.
    Exit 1 when the file is written and doctor fails.
    Exit 2 when the arguments are rejected.
    """
    mutating = provider is not None or model is not None or option is not None or clear_option is not None
    interactive = sys.stdin.isatty() and not as_json and not mutating
    raise typer.Exit(
        run_config_model(
            find_workspace(invocation_cwd()),
            key=key,
            provider=provider,
            model=model,
            options=tuple(option or ()),
            clear_options=tuple(clear_option or ()),
            as_json=as_json,
            interactive=interactive,
        )
    )


@config_app.command("role")
def role_command(
    roles: Annotated[
        Optional[list[str]],
        typer.Argument(help="Roles to show or update.", show_default=False),
    ] = None,
    model_name: Annotated[
        Optional[str],
        typer.Option(
            "--model-name",
            metavar="NAME",
            help="Point the roles at this model.",
        ),
    ] = None,
    provider: Annotated[
        Optional[str],
        typer.Option(
            "--provider",
            help="Provider for this role only. Pass with --model.",
        ),
    ] = None,
    model: Annotated[
        Optional[str],
        typer.Option(
            "--model",
            help="Model id for this role only. Pass with --provider.",
        ),
    ] = None,
    option: Annotated[
        Optional[list[str]],
        typer.Option("--option", help="KEY=VALUE stored under this role's options. Pass one role."),
    ] = None,
    clear_option: Annotated[
        Optional[list[str]],
        typer.Option("--clear-option", help="Remove one key from this role's options."),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON object."),
    ] = False,
) -> None:
    """List roles, or point roles at a model.

    With no role in a terminal, choose a role or add one. --model-name changes
    only those roles' default_model. --provider and --model add or reuse a
    model entry and point one role at it, leaving a shared entry unchanged.
    --option writes roles.<role>.options.
    Exit 0 when the list is shown or the write passes doctor.
    Exit 1 when the file is written and doctor fails.
    Exit 2 when the arguments are rejected.
    """
    mutating = any(
        value is not None for value in (model_name, provider, model, option, clear_option)
    )
    interactive = sys.stdin.isatty() and not as_json and not mutating
    raise typer.Exit(
        run_config_role(
            find_workspace(invocation_cwd()),
            roles=tuple(roles or ()),
            model_name=model_name,
            provider=provider,
            model=model,
            options=tuple(option or ()),
            clear_options=tuple(clear_option or ()),
            as_json=as_json,
            interactive=interactive,
        )
    )


def run_config_show(workspace: Path) -> int:
    text = (workspace / ".agentflow" / "config.yaml").read_text(encoding="utf-8")
    sys.stdout.write(text)
    return 0


def run_config_model(
    workspace: Path,
    *,
    key: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    options: tuple[str, ...] = (),
    clear_options: tuple[str, ...] = (),
    as_json: bool = False,
    interactive: bool = False,
    adapters: Optional[Mapping[str, BaseCLIAdapter]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    prompter: Optional[ConfigPrompter] = None,
    run_command: Optional[Callable[[list[str]], str]] = None,
) -> int:
    try:
        return _run_model(
            workspace,
            key=key,
            provider=provider,
            model=model,
            options=options,
            clear_options=clear_options,
            as_json=as_json,
            interactive=interactive,
            adapters=default_adapters() if adapters is None else adapters,
            which=shutil.which if which is None else which,
            prompter=InquirerConfigPrompter() if prompter is None else prompter,
            run_command=_run_command if run_command is None else run_command,
        )
    except (ConfigurationError, ConfigEditError, ValueError) as exc:
        print_error(str(exc))
        return 2


def run_config_role(
    workspace: Path,
    *,
    roles: tuple[str, ...] = (),
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    options: tuple[str, ...] = (),
    clear_options: tuple[str, ...] = (),
    as_json: bool = False,
    interactive: bool = False,
    adapters: Optional[Mapping[str, BaseCLIAdapter]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    prompter: Optional[ConfigPrompter] = None,
    run_command: Optional[Callable[[list[str]], str]] = None,
) -> int:
    try:
        return _run_role(
            workspace,
            roles=roles,
            model_name=model_name,
            provider=provider,
            model=model,
            options=options,
            clear_options=clear_options,
            as_json=as_json,
            interactive=interactive,
            adapters=default_adapters() if adapters is None else adapters,
            which=shutil.which if which is None else which,
            prompter=InquirerConfigPrompter() if prompter is None else prompter,
            run_command=_run_command if run_command is None else run_command,
        )
    except (ConfigurationError, ConfigEditError, ValueError) as exc:
        print_error(str(exc))
        return 2


def _run_model(
    workspace: Path,
    *,
    key: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    options: tuple[str, ...],
    clear_options: tuple[str, ...],
    as_json: bool,
    interactive: bool,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
) -> int:
    option_updates = _option_updates(options, clear_options)
    if (provider is None) != (model is None):
        raise ConfigEditError("Pass both --provider and --model, or omit both.")
    config, workflows, view = _loaded(workspace)
    mutating = provider is not None or option_updates is not UNSET
    if key is None and not mutating:
        if interactive:
            return _interactive_models(
                workspace, config, workflows, view, adapters, which, prompter, run_command, as_json
            )
        _emit_view(view, as_json=as_json, models=True, roles=False)
        return 0
    if key is None:
        raise ConfigEditError("Pass a name.")
    if not mutating:
        if interactive:
            return _edit_model(
                workspace, config, workflows, view, key, adapters, which, prompter, run_command, as_json
            )
        _emit_models(_selected_models(view, (key,)), as_json=as_json)
        return 0
    catalog = None
    if provider is not None and model is not None:
        provider, model, catalog = _catalog_model(adapters, which, run_command, provider, model)
    if option_updates is not UNSET:
        current_provider = provider or _model_provider(config, key)
        current_model = model or load_model_target(config, key).model
        option_updates = _checked_options(
            adapters,
            which,
            run_command,
            current_provider,
            current_model,
            option_updates,
            catalog if provider is not None else None,
        )
    change = upsert_model(
        config,
        key,
        provider=provider,
        model=model,
        options=option_updates,
        workflows=workflows,
    )
    _validate_change(config, change.config, workflows, adapters)
    return _commit(workspace, config, change, as_json=as_json, which=which)


def _run_role(
    workspace: Path,
    *,
    roles: tuple[str, ...],
    model_name: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    options: tuple[str, ...],
    clear_options: tuple[str, ...],
    as_json: bool,
    interactive: bool,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
) -> int:
    option_updates = _option_updates(options, clear_options)
    if model_name is not None and (provider is not None or model is not None):
        raise ConfigEditError("Pass --model-name or --provider and --model.")
    if (provider is None) != (model is None):
        raise ConfigEditError("Pass both --provider and --model, or omit both.")
    config, workflows, view = _loaded(workspace)
    mutating = model_name is not None or provider is not None or option_updates is not UNSET
    if not roles and not mutating:
        if interactive:
            return _interactive_roles(
                workspace, config, workflows, view, adapters, which, prompter, run_command, as_json
            )
        _emit_view(view, as_json=as_json, models=False, roles=True)
        return 0
    if not roles:
        raise ConfigEditError("Pass a role.")
    if len(roles) > 1 and (provider is not None or option_updates is not UNSET):
        raise ConfigEditError("Pass one role to set options or a provider model.")
    if not mutating:
        if interactive and len(roles) == 1:
            _known_role(view, roles[0])
            return _point_role(
                workspace,
                config,
                workflows,
                view,
                roles[0],
                adapters,
                which,
                prompter,
                run_command,
                as_json,
            )
        _emit_roles(_selected_roles(view, roles), as_json=as_json)
        return 0
    if provider is not None and model is not None:
        provider, model, catalog = _catalog_model(adapters, which, run_command, provider, model)
        if option_updates is not UNSET:
            option_updates = _checked_options(
                adapters, which, run_command, provider, model, option_updates, catalog
            )
        change = place_role_model(
            config,
            roles[0],
            provider=provider,
            model=model,
            options=option_updates,
            workflows=workflows,
        )
    elif model_name is not None:
        change = assign_roles(config, roles, model_name, workflows=workflows)
        if option_updates is not UNSET:
            target = load_model_target(change.config, model_name)
            option_updates = _checked_options(
                adapters,
                which,
                run_command,
                target.provider,
                target.model,
                option_updates,
            )
            follow = set_role_options(
                change.config, roles[0], option_updates, workflows=workflows
            )
            change = describe_change(config, follow.config, workflows)
    else:
        target = _role_model(config, roles[0])
        option_updates = _checked_options(
            adapters,
            which,
            run_command,
            target.provider,
            target.model,
            option_updates,
        )
        change = set_role_options(config, roles[0], option_updates, workflows=workflows)
    _validate_change(config, change.config, workflows, adapters)
    return _commit(workspace, config, change, as_json=as_json, which=which)


def _interactive_models(
    workspace: Path,
    config: dict,
    workflows: tuple[WorkflowDocument, ...],
    view: ConfigView,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
    as_json: bool,
) -> int:
    choices = [(_model_label(model), model.key) for model in view.models]
    choices.append(("New model", NEW))
    selected = prompter.select("Model", choices, view.models[0].key if view.models else NEW)
    if selected == NEW:
        return _add_model(
            workspace, config, workflows, None, adapters, which, prompter, run_command, as_json
        )
    return _edit_model(
        workspace, config, workflows, view, selected, adapters, which, prompter, run_command, as_json
    )


def _edit_model(
    workspace: Path,
    config: dict,
    workflows: tuple[WorkflowDocument, ...],
    view: ConfigView,
    key: str,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
    as_json: bool,
) -> int:
    current = _selected_models(view, (key,))[0]
    provider = _select_provider(prompter, adapters, which, "Provider", current.provider)
    adapter = _require_adapter(adapters, provider)
    catalog = _load_catalog(adapter, which, run_command)
    model_id = prompter.select(
        "Model",
        [(item, item) for item in catalog.ids],
        current.model if current.model in catalog.ids else catalog.default_id,
    )
    model_id = _known_model(model_id, catalog)
    stored = _prompted_options(
        prompter, adapter, model_id, catalog, which, run_command, current.options
    )
    change = upsert_model(
        config,
        key,
        provider=provider,
        model=model_id,
        options=stored,
        workflows=workflows,
    )
    _validate_change(config, change.config, workflows, adapters)
    return _confirm_and_commit(
        workspace, config, change, prompter, as_json=as_json, which=which
    )


def _add_model(
    workspace: Path,
    config: dict,
    workflows: tuple[WorkflowDocument, ...],
    role: Optional[str],
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
    as_json: bool,
) -> int:
    provider = _select_provider(prompter, adapters, which, "Provider", None)
    adapter = _require_adapter(adapters, provider)
    catalog = _load_catalog(adapter, which, run_command)
    model_id = prompter.select(
        "Model",
        [(item, item) for item in catalog.ids],
        catalog.default_id,
    )
    model_id = _known_model(model_id, catalog)
    options = _prompted_options(prompter, adapter, model_id, catalog, which, run_command, {})
    existing = matching_model_name(config, provider=provider, model=model_id, options=options)
    if existing is not None:
        if role is None:
            print(f"Model {existing} already has this provider and model.")
            return 0
        change = assign_roles(config, (role,), existing, workflows=workflows)
        change = describe_change(config, change.config, workflows, reused_model=existing)
    else:
        key = prompter.text("Name").strip()
        if not key:
            raise ConfigEditError("Name is required.")
        if key in config["models"]:
            raise ConfigEditError(f"Name {key} already exists.")
        created = upsert_model(
            config,
            key,
            provider=provider,
            model=model_id,
            options=options,
            workflows=workflows,
        )
        if role is None:
            change = created
        else:
            assigned = assign_roles(created.config, (role,), key, workflows=workflows)
            change = describe_change(config, assigned.config, workflows)
    _validate_change(config, change.config, workflows, adapters)
    return _confirm_and_commit(
        workspace, config, change, prompter, as_json=as_json, which=which
    )


def _interactive_roles(
    workspace: Path,
    config: dict,
    workflows: tuple[WorkflowDocument, ...],
    view: ConfigView,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
    as_json: bool,
) -> int:
    choices = [(_role_label(role), role.role) for role in view.roles]
    choices.append(("New role", NEW))
    selected = prompter.select("Role", choices, view.roles[0].role if view.roles else NEW)
    if selected == NEW:
        name = prompter.text("Role name").strip()
        if not name:
            raise ConfigEditError("Role name is required.")
        if any(role.role == name for role in view.roles):
            raise ConfigEditError(f"Role {name} already exists.")
        return _point_role(
            workspace, config, workflows, view, name, adapters, which, prompter, run_command, as_json
        )
    return _point_role(
        workspace, config, workflows, view, selected, adapters, which, prompter, run_command, as_json
    )


def _point_role(
    workspace: Path,
    config: dict,
    workflows: tuple[WorkflowDocument, ...],
    view: ConfigView,
    role: str,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    prompter: ConfigPrompter,
    run_command: Callable[[list[str]], str],
    as_json: bool,
) -> int:
    current = next((item for item in view.roles if item.role == role), None)
    choices = [(_model_label(model), model.key) for model in view.models]
    choices.append(("New model", NEW))
    default = current.model_name if current is not None else NEW
    selected = prompter.select("Model", choices, default)
    if selected == NEW:
        return _add_model(
            workspace, config, workflows, role, adapters, which, prompter, run_command, as_json
        )
    change = assign_roles(config, (role,), selected, workflows=workflows)
    _validate_change(config, change.config, workflows, adapters)
    return _confirm_and_commit(
        workspace, config, change, prompter, as_json=as_json, which=which
    )


def _confirm_and_commit(
    workspace: Path,
    config: dict,
    change: ConfigChange,
    prompter: ConfigPrompter,
    *,
    as_json: bool,
    which: Callable[[str], Optional[str]],
) -> int:
    _print_change(change)
    if not prompter.confirm("Save this change?"):
        print("Left unchanged.")
        return 0
    return _commit(workspace, config, change, as_json=as_json, which=which, echo=False)


def _commit(
    workspace: Path,
    before: Mapping[str, Any],
    change: ConfigChange,
    *,
    as_json: bool,
    which: Callable[[str], Optional[str]],
    echo: bool = True,
) -> int:
    del before
    path = workspace / ".agentflow" / "config.yaml"
    original = path.read_text(encoding="utf-8")
    updated = patch_config_text(original, change.config)
    if updated == original:
        if as_json:
            print(json.dumps(_change_payload(change, wrote=False, doctor=None), ensure_ascii=False))
        else:
            print("Left unchanged.")
        return 0
    path.write_text(updated, encoding="utf-8")
    if as_json:
        checks = diagnose(workspace, adapters=default_adapters(), which=which)
        doctor = _doctor_payload(checks)
        print(json.dumps(_change_payload(change, wrote=True, doctor=doctor), ensure_ascii=False))
        return 0 if doctor["ok"] else 1
    if echo:
        _print_change(change)
    return run_doctor(workspace, which=which)


def _validate_change(
    before: Mapping[str, Any],
    after: dict[str, Any],
    workflows: Sequence[WorkflowDocument],
    adapters: Mapping[str, BaseCLIAdapter],
) -> None:
    before_models = before.get("models") if isinstance(before.get("models"), dict) else {}
    after_models = after.get("models") if isinstance(after.get("models"), dict) else {}
    changed_keys = {
        key
        for key in after_models
        if key not in before_models
        or load_model_target(before, key) != load_model_target(after, key)
    }
    for key in changed_keys:
        target = load_model_target(after, key)
        _require_adapter(adapters, target.provider).validate_options(target.options)
    before_roles = before.get("roles") if isinstance(before.get("roles"), dict) else {}
    after_roles = after.get("roles") if isinstance(after.get("roles"), dict) else {}
    for name, body in after_roles.items():
        uses_changed = isinstance(body, dict) and body.get("default_model") in changed_keys
        if before_roles.get(name) == body and not uses_changed:
            continue
        if not isinstance(body, dict):
            continue
        model_name = body.get("default_model")
        if not isinstance(model_name, str):
            continue
        target = load_model_target(after, model_name)
        options = dict(target.options)
        role_options = body.get("options")
        if isinstance(role_options, dict):
            options.update(
                {
                    str(name): value.strip()
                    for name, value in role_options.items()
                    if isinstance(value, str) and value.strip()
                }
            )
        _require_adapter(adapters, target.provider).validate_options(options)
    for document in workflows:
        for stage in document.stages.values():
            try:
                before_target = resolve_stage_spec(before, stage)
                after_target = resolve_stage_spec(after, stage)
            except ConfigurationError:
                continue
            if before_target == after_target:
                continue
            _require_adapter(adapters, after_target.provider).validate_options(after_target.options)


def _catalog_model(
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    run_command: Callable[[list[str]], str],
    provider: str,
    model: str,
) -> tuple[str, str, ModelCatalog]:
    adapter = _require_adapter(adapters, provider)
    catalog = _load_catalog(adapter, which, run_command)
    return provider, _known_model(model, catalog), catalog


def _checked_options(
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    run_command: Callable[[list[str]], str],
    provider: str,
    model: str,
    updates: Mapping[str, Optional[str]],
    catalog: Optional[ModelCatalog] = None,
) -> dict[str, Optional[str]]:
    adapter = _require_adapter(adapters, provider)
    if catalog is None:
        catalog = _load_catalog(adapter, which, run_command)
    present = {key: value for key, value in updates.items() if value is not None}
    if present:
        _accepted_options(adapter, model, catalog, which, run_command, present)
    return dict(updates)


def _prompted_options(
    prompter: ConfigPrompter,
    adapter: BaseCLIAdapter,
    model: str,
    catalog: ModelCatalog,
    which: Callable[[str], Optional[str]],
    run_command: Callable[[list[str]], str],
    current: Mapping[str, str],
) -> dict[str, Optional[str]]:
    stored: dict[str, Optional[str]] = {}
    for spec in adapter.provider_options():
        if not spec.prompt:
            continue
        values = _load_option_values(adapter, spec.name, model, catalog, which, run_command)
        if not values:
            continue
        choices = [(value, value) for value in values]
        if spec.allow_default:
            choices = [("provider default", ""), *choices]
        current_value = current.get(spec.name, "")
        picked = prompter.select(
            spec.name,
            choices,
            current_value if current_value in values else "",
        )
        stored[spec.name] = picked or None
    return stored


def _option_updates(
    options: tuple[str, ...], clear_options: tuple[str, ...]
) -> object:
    if not options and not clear_options:
        return UNSET
    parsed = _parse_options(options)
    overlap = set(parsed) & set(clear_options)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ConfigEditError(f"Pass either --option or --clear-option for {names}.")
    updates: dict[str, Optional[str]] = dict(parsed)
    for name in clear_options:
        if not name.strip():
            raise ConfigEditError("Option name is required.")
        updates[name.strip()] = None
    return updates


def _select_provider(
    prompter: ConfigPrompter,
    adapters: Mapping[str, BaseCLIAdapter],
    which: Callable[[str], Optional[str]],
    message: str,
    default: Optional[str],
) -> str:
    rows = _detected(adapters, which)
    if not rows:
        raise ValueError("No providers are available.")
    names = {name for name, _path, _adapter in rows}
    return prompter.select(
        message,
        [(f"{name}  {path or 'not found'}", name) for name, path, _adapter in rows],
        default if default in names else rows[0][0],
    )


def _loaded(workspace: Path) -> tuple[dict, tuple[WorkflowDocument, ...], ConfigView]:
    config = load_yaml_mapping(workspace / ".agentflow" / "config.yaml")
    workflows = _documents(workspace)
    return config, workflows, inspect_config(config, workflows)


def _documents(workspace: Path) -> tuple[WorkflowDocument, ...]:
    return tuple(load_workflow_catalog(workspace).documents.values())


def _model_provider(config: Mapping[str, Any], key: str) -> str:
    return load_model_target(config, key).provider


def _role_model(config: Mapping[str, Any], role: str):
    roles = config.get("roles")
    if not isinstance(roles, dict) or not isinstance(roles.get(role), dict):
        raise ConfigEditError(f"Unknown role: {role}.")
    model_name = roles[role].get("default_model")
    if not isinstance(model_name, str):
        raise ConfigurationError(f"Missing or invalid roles.{role}.default_model.")
    return load_model_target(config, model_name)


def _selected_models(view: ConfigView, keys: Sequence[str]) -> tuple[ModelView, ...]:
    by_key = {model.key: model for model in view.models}
    selected = []
    for key in keys:
        if key not in by_key:
            raise ConfigEditError(f"Unknown name: {key}.")
        selected.append(by_key[key])
    return tuple(selected)


def _selected_roles(view: ConfigView, names: Sequence[str]) -> tuple[RoleView, ...]:
    by_name = {role.role: role for role in view.roles}
    selected = []
    for name in names:
        selected.append(_known_role(view, name, by_name))
    return tuple(selected)


def _known_role(
    view: ConfigView,
    name: str,
    by_name: Optional[Mapping[str, RoleView]] = None,
) -> RoleView:
    found = (by_name or {role.role: role for role in view.roles}).get(name)
    if found is None:
        raise ConfigEditError(f"Unknown role: {name}.")
    return found


def _emit_view(view: ConfigView, *, as_json: bool, models: bool, roles: bool) -> None:
    if as_json:
        payload: dict[str, object] = {}
        if models:
            payload["models"] = [_model_json(model) for model in view.models]
        if roles:
            payload["roles"] = [_role_json(role) for role in view.roles]
        print(json.dumps(payload, ensure_ascii=False))
        return
    if models:
        _emit_models(view.models, as_json=False)
    if roles:
        _emit_roles(view.roles, as_json=False)


def _emit_models(models: Sequence[ModelView], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"models": [_model_json(model) for model in models]}, ensure_ascii=False))
        return
    print("Models")
    if not models:
        print("  none")
        return
    for model in models:
        print(f"  {_model_label(model)}")


def _emit_roles(roles: Sequence[RoleView], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"roles": [_role_json(role) for role in roles]}, ensure_ascii=False))
        return
    print("Roles")
    if not roles:
        print("  none")
        return
    for role in roles:
        print(f"  {_role_label(role)}")


def _model_label(model: ModelView) -> str:
    return f"{model.key} - {model.model}"


def _role_label(role: RoleView) -> str:
    return f"{role.role} - {role.model}"


def _model_json(model: ModelView) -> dict[str, object]:
    return {
        "key": model.key,
        "provider": model.provider,
        "model": model.model,
        "options": dict(model.options),
        "roles": list(model.roles),
        "stages": [
            {"workflow": stage.workflow, "stage": stage.stage} for stage in model.stages
        ],
    }


def _role_json(role: RoleView) -> dict[str, object]:
    return {
        "role": role.role,
        "model_name": role.model_name,
        "provider": role.provider,
        "model": role.model,
        "options": dict(role.options),
        "shared_with": list(role.shared_with),
        "stages": [
            {
                "workflow": stage.workflow,
                "stage": stage.stage,
                "model_name": stage.model_name,
                "options": dict(stage.options),
            }
            for stage in role.stages
        ],
    }


def _print_change(change: ConfigChange) -> None:
    if change.created_model:
        print(f"Created model {change.created_model}.")
    if change.reused_model:
        print(f"Reused model {change.reused_model}.")
    if change.updated_model:
        print(f"Updated model {change.updated_model}.")
    if change.roles:
        print("Roles: " + ", ".join(change.roles))
    if change.stages_updated:
        print("Stages: " + ", ".join(f"{workflow}.{stage}" for workflow, stage in change.stages_updated))
    for stage in change.stages_unchanged:
        print(
            f"Unchanged: {stage.workflow}.{stage.stage} ({', '.join(stage.ignores)})"
        )
    if change.orphaned_models:
        print("Orphaned models: " + ", ".join(change.orphaned_models))


def _change_payload(
    change: ConfigChange, *, wrote: bool, doctor: Optional[dict[str, object]]
) -> dict[str, object]:
    return {
        "wrote": wrote,
        "created_model": change.created_model,
        "reused_model": change.reused_model,
        "updated_model": change.updated_model,
        "roles": list(change.roles),
        "stages_updated": [
            {"workflow": workflow, "stage": stage}
            for workflow, stage in change.stages_updated
        ],
        "stages_unchanged": [
            {
                "workflow": stage.workflow,
                "stage": stage.stage,
                "ignores": list(stage.ignores),
            }
            for stage in change.stages_unchanged
        ],
        "orphaned_models": list(change.orphaned_models),
        "doctor": doctor,
    }


def _doctor_payload(checks: Sequence[Any]) -> dict[str, Any]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "items": list(check.items),
                "problems": list(check.problems),
            }
            for check in checks
        ],
    }
