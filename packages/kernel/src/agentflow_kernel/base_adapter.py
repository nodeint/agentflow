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
class ProviderOption:
    """One provider CLI option.

    `prompt` asks for the option after a model is chosen. `allow_default`
    keeps the provider's own value when the user leaves it unset.
    `overridable` lets a role, stage, or turn replace the model value, and
    that value is part of the provider session profile.
    """

    name: str
    prompt: bool = False
    allow_default: bool = True
    overridable: bool = False


@dataclass(frozen=True)
class ModelCatalog:
    ids: tuple[str, ...]
    default_id: Optional[str] = None
    option_values: tuple[tuple[str, str, tuple[str, ...]], ...] = ()

    def values_for(self, model_id: str, option: str) -> Optional[tuple[str, ...]]:
        for model, name, values in self.option_values:
            if model == model_id and name == option:
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

    def provider_options(self) -> tuple[ProviderOption, ...]:
        """Options this provider accepts. Prompted options are asked after a model is chosen."""
        return ()

    def profile_options(self, options: Mapping[str, str]) -> Dict[str, str]:
        """Overridable options that identify a provider session."""
        names = {item.name for item in self.provider_options() if item.overridable}
        return {key: options[key] for key in options if key in names}

    def option_values_command(self, name: str, model: str) -> Optional[List[str]]:
        """Argv that lists values for one option. None when the catalog already has them."""
        del name, model
        return None

    def parse_option_values(self, name: str, text: str, *, model: str) -> tuple[str, ...]:
        """Read option values from the option command's output."""
        del name, text, model
        raise ValueError(f"{self.command} does not list option values.")

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
