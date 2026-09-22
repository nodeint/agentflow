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

`thinking` is normalized across the supported providers:

- Codex receives it as `model_reasoning_effort`.
- Grok receives it as `--reasoning-effort`.

All other options remain provider-specific. Codex receives each entry as
`codex exec -c key=value`. Grok receives each entry as a long flag, with
underscores converted to hyphens.

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
