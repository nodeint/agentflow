# Standalone agent

`./agentflow agent` runs one config role through the existing runner.
It does not read `.agentflow/workflows/` and does not write a workflow file.
`workflow_id` is the reserved value `agent`.
A file named `workflows/agent.yaml`, if present, is unrelated.

## Flags

Required:

- `--role`: a `roles.<key>` entry from `.agentflow/config.yaml`.
- Exactly one of `--prompt`, `--prompt-file`, or `--prompt-stdin`.

Optional:

- `--task`: label stored on a new run; default is the role key; ignored when attaching to an existing run.
- `--run-id`: attach to an existing agent run.
- `--provider` and `--model`: native override pair; both or neither.
- `--thinking`: thinking override.
- `--runner-session-id` / `--session-id`: continue a runner session; `--session-id` is a legacy alias.

`--workflow-id` is not an `agent` flag.
`--stage-id` is the one-shot flag on `execute`.
`--stage-attempt` and `--execution-order` are not public CLI flags.

## Resolution

`--role` is validated against `roles.<role>` even when `--provider` and `--model` override the cascade.
Missing roles are configuration errors. Do not substitute another role.

Without the override pair, the model key is `roles.<role>.default_model`.
Thinking is `--thinking` if set; else `roles.<role>.thinking` if set; else `models.<key>.thinking.default`.
The thinking level must be in `models.<key>.thinking.allowed`.

Pass both `--provider` and `--model` to skip that cascade and use the native pair.
`--thinking` is then passed through unvalidated.

## Run identity

Execution `stage_id` is the role key.
`--run-id` and `--runner-session-id` must identify the same existing `workflow_id: agent` run.

Stdout JSON keys match an execute stage object: `run_id`, `runner_session_id`, `provider_session_id`,
`thinking`, `execution_id`, `artifact_directory`, `outcome_status`, `outcome_decision`,
`run_status`, `response`.

Every response starts with the same status header as a workflow stage.
Standalone agent runs do not publish `runs/<run-id>/outputs/` by default.
A `status: complete` turn marks the run `completed`.
Continue with `--run-id` on that completed run to start a new execution.
