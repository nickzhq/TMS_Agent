"""LLM client abstraction: a thin, provider-agnostic interface.

Business code depends only on this interface. Concrete providers (OpenAI-compatible now,
LiteLLM or others later) implement it, so switching providers never touches business logic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(ABC):
    """Minimal LLM interface: plain chat + schema-constrained structured output."""

    @abstractmethod
    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """Return the assistant's text reply for a list of chat messages."""
        ...

    @abstractmethod
    def structured(self, messages: list[dict], schema: type[T], **kwargs: Any) -> T:
        """Return an instance of `schema` (pydantic) extracted from the model reply."""
        ...
