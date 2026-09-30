"""OpenAI-compatible LLM client (bare base_url; works with any compatible endpoint).

Switch provider by changing config (base_url + model) only; no code change.
Adds retry (exponential backoff + jitter) and timeout for transient failures.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Callable, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from src.config.settings import LLMSettings
from src.llm.base import LLMClient

T = TypeVar("T", bound=BaseModel)


class OpenAICompatClient(LLMClient):
    """LLMClient over the OpenAI SDK pointed at any OpenAI-compatible base_url."""

    def __init__(self, settings: LLMSettings, timeout: float = 30.0, max_retries: int = 3):
        self.model = settings.model
        self.max_retries = max_retries
        self._client = OpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=timeout,
            max_retries=0,   # disable the SDK's own retries; we do explicit backoff below
        )

    def chat(self, messages: list[dict], **kwargs: Any) -> str:
        """Chat completion -> assistant text (retries transient errors)."""
        resp = self._with_retry(
            lambda: self._client.chat.completions.create(
                model=self.model, messages=messages, **kwargs
            )
        )
        return resp.choices[0].message.content or ""

    def structured(self, messages: list[dict], schema: type[T], **kwargs: Any) -> T:
        """Structured extraction -> a validated `schema` (pydantic) instance.

        Injects a JSON-schema system hint, requests JSON mode, then validates with pydantic.
        Works with any OpenAI-compatible endpoint supporting response_format=json_object.
        """
        hint = {
            "role": "system",
            "content": (
                "Respond with ONLY a JSON object matching this schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            ),
        }
        resp = self._with_retry(
            lambda: self._client.chat.completions.create(
                model=self.model,
                messages=[hint, *messages],
                response_format={"type": "json_object"},
                **kwargs,
            )
        )
        raw = resp.choices[0].message.content or "{}"
        return schema.model_validate_json(raw)

    def _with_retry(self, fn: Callable[[], Any]) -> Any:
        """Run fn, retrying transient errors with exponential backoff + jitter."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return fn()
            except Exception as exc:   # retry on any transient / provider error
                last_exc = exc
                if attempt < self.max_retries - 1:
                    # exponential backoff + jitter: ~0.5s, ~1s, ~2s, ...
                    time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.3))
        assert last_exc is not None
        raise last_exc
