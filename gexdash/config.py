""".env loading.

Every setting in this project is read from ``os.environ``. A ``.env`` file is
the obvious way to keep credentials out of a shell profile, but nothing was
reading one -- the file sat inert and the app silently fell back to the mock
provider, which looks exactly like a working dashboard.

Deliberately not a dependency. python-dotenv would do, but this is 30 lines
and the project's whole posture is that its dependency list is short enough
to audit.

Precedence: **a real environment variable always wins.** A ``.env`` is a
convenience for local development; an explicitly exported variable is a
deliberate act, and CI sets ``GEXDASH_HOME`` that way.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv", "find_dotenv", "settings_report", "restore_sudo_ownership"]

#: Keys this project understands, and the rows settings_report() shows (on the
#: /connect page) with where each value came from. Anything else in a .env is
#: passed through unchecked, so a misspelt key reads as unset.
KNOWN_KEYS = {
    "SCHWAB_APP_KEY",
    "SCHWAB_APP_SECRET",
    "SCHWAB_CALLBACK_URL",
    "GEXDASH_PROVIDER",
    "GEXDASH_HOME",
    "GEXDASH_HOST",
    "GEXDASH_PORT",
    "GEXDASH_EXERCISE_OVERRIDES",
}

_SENSITIVE = ("KEY", "SECRET", "TOKEN", "PASSWORD")


def find_dotenv(start: Path | None = None) -> Path | None:
    """Nearest .env walking up from ``start`` (default: the package root)."""
    here = (start or Path(__file__).resolve().parent).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():   # stop at the repo boundary
            break
    return None


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines.

    Tolerates ``export`` prefixes, whitespace around ``=``, and matched
    surrounding quotes. Whitespace is stripped from both sides because
    ``SCHWAB_APP_KEY = abc`` is what people actually write, and a leading
    space in an API key produces a 401 that is very hard to look at.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[len("export "):].lstrip()
        if "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a .env into ``os.environ``. Returns the keys actually applied.

    Existing environment variables are left alone unless ``override``.
    """
    path = path or find_dotenv()
    if path is None or not path.is_file():
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_dotenv(path.read_text()).items():
        if not value:
            continue                      # an empty value is "unset", not ""
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def settings_report() -> list[tuple[str, str, str]]:
    """(key, displayable value, source) for every known setting.

    Secrets are reported by length only -- enough to tell "set" from "unset"
    and to catch a truncated paste, without putting a live credential in a
    terminal, a log, or a screenshot.
    """
    dotenv_path = find_dotenv()
    in_file = set(parse_dotenv(dotenv_path.read_text())) if dotenv_path else set()

    rows = []
    for key in sorted(KNOWN_KEYS):
        value = os.environ.get(key, "")
        if not value:
            rows.append((key, "(unset)", "-"))
            continue
        shown = f"<set, {len(value)} chars>" if any(t in key for t in _SENSITIVE) else value
        rows.append((key, shown, ".env" if key in in_file else "environment"))
    return rows


def restore_sudo_ownership(*paths: Path | str) -> list[str]:
    """Give files created under ``sudo`` back to the invoking user.

    Binding port 443 needs root, so the loopback login may run under sudo --
    and anything it writes lands owned by root with mode 0600. The token and
    the cert would then be unreadable by the normal non-root app, so login
    would appear to succeed and every request afterwards would fail with a
    permission error that points nowhere near the cause.

    A no-op when not running under sudo. Returns the paths actually changed.
    """
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not uid or os.geteuid() != 0:
        return []

    changed = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        try:
            os.chown(path, int(uid), int(gid) if gid else -1)
            changed.append(str(path))
        except OSError:
            pass
    return changed
