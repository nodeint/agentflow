# config

`.agentflow/config.yaml` declares the models a project uses and the roles that call them.

A workflow stage and `agentflow agent` name a role. `agentflow coordinator` names the coordinator.
A role that is not declared here is a configuration error. Agentflow does not substitute another role, provider, or model.

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

## Fields

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `project` | No | Project name. |
| `version` | No | Schema version of this file. |
| `runtime.coordinator` | For `agentflow coordinator` | Interactive coordinator. Not a workflow role. |
| `runtime.coordinator.model` | When the coordinator is set | Key in `models`. |
| `runtime.coordinator.thinking` | No | Thinking level. When omitted, the model default is used. |
| `models` | Yes | Provider and native model for each key. |
| `models.<key>.provider` | Yes | Provider id, such as `grok` or `codex`. |
| `models.<key>.model` | Yes | Model id as that provider names it. |
| `models.<key>.thinking` | No | Thinking levels this model accepts. |
| `models.<key>.thinking.allowed` | When `thinking` is set | Permitted levels. |
| `models.<key>.thinking.default` | When `thinking` is set | Level used when the role and the stage set none. |
| `roles` | Yes | Specialists a stage or `agentflow agent` can name. |
| `roles.<key>.role` | No | Title of the role. |
| `roles.<key>.description` | No | Responsibility of the role. |
| `roles.<key>.default_model` | Yes | Key in `models`. |
| `roles.<key>.thinking` | No | Thinking level for this role. Overrides the model default. |

`agentflow agent --role` still requires `roles.<key>` when `--provider` and `--model` replace the resolved model.

Stage resolution is specified in `agentflow man stage`. The standalone cascade is specified in `agentflow man agent`.
