# Providers and model configuration

Models describe execution. Roles describe intent. Both are declared in
`.agentflow/config.yaml`:

```yaml
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
