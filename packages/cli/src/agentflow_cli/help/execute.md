# execute

`./agentflow execute` runs a named workflow.
Without `--stage-id`, the kernel loops from the first eligible stage.
It stops at `completed`, `blocked`, `cancelled`, or the retry cap.
With `--stage-id`, it dispatches that stage once and stops.
The named stage must be the stage the kernel would select next.
The flag does not choose a different stage.
It builds the stage prompt and calls the in-process dispatch.
It does not take a caller prompt, provider, model, thinking level, or session.

```bash
./agentflow execute --workflow-id plan-review --task "..."
./agentflow execute --workflow-id plan-implement --task "..." --prior-run-id <id>
./agentflow execute --run-id <id>
./agentflow execute --workflow-id plan-review --task "..." --stage-id plan
./agentflow execute --run-id <id> --stage-id <stage>
```

## Flags

| Flag | Meaning |
| :--- | :--- |
| `--workflow-id` | Required when creating a run. Forbidden with `--run-id` unless it matches the stored workflow. |
| `--task` | Required when creating a run. Ignored when continuing an existing run. |
| `--prior-run-id` | Required on create when the workflow declares `requires`. On continue, must match the stored `prior_run_id` if passed. Checked before stage eligibility. |
| `--run-id` | Continue an existing named-workflow run. |
| `--stage-id` | Dispatch this eligible stage once, then stop. |
| `--max-attempts` | Loop only. Cap of total `failed` executions for that `stage_id` on the run. Default 3. Not consulted when `--stage-id` is set. |

There is no `--prompt` or `--runner-session-id`.
Provider, model, and thinking come from workflow and config resolution.
`session.resume_from` is filled by the kernel.

## One stage

`--stage-id` must match the stage `next_stage` would select.
On create, that check uses an empty run and happens after the prior-run check.
A mismatch exits 2 and does not create a run.
On an active run, a mismatch exits 2 and does not dispatch.
The message is `Stage <requested> is not the eligible stage <selected>.`
A completed stage runs again only when a later route selects it.

`--max-attempts` does not apply to this dispatch.
A one-shot call with `--max-attempts 0` still dispatches once.
A failed execution is still recorded.
A later `execute --run-id` without `--stage-id` exits 3 when the cap is already spent.

Stdout is the stage JSON, then the final object.

| Result | `stop_reason` | Exit |
| :--- | :--- | :--- |
| Dispatch finished, run still `active` | `stage` | 0 |
| That dispatch completed the run | `completed` | 0 |
| That dispatch blocked the run | `blocked` | 1 |
| Cancelled during the dispatch | `cancelled` | 130 |
| Bad flags, illegal or non-eligible stage, running execution | none; stderr `agentflow:` | 2 |

Exit 0 with `stop_reason: stage` means the dispatch finished, not that `outcome_status` is `complete`.
Read `outcome_status` and `outcome_decision` from the stage JSON.
A missing header leaves the run `active`. Retry with the same `--run-id` and the same `--stage-id`.

## Prompt

Each stage prompt is a template with the run task, stage `instructions`, workflow `constraints`,
upstream artifact paths, and prior-run `outputs/` paths.
Empty sections are omitted.
The rendered prompt is stored at `executions/<execution-id>/prompt.md`.

## Continue and recovery

`execute --run-id` does not redispatch a terminal run.
`--stage-id` does not change that.

- `completed`: print a snapshot and exit 0.
- `blocked` or `cancelled`: print a snapshot and exit 1 or 130.
- After `blocked` or `cancelled`, start a new run.
- `active`, without `--stage-id`: retry while `--max-attempts` still has room.
- `active` after exit 3: `execute --run-id --stage-id <id>` of the failed stage dispatches once and ignores the cap.

`--max-attempts` counts persisted `failed` executions for that stage id on the loop.
A later `execute --run-id` does not reset the count.
A value below 1 is a configuration error on the loop.
With `--stage-id`, that check is skipped.

## Stdout and exit codes

Stdout is one JSON object per finished stage, then a final object with `run_id`,
`run_status`, `outputs`, and `stop_reason`.
Stderr is `[agentflow]` and progress lines.

| Code | When |
| :--- | :--- |
| 0 | `run_status` is `completed`, or one stage finished and the run is still `active` |
| 1 | `blocked` |
| 2 | configuration error |
| 3 | retry budget exhausted on the loop; the run stays `active` |
| 130 | cancelled |

An ambiguous next stage, a missing required prior run,
a non-eligible `--stage-id`, or an illegal YAML graph is a configuration error.

Stage fields and routing: `./agentflow help stage`.
Workflow `instructions` and `constraints`: `./agentflow help workflow`.
Run records: `./agentflow help runs`.
