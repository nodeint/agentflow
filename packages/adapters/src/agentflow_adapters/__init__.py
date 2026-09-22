from __future__ import annotations

from typing import Dict

from agentflow_kernel.base_adapter import BaseCLIAdapter
from .codex_adapter import CodexAdapter
from .grok_adapter import GrokAdapter


def default_adapters() -> Dict[str, BaseCLIAdapter]:
    return {"codex": CodexAdapter(), "grok": GrokAdapter()}
