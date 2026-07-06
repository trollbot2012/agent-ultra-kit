"""Adapters — thin, runtime-specific glue around the portable core.

Nothing here is imported by the core. Each adapter is optional and may make
runtime-specific assumptions; the generic CLI adapter has zero third-party
dependencies. Import the one you need directly, e.g.:

    from agent_ultra.adapters.litellm_routes import litellm_pool
    from agent_ultra.adapters.mneme_memory import MnemeHooks
"""
