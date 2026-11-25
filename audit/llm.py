"""LLM clients. Anything with `.model`, `.base_url`, `.temperature` and
`.complete(system, user) -> str` can be used by the extractor."""
from typing import Protocol

from .config import Settings


class LLMClient(Protocol):
    model: str
    base_url: str | None
    temperature: float

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatClient:
    """Works with any OpenAI-compatible chat completions endpoint."""

    def __init__(self, base_url: str, model: str, api_key: str, temperature: float = 0.0):
        from openai import OpenAI

        if not model or not api_key:
            raise ValueError("LLM_MODEL and LLM_API_KEY must be set in .env")
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    @classmethod
    def from_settings(cls, s: Settings) -> "OpenAICompatClient":
        return cls(s.llm_base_url, s.llm_model, s.llm_api_key, s.llm_temperature)

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


class ReplayClient:
    """Returns canned responses in order. For tests and the demo."""

    def __init__(self, responses: list[str], model: str = "replay"):
        self.responses = list(responses)
        self.model = model
        self.base_url = None
        self.temperature = 0.0
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.responses:
            return '{"records": []}'
        return self.responses.pop(0)
