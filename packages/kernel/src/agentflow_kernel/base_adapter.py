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


@dataclass(frozen=True)
class ModelCatalog:
    ids: tuple[str, ...]
    default_id: Optional[str] = None
    thinking: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def thinking_for(self, model_id: str) -> Optional[tuple[str, ...]]:
        for model, values in self.thinking:
            if model == model_id:
                return values
        return None


class BaseCLIAdapter(ABC):
    display_name = ""

    @property
    @abstractmethod
    def command(self) -> str:
        """Executable name for this provider."""

    @abstractmethod
    def validate_options(self, options: Mapping[str, str]) -> None:
        """Reject options this provider does not accept."""

    def model_catalog_command(self) -> List[str]:
        """Argv that prints this provider's model catalog."""
        raise ValueError(f"{self.command} does not list models.")

    def parse_model_catalog(self, stdout: str) -> ModelCatalog:
        """Read model ids from the catalog command's stdout."""
        del stdout
        raise ValueError(f"{self.command} does not list models.")

    def thinking_command(self, model: str) -> List[str]:
        """Argv that prints thinking values for one model."""
        del model
        raise ValueError(f"{self.command} does not list thinking values.")

    def parse_thinking_values(self, text: str, *, model: str) -> tuple[str, ...]:
        """Read thinking values for `model` from the thinking command's output."""
        del text, model
        raise ValueError(f"{self.command} does not list thinking values.")

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
