# Agentflow

Agentflow runs repeatable, file-backed AI workflows from a project's own
`.agentflow/` directory. Define the roles, models, stages, decisions, and
artifacts once; Agentflow runs the next eligible stage and retains its local
session history.

It currently supports the headless `codex` and `grok` CLIs.

## Install from this repository

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and any provider
CLI named in your configuration installed and authenticated.

```bash
git clone <repository-url>
cd agentflow
uv sync --all-packages
uv run agentflow --help
```

The workspace contains three packages:

| Package | Purpose |
| --- | --- |
| `agentflow-kernel` | Workflow validation, scheduling, prompts, and session records. |
| `agentflow-adapters` | Adapters for provider CLIs. |
| `agentflow-cli` | The `agentflow` command. |

## Add Agentflow to a project

From the root of the project you want agents to work in, create this layout:

```text
.agentflow/
├── config.yaml
└── workflows/
    └── plan-review.yaml
```

`config.yaml` defines named models and roles. `options` holds parameters for
that provider's CLI. Agentflow passes the map through; the adapter turns each
entry into that CLI's own flags. Use values the installed CLI accepts.

```yaml
models:
  implementation:
    provider: codex
    model: gpt-5
    options:
      thinking: medium
      temperature: 0.2
  review:
    provider: grok
    model: grok-4
    options:
      thinking: high

roles:
  developer:
    default_model: implementation
  reviewer:
    default_model: review
```

Codex writes each option as `codex exec -c key=value`. `thinking` is sent as
`model_reasoning_effort`. Grok writes each option as a long flag, with
underscores turned into hyphens. `thinking` is sent as `--reasoning-effort`.
A role or stage `thinking` value replaces `options.thinking` for that turn.

A workflow is a directed graph of stages. A stage can produce a single named
artifact; a decision routes to another stage or completes the workflow.

```yaml
# .agentflow/workflows/plan-review.yaml
id: plan-review
constraints:
  - Do not write implementation code.

stages:
  - id: plan
    role: developer
    depends_on: []
    instructions:
      - Write an implementation plan.
    produces:
      artifact: plan.md

  - id: review-plan
    role: reviewer
    depends_on: [plan]
    decision:
      values: [approved, revise]
      routes:
        approved: complete
        revise: plan

completion:
  stage: review-plan
  decision: approved
  outputs:
    - name: plan
      from_stage: plan
      artifact: plan.md
```

Commit `config.yaml` and `workflows/`. Do not commit `.agentflow/sessions/`:
it contains machine-local execution state, event logs, and generated artifacts.

## Run a workflow

Run commands from the configured project or any of its subdirectories.

```bash
# Create a session and run until it completes, blocks, or exhausts attempts.
agentflow start plan-review --task "Plan the account settings redesign"

# Advance exactly one eligible stage. Useful for controlled orchestration.
agentflow stage --workflow plan-review --task "Plan the account settings redesign"

# Resume a session returned by an earlier command.
agentflow continue <session-id>

# Inspect local session state or follow execution events.
agentflow sessions
agentflow watch <session-id>
```

`start` and `continue` allow three failed stage attempts by
default; pass `--attempts N` to change that limit. Workflow results are printed
as JSON on stdout, while progress is written to stderr.

For workflows that declare `requires`, supply a completed prerequisite session:

```bash
agentflow start implement --task "Build the approved plan" --prior <session-id>
```

## Run a role without a workflow

Use `agent` for a one-off turn that still records a local session. The role must
exist in `.agentflow/config.yaml`.

```bash
agentflow agent start --role reviewer --prompt "Review the latest changes"
agentflow agent continue <session-id> --role reviewer --prompt "Now summarize the risks"
```

Pass `--json` for a machine-readable result. `--file PATH` and `--stdin` are
alternatives to `--prompt`.

## Workflow notes

- `depends_on` makes upstream artifacts available to a stage.
- A stage with `session.resume_from` resumes the provider session from one of
  its dependencies.
- `completion.outputs` publishes selected artifacts from successful stages.
- Provider responses must report `status: complete` or `status: blocked`.
- Run `agentflow <command> --help` for the current command contract; CLI help
  is the source of truth for flags and exit codes.

## Development

See [CONTRIBUTION.md](CONTRIBUTION.md) for repository layout, test conventions,
and commands.
