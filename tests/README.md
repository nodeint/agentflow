# Agentflow tests

Rules for unittest files in this directory.

## Layout

- Tests must stay in this directory.
- Each production module must map to at most one `test_<module>.py` file.
- Shared fakes and workspace helpers must live in `support.py`.
- A `test_*.py` module must not import another `test_*.py` module.
- Production packages under `packages/` must not contain `test_*.py`.
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

From the repository root:

```bash
uv run python -m unittest discover -s tests -t . -p 'test_*.py'
```

Touched tests must map to a production module and a High path.
