# CLI commands and execution controls

Run commands from the configured project or any of its subdirectories.

## Workflow commands

```bash
# Create a session and run until it stops.
agentflow start <workflow> --task "<task>"

# Resume an active session and run until it stops.
agentflow continue <session-id>

# Create or advance a session by exactly one eligible stage.
agentflow stage --workflow <workflow> --task "<task>"
agentflow stage <session-id>
```

`start` and `continue` default to three failed execution attempts per stage and
20 stage dispatches per command. Configure those guards with:

```bash
agentflow continue <session-id> --attempts 5
agentflow continue <session-id> --max-dispatches 50
agentflow continue <session-id> --unlimited-dispatches
```

`--attempts N` is the total failed-execution allowance for one stage, not an
additional retry count. `stage` dispatches only once and has no dispatch
ceiling. Revision limits are defined by `max_revisions` in the workflow.

Workflow commands write progress to stderr and one JSON result to stdout. Run
`agentflow <command> --help` for its fields, stop reasons, and exit codes.

## Session commands

```bash
agentflow sessions
agentflow sessions --json
agentflow watch <session-id>
```

## Ad hoc role execution

Configured roles can also run directly without a workflow while retaining a
local session:

```bash
agentflow agent start --role reviewer --prompt "Review the latest changes"
agentflow agent continue <session-id> --role reviewer --prompt "Summarize the risks"
```

Use `--file PATH` or `--stdin` instead of `--prompt`. Pass `--json` for
machine-readable output.
