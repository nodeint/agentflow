# runs/

Local execution state under `.agentflow/runs/`. Each retry or loop gets a new execution directory.

```text
runs/<run_id>/
├── manifest.yaml
├── status.json
├── events.jsonl
├── outputs/
│   └── <name>
└── executions/
    └── <order>-<stage-id>--attempt-<number>/
        ├── execution.json
        ├── session.json
        ├── process.identity
        ├── prompt.md
        ├── response.md
        └── artifacts/
```

| File | Purpose |
| :--- | :--- |
| `manifest.yaml` | Run identity and immutable context. |
| `status.json` | Latest operational status, including published outputs. |
| `events.jsonl` | Append-only timeline. `./agentflow watch` follows this file. |
| `outputs/<name>` | Current published copy of a declared completion output. |
| `execution.json` | Lifecycle, outcome, stage metadata, `schema_version`, `resumes_execution_id`, and optional process identity keys. |
| `session.json` | Session continuity and prompt history at that execution. |
| `process.identity` | POSIX lock file held by the provider session leader; binds `execution.json` process keys. |
| `prompt.md` | Prompt sent to the provider. This is the only prompt snapshot. |
| `response.md` | Final response. |
| `artifacts/` | Files written by the specialist for this attempt. Immutable after the attempt ends. |

Do not add run-level `inputs/` or `work/` directories.
`executions/<execution-id>/prompt.md` is the canonical stored prompt.
`--prompt`, `--prompt-file`, and `--prompt-stdin` are transports into that file.

`runner_session_id` is the runner-owned session ID.
`provider_session_id` is the provider-native conversation ID.
They may be equal when a provider accepts a caller-assigned ID.

A resume always creates a new execution directory.
`execution.json.resumes_execution_id` links to the previous attempt.
A terminal execution (`completed`, `failed`, `cancelled`) is immutable.
Earlier prompt, response, session snapshot, and artifacts stay immutable.

A failed execution (`provider` error, missing or invalid `status:` header,
a missing or invalid `decision:` header on a decision stage,
or a missing stage artifact)
keeps the run `active`. Retry the same `run_id`; a new execution directory is
created. Do not start a new run only because an execution failed.
Do not rewrite a finished attempt in place.
`watch --until-terminal --execution-id <id>` waits on that attempt's `execution.json`, not on `status.json`.
`watch --once` prints the current snapshot and `events_cursor`; it does not replay `events.jsonl`.
Follow with `--cursor <byte>` to continue from that snapshot.
Event timestamps include the date and timezone offset.

After the provider identity lock is held, `execution.json` also stores
`hostname`, `boot_id`, `owner_pid`, `owner_start_key`, `provider_pid`,
`provider_pgid`, `provider_start_key`, `identity_path`, and `identity_token`.

Owner death is not user cancel.
The next `execute`, `agent`, or `watch` of that run may mark the execution `failed` only after a verified reap,
a conclusive provider `ESRCH`, or a boot-id mismatch.
An unlocked or missing `process.identity` lock is not provider exit.
If the recorded provider pid is still alive and identity cannot be verified,
the execution stays `running` and retry is refused.
`watch` may reap only those `failed` cases on the run it is following.
`runs` does not reap.

On the workflow `completion` decision, `status.json` becomes `completed`,
`manifest.yaml` becomes `completed`, and `events.jsonl` appends `run.completed`.
Further stages on that `run_id` are refused.

On `blocked`, `status.json` records the blocking stage and execution,
`manifest.yaml` becomes `blocked`, and `events.jsonl` appends `stage.blocked` then `run.blocked`.
Further stages on that `run_id` are refused; start a new run after the blocker is resolved.
`cancelled` is also terminal for that `run_id`.

`provider.warning` is a control-plane issue such as a model-catalog refresh timeout.
It is not an execution failure. `provider.error` and `provider.turn_failed` are execution failures.

## Published outputs

On the workflow `completion` decision, Agentflow copies each declared output
from the exact upstream execution that was reviewed and approved:

`executions/<source-execution-id>/artifacts/<artifact>`
→ `runs/<run-id>/outputs/<name>`

The copy is an atomic replace. The source attempt artifact is retained.
`status.json` records each published output as `path`, `source_execution_id`,
and `published_by_execution_id`.

These never publish an output:

- `failed`, `blocked`, or `cancelled` executions
- a `revise` decision
- a missing or empty source artifact
- a standalone `./agentflow agent` run

A missing or empty declared output fails the completion execution.
The run stays `active`.

Standalone `./agentflow agent` runs use this layout.
`workflow_id` is the reserved value `agent`.
There is no `workflow_path`.
Execution `stage_id` is the role key.
They do not publish `outputs/` by default.
A complete agent turn marks the run `completed`.
A later `./agentflow agent --run-id` on that run is allowed and starts a new execution.

`./agentflow runs` prints `run_id`, workflow, status, latest execution, latest
decision, published outputs, and task.
