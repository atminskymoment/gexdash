"""Provider registry.

``GEXDASH_PROVIDER`` selects the source; it defaults to ``mock`` so the
dashboard runs with no credentials. Set it to ``schwab`` once you have
completed the OAuth login.
"""

from __future__ import annotations

import os

from .base import ChainProvider, NotAuthenticated, ProviderError
from .mock import MockProvider

__all__ = [
    "ChainProvider",
    "ProviderError",
    "NotAuthenticated",
    "MockProvider",
    "build_provider",
]


def build_provider(name: str | None = None):
    """Instantiate a provider by name.

    Falls back to the mock provider with a printed warning if Schwab is
    requested but not authenticated -- a dashboard that boots showing clearly
    labelled fake data beats one that refuses to start.
    """
    name = (name or os.environ.get("GEXDASH_PROVIDER") or "mock").lower()

    if name == "mock":
        return MockProvider()

    if name == "schwab":
        from .schwab import SchwabProvider

        provider = SchwabProvider()
        if not provider.auth.configured:
            print(
                "[gexdash] SCHWAB_APP_KEY/SECRET not set -- falling back to the mock provider.",
                flush=True,
            )
            return MockProvider()
        return provider

    raise ValueError(f"unknown provider {name!r}; known: mock, schwab")
