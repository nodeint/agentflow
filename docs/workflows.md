# Writing workflows

A workflow is a set of stages connected by dependencies and explicit decision
routes. Definitions live in `.agentflow/workflows/<id>.yaml`.

## Stages and artifacts

`depends_on` makes successful upstream artifacts available to a stage. A stage
may produce one named file artifact:

```yaml
- id: plan
  role: planner
  depends_on: []
  instructions:
    - Write an implementation plan.
  produces:
    artifact: plan.md
```

Workflow-level `constraints` and stage `instructions` are included in the
provider prompt.

## Decisions and routes

A decision stage declares every accepted value and its destination:

```yaml
- id: review-plan
  role: reviewer
  depends_on: [plan]
  decision:
    values: [approved, revise]
    routes:
      approved: complete
      revise: plan
```

A destination is another declared stage or `complete`. Route keys must exactly
match the declared decision values.

## Revision limits

Put `max_revisions` on a stage that a route can revisit:

```yaml
- id: plan
  role: planner
  max_revisions: 3
```

With `max_revisions: 3`, the sequence is:

```text
plan -> review -> revise  # revision 1
plan -> review -> revise  # revision 2
plan -> review -> revise  # revision 3
plan -> review -> revise  # refused; session remains active
```

The review after the third revision still runs. Agentflow stops only when its
decision would exceed the configured limit.

## Completion outputs

`completion` defines the terminal stage and publishes selected artifacts:

```yaml
completion:
  stage: review-plan
  decision: approved
  outputs:
    - name: plan
      from_stage: plan
      artifact: plan.md
```

## Stage protocol

A workflow stage begins its final response with a structured outcome:

```text
status: complete
decision: approved

The plan covers the required behavior and tests.
```

The status must be `complete` or `blocked`. A blocked stage may include a
reason:

```text
status: blocked
blocker: The required API schema is missing.
```

If the stage declares `decision.values`, a completed response must include one
of them in the `decision` header.
