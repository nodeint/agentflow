# Stage contract

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `id` | Yes | Unique within the workflow. |
| `role` | Yes | Role key from `config.yaml`. |
| `model` | No | Model key overriding the role default. |
| `thinking` | No | Thinking override for this stage. |
| `depends_on` | Yes | Stage IDs that must complete first. |
| `instructions` | No | Relayed to the specialist verbatim. |
| `produces.artifact` | For file-producing stages | Filename written under that execution's `artifacts/`. |
| `session.resume_from` | No | Earlier stage session to continue. |
| `decision` | No | Flow result values and routes. |

## Outcomes

Every stage response starts with a header, then a blank line, then the body.

```text
status: complete
```

```text
status: complete
decision: approved
```

A stage that declares `decision.values` must include `decision: <value>` on complete.
The value must be one of the declared values. Prose such as `Approved.` is not a decision.

```text
status: blocked
blocker: the approved tenant hydration contract is missing
```

`status` is runtime control, not a workflow `decision`.
Only `complete` allows dependency or `decision` routing.
Route from the parsed `decision` header, also returned as `outcome_decision` in the stage JSON.
`blocked` or `cancelled` stops the run.
The completion decision marks the run `completed` and publishes `completion.outputs`.
A missing or invalid header, a missing or invalid `decision:` on a decision stage,
or a provider error, fails that execution only;
retry the same `run_id` with `./agentflow execute --run-id --stage-id` of that stage.
Do not dispatch a later stage until this stage completes.
Owner death becomes execution `failed` when the next `execute`, `agent`, or `watch`
of that run can reap a verified provider or observe `ESRCH`.
If the provider pid is still alive and identity cannot be verified, stay `running` and refuse retry.
Retry uses the same `run_id` only after `failed`.

## Model resolution

1. `stage.model` when declared.
2. Otherwise `roles.<stage.role>.default_model`.
3. Resolve that key from `models` to provider and native model name.

Missing roles or model keys are configuration errors. Do not substitute another role, provider, or model.

Thinking, after the model is resolved:

1. `stage.thinking` when declared.
2. Otherwise `roles.<stage.role>.thinking` when declared.
3. Otherwise `models.<model-key>.thinking.default`.
4. The level must be in `models.<model-key>.thinking.allowed`.

A missing thinking capability, default, or allowed level is a configuration error.

## Artifacts

A stage that creates a file declares `produces.artifact` as a filename, not a path.
The specialist writes that file under the execution `artifacts/` directory.
A complete response without that nonempty file fails the execution.
That attempt directory stays immutable after the execution ends.

A decision-only review stage omits `produces`.
It records `decision` in `response.md` and is not prompted to write an artifact.

Workflow `completion.outputs` names the files published on the completion decision.
See `./agentflow help runs` and `./agentflow help workflow`.

## Session continuity

`session.resume_from` continues an earlier stage's provider conversation, not a role.

```yaml
- id: write-tests
  role: developer
  session:
    resume_from: implement
  depends_on:
    - implement
    - review-work
```

The source stage must be a dependency.
The runtime resumes that stage's `runner_session_id` on the same `run_id`.
Do not pass a session from another run.
Both stages must resolve to the same provider, model, and thinking level, or the workflow is invalid.

## Decisions

`decision` exists only on a stage that declares a flow result.
The runtime requires the `decision:` header, checks `values`, and records the result.
`./agentflow execute --stage-id` dispatches that stage only when it is the stage the kernel would select.
A completed stage is selected again only when a later route targets it.
Without `--stage-id`, `./agentflow execute` selects the next stage and continues.
It does not infer a route from prose.

```yaml
decision:
  values:
    - approved
    - revise
  routes:
    approved: complete
    revise: implement
```

Every route target is `complete` or a declared stage ID.
`completion` may also require a decision value from its completion stage.
On that declared completion decision, Agentflow publishes `completion.outputs`
and marks the run `completed`.
If a declared output is missing or empty, that completion execution fails and the
run stays `active`.
`revise`, `blocked`, `cancelled`, and failed executions do not publish.
