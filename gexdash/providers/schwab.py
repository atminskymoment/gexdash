"""Schwab Trader API provider.

Free with a Schwab brokerage account, and it is the same data that backs
thinkorswim. That makes it the natural starting point -- and its limits are
the natural reason to graduate later: it is a brokerage API with rate limits,
not a market-data firehose, so chain-wide polling lands in the seconds, not
milliseconds. For tier A that is fine, because open interest only changes once
a day anyway.

Setup
-----
1. Register an app at https://developer.schwab.com (Market Data Production).
2. Set the callback URL to ``https://127.0.0.1:8182`` (must be https).
3. Export credentials::

       export SCHWAB_APP_KEY=...
       export SCHWAB_APP_SECRET=...
       export SCHWAB_CALLBACK_URL=https://127.0.0.1:8182

4. Run ``python -m gexdash.providers.schwab login`` and paste the redirect URL.

Tokens: the access token lasts 30 minutes and is refreshed automatically. The
refresh token lasts 7 days, after which ``login`` must be run again -- Schwab
provides no way around that re-consent.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from ..chain import OptionChain, build_chain
from ..sessions import exchange_today, previous_session
from ..instruments import Instrument
from .base import NotAuthenticated, ProviderError

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
BASE_URL = "https://api.schwabapi.com/marketdata/v1"

DEFAULT_TOKEN_PATH = Path(os.environ.get("GEXDASH_HOME", Path.home() / ".gexdash")) / "schwab_token.json"

log = logging.getLogger(__name__)

#: Schwab returns this in numeric fields when a value is unavailable.
_SENTINEL = -999.0


def _clean(value, default=None):
    """Map Schwab's -999.0 / NaN sentinels onto None."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f == _SENTINEL or f != f:  # sentinel or NaN
        return default
    return f


class SchwabAuth:
    """OAuth2 token lifecycle with on-disk persistence."""

    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        callback_url: str | None = None,
        token_path: Path | None = None,
    ) -> None:
        self.app_key = app_key or os.environ.get("SCHWAB_APP_KEY", "")
        self.app_secret = app_secret or os.environ.get("SCHWAB_APP_SECRET", "")
        self.callback_url = callback_url or os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
        self.token_path = Path(token_path or DEFAULT_TOKEN_PATH)
        self._token: dict | None = None

    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def _basic_header(self) -> str:
        raw = f"{self.app_key}:{self.app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _save(self, token: dict) -> None:
        token["expires_at"] = time.time() + float(token.get("expires_in", 1800)) - 60
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token, indent=2))
        self.token_path.chmod(0o600)
        self._token = token

    def _load(self) -> dict | None:
        if self._token is not None:
            return self._token
        if not self.token_path.exists():
            return None
        try:
            self._token = json.loads(self.token_path.read_text())
        except json.JSONDecodeError:
            return None
        return self._token

    # ------------------------------------------------------------------

    def authorize_url(self) -> str:
        return f"{AUTH_URL}?client_id={self.app_key}&redirect_uri={self.callback_url}"

    def exchange_code(self, redirected_url: str) -> dict:
        """Exchange the ``code`` in the pasted redirect URL for tokens."""
        qs = parse_qs(urlparse(redirected_url).query)
        code = qs.get("code", [None])[0]
        if not code:
            raise ProviderError(f"no ?code= found in {redirected_url!r}")

        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.callback_url,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise ProviderError(f"token exchange failed [{resp.status_code}]: {resp.text}")
        token = resp.json()
        self._save(token)
        return token

    def refresh(self) -> dict:
        token = self._load()
        if not token or "refresh_token" not in token:
            raise NotAuthenticated("no refresh token on disk; run `python -m gexdash.providers.schwab login`")

        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
            timeout=30,
        )
        if resp.status_code != 200:
            raise NotAuthenticated(
                "refresh failed -- the 7-day refresh token has likely expired. "
                f"Re-run `python -m gexdash.providers.schwab login`. [{resp.status_code}] {resp.text}"
            )
        new = resp.json()
        # Schwab does not always echo the refresh token back.
        new.setdefault("refresh_token", token["refresh_token"])
        self._save(new)
        return new

    def access_token(self) -> str:
        if not self.configured:
            raise NotAuthenticated("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")
        token = self._load()
        if not token:
            raise NotAuthenticated("not logged in; run `python -m gexdash.providers.schwab login`")
        if time.time() >= token.get("expires_at", 0):
            token = self.refresh()
        return token["access_token"]


class SchwabProvider:
    name = "schwab"

    def __init__(self, auth: SchwabAuth | None = None, timeout: float = 30.0) -> None:
        self.auth = auth or SchwabAuth()
        self._client = httpx.Client(timeout=timeout)
        self._warned_futures: set[str] = set()

    def _get(self, path: str, params: dict) -> dict:
        resp = self._client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.auth.access_token()}"},
        )
        if resp.status_code == 401:
            self.auth.refresh()
            resp = self._client.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.auth.access_token()}"},
            )
        if resp.status_code != 200:
            raise ProviderError(f"GET {path} failed [{resp.status_code}]: {resp.text[:400]}")
        return resp.json()

    # ------------------------------------------------------------------

    def get_spot(self, instrument: Instrument) -> float:
        data = self._get("/quotes", {"symbols": instrument.quote_symbol})
        node = data.get(instrument.quote_symbol)
        if not node:
            raise ProviderError(f"no quote returned for {instrument.quote_symbol}")
        quote = node.get("quote", {})
        for field in ("lastPrice", "mark", "closePrice"):
            value = _clean(quote.get(field))
            if value:
                return value
        raise ProviderError(f"no usable price in quote for {instrument.quote_symbol}: {quote}")

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None) -> OptionChain:
        if instrument.is_future and instrument.key not in self._warned_futures:
            # Logged once per instrument per process, not printed per call.
            # A cache miss or a failed futures fetch (which CachedProvider
            # catches and retries next request) would otherwise repeat this
            # on every tick of an SSE stream.
            self._warned_futures.add(instrument.key)
            log.warning(
                "Requesting a futures chain (%s) from Schwab. This path is UNVERIFIED: "
                "Schwab's chains endpoint documents equity and index options, and %s is a "
                "continuous-contract alias rather than a dated contract. If this fails, use a "
                "dated root or a CME-carrying provider.",
                instrument.key,
                instrument.chain_symbol,
            )

        params = {
            "symbol": instrument.chain_symbol,
            "contractType": "ALL",
            "includeUnderlyingQuote": "true",
            "strategy": "SINGLE",
        }
        if max_dte is not None:
            today = exchange_today()
            params["fromDate"] = today.isoformat()
            params["toDate"] = (today.fromordinal(today.toordinal() + int(max_dte) + 1)).isoformat()

        data = self._get("/chains", params)
        if data.get("status") == "FAILED":
            raise ProviderError(f"chain request failed for {instrument.chain_symbol}: {data}")

        spot = _clean(data.get("underlyingPrice"))
        if spot is None:
            underlying = data.get("underlying") or {}
            spot = _clean(underlying.get("last")) or _clean(underlying.get("mark"))
        if spot is None:
            raise ProviderError("chain response carried no underlying price")

        rows = list(self._iter_contracts(data.get("callExpDateMap", {}), "C"))
        rows += list(self._iter_contracts(data.get("putExpDateMap", {}), "P"))
        if not rows:
            raise ProviderError("chain response contained no contracts")

        return build_chain(
            instrument,
            spot,
            rows,
            asof=datetime.now(timezone.utc),
            # Schwab surfaces the OCC field, which is a prior-session figure.
            oi_asof=self._infer_oi_date(),
            meta={"provider": "schwab", "symbol": instrument.chain_symbol},
        )

    @staticmethod
    def _iter_contracts(exp_map: dict, right: str):
        for _exp_key, strikes in exp_map.items():
            for _strike_key, contracts in strikes.items():
                for c in contracts:
                    iv_pct = _clean(c.get("volatility"))
                    expiry_raw = c.get("expirationDate")
                    if not expiry_raw:
                        continue
                    yield {
                        "strike": float(c["strikePrice"]),
                        # e.g. "2026-08-10T20:00:00.000+00:00"
                        "expiry": datetime.fromisoformat(expiry_raw.replace("Z", "+00:00")).replace(tzinfo=None),
                        "right": right,
                        # Schwab quotes volatility in percent.
                        "iv": (iv_pct / 100.0) if iv_pct is not None else 0.0,
                        "open_interest": _clean(c.get("openInterest"), 0.0) or 0.0,
                        "volume": _clean(c.get("totalVolume"), 0.0) or 0.0,
                        # Kept purely so tests/test_crosscheck.py can diff our
                        # greeks against Schwab's and catch a bad rate or
                        # dividend assumption before it corrupts VANX/CEX.
                        "delta": _clean(c.get("delta")),
                        "gamma": _clean(c.get("gamma")),
                    }

    @staticmethod
    def _infer_oi_date() -> date:
        """OCC publishes OI before the open for the prior session."""
        return previous_session()


def _login_cli() -> int:
    auth = SchwabAuth()
    if not auth.configured:
        print("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET first.", file=sys.stderr)
        return 1

    url = auth.authorize_url()
    print("\n1. Opening Schwab authorization in your browser:")
    print(f"   {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("2. Log in and approve.")
    print("3. Your browser will fail to load the callback -- that is expected.")
    print("   Copy the FULL url from the address bar and paste it below.\n")

    redirected = input("Redirect URL: ").strip()
    auth.exchange_code(redirected)
    print(f"\nSaved token to {auth.token_path}")
    print("Refresh token is valid for 7 days.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        raise SystemExit(_login_cli())
    print(__doc__)
