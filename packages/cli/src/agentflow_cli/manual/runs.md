# runs

A run is one directory under `.agentflow/runs/<run-id>/`. Each dispatch, retry, or resumed turn writes a new execution directory. A finished attempt is not rewritten.

```text
runs/<run-id>/
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

There is no run-level `inputs/` directory and no run-level `work/` directory.

## Files

| File | Holds |
| :--- | :--- |
| `manifest.yaml` | Run identity and the context fixed at creation. |
| `status.json` | Latest operational status, including published outputs. |
| `events.jsonl` | Append-only timeline. `agentflow watch` follows this file. |
| `outputs/<name>` | Current published copy of one declared completion output. |
| `execution.json` | Lifecycle, outcome, stage metadata, `schema_version`, `resumes_execution_id`, and process-identity keys once the provider lock is held. |
| `session.json` | Session continuity and the prompt history visible at that execution. |
| `process.identity` | POSIX lock held by the provider session leader. Binds the process keys in `execution.json`. |
| `prompt.md` | Prompt sent to the provider. The only stored prompt. |
| `response.md` | Final response. |
| `artifacts/` | Files the specialist wrote for this attempt. Immutable after the attempt ends. |

`executions/<execution-id>/prompt.md` is the canonical prompt. `--prompt`, `--file`, and `--stdin` are ways of supplying that text.

`runner_session_id` is the session id owned by the runner. `provider_session_id` is the conversation id owned by the provider. They may be equal when a provider accepts a caller-assigned id.

## Attempts

A resume creates a new execution directory. `execution.json` records `resumes_execution_id` pointing at the previous attempt. An execution in `completed`, `failed`, or `cancelled` is immutable, including its prompt, response, session snapshot, and artifacts.

A failed execution leaves the run `active`. Causes include a provider error, a missing or invalid `status:` header, a missing or invalid `decision:` header on a decision stage, and a missing stage artifact. Retry the same `run_id`. The retry gets a new execution directory. An execution failure is not a reason to start a new run.

`agentflow watch RUN --until --execution <id>` waits on that attempt's `execution.json`, not on `status.json`. The watch command is specified in `agentflow man watch`.
`agentflow watch --once` prints the current snapshot and `events_cursor`. It does not replay `events.jsonl`. Continue from that snapshot with `--cursor <byte>`. Event timestamps include the date and a timezone offset.

## Process identity

After the provider identity lock is held, `execution.json` also stores `hostname`, `boot_id`, `owner_pid`, `owner_start_key`, `provider_pid`, `provider_pgid`, `provider_start_key`, `identity_path`, and `identity_token`.

Exit of the owning process is not a user cancellation. The next `execute`, `agent`, or `watch` of that run may mark the execution `failed` only after a verified reap, a conclusive provider `ESRCH`, or a boot-id mismatch. An unlocked or missing `process.identity` file is not evidence that the provider exited. When the recorded provider pid is still alive and the identity cannot be verified, the execution stays `running` and retry is refused.

`watch` reaps only those failed cases, and only for the run it is following. `agentflow runs` does not reap.

`provider.warning` records a control-plane problem, such as a model-catalog refresh timeout. It is not an execution failure. `provider.error` and `provider.turn_failed` are execution failures.

## Terminal runs

On the workflow completion decision, `status.json` and `manifest.yaml` become `completed`, and `events.jsonl` appends `run.completed`. Further stages on that `run_id` are refused.

On `blocked`, `status.json` records the blocking stage and execution, `manifest.yaml` becomes `blocked`, and `events.jsonl` appends `stage.blocked` then `run.blocked`. Further stages on that `run_id` are refused. Start a new run after the blocker is resolved.

`cancelled` is terminal for that `run_id` as well.

## Published outputs

On the completion decision, Agentflow copies each declared output from the upstream execution that was reviewed:

`executions/<source-execution-id>/artifacts/<artifact>` to `runs/<run-id>/outputs/<name>`.

The copy replaces the destination atomically. The source artifact stays in the attempt directory. `status.json` records each published output as `path`, `source_execution_id`, and `published_by_execution_id`.

Nothing is published for a `failed`, `blocked`, or `cancelled` execution, for a `revise` decision, for a missing or empty source artifact, or for an `agentflow agent` run. A missing or empty declared output fails the completion execution. The run stays `active`.

## Agent runs

`agentflow agent` uses this layout. `workflow_id` is `agent`. There is no `workflow_path`. The execution `stage_id` is the role key. `outputs/` is not published. A complete turn marks the run `completed`. A later `agentflow agent continue RUN` on that run is allowed and starts a new execution.

## Listing

`agentflow runs` prints run id, workflow, status, latest execution, latest decision, published outputs, and task.

Workflow outputs are declared in `agentflow man workflow`. The completion decision is specified in `agentflow man stage`.
