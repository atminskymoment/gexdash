"""Capture the OAuth redirect on a local HTTPS listener.

The copy-paste flow works, but it has two real costs: it is tedious, and it
races the authorization code's lifetime. Schwab codes are single-use and
short-lived, so every second spent selecting a URL, switching windows and
running a command is a second the code is ageing. Catching the redirect
in-process exchanges it milliseconds after the browser is redirected.

Two things make this fiddlier than the usual loopback OAuth dance:

* **The callback is https, not http.** So the listener needs a certificate.
  A self-signed one is fine — nothing sensitive rides on its authenticity,
  and we only need the browser to eventually deliver the GET — but the
  browser will show an interstitial that has to be clicked through.
* **A callback with no port means 443**, which is privileged. Either run the
  listener under sudo, or register a high-port callback on the Schwab app and
  skip sudo entirely. Both are supported; the CLI says which applies.
"""

from __future__ import annotations

import datetime as _dt
import http.server
import ipaddress
import os
import socket
import ssl
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__all__ = ["CertPaths", "ensure_self_signed_cert", "can_bind", "capture_code", "port_for"]

def gexdash_home() -> Path:
    """Resolved per call, so GEXDASH_HOME is honoured after import."""
    return Path(os.environ.get("GEXDASH_HOME", Path.home() / ".gexdash"))


class CertPaths:
    def __init__(self, home: Path | None = None) -> None:
        base = Path(home) if home else gexdash_home()
        self.cert = base / "loopback-cert.pem"
        self.key = base / "loopback-key.pem"


def port_for(callback_url: str) -> int:
    """Port implied by a callback URL. No port means the scheme default."""
    parts = urlparse(callback_url)
    return parts.port or (443 if parts.scheme == "https" else 80)


def can_bind(port: int, host: str = "127.0.0.1") -> tuple[bool, str]:
    sock = socket.socket()
    try:
        sock.bind((host, port))
        return True, ""
    except PermissionError:
        return False, f"binding {host}:{port} requires root (ports below 1024 are privileged)"
    except OSError as exc:
        return False, f"cannot bind {host}:{port}: {exc}"
    finally:
        sock.close()


def ensure_self_signed_cert(paths: CertPaths | None = None, *, days: int = 825) -> CertPaths:
    """Create a self-signed cert for 127.0.0.1 if one is not already cached.

    Only ever presented to a browser on the loopback interface, so its
    authenticity carries no weight — its job is to let TLS complete. The key
    is still written 0600 out of habit rather than necessity.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    paths = paths or CertPaths()
    if paths.cert.exists() and paths.key.exists():
        return paths

    paths.cert.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = _dt.datetime.now(_dt.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    paths.key.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    paths.key.chmod(0o600)
    paths.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # Same reason as the token: generated under sudo, these would be
    # root-owned and the unprivileged app could not reuse them.
    from .config import restore_sudo_ownership
    restore_sudo_ownership(paths.cert, paths.key, paths.cert.parent)

    return paths


_PAGE = """<!doctype html><meta charset="utf-8"><title>gexdash</title>
<style>body{{font:15px/1.5 system-ui,-apple-system,sans-serif;margin:14vh auto;max-width:34rem;
padding:0 1.5rem;color:#111}}h1{{font-size:1.35rem;margin:0 0 .4rem}}
.ok{{color:#0a7a2f}}.bad{{color:#b3261e}}code{{background:#f1f1ef;padding:.1rem .3rem;border-radius:4px}}
p{{color:#444}}</style>
<h1 class="{cls}">{title}</h1><p>{body}</p><p>You can close this tab.</p>"""


def capture_code(
    authorize_url: str,
    port: int,
    *,
    timeout: float = 180.0,
    open_browser: bool = True,
    paths: CertPaths | None = None,
    on_code=None,
) -> dict:
    """Serve on 127.0.0.1:port until the redirect arrives.

    ``on_code(code)`` is invoked *inside the request handler*, so the token
    exchange happens before the browser has even finished rendering — which
    is the entire point. It should return None on success or an error string.

    Returns ``{"code": ..., "error": ..., "exchange_error": ...}``.
    """
    paths = ensure_self_signed_cert(paths)
    result: dict = {"code": None, "error": None, "exchange_error": None}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            query = parse_qs(urlparse(self.path).query)

            # Browsers ask for /favicon.ico and similar; only the request
            # actually carrying the grant should end the wait.
            if "code" not in query and "error" not in query:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if "error" in query:
                result["error"] = query.get("error_description", query["error"])[0]
                page = _PAGE.format(cls="bad", title="Authorization refused",
                                    body=result["error"])
            else:
                result["code"] = query["code"][0]
                err = on_code(result["code"]) if on_code else None
                result["exchange_error"] = err
                page = (
                    _PAGE.format(cls="bad", title="Token exchange failed", body=err)
                    if err else
                    _PAGE.format(cls="ok", title="Authorized",
                                 body="gexdash has stored your Schwab tokens.")
                )

            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *_args):        # keep the console clean
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(paths.cert, paths.key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.timeout = 1.0

    thread = threading.Thread(target=_serve_until, args=(httpd, done, timeout), daemon=True)
    thread.start()

    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

    done.wait(timeout)

    # NOT httpd.shutdown(): it blocks until serve_forever() returns, and this
    # server is driven by handle_request() in a loop instead — so calling it
    # hangs the process forever. The worker is a daemon watching `done`, so
    # closing the socket is both necessary and sufficient.
    try:
        httpd.server_close()
    except Exception:
        pass
    thread.join(timeout=2.0)

    if result["code"] is None and result["error"] is None:
        result["error"] = f"timed out after {timeout:.0f}s waiting for the redirect"
    return result


def _serve_until(httpd, done: threading.Event, timeout: float) -> None:
    """One request at a time until the grant arrives or the clock runs out.

    handle_request() rather than serve_forever(): a TLS handshake the browser
    abandons after the certificate warning raises out of the handler, and
    that must not end the wait.
    """
    import time

    deadline = time.monotonic() + timeout
    while not done.is_set() and time.monotonic() < deadline:
        try:
            httpd.handle_request()
        except Exception:
            continue
