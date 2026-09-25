# Providers and model configuration

Models describe execution. Roles describe intent. Both are declared in
`.agentflow/config.yaml`:

```yaml
schema_version: 1
models:
  implementation:
    provider: codex
    model: gpt-5
    options:
      model_reasoning_effort: medium

roles:
  developer:
    default_model: implementation
```

Use model identifiers and option values supported by the installed provider
CLI.

## Options

Each adapter names its own options and lists the values for the selected
model. `agentflow init` and `agentflow config` ask for options marked as
prompts. Leaving one unset keeps the provider default.

- Codex prompts for `model_reasoning_effort`, read from
  `supported_reasoning_levels` in `codex debug models`. Any other option is a
  Codex config override passed as `codex exec -c key=value`.
- Grok prompts for `reasoning-effort`, read from the `grok` CLI for that
  model, and sends it as `--reasoning-effort`. Grok also accepts `max_turns`,
  `tools`, `disallowed_tools`, `permission_mode`, `rules`, `allow`, `deny`,
  and `sandbox`. Other keys are rejected.

A role or stage `options` map overrides the same keys on the model. Only
options the adapter marks as overridable are part of the provider session.

## Schema version

`schema_version` on `.agentflow/config.yaml` and on each workflow file is the
document version. A missing value is version 0. A command that uses those
files stops while the recorded version is older and tells you to run
`agentflow migrate`. That command asks before writing. `agentflow migrate --yes`
writes without asking.

The kernel walks one version at a time. A file at version 1 that is moving to
version 3 runs the version 1 function, then the version 2 function. Each
adapter defines those functions itself, separately for a model entry, a role,
and a workflow stage. Grok version 0 moves `thinking` to `reasoning-effort`.
Codex version 0 moves `thinking` to `model_reasoning_effort`. A project's own
`version` field is left as it is.

## Selection and overrides

A stage normally uses its role's default model. It may select another named
model or override the reasoning level:

```yaml
stages:
  - id: security-review
    role: reviewer
    model: security-model
    options:
      model_reasoning_effort: high
```

Model options provide the base configuration. A role `options` map overrides
those keys, and a stage `options` map overrides them for that turn.

Agentflow currently includes adapters for the headless `codex` and `grok` CLIs.
Those CLIs own authentication, tool use, and agent execution; Agentflow owns
selection, prompts, routing, and persistence.
