# Providers and model configuration

Models describe execution. Roles describe intent. Both are declared in
`.agentflow/config.yaml`:

```yaml
models:
  implementation:
    provider: codex
    model: gpt-5
    options:
      thinking: medium

roles:
  developer:
    default_model: implementation
```

Use model identifiers and option values supported by the installed provider
CLI.

## Options

`thinking` is normalized across the supported providers. The allowed values
are the ones that provider lists for the selected model. `agentflow init` and
`agentflow config` read that list after the model is chosen.

- Codex reads `supported_reasoning_levels` from `codex debug models` and sends
  the choice as `model_reasoning_effort`. Any other option is a Codex config
  override passed as `codex exec -c key=value`.
- Grok asks the `grok` CLI which efforts that model accepts and sends the
  choice as `--reasoning-effort`. Grok also accepts `max_turns`, `tools`,
  `disallowed_tools`, `permission_mode`, `rules`, `allow`, `deny`, and
  `sandbox`. Other keys are rejected.

## Selection and overrides

A stage normally uses its role's default model. It may select another named
model or override the reasoning level:

```yaml
stages:
  - id: security-review
    role: reviewer
    model: security-model
    thinking: high
```

Model options provide the base configuration. A role-level `thinking` value
overrides `options.thinking`, and a stage-level value overrides it for that
turn.

Agentflow currently includes adapters for the headless `codex` and `grok` CLIs.
Those CLIs own authentication, tool use, and agent execution; Agentflow owns
selection, prompts, routing, and persistence.
