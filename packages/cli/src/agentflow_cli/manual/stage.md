# stage

A stage is one specialist turn. Its contract is the response header, the artifact it must leave, and the route taken from a declared decision.

## Fields

| Field | Required | Meaning |
| :--- | :--- | :--- |
| `id` | Yes | Unique within the workflow. |
| `role` | Yes | Key under `roles` in `config.yaml`. |
| `model` | No | Model key. Overrides the role default. |
| `thinking` | No | Thinking level for this stage. |
| `depends_on` | Yes | Stage ids that must have completed. |
| `instructions` | No | Copied into the prompt unchanged. |
| `produces.artifact` | When the stage writes a file | Filename under this execution's `artifacts/`. |
| `session.resume_from` | No | Earlier stage whose provider session to continue. |
| `decision` | No | Values this stage may return, and the route for each. |

## Response

A response is a header, a blank line, and a body.

```text
status: complete
```

```text
status: complete
decision: approved
```

```text
status: blocked
blocker: the approved tenant hydration contract is missing
```

`status` controls the run. It is not a workflow decision.

- `complete` is the only status that satisfies a dependency or follows a route.
- A stage that declares `decision.values` includes `decision: <value>` when status is `complete`. The value is one of those values. A sentence such as `Approved.` is not a decision.
- The route is taken from the parsed `decision` header. The stage result reports it as `outcome_decision`.
- `blocked` and `cancelled` stop the run.
- The completion decision marks the run `completed` and publishes `completion.outputs`.
- A missing or invalid header, a missing or invalid `decision:` on a decision stage, or a provider error fails that execution only. The run stays `active`. Retry the same run with `agentflow execute stage RUN`. A later stage stays ineligible until this stage completes.

## Provider loss

Exit of the owning process is not a user cancellation.

The next `execute`, `agent`, or `watch` of that run marks the execution `failed` only after a verified reap, a conclusive provider `ESRCH`, or a boot-id mismatch. When the recorded provider pid is still alive and the identity cannot be verified, the execution stays `running` and retry is refused.

Retry uses the same `run_id`, and only after the execution is `failed`.

## Model

The model key is resolved in order:

1. `stage.model`, when set.
2. Otherwise `roles.<stage.role>.default_model`.
3. That key selects `models.<key>.provider` and `models.<key>.model`.

A missing role or model key is a configuration error.

Thinking is resolved after the model key:

1. `stage.thinking`, when set.
2. Otherwise `roles.<stage.role>.thinking`, when set.
3. Otherwise `models.<model-key>.thinking.default`.
4. The level is a member of `models.<model-key>.thinking.allowed`.

A missing thinking block, default, or allowed level is a configuration error.

## Artifacts

`produces.artifact` is a filename, not a path. The specialist writes it under the execution `artifacts/` directory. A `complete` response without that nonempty file fails the execution. The attempt directory is immutable once the execution ends.

A review stage that only records a decision omits `produces`. The decision is stored in `response.md`. The prompt does not ask for an artifact.

`completion.outputs` names the files published when the completion decision is reached. See `agentflow man workflow` and `agentflow man runs`.

## Sessions

`session.resume_from` continues the provider conversation of an earlier stage. It does not select a role.

```yaml
- id: write-tests
  role: developer
  session:
    resume_from: implement
  depends_on:
    - implement
    - review-work
```

The named stage is a dependency. The runtime resumes that stage's `runner_session_id` on the same `run_id`. A session from another run is rejected. Both stages resolve to the same provider, model, and thinking level; otherwise the workflow is invalid.

## Decisions

`decision` is present only on a stage that returns a flow result. The runtime requires the `decision:` header, checks it against `values`, and records the result. A route is not inferred from the body.

`agentflow execute stage` dispatches the next eligible stage once. A completed stage runs again only when a later route selects it. `execute start` and `execute continue` keep selecting the next stage until the run stops.

```yaml
decision:
  values:
    - approved
    - revise
  routes:
    approved: complete
    revise: implement
```

A route target is `complete` or a stage id in the same workflow. `completion` may require one of those values from its completion stage. On that value, Agentflow publishes `completion.outputs` and marks the run `completed`. A missing or empty declared output fails the completion execution and leaves the run `active`.

A `revise` decision, a `blocked` or `cancelled` status, and a failed execution do not publish.
