"""`.env` loading and Schwab token lifecycle."""

from __future__ import annotations

import json
import os
import time

import pytest

from gexdash.config import KNOWN_KEYS, find_dotenv, load_dotenv, parse_dotenv, settings_report
from gexdash.providers.schwab import SchwabAuth


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def test_parses_the_shapes_people_actually_write():
    text = """
        # a comment
        SCHWAB_APP_KEY=abc123

        # spaces around the equals is what people type
        SCHWAB_APP_SECRET = s3cret
        export GEXDASH_PROVIDER=schwab
        QUOTED="https://127.0.0.1/"
        SINGLE='value'
        EMPTY=
        no_equals_here
    """
    out = parse_dotenv(text)
    assert out["SCHWAB_APP_KEY"] == "abc123"
    assert out["SCHWAB_APP_SECRET"] == "s3cret"      # whitespace stripped
    assert out["GEXDASH_PROVIDER"] == "schwab"       # export prefix handled
    assert out["QUOTED"] == "https://127.0.0.1/"     # quotes removed
    assert out["SINGLE"] == "value"
    assert out["EMPTY"] == ""
    assert "no_equals_here" not in out


def test_whitespace_is_stripped_from_values():
    """A leading space in an API key produces a 401 that is very hard to see."""
    assert parse_dotenv("SCHWAB_APP_KEY =   abc   ")["SCHWAB_APP_KEY"] == "abc"


def test_trailing_slash_in_a_url_is_preserved():
    """The callback must byte-match what Schwab has registered."""
    assert parse_dotenv("SCHWAB_CALLBACK_URL=https://127.0.0.1/")["SCHWAB_CALLBACK_URL"] == "https://127.0.0.1/"


# ----------------------------------------------------------------------
# Loading and precedence
# ----------------------------------------------------------------------


def test_real_environment_wins_over_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEXDASH_PROVIDER=schwab\nSCHWAB_APP_KEY=from_file\n")
    monkeypatch.setenv("GEXDASH_PROVIDER", "mock")

    applied = load_dotenv(env)
    assert os.environ["GEXDASH_PROVIDER"] == "mock", "an exported variable must win"
    assert os.environ["SCHWAB_APP_KEY"] == "from_file"
    assert "GEXDASH_PROVIDER" not in applied


def test_override_forces_the_file_to_win(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEXDASH_PROVIDER=schwab\n")
    monkeypatch.setenv("GEXDASH_PROVIDER", "mock")
    load_dotenv(env, override=True)
    assert os.environ["GEXDASH_PROVIDER"] == "schwab"


def test_empty_values_are_treated_as_unset(tmp_path, monkeypatch):
    """A blank line in a template must not shadow a real environment value."""
    env = tmp_path / ".env"
    env.write_text("SCHWAB_APP_KEY=\n")
    monkeypatch.setenv("SCHWAB_APP_KEY", "real")
    load_dotenv(env)
    assert os.environ["SCHWAB_APP_KEY"] == "real"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_settings_report_never_prints_a_secret(monkeypatch):
    monkeypatch.setenv("SCHWAB_APP_KEY", "SUPERSECRETVALUE")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "ANOTHERSECRET")
    monkeypatch.setenv("GEXDASH_PROVIDER", "schwab")

    rows = settings_report()
    flat = " ".join(f"{k} {v} {s}" for k, v, s in rows)
    assert "SUPERSECRETVALUE" not in flat
    assert "ANOTHERSECRET" not in flat
    assert "<set, 16 chars>" in flat   # len("SUPERSECRETVALUE")          # length is enough to spot a truncated paste
    assert "schwab" in flat                   # non-secrets are shown


def test_report_covers_every_known_key():
    assert {k for k, _, _ in settings_report()} == KNOWN_KEYS


# ----------------------------------------------------------------------
# Token lifecycle — the 7-day cliff
# ----------------------------------------------------------------------


def _auth(tmp_path, **kw):
    return SchwabAuth(app_key="k", app_secret="s", token_path=tmp_path / "tok.json", **kw)


def test_status_before_login(tmp_path):
    st = _auth(tmp_path).status()
    assert st["authenticated"] is False
    assert "not connected" in st["detail"]
    # The callback is reported even with no token: a mismatch is the most
    # common reason there is no token, so this is exactly when it is needed.
    assert st["callback_url"]


def test_consent_starts_the_seven_day_clock(tmp_path):
    a = _auth(tmp_path)
    a._save({"access_token": "x", "refresh_token": "r", "expires_in": 1800}, new_consent=True)

    st = a.status()
    assert st["authenticated"] is True
    assert st["refresh_expires_in_days"] == pytest.approx(7.0, abs=0.01)
    assert st["access_expires_in_minutes"] == pytest.approx(29.0, abs=0.5)


def test_refreshing_the_access_token_does_not_reset_the_seven_days(tmp_path):
    """The critical property.

    Schwab's refresh token cannot be renewed programmatically. If each
    access-token refresh reset the clock, the app would report six days
    remaining forever and the real deadline would arrive unannounced.
    """
    a = _auth(tmp_path)
    a._save({"access_token": "x", "refresh_token": "r", "expires_in": 1800}, new_consent=True)

    # Backdate consent to five days ago.
    token = json.loads((tmp_path / "tok.json").read_text())
    token["refresh_issued_at"] = time.time() - 5 * 86400
    (tmp_path / "tok.json").write_text(json.dumps(token))
    a._token = None

    # A routine access-token refresh writes the token again.
    a._save({"access_token": "y", "refresh_token": "r", "expires_in": 1800})

    assert a.status()["refresh_expires_in_days"] == pytest.approx(2.0, abs=0.05)


def test_expired_refresh_token_is_reported_not_retried(tmp_path):
    """Fail early rather than spend a round trip that cannot succeed."""
    from gexdash.providers.base import NotAuthenticated

    a = _auth(tmp_path)
    a._save({"access_token": "x", "refresh_token": "r", "expires_in": 1800}, new_consent=True)
    token = json.loads((tmp_path / "tok.json").read_text())
    token["refresh_issued_at"] = time.time() - 8 * 86400
    token["refresh_expires_at"] = time.time() - 86400
    token["expires_at"] = 0
    (tmp_path / "tok.json").write_text(json.dumps(token))
    a._token = None

    assert a.status()["refresh_expired"] is True
    with pytest.raises(NotAuthenticated, match="7-day refresh token has expired"):
        a.access_token()


def test_token_file_is_not_world_readable(tmp_path):
    """A leaked refresh token is live brokerage-account access."""
    a = _auth(tmp_path)
    a._save({"access_token": "x", "refresh_token": "r", "expires_in": 1800}, new_consent=True)
    assert oct((tmp_path / "tok.json").stat().st_mode)[-3:] == "600"


def test_callback_url_is_reported_so_a_mismatch_is_visible(tmp_path):
    a = SchwabAuth(app_key="k", app_secret="s", callback_url="https://127.0.0.1/",
                   token_path=tmp_path / "tok.json")
    a._save({"access_token": "x", "refresh_token": "r", "expires_in": 1800}, new_consent=True)
    assert a.status()["callback_url"] == "https://127.0.0.1/"


def test_authorize_url_embeds_the_callback_verbatim(tmp_path):
    """Schwab rejects the exchange unless this matches the registered value."""
    a = SchwabAuth(app_key="APPKEY", app_secret="s", callback_url="https://127.0.0.1/",
                   token_path=tmp_path / "tok.json")
    url = a.authorize_url()
    assert "client_id=APPKEY" in url
    assert "redirect_uri=https://127.0.0.1/" in url


# ----------------------------------------------------------------------
# Login CLI — must work without a terminal
# ----------------------------------------------------------------------


def test_extract_code_from_the_shapes_a_user_might_paste():
    from gexdash.providers.schwab import _extract_code

    # Schwab codes are URL-encoded and end in '@' once decoded — the single
    # most common hand-editing mistake, so take the whole URL and decode.
    assert _extract_code("https://127.0.0.1/?code=ABC123%40&session=x") == "ABC123@"
    assert _extract_code("https://127.0.0.1/?session=x&code=DEF456") == "DEF456"
    assert _extract_code("'https://127.0.0.1/?code=GHI789'") == "GHI789"   # shell quotes
    assert _extract_code("  https://127.0.0.1/?code=JKL \n") == "JKL"
    assert _extract_code("BARECODE") == "BARECODE"
    assert _extract_code("") == ""
    assert _extract_code("   ") == ""


def test_login_without_a_code_instructs_instead_of_crashing(monkeypatch, capsys, tmp_path):
    """input() raises EOFError the moment stdin is not a terminal.

    That is every non-interactive shell, CI job and container, so the CLI
    must degrade to printing the two-step flow rather than a traceback.
    """
    from gexdash.providers import schwab

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setattr(schwab.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(schwab.sys.stdin, "read", lambda: "")

    assert schwab._login_cli([]) == 2
    err = capsys.readouterr().err
    assert "No authorization code supplied" in err
    assert "oauth/authorize" in err          # the URL they need
    assert "single-use" in err


def test_login_reads_a_piped_redirect_url(monkeypatch, tmp_path):
    """`pbpaste | ... login` keeps the code out of shell history."""
    from gexdash.providers import schwab

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setattr(schwab.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(schwab.sys.stdin, "read", lambda: "https://127.0.0.1/?code=PIPED%40\n")

    seen = {}
    monkeypatch.setattr(schwab.SchwabAuth, "exchange_code_value",
                        lambda self, code, redirect_uri=None: seen.setdefault("code", code) or {})
    monkeypatch.setattr(schwab.SchwabAuth, "status",
                        lambda self: {"access_expires_in_minutes": 29.0, "refresh_expires_in_days": 7.0})
    monkeypatch.setattr(schwab.SchwabAuth, "token_path", tmp_path / "t.json", raising=False)

    assert schwab._login_cli([]) == 0
    assert seen["code"] == "PIPED@"


def test_redirect_candidates_cover_the_trailing_slash_both_ways(tmp_path):
    """Schwab's authorize endpoint tolerates a trailing-slash difference that
    its token endpoint rejects, so a callback can pass step 1 and fail step 2
    with a bare invalid_grant. Both forms get tried against one code."""
    from gexdash.providers.schwab import SchwabAuth

    with_slash = SchwabAuth(app_key="k", app_secret="s", callback_url="https://127.0.0.1/",
                            token_path=tmp_path / "a.json").redirect_candidates()
    without = SchwabAuth(app_key="k", app_secret="s", callback_url="https://127.0.0.1",
                         token_path=tmp_path / "b.json").redirect_candidates()

    assert with_slash[0] == "https://127.0.0.1/"     # configured form first
    assert "https://127.0.0.1" in with_slash
    assert without[0] == "https://127.0.0.1"
    assert "https://127.0.0.1/" in without
    assert len(with_slash) == len(set(with_slash)), "no duplicate attempts"


def test_sudo_ownership_restore_is_a_noop_when_not_root(tmp_path, monkeypatch):
    """Guard: must never chown when it has no business doing so."""
    from gexdash.config import restore_sudo_ownership

    f = tmp_path / "x"; f.write_text("hi")
    monkeypatch.delenv("SUDO_UID", raising=False)
    assert restore_sudo_ownership(f) == []

    # SUDO_UID present but we are not root -> still a no-op.
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SUDO_GID", "20")
    assert restore_sudo_ownership(f) == []


def test_sudo_ownership_restore_chowns_when_root(tmp_path, monkeypatch):
    """Under sudo the token and cert must go back to the invoking user, or
    the unprivileged app cannot read what login just wrote."""
    import gexdash.config as cfg

    f = tmp_path / "token.json"; f.write_text("{}")
    calls = []
    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SUDO_GID", "20")
    monkeypatch.setattr(cfg.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cfg.os, "chown", lambda p, u, g: calls.append((str(p), u, g)))

    changed = cfg.restore_sudo_ownership(f, tmp_path / "missing")
    assert changed == [str(f)]                       # missing paths skipped
    assert calls == [(str(f), 501, 20)]
