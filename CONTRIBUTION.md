# Contribution

The workspace has three packages:

| Package | Import | Role |
| :--- | :--- | :--- |
| `packages/kernel` | `agentflow_kernel` | Workflow selection, execute, session records, and the session store. |
| `packages/adapters` | `agentflow_adapters` | Headless `grok` and `codex` CLIs. |
| `packages/cli` | `agentflow_cli` | `agentflow` command and usage help. |

This repository does not contain a project's `.agentflow/` directory.

## Setup

```bash
uv sync --all-packages
uv run agentflow --help
```

`agentflow --help` prints command usage. `agentflow <command> --help` prints that command.

## Tests

Package tests live in `packages/<name>/tests/`, one `test_<module>.py` per production module, with fakes in that package's `tests/support.py`. Use stdlib `unittest`. A test module must not import another `test_*.py` or another package's tests, and `src/` must not contain tests.

Assert an outcome, a state change, a reject reason, or a guard by calling the policy function. Do not dump help text, argv lists, prompt copy, or workflow YAML field by field, and do not drive a full `execute` journey to prove one function. Workspaces use `tempfile.TemporaryDirectory`. Provider tests use in-process fakes and must not invoke `grok` or `codex`. POSIX-only process tests use `@unittest.skipUnless(os.name == "posix", ...)`.

Classes are `<Unit>Tests`. Methods are `test_<behavior>` and name the outcome. Each module ends with `unittest.main()`.

```bash
uv run python -m unittest discover -s packages/kernel/tests -t packages/kernel -p 'test_*.py'
uv run python -m unittest discover -s packages/adapters/tests -t packages/adapters -p 'test_*.py'
uv run python -m unittest discover -s packages/cli/tests -t packages/cli -p 'test_*.py'
uv run python -m unittest discover -s integration -t . -p 'test_*.py'
```

Integration tests live in `integration/`. They call `agentflow_cli.run.main` through `integration/fixtures/bin/grok` and `integration/fixtures/bin/codex` on `PATH`. Those shims must not invoke the real CLIs. Assert the exit code and the session record, and do not re-test a policy that already has a package test.
