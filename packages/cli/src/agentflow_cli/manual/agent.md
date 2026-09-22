# agent

`agentflow agent` runs one role from `.agentflow/config.yaml`. It does not read workflows. The run is stored with `workflow_id` `agent`. A file named `workflows/agent.yaml` is unrelated.

```text
agentflow agent start --role ROLE (--prompt TEXT | --file PATH | --stdin)
                      [--task TEXT] [--provider P --model M] [--thinking LEVEL] [--json]
agentflow agent continue RUN --role ROLE (--prompt TEXT | --file PATH | --stdin)
                      [--session ID] [--provider P --model M] [--thinking LEVEL] [--json]
```

`start` creates a run and dispatches one turn. `continue` appends a turn to `RUN`.

`--role` is required on both, and the key must exist under `roles`. `--provider` and `--model` do not create a role. Pass both to replace that role's model, or pass neither. The parser rejects one without the other.

Without that pair, the model key is `roles.<role>.default_model`. Thinking is `--thinking` when set, otherwise the role thinking, otherwise the model default. The level must be in the model's allow-list. With the pair, `--thinking` is passed through without that check.

`--task` is only on `start`. It defaults to the role key. `--session` is only on `continue`. It continues that runner session on `RUN`. It does not select the run. The session must already belong to that run when it has a history.

## Output

Without `--json`, stdout is a short record: role, model, outcome, run, and execution. With `--json`, stdout is one object with `run_id`, `execution_id`, `outcome_status`, `outcome_decision`, `run_status`, and `response`.

Notices go to stderr.

## Exit status

| Code | When |
| :--- | :--- |
| 0 | `outcome_status` is `complete`. The run is `completed`. |
| 1 | The turn did not complete. |
| 2 | Configuration error, or the prompt could not be read. |
| 130 | Cancelled. |

The response uses the same `status:` header as a workflow stage. An agent run does not publish `outputs/`. A later `agent continue RUN` starts another execution on that run.

Run files: `agentflow man runs`. The header: `agentflow man stage`.
