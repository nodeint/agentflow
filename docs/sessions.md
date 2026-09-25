# Sessions and workflow composition

Agentflow stores local execution state under `.agentflow/sessions/`. A session
records stage executions, provider events, decisions, artifacts, and published
outputs.

Do not commit this directory. Commit `.agentflow/config.yaml` and
`.agentflow/workflows/` instead.

## On-disk layout

Each run has its own directory, named by its session ID:

```text
.agentflow/sessions/<session-id>/
├── manifest.yaml
├── status.json
├── events.jsonl
├── outputs/                              # present after outputs are published
│   └── <output-name>
└── executions/
    └── <order>-<stage-id>--attempt-<n>/
        ├── execution.json
        ├── conversation.json             # provider conversation checkpoint
        ├── prompt.md
        ├── response.md                   # present after a response is saved
        └── artifacts/
            └── <stage artifact files>
```

The session-level files have distinct purposes:

- `manifest.yaml` identifies the session, workflow, task, creation time, and
  terminal status. An ad hoc agent session also records its role; a workflow
  with `requires` records its prerequisite as `prior_session_id`.
- `status.json` is the current, machine-readable session snapshot. It includes
  the latest execution and stage state, and records decisions, blockers, and
  published outputs when applicable. Commands such as `sessions` and `watch`
  read this file.
- `events.jsonl` is the append-only event stream. Each line is one JSON object.
  `watch --cursor` uses byte offsets into this file.
- `executions/` contains one record for every stage attempt. A completed,
  failed, or cancelled execution record is immutable. Execution directory names
  are ordered, for example `0002-review--attempt-01`.
- `outputs/` contains the artifacts selected by the workflow's `completion`
  block. It is created only when the workflow reaches its configured completion
  condition and all selected artifacts are non-empty.

Each execution directory contains:

- `execution.json`: execution identity, order, stage and attempt, provider and
  model, lifecycle status, timestamps, and the parsed stage outcome.
- `conversation.json`: provider conversation checkpoint used when resuming a
  provider session. It stores the provider and model, native agent ID, options
  setting, lifecycle status, and fallback message history. This is separate
  from the Agentflow session-level `status.json`.
- `prompt.md` and `response.md`: the exact prompt sent for the attempt and the
  response saved from the provider.
- `artifacts/`: files written by that stage. These are per-attempt working
  artifacts; only explicitly configured completion outputs are copied to the
  session-level `outputs/` directory.

These files are Agentflow's resumable local state, not a user-authored storage
format. Prefer the CLI commands below for inspection and automation instead of
editing session files directly.

Older executions may contain the former name `session.json`. Agentflow still
reads that file when resuming, but all new checkpoints are written as
`conversation.json`.

## Published outputs

A completion output is copied from a successful stage artifact to
`outputs/<output-name>`. For example:

```yaml
completion:
  stage: review-plan
  decision: approved
  outputs:
    - name: plan
      from_stage: plan
      artifact: plan.md
```

publishes:

```text
.agentflow/sessions/<session-id>/outputs/plan
```

`status.json` records both the relative path and provenance of the published
file:

```json
{
  "outputs": {
    "plan": {
      "path": "outputs/plan",
      "source_execution_id": "0001-plan--attempt-01",
      "published_by_execution_id": "0002-review-plan--attempt-01"
    }
  }
}
```

`source_execution_id` identifies the attempt that produced the artifact.
`published_by_execution_id` identifies the completion attempt that caused it to
be published.

## Inspect and resume

```bash
agentflow sessions
agentflow watch <session-id>
agentflow continue <session-id>
```

`sessions` lists recorded runs. `watch` follows execution events. `continue`
resumes an active workflow session from its recorded state.

## Artifact dependency versus provider-session resume

`depends_on` gives a stage the output of an upstream stage:

```yaml
depends_on: [plan]
```

`session.resume_from` additionally continues the provider's prior conversational
session instead of starting a new one:

```yaml
depends_on: [plan]
session:
  resume_from: plan
```

The resumed stage therefore receives both the declared upstream context and the
provider's conversation history. `resume_from` must name one of the stage's
dependencies.

## Reusable workflow boundaries

One workflow can require an approved run of another. This lets teams separate
reusable processes—for example, plan approval from implementation:

```yaml
id: implement
requires:
  workflow: plan
  decision: approved
```

Start it with the completed prerequisite session:

```bash
agentflow start implement \
  --task "Build the approved plan" \
  --prior <plan-session-id>
```
