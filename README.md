# Agentflow

Coordinator and workflow runner. The workspace has three packages:

| Package | Import | Role |
| :--- | :--- | :--- |
| `packages/kernel` | `agentflow_kernel` | Workflow selection, execute, run records, and the session store. |
| `packages/adapters` | `agentflow_adapters` | Headless `grok` and `codex` CLIs. |
| `packages/cli` | `agentflow_cli` | `agentflow` command, help text, and the interactive coordinator. |

A project keeps its own `.agentflow/` config, workflows, and runs. This repository does not.

## Setup

```bash
uv sync --all-packages
uv run agentflow help
```

## Tests

```bash
uv run python -m unittest discover -s packages/kernel/tests -t packages/kernel -p 'test_*.py'
uv run python -m unittest discover -s packages/adapters/tests -t packages/adapters -p 'test_*.py'
uv run python -m unittest discover -s packages/cli/tests -t packages/cli -p 'test_*.py'
```

Rules: [tests/README.md](tests/README.md).
