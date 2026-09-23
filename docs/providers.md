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

`thinking` is normalized across the supported providers. Each adapter checks
the value before a turn starts:

- Codex accepts `minimal`, `low`, `medium`, `high`, and `xhigh`, and sends it
  as `model_reasoning_effort`. Any other option is a Codex config override
  passed as `codex exec -c key=value`.
- Grok accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`,
  and sends it as `--reasoning-effort`. Grok also accepts `max_turns`, `tools`,
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
