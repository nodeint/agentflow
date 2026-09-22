# workflow

A workflow is one file, `.agentflow/workflows/<workflow-id>.yaml`.
`agentflow execute start WORKFLOW` loads that file. The `id` field names the workflow.

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

## Fields

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `id` | Yes | Workflow id. |
| `description` | No | What the workflow produces. |
| `requires` | No | Prior workflow result this run depends on. |
| `requires.workflow` | When `requires` is set | Workflow id of that prior run. |
| `requires.decision` | When `requires` is set | Decision value the prior run must have declared. Pass `--prior` when creating the run. |
| `stages` | Yes | The stages of the workflow. |
| `completion` | No | Stage that can finish the run, the decision it must return, and the outputs to publish. |
| `completion.outputs` | No | Files copied to `runs/<run-id>/outputs/<name>` when that decision is reached. |
| `completion.outputs[].name` | When an output is declared | Filename under `outputs/`. |
| `completion.outputs[].from_stage` | When an output is declared | Stage whose reviewed attempt holds the artifact. |
| `completion.outputs[].artifact` | When an output is declared | Filename under that attempt's `artifacts/`. |
| `constraints` | No | Constraints copied into every stage prompt. |

## Stage output

A stage that writes a file sets `produces.artifact` to the filename.
A review stage that only chooses a route omits `produces`. That stage is not a published output.

Stage `instructions` and workflow `constraints` are copied into the stage prompt unchanged.
The prompt is specified in `agentflow man execute`.

## How a run moves

`agentflow execute start` and `execute continue` select the next eligible stage and continue until the run stops.
`agentflow execute stage` dispatches that next stage once.
Any other id is a configuration error.

Stage fields, outcomes, sessions, and routes: `agentflow man stage`.
The execute command: `agentflow man execute`.
Run files: `agentflow man runs`.
