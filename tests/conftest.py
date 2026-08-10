"""Test isolation from developer-local configuration.

``gexdash/__init__.py`` loads a ``.env`` at import time so the app picks up
credentials without a shell profile. That is right for the app and wrong for
the tests: a developer with a real ``.env`` would run the suite with
``GEXDASH_PROVIDER=schwab`` and live credentials in the environment, so any
test that reached ``build_provider()`` would quietly hit the network — slow,
rate-limited, and green or red depending on whose machine it ran on.

So the suite pins the environment it wants. Tests that need a specific
setting set it explicitly.
"""

from __future__ import annotations

import os

import pytest

#: Cleared for every test. Credentials must never leak into a test run, and
#: the provider must be the mock unless a test says otherwise.
_NEUTRALISED = (
    "SCHWAB_APP_KEY",
    "SCHWAB_APP_SECRET",
    "SCHWAB_CALLBACK_URL",
    "GEXDASH_EXERCISE_OVERRIDES",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Neutralise .env-derived settings and redirect on-disk state."""
    for key in _NEUTRALISED:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("GEXDASH_PROVIDER", "mock")
    # Anything that resolves GEXDASH_HOME at call time lands here rather than
    # in the developer's real ~/.gexdash. Modules that bound it at import
    # time are handled by passing explicit paths in the tests themselves.
    monkeypatch.setenv("GEXDASH_HOME", str(tmp_path / "gexdash-home"))
    yield


def pytest_report_header(config):
    return f"gexdash: provider=mock, credentials neutralised ({len(_NEUTRALISED)} keys)"
