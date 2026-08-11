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

from ..bars import PriceBars, build_bars
from ..breadth import BreadthSnapshot, build_snapshot, BREADTH_SYMBOLS
from ..chain import OptionChain, build_chain
from ..sessions import exchange_today, previous_session
from .. import instruments as _instruments
from ..instruments import Instrument
from .base import BreadthUnavailable, NotAuthenticated, PriceHistoryUnavailable, ProviderError

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
BASE_URL = "https://api.schwabapi.com/marketdata/v1"

def default_token_path() -> Path:
    """Where the token lives, resolved per call.

    A module-level constant would bind GEXDASH_HOME at import, which makes
    the setting un-overridable afterwards -- and, less obviously, let the
    test suite read the developer's real token instead of a temporary one.
    """
    return Path(os.environ.get("GEXDASH_HOME", Path.home() / ".gexdash")) / "schwab_token.json"

log = logging.getLogger(__name__)

#: Breadth metrics where a literal 0.0 means "not populated" rather than a
#: reading. TICK is deliberately excluded -- zero is a legitimate TICK.
_ZERO_MEANS_MISSING = frozenset({"advancing", "declining", "net", "up_volume", "down_volume", "trin"})

#: Schwab returns this in numeric fields when a value is unavailable.
_SENTINEL = -999.0

#: A second, undocumented sentinel seen on the breadth feed: 92233720368.54776
#: is Java's Long.MAX_VALUE scaled by 1e8, i.e. an unset 64-bit field rather
#: than a quantity. Observed live on $UVOL, where taking it at face value
#: would report 92 billion shares of advancing volume and poison every ratio
#: derived from it.
_LONG_MAX_SENTINELS = (9223372036854775807.0, 92233720368.54776)
_SENTINEL_REL_TOL = 1e-9


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
    for sentinel in _LONG_MAX_SENTINELS:
        if abs(f - sentinel) <= abs(sentinel) * _SENTINEL_REL_TOL:
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
        self.token_path = Path(token_path) if token_path else default_token_path()
        self._token: dict | None = None

    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def _basic_header(self) -> str:
        raw = f"{self.app_key}:{self.app_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    #: Schwab refresh tokens live 7 days and CANNOT be renewed programmatically
    #: -- the only way past it is another browser consent. Tracked so the
    #: deadline is visible in advance rather than discovered by a failed call.
    REFRESH_TOKEN_LIFETIME_DAYS = 7

    def _save(self, token: dict, *, new_consent: bool = False) -> None:
        token["expires_at"] = time.time() + float(token.get("expires_in", 1800)) - 60

        # The refresh token's clock starts at consent and is NOT reset by
        # refreshing the access token, so carry the original timestamp
        # forward. Resetting it here would quietly hide the real deadline.
        if new_consent:
            token["refresh_issued_at"] = time.time()
        elif "refresh_issued_at" not in token:
            # Read the prior stamp from DISK, not just the in-memory cache.
            # Depending on self._token being populated is an implicit
            # precondition, and when it is not met the clock silently resets
            # to "7 days left" forever -- hiding the exact deadline this is
            # here to expose.
            prior = (self._load() or {}).get("refresh_issued_at")
            token["refresh_issued_at"] = prior or time.time()
        token["refresh_expires_at"] = (
            token["refresh_issued_at"] + self.REFRESH_TOKEN_LIFETIME_DAYS * 86400
        )

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token, indent=2))
        self.token_path.chmod(0o600)

        # Under sudo (the privileged-port login path) this file would be
        # owned by root and unreadable by the normal app.
        from ..config import restore_sudo_ownership
        restore_sudo_ownership(self.token_path, self.token_path.parent)

        self._token = token

    def status(self) -> dict:
        """Authentication state, safe to print. No secret material."""
        token = self._load()
        now = time.time()
        if not token:
            return {
                "configured": self.configured,
                "authenticated": False,
                "token_path": str(self.token_path),
                # Included even with no token: a callback mismatch is the most
                # common reason there IS no token, so this is precisely when
                # you want to see it.
                "callback_url": self.callback_url,
                "detail": "not connected -- open /connect, or run `python -m gexdash.providers.schwab login`",
            }

        access_left = token.get("expires_at", 0) - now
        refresh_left = token.get("refresh_expires_at", 0) - now
        return {
            "configured": self.configured,
            "authenticated": refresh_left > 0,
            "token_path": str(self.token_path),
            "access_token_valid": access_left > 0,
            "access_expires_in_minutes": round(access_left / 60, 1),
            "refresh_expires_in_days": round(refresh_left / 86400, 2),
            "refresh_expired": refresh_left <= 0,
            "callback_url": self.callback_url,
            "detail": (
                "refresh token expired -- re-run `python -m gexdash.providers.schwab login`"
                if refresh_left <= 0
                else f"re-login needed within {refresh_left / 86400:.1f} days"
            ),
        }

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
        """Exchange the ``code`` in a pasted redirect URL for tokens."""
        qs = parse_qs(urlparse(redirected_url).query)
        code = qs.get("code", [None])[0]
        if not code:
            raise ProviderError(f"no ?code= found in {redirected_url!r}")
        return self.exchange_code_value(code)

    def redirect_candidates(self) -> list[str]:
        """The configured callback first, then trailing-slash variants.

        Schwab's *authorize* endpoint tolerates a trailing-slash difference
        while its *token* endpoint does not, so a callback that sails through
        step 1 can fail step 2 with a bare "invalid_grant" that names nothing.
        Trying the variants against one code beats one browser round-trip per
        guess -- a rejected redirect_uri does not consume the code.
        """
        base = self.callback_url
        variants = [base, base.rstrip("/"), base.rstrip("/") + "/"]
        seen, out = set(), []
        for v in variants:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def exchange_code_value(self, code: str, *, redirect_uri: str | None = None) -> dict:
        """Exchange an already-extracted authorization code for tokens."""
        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or self.callback_url,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise ProviderError(f"token exchange failed [{resp.status_code}]: {resp.text}")
        token = resp.json()
        self._save(token, new_consent=True)   # starts the 7-day clock
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
            age = ""
            issued = (token or {}).get("refresh_issued_at")
            if issued:
                age = f" The refresh token was issued {(time.time() - issued) / 86400:.1f} days ago."
            raise NotAuthenticated(
                "refresh failed -- the 7-day refresh token has likely expired."
                f"{age} Re-run `python -m gexdash.providers.schwab login`. "
                f"[{resp.status_code}] {resp.text[:200]}"
            )
        new = resp.json()
        # Schwab does not always echo the refresh token back.
        new.setdefault("refresh_token", token["refresh_token"])
        self._save(new)
        return new

    def access_token(self) -> str:
        if not self.configured:
            raise NotAuthenticated(
                "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set. If they are in a .env, "
                "check that gexdash.config.load_dotenv() ran before the provider was built."
            )
        token = self._load()
        if not token:
            raise NotAuthenticated("not logged in; run `python -m gexdash.providers.schwab login`")

        # Check the 7-day clock before spending a round trip on a refresh
        # that cannot succeed.
        refresh_expires = token.get("refresh_expires_at")
        if refresh_expires and time.time() >= refresh_expires:
            raise NotAuthenticated(
                "the 7-day refresh token has expired -- Schwab requires a new browser "
                "consent. Run `python -m gexdash.providers.schwab login`."
            )

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
                self._assert_plausible(instrument, value, "quote")
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

        today = exchange_today()
        span_days = int(max_dte) + 1 if max_dte is not None else 400
        data = self._fetch_chain_span(
            instrument, today, date.fromordinal(today.toordinal() + span_days)
        )
        if data.get("status") == "FAILED":
            raise ProviderError(f"chain request failed for {instrument.chain_symbol}: {data}")

        spot = _clean(data.get("underlyingPrice"))
        if spot is None:
            underlying = data.get("underlying") or {}
            spot = _clean(underlying.get("last")) or _clean(underlying.get("mark"))
        if spot is None:
            raise ProviderError("chain response carried no underlying price")
        self._assert_plausible(instrument, spot, "chain")

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

    def get_breadth(self, exchange: str) -> BreadthSnapshot:
        """Exchange breadth from the thinkorswim internals symbols.

        One batched /quotes call for the whole panel, rather than 500
        per-constituent quotes. Each metric resolves independently: a ticker
        that this entitlement does not carry degrades that one field and is
        reported by name, since the exact symbols vary.
        """
        try:
            table = BREADTH_SYMBOLS[exchange]
        except KeyError:
            raise BreadthUnavailable(f"no breadth symbols defined for {exchange!r}") from None

        try:
            data = self._get("/quotes", {"symbols": ",".join(table.values())})
        except ProviderError as exc:
            raise BreadthUnavailable(str(exc)) from None

        invalid = set(data.get("errors", {}).get("invalidSymbols", []) or [])

        values: dict[str, float | None] = {}
        unresolved: dict[str, str] = {}
        for metric, symbol in table.items():
            node = data.get(symbol)
            value = None
            if node and symbol not in invalid:
                quote = node.get("quote", node)
                for fld in ("lastPrice", "mark", "closePrice", "netChange"):
                    value = _clean(quote.get(fld))
                    if value is not None:
                        break

            # An exchange series sitting at exactly 0.0 is not populated on
            # this entitlement. Observed live: every Nasdaq internal returns
            # 0.0, and $ADD returns 0.0 while $ADVN/$DECN carry real counts.
            # Treating those zeros as data would render "0 advancing issues"
            # and a net that contradicts the counts beside it.
            if value == 0.0 and metric in _ZERO_MEANS_MISSING:
                value = None

            if value is None:
                unresolved[metric] = symbol
            values[metric] = value

        if not values.get("advancing") and not values.get("declining"):
            raise BreadthUnavailable(
                f"{exchange} advance/decline is not populated on this entitlement "
                f"({table['advancing']}, {table['declining']} returned "
                f"{'invalid symbol' if table['advancing'] in invalid else 'no value'}). "
                "Schwab carries the NYSE series; the Nasdaq equivalents resolve but "
                "return zeros. See BREADTH_SYMBOLS in gexdash/breadth.py."
            )

        if unresolved:
            log.warning("%s breadth: unresolved symbols %s", exchange, unresolved)

        return build_snapshot(exchange, values, unresolved=unresolved)

    def get_price_history(
        self,
        instrument: Instrument,
        *,
        interval_minutes: int = 1,
        lookback_hours: float = 7.0,
        plan=None,
    ) -> PriceBars:
        """Candles from /marketdata/v1/pricehistory.

        With a :class:`~gexdash.history.Plan` the range/interval vocabulary
        drives the request; without one it falls back to the original
        minutes-and-hours form so existing callers keep working.
        """
        if instrument.is_future:
            raise PriceHistoryUnavailable(
                f"price history for {instrument.chain_symbol} is unverified against Schwab -- "
                "the chains endpoint documents equity and index symbols, and this is a "
                "continuous-contract alias. Use a dated root or a CME-carrying provider."
            )

        if plan is not None:
            params = {
                "symbol": instrument.quote_symbol,
                **plan.request_params(),
                "needExtendedHoursData": "true" if plan.frequency_type == "minute" else "false",
            }
            served_minutes = plan.frequency if plan.frequency_type == "minute" else 0
        else:
            freq = min((1, 5, 10, 15, 30), key=lambda f: abs(f - interval_minutes))
            params = {
                "symbol": instrument.quote_symbol,
                "periodType": "day", "period": 1,
                "frequencyType": "minute", "frequency": freq,
                "needExtendedHoursData": "true",
            }
            served_minutes = freq

        try:
            data = self._get("/pricehistory", params)
        except ProviderError as exc:
            raise PriceHistoryUnavailable(str(exc)) from None

        if data.get("empty") or not data.get("candles"):
            raise PriceHistoryUnavailable(
                f"no candles returned for {instrument.quote_symbol} "
                f"({params['periodType']}/{params['period']} "
                f"{params['frequencyType']}/{params['frequency']})"
            )

        candles = data["candles"]
        if plan is None:
            wanted = int(lookback_hours * 60 / max(served_minutes, 1))
            candles = candles[-wanted:] if wanted > 0 else candles

        bars = build_bars(
            instrument,
            [
                {"time": c["datetime"], "open": c["open"], "high": c["high"],
                 "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)}
                for c in candles
            ],
            interval_minutes=served_minutes or 1440,
            meta={"provider": "schwab", "symbol": instrument.quote_symbol,
                  "provider_request": params},
        )

        if plan is not None and plan.aggregate_minutes:
            from ..history import aggregate
            bars = aggregate(bars, plan.aggregate_minutes)
        return bars

    #: Recursion cap for the date-bisection below. 2^6 = 64 slices is far
    #: more than any 60-day window needs; a deeper split means something
    #: other than response size is failing.
    _MAX_CHAIN_SPLITS = 6

    def _fetch_chain_span(self, instrument: Instrument, start: date, end: date, depth: int = 0) -> dict:
        """Fetch a chain over a date span, bisecting if the response is too big.

        Schwab's gateway rejects oversized responses with a 502
        ``protocol.http.TooBigBody``. A full SPX or NDX chain across a 60-day
        window comfortably exceeds it -- thousands of strikes times a dozen
        expiries -- while SPY and QQQ fit.

        The obvious fix is ``strikeCount``, but that silently truncates the
        wings, and the wings are where put walls live: the aggregate would
        come back smaller with nothing to say it had. Splitting by date and
        merging returns the *complete* chain, at the cost of a few extra
        requests on the two big index symbols.
        """
        params = {
            "symbol": instrument.chain_symbol,
            "contractType": "ALL",
            "includeUnderlyingQuote": "true",
            "strategy": "SINGLE",
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
        }
        try:
            return self._get("/chains", params)
        except ProviderError as exc:
            too_big = "TooBigBody" in str(exc) or "Body buffer overflow" in str(exc)
            if not too_big or depth >= self._MAX_CHAIN_SPLITS or (end - start).days < 1:
                raise

        mid_ord = start.toordinal() + max((end.toordinal() - start.toordinal()) // 2, 1)
        mid = date.fromordinal(mid_ord)
        log.info("%s chain too large for %s..%s; splitting at %s", instrument.key, start, end, mid)
        left = self._fetch_chain_span(instrument, start, mid, depth + 1)
        right = self._fetch_chain_span(instrument, date.fromordinal(mid_ord + 1), end, depth + 1)
        return self._merge_chain_responses(left, right)

    @staticmethod
    def _merge_chain_responses(left: dict, right: dict) -> dict:
        """Combine two chain payloads.

        The expiry maps are keyed ``"YYYY-MM-DD:DTE"``, and the date slices do
        not overlap, so a shallow merge per side is enough. Strike maps within
        a shared expiry key are merged too, in case a boundary lands mid-expiry.
        """
        merged = dict(left)
        for side in ("callExpDateMap", "putExpDateMap"):
            combined = dict(left.get(side) or {})
            for exp_key, strikes in (right.get(side) or {}).items():
                if exp_key in combined:
                    combined[exp_key] = {**combined[exp_key], **strikes}
                else:
                    combined[exp_key] = strikes
            merged[side] = combined

        # Prefer whichever slice actually carried an underlying price.
        if not _clean(merged.get("underlyingPrice")):
            merged["underlyingPrice"] = right.get("underlyingPrice")
        if not merged.get("underlying"):
            merged["underlying"] = right.get("underlying")
        return merged

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
    def _assert_plausible(instrument: Instrument, spot: float, what: str) -> None:
        """Refuse a price that cannot belong to this instrument.

        A root that resolves to the wrong security is the worst failure this
        client can have, because nothing downstream would notice: the greeks,
        the multiplier and the whole exposure surface would be computed
        correctly for an instrument nobody asked about.
        """
        lo, hi = instrument.plausible_spot
        if not (lo <= spot <= hi):
            raise ProviderError(
                f"{instrument.key} {what} returned {spot:,.2f}, outside the plausible band "
                f"{lo:,.0f}-{hi:,.0f}. The symbol {instrument.chain_symbol!r} has almost "
                f"certainly resolved to a different security -- Schwab's bare 'ES', for "
                f"instance, is Eversource Energy."
            )

    @staticmethod
    def _infer_oi_date() -> date:
        """OCC publishes OI before the open for the prior session."""
        return previous_session()


def _extract_code(text: str) -> str:
    """Pull the auth code out of a pasted redirect URL, or accept a bare code.

    Schwab's ``code`` is URL-encoded and ends in ``@`` once decoded, which
    trips people up when they hand-edit it -- so take the whole URL wherever
    possible and let urllib do the decoding.
    """
    text = text.strip().strip("'\"")
    if not text:
        return ""

    parsed = urlparse(text)
    if parsed.scheme or parsed.query or "?" in text:
        # It is a URL. If it carries no ?code= there is nothing to extract --
        # returning the whole URL as if it were the code would send the
        # entire address to the token endpoint and produce a baffling
        # invalid_grant.
        return parse_qs(parsed.query).get("code", [""])[0]

    return text                      # a bare code, pasted on its own


def _login_cli(argv: list[str]) -> int:
    """Exchange an authorization code for tokens.

    Works three ways, because ``input()`` raises EOFError the moment stdin is
    not a terminal -- which is every non-interactive shell, CI job and
    container:

        python -m gexdash.providers.schwab login '<redirect url>'
        echo '<redirect url>' | python -m gexdash.providers.schwab login
        python -m gexdash.providers.schwab login          # interactive prompt
    """
    auth = SchwabAuth()
    if not auth.configured:
        print("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set.", file=sys.stderr)
        print("Check your .env sits at the repo root; `status` shows what resolved.", file=sys.stderr)
        return 1

    raw = argv[0] if argv else ""

    if not raw and not sys.stdin.isatty():
        raw = sys.stdin.read()               # piped

    if not raw and sys.stdin.isatty():
        print("\n1. Opening Schwab authorization in your browser:")
        print(f"   {auth.authorize_url()}\n")
        try:
            webbrowser.open(auth.authorize_url())
        except Exception:
            pass
        print("2. Log in and approve.")
        print("3. The browser will fail to load the callback -- that is expected.")
        print("   Copy the FULL url from the address bar and paste it below.\n")
        raw = input("Redirect URL: ")

    code = _extract_code(raw)
    if not code:
        print("\nNo authorization code supplied. Two steps:\n", file=sys.stderr)
        print("  1. Open this and approve:\n", file=sys.stderr)
        print(f"     {auth.authorize_url()}\n", file=sys.stderr)
        print("  2. Copy the full URL the browser lands on and run:\n", file=sys.stderr)
        print("     python -m gexdash.providers.schwab login '<paste it here>'\n", file=sys.stderr)
        print("  The code is single-use and expires within minutes. Piping it via", file=sys.stderr)
        print("  stdin keeps it out of shell history:", file=sys.stderr)
        print("     pbpaste | python -m gexdash.providers.schwab login\n", file=sys.stderr)
        return 2

    candidates = auth.redirect_candidates()
    errors: list[tuple[str, str]] = []
    accepted = None
    for candidate in candidates:
        try:
            auth.exchange_code_value(code, redirect_uri=candidate)
            accepted = candidate
            break
        except ProviderError as exc:
            errors.append((candidate, str(exc)[:180]))

    if accepted is None:
        print("\nToken exchange failed for every redirect_uri form tried:\n", file=sys.stderr)
        for candidate, err in errors:
            print(f"  {candidate!r}\n      {err}\n", file=sys.stderr)
        print("Credentials are not the problem if `status` showed Basic auth accepted.", file=sys.stderr)
        print("Remaining causes, in order:", file=sys.stderr)
        print("  1. The app is not 'Ready For Use' on developer.schwab.com. An app still", file=sys.stderr)
        print("     'Approved - Pending' authorizes fine but cannot exchange codes.", file=sys.stderr)
        print("  2. The code was already used -- each one works exactly once.", file=sys.stderr)
        print("  3. The registered callback differs from all of the above forms.", file=sys.stderr)
        return 1

    if accepted != auth.callback_url:
        print(f"\nNOTE: the configured callback {auth.callback_url!r} was rejected;", flush=True)
        print(f"      {accepted!r} was accepted. Update SCHWAB_CALLBACK_URL in .env to match.", flush=True)

    st = auth.status()
    print(f"\nSaved token to {auth.token_path}")
    print(f"Access token valid for {st['access_expires_in_minutes']:.0f} minutes (auto-refreshed).")
    print(f"Refresh token valid for {st['refresh_expires_in_days']:.1f} days -- then browser consent again.")
    return 0


def _autologin_cli(argv: list[str]) -> int:
    """Browser -> local HTTPS listener -> token, with no copy-paste.

    Exchanges the code inside the redirect handler, so it is spent
    milliseconds after Schwab issues it rather than after a human round-trip.
    """
    from ..oauth_loopback import can_bind, capture_code, port_for

    auth = SchwabAuth()
    if not auth.configured:
        print("SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set.", file=sys.stderr)
        return 1

    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else port_for(auth.callback_url)

    ok, why = can_bind(port)
    if not ok:
        print(f"\nCannot listen on 127.0.0.1:{port} -- {why}\n", file=sys.stderr)
        print("Two ways forward:\n", file=sys.stderr)
        print("  a) Run this one command with sudo (only the listener needs the port):", file=sys.stderr)
        print("       sudo -E python -m gexdash.providers.schwab autologin\n", file=sys.stderr)
        print("  b) Change the callback on developer.schwab.com to a high port, e.g.", file=sys.stderr)
        print("       https://127.0.0.1:8182/", file=sys.stderr)
        print("     set the same value in .env, and re-run without sudo. No root at all.", file=sys.stderr)
        print("\n  Or keep using the paste flow:  pbpaste | ... login", file=sys.stderr)
        return 2

    exchanged: dict = {}

    def _exchange(code: str):
        # Runs inside the redirect handler. Try each redirect_uri form,
        # because Schwab's authorize endpoint tolerates a trailing-slash
        # difference that its token endpoint rejects.
        errors = []
        for candidate in auth.redirect_candidates():
            try:
                auth.exchange_code_value(code, redirect_uri=candidate)
                exchanged["redirect_uri"] = candidate
                return None
            except ProviderError as exc:
                errors.append(f"{candidate}: {str(exc)[:150]}")
        return " | ".join(errors)

    print(f"\nListening on https://127.0.0.1:{port}/  (self-signed certificate)")
    print("Your browser will warn about the certificate -- that is expected on a")
    print("loopback listener. Choose Advanced -> Proceed so the redirect can land.\n")
    print("Opening Schwab authorization...")

    outcome = capture_code(auth.authorize_url(), port, on_code=_exchange)

    if outcome["error"]:
        print(f"\nFailed: {outcome['error']}", file=sys.stderr)
        return 1
    if outcome["exchange_error"]:
        print(f"\nGot the code, but the exchange failed:\n  {outcome['exchange_error']}\n", file=sys.stderr)
        print("Credentials are fine if `status` showed Basic auth accepted, so the", file=sys.stderr)
        print("remaining cause is app state: the app must be 'Ready For Use' on", file=sys.stderr)
        print("developer.schwab.com. 'Approved - Pending' authorizes but cannot", file=sys.stderr)
        print("exchange codes.", file=sys.stderr)
        return 1

    used = exchanged.get("redirect_uri")
    if used and used != auth.callback_url:
        print(f"\nNOTE: {auth.callback_url!r} was rejected; {used!r} worked.")
        print("      Update SCHWAB_CALLBACK_URL in .env to match.")

    st = auth.status()
    print(f"\nSaved token to {auth.token_path}")
    print(f"Access token valid {st['access_expires_in_minutes']:.0f} min (auto-refreshed).")
    print(f"Refresh token valid {st['refresh_expires_in_days']:.1f} days, then consent again.")
    return 0


def _status_cli() -> int:
    from ..config import settings_report

    print("Settings")
    for key, value, source in settings_report():
        print(f"  {key:28} {value:26} {source}")

    auth = SchwabAuth()
    print("\nAuthentication")
    st = auth.status()
    for k, v in st.items():
        print(f"  {k:28} {v}")

    if not st.get("authenticated"):
        return 1

    # An unexpired token still proves nothing until a call succeeds.
    print("\nLive check")
    try:
        provider = SchwabProvider(auth)
        spot = provider.get_spot(_instruments.SPX)
        print(f"  $SPX spot                    {spot}")
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if cmd == "login":
        raise SystemExit(_login_cli(rest))
    if cmd == "status":
        raise SystemExit(_status_cli())
    if cmd == "autologin":
        raise SystemExit(_autologin_cli(rest))
    if cmd == "authurl":
        print(SchwabAuth().authorize_url())
        raise SystemExit(0)
    print(__doc__)
    print("Commands:\n"
          "  autologin [--port N]    browser -> local listener -> token (no paste)\n"
          "  authurl                 print the authorization URL\n"
          "  login '<redirect url>'  exchange the code (also accepts stdin)\n"
          "  status                  settings, both token clocks, live check")
