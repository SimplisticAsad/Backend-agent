from __future__ import annotations

from app.config.settings import Settings
from app.llm.base import LLMProvider


def create_provider(settings: Settings, *, mock: bool = False) -> LLMProvider:
    name = "mock" if mock else settings.llm_provider
    if name == "mock":
        from app.llm.mock import MockLLMProvider

        return MockLLMProvider()
    if name in ("anthropic", "openai_compatible", "openai", "ollama"):
        from app.llm.http_providers import AnthropicProvider, OpenAICompatibleProvider

        if name == "anthropic":
            return AnthropicProvider(settings)
        return OpenAICompatibleProvider(settings, ollama=(name == "ollama"))
    raise ValueError(f"unknown LLM provider '{name}' (use mock, anthropic, openai_compatible, ollama)")
