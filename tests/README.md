# Agentflow tests

Rules for unittest files.

## Layout

- Tests for a package live in `packages/<name>/tests/`, next to `src/`.
- Each production module maps to one `test_<module>.py` inside that package.
- Fakes and workspace helpers live in that package's `tests/support.py`.
- A `test_*.py` module must not import another `test_*.py` module.
- A package test module must not import another package's `tests` modules.
- `src/` must not contain `test_*.py`.
- The runner must be stdlib `unittest`.

## Value

Keep High paths only: branching policy, guards, migrate/legacy, store deltas, reject, and idempotency.

- Tests must assert an outcome, state delta, reject reason, or guard.
- Tests must call the policy function when the rule lives in that function.
- One runner or CLI case is allowed when the rule exists only at that boundary.
- Do not add argparse field dumps, help text, argv lists, prompt copy, or getter mirrors.
- Do not snapshot `.agentflow/workflows/*.yaml` field by field.
- Do not drive a full `execute` journey to prove a function such as `stage_dispatch_error`.

## Isolation

- Workspace fixtures must use `tempfile.TemporaryDirectory` or `TestCase.addCleanup`.
- Provider tests must use in-process fakes; they must not invoke grok or codex.
- POSIX-only process tests must use `@unittest.skipUnless(os.name == "posix", ...)`.

## Naming

- Test classes must be `unittest.TestCase` subclasses named `<Unit>Tests`.
- Test methods must be `test_<behavior>` in snake_case and name the outcome.
- Each test module must end with `if __name__ == "__main__": unittest.main()`.

## Verification

From the repository root, one process per package:

```bash
uv run python -m unittest discover -s packages/kernel/tests -t packages/kernel -p 'test_*.py'
uv run python -m unittest discover -s packages/adapters/tests -t packages/adapters -p 'test_*.py'
uv run python -m unittest discover -s packages/cli/tests -t packages/cli -p 'test_*.py'
uv run python -m unittest discover -s integration -t . -p 'test_*.py'
```

## Integration

- Integration tests live in `integration/` at the repository root.
- They call `agentflow_cli.run.main` and reach the real adapters through executables named `grok` and `codex` on `PATH`.
- Those executables are `integration/fixtures/bin/grok` and `integration/fixtures/bin/codex`. They must not invoke the real grok or codex CLIs.
- An integration module must not import a package `tests` module.
- Assert the exit code and the run record. Do not re-test a policy that already has a package test.

Touched tests must map to a production module and a High path.
