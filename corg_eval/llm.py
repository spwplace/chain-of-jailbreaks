from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class LLMError(RuntimeError):
    """Raised when an OpenAI-compatible model endpoint returns an error."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    raw: dict[str, Any]
    latency_s: float
    finish_reason: str | None
    reasoning_chars: int


class OpenAICompatibleClient:
    """Small dependency-free client for LM Studio and other OpenAI-compatible APIs."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        api_key: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def list_models(self) -> list[str]:
        payload = self._request("GET", "/models")
        return [item["id"] for item in payload.get("data", []) if "id" in item]

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = 0.0,
        max_tokens: int = 384,
        top_p: float | None = None,
        seed: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if seed is not None:
            body["seed"] = seed
        if extra:
            body.update(extra)

        start = time.perf_counter()
        payload = self._request("POST", "/chat/completions", body)
        latency_s = time.perf_counter() - start

        try:
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            reasoning_content = message.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected chat response shape: {payload!r}") from exc
        return ChatResult(
            content=content,
            raw=payload,
            latency_s=latency_s,
            finish_reason=choice.get("finish_reason"),
            reasoning_chars=len(reasoning_content),
        )

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"HTTP {exc.code} from {path}: {details}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"Could not reach model endpoint {self.base_url}: {exc}") from exc

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Non-JSON response from {path}: {response_body[:500]}") from exc
