# Sessions and workflow composition

Agentflow stores local execution state under `.agentflow/sessions/`. A session
records stage executions, provider events, decisions, artifacts, and published
outputs.

Do not commit this directory. Commit `.agentflow/config.yaml` and
`.agentflow/workflows/` instead.

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
  workflow: plan-review
  decision: approved
```

Start it with the completed prerequisite session:

```bash
agentflow start implement \
  --task "Build the approved plan" \
  --prior <plan-review-session-id>
```
