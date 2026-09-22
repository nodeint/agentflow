# Agentflow help

`.agentflow/` holds `config.yaml`, `workflows/`, and local `runs/`.
Commit `config.yaml` and `workflows/`. Do not commit `runs/` by default.

```text
.agentflow/
├── config.yaml
├── workflows/
│   └── <workflow-id>.yaml
└── runs/
```

| Topic | Command |
| :--- | :--- |
| This index | `./agentflow help` |
| `config.yaml` | `./agentflow help config` |
| Named workflows | `./agentflow help workflow` |
| Stage fields and routing | `./agentflow help stage` |
| Named workflow run | `./agentflow help execute` |
| Standalone agent | `./agentflow help agent` |
| Run records | `./agentflow help runs` |
