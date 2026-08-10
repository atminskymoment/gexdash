"""The local HTTPS listener that catches the OAuth redirect.

Exists to remove the copy-paste step, and with it the race against the
authorization code's lifetime: the exchange runs inside the request handler,
milliseconds after Schwab issues the code.
"""

from __future__ import annotations

import http.client
import ssl
import threading
import time

import pytest

from gexdash.oauth_loopback import CertPaths, can_bind, capture_code, ensure_self_signed_cert, port_for


@pytest.mark.parametrize("url,expected", [
    ("https://127.0.0.1/", 443),          # no port means the scheme default
    ("https://127.0.0.1", 443),
    ("https://127.0.0.1:8182/", 8182),
    ("http://127.0.0.1/", 80),
])
def test_port_for(url, expected):
    assert port_for(url) == expected


def test_privileged_port_is_reported_not_attempted():
    ok, why = can_bind(443)
    if not ok:
        assert "root" in why or "privileged" in why or "cannot bind" in why


def test_cert_is_generated_once_and_key_is_private(tmp_path):
    paths = CertPaths(tmp_path)
    ensure_self_signed_cert(paths)
    assert paths.cert.exists() and paths.key.exists()
    assert oct(paths.key.stat().st_mode)[-3:] == "600"

    # Cached, not regenerated — a new cert every run would re-trigger the
    # browser's interstitial each time.
    first = paths.cert.read_bytes()
    ensure_self_signed_cert(paths)
    assert paths.cert.read_bytes() == first


def _free_port() -> int:
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    return port


def _get(port, path, results, key):
    ctx = ssl._create_unverified_context()      # self-signed, as a browser does after "Proceed"
    for _ in range(40):                          # wait for the listener to come up
        try:
            conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            results[key] = (resp.status, resp.read().decode())
            conn.close()
            return
        except Exception:
            time.sleep(0.1)


def test_captures_the_code_and_exchanges_it_in_the_handler(tmp_path):
    port = _free_port()
    seen, results = {}, {}

    def on_code(code):
        seen["code"] = code
        return None                              # exchange succeeded

    threading.Thread(target=_get, args=(port, "/?code=ABC%40&session=x", results, "r"), daemon=True).start()
    out = capture_code("https://example.invalid/auth", port, timeout=20,
                       open_browser=False, paths=CertPaths(tmp_path), on_code=on_code)

    assert out["code"] == "ABC@", "the %40 suffix Schwab codes carry must survive"
    assert out["error"] is None and out["exchange_error"] is None
    assert seen["code"] == "ABC@", "on_code must run inside the handler"
    assert results["r"][0] == 200 and "Authorized" in results["r"][1]


def test_a_failed_exchange_is_shown_on_the_page(tmp_path):
    port = _free_port()
    results = {}
    threading.Thread(target=_get, args=(port, "/?code=BAD", results, "r"), daemon=True).start()
    out = capture_code("https://example.invalid/auth", port, timeout=20,
                       open_browser=False, paths=CertPaths(tmp_path),
                       on_code=lambda code: "invalid_grant")

    assert out["exchange_error"] == "invalid_grant"
    assert "Token exchange failed" in results["r"][1]


def test_authorization_refusal_is_captured(tmp_path):
    port = _free_port()
    results = {}
    threading.Thread(target=_get, args=(port, "/?error=access_denied", results, "r"), daemon=True).start()
    out = capture_code("https://example.invalid/auth", port, timeout=20,
                       open_browser=False, paths=CertPaths(tmp_path))
    assert out["code"] is None
    assert "access_denied" in str(out["error"])


def test_timeout_returns_rather_than_hanging(tmp_path):
    """Regression: HTTPServer.shutdown() blocks unless serve_forever() is
    running, and this server uses handle_request() — calling it hung the
    process forever."""
    port = _free_port()
    start = time.monotonic()
    out = capture_code("https://example.invalid/auth", port, timeout=2.0,
                       open_browser=False, paths=CertPaths(tmp_path))
    elapsed = time.monotonic() - start
    assert out["code"] is None and "timed out" in out["error"]
    assert elapsed < 8.0, f"took {elapsed:.1f}s — did shutdown() block again?"
