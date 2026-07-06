"""Model route clients. Stdlib-only OpenAI-compatible chat client.

Works against any /chat/completions endpoint: OpenAI, LiteLLM, vLLM, Ollama
(with the OpenAI compat layer), llama.cpp server, LM Studio, and most hosted
providers. API keys are resolved from an ENVIRONMENT VARIABLE by name — never
pass raw secrets through config files you might commit.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Protocol


class RouteError(Exception):
    """One model call failed (HTTP error, timeout, or empty content)."""


class TransientRouteError(RouteError):
    """A 429/5xx/network blip — retryable with backoff before giving up."""


class ChatClient(Protocol):
    """The one method every backend must provide."""

    def complete(self, model: str, prompt: str, max_tokens: int) -> str: ...


class OpenAIChatClient:
    """Minimal chat client for any OpenAI-compatible endpoint.

    Empty content counts as failure: reasoning models sometimes spend the
    whole budget on hidden reasoning and return 200 with no text. We retry
    once with a doubled budget, and retry transient 429/5xx with jittered
    backoff before raising.
    """

    def __init__(self, base_url: str, api_key: str = "",
                 api_key_env: str = "", timeout: int = 120, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_key_env = api_key_env
        self.timeout = timeout
        self.retries = retries
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._usage_lock = threading.Lock()

    def _key(self) -> str:
        if self._api_key_env:
            return os.environ.get(self._api_key_env, "") or self._api_key
        return self._api_key

    def usage_snapshot(self) -> dict:
        with self._usage_lock:
            return dict(self._usage)

    def complete(self, model: str, prompt: str, max_tokens: int) -> str:
        tokens = max_tokens
        for attempt in range(2):
            content = self._with_backoff(model, prompt, tokens)
            if content.strip():
                return content
            tokens = max_tokens * (2 ** (attempt + 1))
        raise RouteError(f"{model}: empty content (reasoning starvation?)")

    def _with_backoff(self, model: str, prompt: str, max_tokens: int) -> str:
        last: Exception | None = None
        for i in range(self.retries + 1):
            try:
                return self._once(model, prompt, max_tokens)
            except TransientRouteError as e:
                last = e
                if i < self.retries:
                    time.sleep(min(4.0, 0.5 * (2 ** i)) + random.uniform(0, 0.3))
                    continue
                raise RouteError(str(e))
        raise RouteError(str(last) if last else f"{model}: unknown error")

    def _once(self, model: str, prompt: str, max_tokens: int) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code == 429 or 500 <= e.code < 600:
                raise TransientRouteError(f"{model}: HTTP {e.code} {detail}")
            raise RouteError(f"{model}: HTTP {e.code} {detail}")
        except urllib.error.URLError as e:
            raise TransientRouteError(f"{model}: URLError {e.reason}")
        except Exception as e:
            raise RouteError(f"{model}: {e.__class__.__name__}: {e}")
        try:
            content = body["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise RouteError(f"{model}: malformed response {str(body)[:200]}")
        usage = body.get("usage") or {}
        if usage:
            with self._usage_lock:
                self._usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                self._usage["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        return content
