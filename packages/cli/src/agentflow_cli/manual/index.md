# Manual

Agentflow runs named workflows and standalone specialist roles for one project.
Configuration, workflow definitions, and run records live in `.agentflow/`.

`config.yaml` and `workflows/` belong in version control. `runs/` is local execution state.

```text
.agentflow/
├── config.yaml
├── workflows/
│   └── <workflow-id>.yaml
└── runs/
    └── <run-id>/
```

`agentflow help` is command usage. `agentflow man` is this specification.
`agentflow manual` is the same command as `agentflow man`.

## Topics

| Topic | Page |
| :--- | :--- |
| This index | `agentflow man` |
| Configuration | `agentflow man config` |
| Workflow files | `agentflow man workflow` |
| Stage contract | `agentflow man stage` |
| Workflow execution | `agentflow man execute` |
| Standalone role | `agentflow man agent` |
| Run records | `agentflow man runs` |
| Watch | `agentflow man watch` |

## Commands

| Command | Usage | Specification |
| :--- | :--- | :--- |
| `coordinator` | `agentflow help coordinator` | `agentflow man config` |
| `runs` | `agentflow help runs` | `agentflow man runs` |
| `execute` | `agentflow help execute` | `agentflow man execute` |
| `agent` | `agentflow help agent` | `agentflow man agent` |
| `watch` | `agentflow help watch` | `agentflow man watch` |
| `help` | `agentflow help help` | — |
| `manual`, `man` | `agentflow help manual` | `agentflow man` |
