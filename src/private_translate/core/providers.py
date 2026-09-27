from __future__ import annotations

from typing import Any, Protocol
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .model import TranslationUnit


class TranslationProvider(Protocol):
    def translate(
        self, units: list[TranslationUnit], context: str, glossary: list[dict[str, str]], config: dict[str, Any]
    ) -> dict[str, str]: ...

    def review(self, source: str, target: str, context: str, rubric: str, config: dict[str, Any]) -> list[dict[str, Any]]: ...


class OpenAICompatibleProvider:
    """OpenAI-compatible chat API adapter; provider details stay outside core."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: int = 90):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout
        if not self.api_key:
            raise ValueError("Set OPENAI_API_KEY in the environment")

    def _complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                return payload["choices"][0]["message"]["content"]
            except HTTPError as error:
                if attempt == 0 and (error.code == 429 or error.code >= 500):
                    time.sleep(1)
                    continue
                raise RuntimeError(f"Translation provider returned HTTP {error.code}") from error
            except URLError as error:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise RuntimeError(f"Translation provider request failed: {error.reason}") from error
        raise AssertionError("unreachable")

    def translate(self, units, context, glossary, config):
        response = json.loads(self._complete(
            config["translation_prompt"],
            json.dumps({
                "context": context,
                "glossary": glossary,
                "units": [{"id": unit.id, "text": unit.source_text} for unit in units],
                "required_output": {"translations": [{"id": "unit id", "target_text": "translation"}]},
            }, ensure_ascii=False),
        ))
        translations = {row["id"]: row["target_text"] for row in response["translations"]}
        if set(translations) != {unit.id for unit in units}:
            raise ValueError("Provider response must contain exactly one translation per requested unit")
        return translations

    def review(self, source, target, context, rubric, config):
        response = json.loads(self._complete(
            rubric,
            json.dumps({
                "source": source,
                "target": target,
                "context": context,
                "required_output": {"issues": [{"unit_id": "unit id", "severity": "P1", "description": "issue"}]},
            }, ensure_ascii=False),
        ))
        return response["issues"]
