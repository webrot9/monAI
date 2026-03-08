"""OpenAI LLM integration for monAI agents."""

from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from monai.config import Config


class LLM:
    """Wrapper around OpenAI API for agent use."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()
        self.client = OpenAI(api_key=self.config.llm.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model or self.config.llm.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.llm.temperature,
            "max_tokens": max_tokens or self.config.llm.max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content

    def chat_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
    ) -> dict:
        raw = self.chat(messages, model=model, temperature=temperature, json_mode=True)
        return json.loads(raw)

    def quick(self, prompt: str, system: str = "", model: str | None = None) -> str:
        """Quick single-prompt call. Uses mini model by default for cost savings."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, model=model or self.config.llm.model_mini)

    def quick_json(self, prompt: str, system: str = "") -> dict:
        """Quick JSON response using mini model."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat_json(messages, model=self.config.llm.model_mini)
