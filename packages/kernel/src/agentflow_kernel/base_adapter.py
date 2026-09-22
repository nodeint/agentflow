from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CommandSpec:
    argv: List[str]
    stdin: Optional[str] = None


@dataclass(frozen=True)
class NewSession:
    provider_session_id: Optional[str] = None
    provider_session_source: Optional[str] = None


class BaseCLIAdapter(ABC):
    def create_new_session(self, runner_session_id: str) -> NewSession:
        del runner_session_id
        return NewSession()

    @abstractmethod
    def build_command(
        self,
        model: str,
        workspace: str,
        prompt: str,
        provider_session_id: Optional[str],
        new_provider_session_id: Optional[str],
        prompt_file: Path,
        last_message_file: Path,
        thinking: Optional[str] = None,
    ) -> CommandSpec:
        """Build the provider CLI invocation for a single turn."""

    @abstractmethod
    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        """Return (response_text, provider_session_id)."""

    def parse_progress_event(self, line: str) -> Optional[Dict[str, Any]]:
        """Return safe, structured progress from one provider stdout line."""
        del line
        return None
