# workflows/

Each file in `.agentflow/workflows/` is one named workflow.

```yaml
id: plan-review
description: Write and approve a plan before implementation.
stages:
  - id: plan
    role: planner
    depends_on: []
    produces:
      artifact: plan.md
  - id: review-plan
    role: reviewer
    depends_on:
      - plan
    decision:
      values:
        - approved
        - revise
      routes:
        approved: complete
        revise: plan
completion:
  stage: review-plan
  decision: approved
  outputs:
    - name: plan
      from_stage: plan
      artifact: plan.md
```

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `id` | Yes | Unique workflow identifier. |
| `description` | No | Workflow purpose. |
| `requires` | No | Required prior workflow result. |
| `requires.workflow` | With `requires` | Required workflow ID. |
| `requires.decision` | With `requires` | Required declared decision value. Pass `--prior-run-id` on the first stage. |
| `stages` | Yes | Ordered executable stages. |
| `completion` | No | Completing stage, optional decision, and published outputs. |
| `completion.outputs` | No | Outputs copied into `runs/<run-id>/outputs/<name>` on the completion decision. |
| `completion.outputs[].name` | With an output | Published filename under `outputs/`. |
| `completion.outputs[].from_stage` | With an output | Stage whose reviewed attempt supplies the artifact. |
| `completion.outputs[].artifact` | With an output | Filename under that attempt's `artifacts/`. |
| `constraints` | No | Workflow constraints relayed to every stage. |

A file-producing stage declares `produces.artifact`.
A decision-only review stage omits `produces` and is not a published output.

Stage `instructions` and workflow `constraints` are relayed verbatim.
`./agentflow execute` injects both through its prompt template.

`./agentflow execute --stage-id` dispatches that stage only when it is the one the kernel would select.
Any other id is a configuration error.
Without `--stage-id`, `./agentflow execute` selects the next eligible stage and continues on the same dispatch kernel.
Stage fields, outcomes, resolution, sessions, and decisions: `./agentflow help stage`.
Published run files: `./agentflow help runs`.
Unattended A→Z: `./agentflow help execute`.
