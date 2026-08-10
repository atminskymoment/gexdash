"""Provider registry.

``GEXDASH_PROVIDER`` selects the source; it defaults to ``mock`` so the
dashboard runs with no credentials. Set it to ``schwab`` once you have
completed the OAuth login.
"""

from __future__ import annotations

import os

from .base import (
    BreadthProvider,
    BreadthUnavailable,
    ChainProvider,
    NotAuthenticated,
    PriceHistoryProvider,
    PriceHistoryUnavailable,
    ProviderError,
)
from .mock import MockProvider

__all__ = [
    "BreadthProvider",
    "BreadthUnavailable",
    "ChainProvider",
    "PriceHistoryProvider",
    "ProviderError",
    "NotAuthenticated",
    "PriceHistoryUnavailable",
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
                "[gexdash] SCHWAB_APP_KEY/SECRET not set -- falling back to the mock provider.\n"
                "          If they are in a .env, check it sits at the repo root.",
                flush=True,
            )
            return MockProvider()

        # Credentials present but no valid consent. Deliberately NOT falling
        # back to the mock here: the caller explicitly asked for live data,
        # and serving synthetic numbers that look real is the worse failure.
        # Requests will raise NotAuthenticated until login runs.
        status = provider.auth.status()
        if not status.get("authenticated"):
            print(
                f"[gexdash] Schwab credentials found but not authenticated: {status['detail']}\n"
                f"          callback in use: {provider.auth.callback_url}\n"
                "          Run: python -m gexdash.providers.schwab login",
                flush=True,
            )
        elif status.get("refresh_expires_in_days", 99) < 2:
            print(
                f"[gexdash] Schwab refresh token expires in "
                f"{status['refresh_expires_in_days']:.1f} days -- re-login soon.",
                flush=True,
            )
        return provider

    raise ValueError(f"unknown provider {name!r}; known: mock, schwab")
