# execute

`agentflow execute` runs a named workflow. Each subcommand is one action.

```text
agentflow execute start WORKFLOW --task TEXT [--prior RUN] [--attempts N]
agentflow execute continue RUN [--prior RUN] [--attempts N]
agentflow execute stage RUN
agentflow execute stage --workflow WORKFLOW --task TEXT [--prior RUN]
```

`start` creates a run and dispatches eligible stages until the run stops.
`continue` resumes a run and does the same.
`stage` dispatches the next eligible stage once and stops. It does not take a stage id. The kernel selects that stage. On a new run, pass `--workflow` and `--task` instead of `RUN`.

`--attempts` is the number of failed executions allowed for one stage on `start` and `continue`. The default is 3. A value below 1 is an error. `stage` does not accept `--attempts` and does not consult the budget.

`--prior` is the completed run named by `requires`. On create it is required when the workflow declares `requires`, and its workflow and decision must match that declaration. On `continue` it must match the stored prior run when given. It is checked before stage selection.

There is no prompt, provider, model, or session flag on this command. Those come from the workflow and from `config.yaml`.

## Stdout

Progress is written to stderr. Stdout is one JSON object:

```json
{
  "run_id": "",
  "run_status": "",
  "stop_reason": "",
  "outputs": {},
  "stages": []
}
```

Each entry in `stages` is one dispatch: `outcome_status`, `outcome_decision`, `execution_id`, `run_id`, and `response`.

## Exit status

| Code | When |
| :--- | :--- |
| 0 | The run is `completed`, or `stage` finished an execution whose `outcome_status` is `complete`. |
| 1 | The run is `blocked`, or `stage` finished an execution that is not `complete`. |
| 2 | Configuration error. Nothing useful was dispatched. |
| 3 | `start` or `continue` spent `--attempts` for a stage. The run stays `active`. |
| 130 | Cancelled. |

`stop_reason` `stage` means the one-shot dispatch finished. Exit 0 means that execution completed. Exit 1 means it did not. A missing or invalid `status:` header, a missing or invalid `decision:` on a decision stage, a provider error, or a missing artifact fails that execution and leaves the run `active`.

A looping command retries a failed stage until `--attempts` is spent, then exits 3. A later `execute stage RUN` dispatches the next eligible stage once and does not consult that budget. `execute continue RUN` does not reset the count.

`continue` and `stage` do not dispatch again when the run is already `completed`, `blocked`, or `cancelled`. They print the object and return 0, 1, or 130.

## Response header

A stage response is a header, a blank line, and a body.

```text
status: complete
```

```text
status: complete
decision: approved
```

`status` controls the run. `complete` is the only status that satisfies a dependency or follows a route. A stage that declares decision values must include `decision: <value>` on `complete`. The value is one of those values. The route is not inferred from the body.

`blocked` stops the run. The completion decision marks the run `completed` and publishes `completion.outputs`.

Workflow files: `agentflow man workflow`. Run files: `agentflow man runs`.
