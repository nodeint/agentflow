from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from .yaml_subset import parse_yaml_document


@dataclass(frozen=True)
class StageSpec:
    id: str
    role: str
    artifact: Optional[str] = None
    decision_values: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    resume_from: Optional[str] = None
    instructions: tuple[str, ...] = ()
    model_key: Optional[str] = None
    thinking: Optional[str] = None
    routes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowOutput:
    name: str
    from_stage: str
    artifact: str


@dataclass(frozen=True)
class WorkflowCompletion:
    stage: str
    decision: Optional[str] = None
    outputs: tuple[WorkflowOutput, ...] = ()


@dataclass(frozen=True)
class WorkflowRequires:
    workflow: str
    decision: str


@dataclass(frozen=True)
class WorkflowDocument:
    id: str
    stages: dict[str, StageSpec]
    completion: Optional[WorkflowCompletion] = None
    requires: Optional[WorkflowRequires] = None
    constraints: tuple[str, ...] = ()


def load_workflow_document(path: Path) -> WorkflowDocument:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Unable to read workflow: {path} ({exc})") from exc
    data = parse_yaml_document(text)
    if not isinstance(data, dict):
        raise ValueError(f"Workflow must be a mapping: {path}")
    workflow_id = _required_string(data.get("id"), "id")
    stages = _parse_stages(data.get("stages"))
    completion_raw = data.get("completion")
    completion = (
        _parse_completion(completion_raw, stages)
        if isinstance(completion_raw, dict) and stages
        else None
    )
    requires = _parse_requires(data.get("requires"))
    constraints = _string_items(data.get("constraints"), "constraints")
    return WorkflowDocument(
        id=workflow_id,
        stages=stages,
        completion=completion,
        requires=requires,
        constraints=constraints,
    )


def load_stage(workspace: Path, workflow_path: str, stage_id: str) -> Optional[StageSpec]:
    path = workspace / workflow_path
    if not path.is_file():
        return None
    try:
        workflow = load_workflow_document(path)
    except ValueError:
        return None
    return workflow.stages.get(stage_id)


def stage_artifact_name(workspace: Path, workflow_path: str, stage_id: str) -> Optional[str]:
    stage = load_stage(workspace, workflow_path, stage_id)
    return None if stage is None else stage.artifact


def _parse_stages(raw: Any) -> dict[str, StageSpec]:
    if not isinstance(raw, list) or not raw:
        return {}
    stages: dict[str, StageSpec] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Workflow stage must be a mapping.")
        stage_id = _required_string(item.get("id"), "stages.id")
        if stage_id in stages:
            raise ValueError(f"Duplicate stage id: {stage_id}")
        role = _required_string(item.get("role"), f"stages.{stage_id}.role")
        decision_values = _stage_decision_values(item, stage_id)
        depends_on = _stage_depends_on(item, stage_id)
        stages[stage_id] = StageSpec(
            id=stage_id,
            role=role,
            artifact=_stage_artifact(item, stage_id),
            decision_values=decision_values,
            depends_on=depends_on,
            resume_from=_stage_resume_from(item, stage_id, depends_on),
            instructions=_string_items(
                item.get("instructions"), f"stages.{stage_id}.instructions"
            ),
            model_key=_optional_stage_string(item, "model", stage_id),
            thinking=_optional_stage_string(item, "thinking", stage_id),
            routes={},
        )
    for item in raw:
        if not isinstance(item, dict):
            continue
        stage_id = _required_string(item.get("id"), "stages.id")
        stage = stages[stage_id]
        routes = _stage_routes(item, stage_id, stage.decision_values, stages)
        stages[stage_id] = replace(stage, routes=routes)
    _validate_stage_graph(stages)
    return stages


def _parse_requires(raw: Any) -> Optional[WorkflowRequires]:
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise ValueError("requires must be a mapping.")
    return WorkflowRequires(
        workflow=_required_string(raw.get("workflow"), "requires.workflow"),
        decision=_required_string(raw.get("decision"), "requires.decision").lower(),
    )


def _string_items(raw: Any, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list.")
    parsed: list[str] = []
    for index, item in enumerate(raw):
        parsed.append(_required_string(item, f"{field}[{index}]"))
    return tuple(parsed)


def _stage_depends_on(stage: dict[str, Any], stage_id: str) -> tuple[str, ...]:
    raw = stage.get("depends_on")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"stages.{stage_id}.depends_on must be a list.")
    parsed: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = _required_string(item, f"stages.{stage_id}.depends_on")
        if value == stage_id:
            raise ValueError(f"stages.{stage_id}.depends_on cannot include itself.")
        if value in seen:
            raise ValueError(f"Duplicate depends_on on stages.{stage_id}: {value}")
        seen.add(value)
        parsed.append(value)
    return tuple(parsed)


def _stage_resume_from(
    stage: dict[str, Any], stage_id: str, depends_on: tuple[str, ...]
) -> Optional[str]:
    session = stage.get("session")
    if session is None or session == {}:
        return None
    if not isinstance(session, dict):
        raise ValueError(f"stages.{stage_id}.session must be a mapping.")
    resume = session.get("resume_from")
    if resume is None:
        return None
    value = _required_string(resume, f"stages.{stage_id}.session.resume_from")
    if value not in depends_on:
        raise ValueError(
            f"stages.{stage_id}.session.resume_from must be a dependency: {value}"
        )
    return value


def _stage_routes(
    stage: dict[str, Any],
    stage_id: str,
    values: tuple[str, ...],
    stages: dict[str, StageSpec],
) -> dict[str, str]:
    if not values:
        return {}
    decision = stage.get("decision")
    if not isinstance(decision, dict):
        return {}
    routes = decision.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ValueError(f"stages.{stage_id}.decision.routes must be a mapping.")
    parsed: dict[str, str] = {}
    for raw_key, raw_target in routes.items():
        key = _required_string(raw_key, f"stages.{stage_id}.decision.routes").lower()
        target = _required_string(
            raw_target, f"stages.{stage_id}.decision.routes.{key}"
        )
        if target != "complete" and target not in stages:
            raise ValueError(
                f"stages.{stage_id}.decision.routes.{key} is not a declared stage: {target}"
            )
        if key in parsed:
            raise ValueError(f"Duplicate route on stages.{stage_id}: {key}")
        parsed[key] = target
    if set(parsed) != set(values):
        raise ValueError(
            f"stages.{stage_id}.decision.routes keys must match decision.values."
        )
    return parsed


def _validate_stage_graph(stages: dict[str, StageSpec]) -> None:
    for stage in stages.values():
        for dep in stage.depends_on:
            if dep not in stages:
                raise ValueError(
                    f"stages.{stage.id}.depends_on is not a declared stage: {dep}"
                )


def _stage_decision_values(stage: dict[str, Any], stage_id: str) -> tuple[str, ...]:
    decision = stage.get("decision")
    if decision is None or decision == {}:
        return ()
    if not isinstance(decision, dict):
        raise ValueError(f"stages.{stage_id}.decision must be a mapping.")
    values = decision.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError(f"stages.{stage_id}.decision.values must be a non-empty list.")
    parsed: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = _required_string(item, f"stages.{stage_id}.decision.values").lower()
        if value in seen:
            raise ValueError(f"Duplicate decision value on stages.{stage_id}: {value}")
        seen.add(value)
        parsed.append(value)
    return tuple(parsed)


def _stage_artifact(stage: dict[str, Any], stage_id: str) -> Optional[str]:
    produces = stage.get("produces")
    if produces is None or produces == {}:
        return None
    if not isinstance(produces, dict):
        raise ValueError(f"stages.{stage_id}.produces must be a mapping.")
    artifact = produces.get("artifact")
    if artifact is None:
        return None
    return _filename(artifact, f"stages.{stage_id}.produces.artifact")


def _parse_completion(raw: dict[str, Any], stages: dict[str, StageSpec]) -> WorkflowCompletion:
    stage_id = _required_string(raw.get("stage"), "completion.stage")
    if stage_id not in stages:
        raise ValueError(f"completion.stage is not a declared stage: {stage_id}")
    decision_raw = raw.get("decision")
    decision = (
        _required_string(decision_raw, "completion.decision")
        if decision_raw is not None
        else None
    )
    outputs_raw = raw.get("outputs") or []
    if outputs_raw == {}:
        outputs_raw = []
    if not isinstance(outputs_raw, list):
        raise ValueError("completion.outputs must be a list.")
    outputs: list[WorkflowOutput] = []
    seen: set[str] = set()
    for item in outputs_raw:
        if not isinstance(item, dict):
            raise ValueError("completion.outputs item must be a mapping.")
        name = _filename(item.get("name"), "completion.outputs.name")
        from_stage = _required_string(item.get("from_stage"), "completion.outputs.from_stage")
        artifact = _filename(item.get("artifact"), "completion.outputs.artifact")
        if name in seen:
            raise ValueError(f"Duplicate completion output name: {name}")
        if from_stage not in stages:
            raise ValueError(
                f"completion.outputs.from_stage is not a declared stage: {from_stage}"
            )
        source = stages[from_stage]
        if source.artifact is None:
            raise ValueError(
                f"completion output {name!r} references stage {from_stage} "
                "which does not produce an artifact."
            )
        if source.artifact != artifact:
            raise ValueError(
                f"completion output {name!r} artifact {artifact!r} does not match "
                f"stages.{from_stage}.produces.artifact {source.artifact!r}."
            )
        seen.add(name)
        outputs.append(WorkflowOutput(name=name, from_stage=from_stage, artifact=artifact))
    return WorkflowCompletion(stage=stage_id, decision=decision, outputs=tuple(outputs))


def _optional_stage_string(stage: dict[str, Any], key: str, stage_id: str) -> Optional[str]:
    if key not in stage:
        return None
    return _required_string(stage.get(key), f"stages.{stage_id}.{key}")


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid {field}.")
    return value.strip()


def _filename(value: Any, field: str) -> str:
    text = _required_string(value, field)
    if "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError(f"Invalid {field}: {text!r}")
    return text
