"""Optional LiteLLM adapter.

LiteLLM exposes an OpenAI-compatible proxy, so the core OpenAIChatClient
already works against it. This helper just builds a RoutePool from a LiteLLM
proxy URL + a list of model aliases, reading the key from an env var.

No hard dependency on the `litellm` package — this talks to the PROXY over
HTTP. Install/run LiteLLM yourself; point AGENT_ULTRA at the proxy.
"""

from __future__ import annotations

import os

from ..routes.client import OpenAIChatClient
from ..routes.pool import RoutePool


def litellm_pool(routes, base_url: str = "", api_key_env: str = "LITELLM_API_KEY",
                 timeout: int = 300) -> RoutePool:
    """Build a RoutePool over a LiteLLM proxy.

    routes: list of model aliases configured in your litellm config.yaml.
    base_url: proxy URL (default env LITELLM_BASE_URL or http://127.0.0.1:4000/v1).
    """
    base = base_url or os.environ.get("LITELLM_BASE_URL",
                                      "http://127.0.0.1:4000/v1")
    client = OpenAIChatClient(base_url=base, api_key_env=api_key_env,
                              timeout=timeout)
    return RoutePool(list(routes), client=client)
