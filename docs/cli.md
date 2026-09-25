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

## Init

```bash
agentflow init
agentflow init --provider codex --model gpt-5
agentflow init --preset plan --provider codex --model gpt-5
```

`init` writes `.agentflow/config.yaml`, one workflow, and `.agentflow/.gitignore`.
The config contains the provider and model you choose. Model ids come from
that provider's CLI, not from a built-in list. Thinking is omitted, so
the provider CLI keeps its own default. Pass `--thinking` only to set
`options.thinking`. The default workflow is `implement`, a single developer
stage. `--preset plan` writes a planner and a reviewer. In a terminal,
provider, model, and thinking are chosen from lists, and `plan` can assign the
reviewer a second model. Pass `--provider` and `--model` together when input
is not a terminal.

An existing valid project is left unchanged. A partial `.agentflow/` directory
stops init and lists the files in the way.

## Config

```bash
agentflow config show
agentflow config model
agentflow config model <name> --provider grok --model grok-4.7 --thinking medium
agentflow config role
agentflow config role reviewer --model-name plain
agentflow config role reviewer --provider grok --model grok-4.7 --thinking high
```

`config` with no command prints help. `config show` prints
`.agentflow/config.yaml` as stored.

In a terminal, `config model` or `config role` with no arguments opens a list.
The last choice adds a model or a role. Choosing a model edits that entry.
Choosing a role points that role at a model, or adds a model for it.

Pass the flags to write without prompts. `--model` is the provider model id.
`--model-name` is the name under `models:` in `.agentflow/config.yaml`.
`--provider` and `--model` are passed together. Model ids come from that
provider's CLI.

`config model <name>` creates a missing name and updates an existing one. An
update changes every role and stage that uses the name. `--thinking` sets
`options.thinking` and leaves other options in place. `--clear-thinking`
removes it.

`config role <role> --model-name <name>` changes only that role's
`default_model`. Several roles can share one `--model-name`.
`config role <role> --provider <provider> --model <model-id>` reuses an entry
with the same provider, model id, and empty options, or adds a new name, and
points only that role at it. A shared entry is left unchanged.
`--thinking` on `role` writes `roles.<role>.thinking` for one role.

After a write, the command names the roles that changed, the stages that
follow, and the stages that stay put because they set their own `model` or
`thinking`. A model name that nothing points at anymore is reported and kept.
The command then runs `doctor`.

`--json` prints one object. Exit 0 when a list is shown or the write passes
doctor. Exit 1 when the file is written and doctor fails. Exit 2 when the
arguments are rejected, before the file changes.

## Project skill

```bash
agentflow skill install
agentflow skill update
```

`skill install` writes the bundled Agentflow usage skill to
`.agents/skills/agentflow/` in the configured project. It refuses to replace an
existing directory.

`skill update` refreshes a skill previously installed by Agentflow. It refuses
to overwrite local changes; use `agentflow skill update --force` when replacing
those changes is intentional. The installed template metadata is kept beside
`SKILL.md` in `.agentflow-template.json`.

## Doctor

```bash
agentflow doctor
agentflow doctor --json
```

`doctor` checks `.agentflow/config.yaml`, every file in `.agentflow/workflows/`,
and whether each provider CLI named in config is on `PATH`. It does not run
those CLIs or check their login. Exit 0 when every check passes, and exit 1
when a check fails.

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
