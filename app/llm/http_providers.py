"""Real LLM providers over plain HTTP (httpx). Keys come from settings/env, are never logged, and errors are scrubbed."""
from __future__ import annotations

import httpx

from app.config.settings import Settings
from app.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from app.observability import scrub


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        if not settings.llm_api_key:
            raise LLMError("ANTHROPIC_API_KEY / LLM_API_KEY is not set (use --mock for offline runs)")
        self.s = settings
        self.http = client or httpx.Client(timeout=settings.llm_timeout_seconds)
        self.base = (settings.llm_base_url or "https://api.anthropic.com").rstrip("/")

    def generate(self, request: LLMRequest) -> LLMResponse:
        try:
            r = self.http.post(f"{self.base}/v1/messages", headers={"x-api-key": self.s.llm_api_key or "", "anthropic-version": "2023-06-01", "content-type": "application/json"},
                               json={"model": self.s.llm_model, "max_tokens": 16000, "system": request.system, "messages": [{"role": "user", "content": request.user}]})
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        except httpx.HTTPError as e:
            raise LLMError(scrub(f"anthropic request failed: {type(e).__name__}: {e}")) from None
        return LLMResponse(text, self.name, self.s.llm_model)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, settings: Settings, client: httpx.Client | None = None, ollama: bool = False) -> None:
        self.s = settings
        self.http = client or httpx.Client(timeout=settings.llm_timeout_seconds)
        self.base = (settings.llm_base_url or ("http://localhost:11434/v1" if ollama else "https://api.openai.com/v1")).rstrip("/")
        if not ollama and not settings.llm_api_key and "localhost" not in self.base:
            raise LLMError("LLM_API_KEY is not set")

    def generate(self, request: LLMRequest) -> LLMResponse:
        headers = {"content-type": "application/json"}
        if self.s.llm_api_key:
            headers["authorization"] = f"Bearer {self.s.llm_api_key}"
        body = {"model": self.s.llm_model, "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}]}
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = self.http.post(f"{self.base}/chat/completions", headers=headers, json=body)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise LLMError(scrub(f"openai-compatible request failed: {type(e).__name__}: {e}")) from None
        return LLMResponse(text, self.name, self.s.llm_model)
