# config.yaml

Reusable provider/model pairs and role defaults.

```yaml
project: Example
version: "1.0"

runtime:
  coordinator:
    model: grok
    thinking: low

models:
  grok:
    provider: grok
    model: grok-4.6
    thinking:
      allowed: [low, medium, high, xhigh]
      default: high

roles:
  developer:
    role: Senior developer
    description: Implements approved work.
    default_model: grok
    thinking: high
```

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `project` | No | Human-readable repository name. |
| `version` | No | Configuration schema version. |
| `runtime.coordinator` | For `agentflow run` | Interactive coordinator; not a workflow role. |
| `runtime.coordinator.model` | With coordinator | Model key from `models`. |
| `runtime.coordinator.thinking` | No | Thinking override; otherwise the model default. |
| `models` | Yes | Stable keys to provider/model pairs. |
| `models.<key>.provider` | Yes | Provider identifier, such as `grok` or `codex`. |
| `models.<key>.model` | Yes | Provider-native model identifier. |
| `models.<key>.thinking` | No | Thinking capability for this model. |
| `models.<key>.thinking.allowed` | With `thinking` | Allowed thinking levels. |
| `models.<key>.thinking.default` | With `thinking` | Default when role and stage omit one. |
| `roles` | Yes | Reusable specialist roles. |
| `roles.<key>.role` | No | Human-readable role title. |
| `roles.<key>.description` | No | Specialist responsibility. |
| `roles.<key>.default_model` | Yes | Model key from `models`. |
| `roles.<key>.thinking` | No | Thinking override for this role. |

`./agentflow agent --role` resolves `roles.<key>` even when `--provider` and `--model` override the model cascade.
