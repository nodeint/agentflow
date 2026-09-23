from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass
class CommandSpec:
    argv: List[str]
    stdin: Optional[str] = None


@dataclass(frozen=True)
class NewSession:
    agent_id: Optional[str] = None
    agent_id_source: Optional[str] = None


class BaseCLIAdapter(ABC):
    @property
    @abstractmethod
    def command(self) -> str:
        """Executable name for this provider."""

    @abstractmethod
    def validate_options(self, options: Mapping[str, str]) -> None:
        """Reject options this provider does not accept."""

    def create_new_session(self) -> NewSession:
        return NewSession()

    @abstractmethod
    def build_command(
        self,
        model: str,
        workspace: str,
        prompt: str,
        agent_id: Optional[str],
        new_agent_id: Optional[str],
        prompt_file: Path,
        last_message_file: Path,
        options: Optional[Mapping[str, str]] = None,
    ) -> CommandSpec:
        """Build the provider CLI invocation for a single turn."""

    @abstractmethod
    def parse_response(
        self, stdout: str, last_message_file: Path
    ) -> Tuple[str, Optional[str]]:
        """Return (response_text, agent_id)."""

    def parse_progress_event(self, line: str) -> Optional[Dict[str, Any]]:
        """Return safe, structured progress from one provider stdout line."""
        del line
        return None
