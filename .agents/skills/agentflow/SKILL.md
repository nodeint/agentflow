---
name: agentflow
description: Set up Agentflow in a project, or configure, run, inspect, and resume its workflows. Use when the user asks about Agentflow configuration, workflows, sessions, stages, or published outputs.
---

# Agentflow

Use Agentflow as the control plane for the project's multi-stage agent workflows.

## Working rules

- For an existing project, work from the root containing
  `.agentflow/config.yaml`. To set up a new project, run `agentflow init` from
  its root and follow the CLI's provider and model prompts or flags.
- Read `.agentflow/config.yaml` and the relevant file under
  `.agentflow/workflows/` before changing or running a workflow.
- Run `agentflow doctor` after editing configuration or workflows.
  `agentflow config model` and `agentflow config role` run it after a write.
- Treat `.agentflow/sessions/` as generated, resumable state. Do not edit its
  files directly.
- Preserve the user's task wording when passing `--task`.
- Before continuing a stopped session, inspect the command's `stop_reason` and
  latest execution. Fix the cause before retrying.

## Choose the command

- Start a workflow: `agentflow start <workflow> --task "<task>"`. If its
  `requires` block names a prerequisite workflow and decision, find a matching
  completed session and add `--prior <session-id>`.
- Resume an active session: `agentflow continue <session-id>`.
- Advance exactly one eligible stage: `agentflow stage <session-id>`.
- List recorded sessions: `agentflow sessions` or `agentflow sessions --json`.
- Inspect a snapshot: `agentflow watch <session-id> --once --json`.
- Follow new events: `agentflow watch <session-id>`.
- Run a configured role outside a workflow only when the user asks for ad hoc
  role execution: `agentflow agent start --role <role> --prompt "<prompt>"`.
- Read `.agentflow/config.yaml`: `agentflow config show`.
- Change a model entry: `agentflow config model <name> --provider <provider> --model <model-id>`.
  This updates every role and stage that uses the name.
- Point one role at a model without changing a shared entry:
  `agentflow config role <role> --provider <provider> --model <model-id>`.
  `agentflow config role <role> --model-name <name>` points it at a name that
  already exists. Add `--json` to read the result.

Workflow commands emit progress on stderr and one result object on stdout.
Report the session ID, session status, stop reason, decisions, and published
outputs that matter to the user. A stopped active session is resumable; do not
describe it as completed.

`retry_exhausted` means the stage has used its total failed-attempt allowance;
`--attempts` on `continue` sets a new total, not extra retries.
`revisions_exhausted` requires changing the workflow's `max_revisions` before
another revision can run. `dispatch_limit` applies to the current command;
continue the same session if further stages are appropriate. Do not start a
replacement session just to reset these limits.

Published output paths are relative to
`.agentflow/sessions/<session-id>/`. Prefer the paths reported by Agentflow over
guessing artifact locations.
