"""Thin on-disk chain cache.

Deliberately not a scheduler. Open interest is an OCC once-daily figure, so
the chain pull *is* the snapshot -- there is nothing to cron. This just avoids
re-hitting the provider on every dashboard reload, and keeps yesterday's
snapshot around so a provider outage degrades to stale-but-labelled rather
than blank.

Retaining history is what would later feed a day-over-day OI-change tile
(tier B), which is why rows are keyed by snapshot date rather than
overwritten in place.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from .chain import OptionChain, build_chain
from .instruments import Instrument, get as get_instrument

def default_db_path() -> Path:
    """Resolved per call, so GEXDASH_HOME is honoured after import."""
    return Path(os.environ.get("GEXDASH_HOME", Path.home() / ".gexdash")) / "chains.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS breadth_sample (
    exchange   TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    advancing  REAL,
    declining  REAL,
    net        REAL,
    PRIMARY KEY (exchange, sampled_at)
);

CREATE TABLE IF NOT EXISTS chain_snapshot (
    instrument   TEXT NOT NULL,
    snapshot_day TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    spot         REAL NOT NULL,
    oi_asof      TEXT,
    provider     TEXT,
    payload      TEXT NOT NULL,
    PRIMARY KEY (instrument, snapshot_day)
);
"""


class ChainStore:
    """SQLite store of chain snapshots and breadth samples.

    Defaults to ``$GEXDASH_HOME/chains.db`` (``~/.gexdash``), created if
    missing. One row per instrument per snapshot day: a later save on the
    same day replaces the earlier one.

    One connection, shared by the server's worker threads, so every use of it
    holds ``_lock``. ``check_same_thread=False`` only switches off Python's
    check; it does not make the connection safe to share. The SQLite build
    Python ships on macOS allows one thread in a connection at a time
    (``sqlite3.threadsafety == 1``), and two concurrent commits crashed the
    whole server process inside SQLite. A fresh install hit it on first load,
    when the page asks for exposure, bars and breadth at once.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------

    def save(self, chain: OptionChain, provider: str, day: date | None = None) -> None:
        """Store a chain as the snapshot for ``day``, replacing any row for it.

        ``day`` defaults to the date part of ``chain.asof``, which providers
        set in UTC -- so it is the UTC date of the fetch, not the exchange
        session date.
        """
        day = day or chain.asof.date()
        payload = json.dumps(
            {
                "strike": chain.strike.tolist(),
                "expiry": [str(e) for e in chain.expiry],
                "right": chain.right.tolist(),
                "iv": chain.iv.tolist(),
                "open_interest": chain.open_interest.tolist(),
                "volume": chain.volume.tolist(),
                "provider_delta": None if chain.provider_delta is None else chain.provider_delta.tolist(),
                "provider_gamma": None if chain.provider_gamma is None else chain.provider_gamma.tolist(),
                "meta": chain.meta,
            }
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO chain_snapshot "
                "(instrument, snapshot_day, fetched_at, spot, oi_asof, provider, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    chain.instrument.key,
                    day.isoformat(),
                    chain.asof.isoformat(),
                    chain.spot,
                    chain.oi_asof.isoformat() if chain.oi_asof else None,
                    provider,
                    payload,
                ),
            )
            self._conn.commit()

    def load_usable(self, instrument: Instrument | str, *, max_lookback: int = 10) -> OptionChain | None:
        """The most recent snapshot that actually carries open interest.

        ``load`` returns the newest row, which is wrong as a fallback: a
        zero-OI snapshot saved before that condition was detected shadows the
        good one behind it, and would be served indefinitely as if it were
        fine. Walks back day by day instead.
        """
        key = instrument if isinstance(instrument, str) else instrument.key
        with self._lock:
            days = self._conn.execute(
                "SELECT snapshot_day FROM chain_snapshot WHERE instrument = ? "
                "ORDER BY snapshot_day DESC LIMIT ?",
                (key, max_lookback),
            ).fetchall()
        for (day_str,) in days:
            chain = self.load(key, date.fromisoformat(day_str))
            if chain is not None and _has_open_interest(chain):
                return chain
        return None

    def load(self, instrument: Instrument | str, day: date | None = None) -> OptionChain | None:
        """The snapshot for ``day``, or the newest one when ``day`` is None.

        None if there is no such row. Returned whether or not it carries open
        interest -- see :meth:`load_usable` for a fallback. The instrument is
        looked up in the registry, so current exercise overrides apply.
        """
        key = instrument if isinstance(instrument, str) else instrument.key
        with self._lock:
            if day is None:
                row = self._conn.execute(
                    "SELECT instrument, fetched_at, spot, oi_asof, payload FROM chain_snapshot "
                    "WHERE instrument = ? ORDER BY snapshot_day DESC LIMIT 1",
                    (key,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT instrument, fetched_at, spot, oi_asof, payload FROM chain_snapshot "
                    "WHERE instrument = ? AND snapshot_day = ?",
                    (key, day.isoformat()),
                ).fetchone()
        if not row:
            return None

        inst_key, fetched_at, spot, oi_asof, payload = row
        data = json.loads(payload)

        chain = build_chain(
            get_instrument(inst_key),
            spot,
            [
                {
                    "strike": data["strike"][i],
                    "expiry": np.datetime64(data["expiry"][i], "s"),
                    "right": data["right"][i],
                    "iv": data["iv"][i],
                    "open_interest": data["open_interest"][i],
                    "volume": data["volume"][i],
                }
                for i in range(len(data["strike"]))
            ],
            asof=datetime.fromisoformat(fetched_at),
            oi_asof=date.fromisoformat(oi_asof) if oi_asof else None,
            meta=data.get("meta", {}),
        )
        if data.get("provider_delta") is not None:
            chain.provider_delta = np.array(data["provider_delta"], dtype=float)
        if data.get("provider_gamma") is not None:
            chain.provider_gamma = np.array(data["provider_gamma"], dtype=float)
        return chain

    # ------------------------------------------------------------------
    # Breadth samples
    #
    # Point-in-time quotes give the counts; the A/D *line* through a session
    # is cumulative and has to be accumulated as the session runs. This is
    # that accumulation -- the same reason snapshots are keyed by date rather
    # than overwritten.
    # ------------------------------------------------------------------

    def save_breadth(self, exchange: str, advancing, declining, net, when: datetime | None = None) -> None:
        """Record one A/D sample, keyed to the minute of ``when`` (default now,
        UTC). Any value may be None."""
        when = when or datetime.now(timezone.utc)
        # Bucketed to the minute: at a 60s server TTL this is one row per
        # minute per exchange, and re-fetches inside a bucket overwrite
        # rather than accumulating duplicates.
        bucket = when.replace(second=0, microsecond=0).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO breadth_sample (exchange, sampled_at, advancing, declining, net) "
                "VALUES (?,?,?,?,?)",
                (exchange, bucket, advancing, declining, net),
            )
            self._conn.commit()

    def breadth_history(self, exchange: str, limit: int = 240) -> list[dict]:
        """Most recent samples, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT sampled_at, advancing, declining, net FROM breadth_sample "
                "WHERE exchange = ? ORDER BY sampled_at DESC LIMIT ?",
                (exchange, limit),
            ).fetchall()
        return [
            {"t": r[0], "advancing": r[1], "declining": r[2], "net": r[3]}
            for r in reversed(rows)
        ]

    def snapshot_days(self, instrument: Instrument | str) -> list[date]:
        """Days with a stored snapshot for ``instrument``, newest first."""
        key = instrument if isinstance(instrument, str) else instrument.key
        with self._lock:
            rows = self._conn.execute(
                "SELECT snapshot_day FROM chain_snapshot WHERE instrument = ? ORDER BY snapshot_day DESC",
                (key,),
            ).fetchall()
        return [date.fromisoformat(r[0]) for r in rows]


#: Shown verbatim in the UI, so it says what happened and what is on screen.
OI_ZERO_REASON = (
    "the provider returned a chain with essentially no open interest - every "
    "exposure measure is proportional to OI, so this is a failed fetch, not a "
    "flat market"
)

#: Minimum share of contracts that must carry open interest for a chain to be
#: believable.
#:
#: Chosen from measurement, not taste. Sampled against one Schwab entitlement
#: with three healthy chains and one degraded one:
#:
#:     SPY  72.7% of contracts carry OI     QQQ  68.3%
#:     NDX  34.3%                           SPX   0.01%  <- degraded
#:
#: The degraded SPX chain had 2 of 16,862 contracts with any OI at all, 896
#: contracts' worth in total, and *none* inside the <=30 DTE window the
#: dashboard analyses. 1% sits ~34x below the thinnest healthy reading, so it
#: separates the two cases with a wide margin while leaving a genuinely thin
#: chain -- few contracts listed, most of them held -- comfortably valid.
MIN_OI_COVERAGE = 0.01


def _has_open_interest(chain: OptionChain) -> bool:
    """Does this chain carry a usable OI surface?

    A share rather than a raw total: "any OI at all" is too permissive, as the
    degraded SPX chain above proves, and an absolute floor would have to be
    set per instrument (SPY carries millions, a quiet single-name does not).
    """
    oi = np.nan_to_num(chain.open_interest)
    if oi.size == 0:
        return False
    return float((oi > 0).sum()) / oi.size >= MIN_OI_COVERAGE


class CachedProvider:
    """Wraps a provider so the chain is fetched at most once per day.

    Spot is *not* cached -- that is the live half of tier A.
    """

    def __init__(
        self,
        provider,
        store: ChainStore | None = None,
        max_age_hours: float = 12.0,
        degraded_retry_seconds: float = 30.0,
        degraded_retry_max_seconds: float = 480.0,
    ) -> None:
        self.provider = provider
        self.store = store or ChainStore()
        self.max_age_hours = max_age_hours
        #: First retry delay after a chain arrives without open interest, and
        #: the ceiling it doubles up to: 30s, 60s, 120s, 240s, 480s.
        #:
        #: Short at first because the common case is transient -- the feed
        #: drops OI around the session boundary and comes back within a minute
        #: or two, and a long first wait leaves the dashboard reading "n/a"
        #: well after the data returned. Doubling stops a genuinely broken
        #: feed from being re-pulled every 30s all night; for SPX one attempt
        #: is four paginated calls and ~17k contracts.
        self.degraded_retry_seconds = degraded_retry_seconds
        self.degraded_retry_max_seconds = degraded_retry_max_seconds
        self.name = getattr(provider, "name", "unknown")
        #: instrument key -> (retry_after_monotonic, chain, consecutive_failures)
        #:
        #: A chain with no open interest is deliberately never written to the
        #: store, which without this would mean re-fetching it on *every*
        #: request. Remember the refusal in memory instead and back off.
        #: ``force`` still overrides it, so the refetch button always reaches
        #: the provider.
        self._degraded: dict[str, tuple[float, OptionChain, int]] = {}

    def _backoff_seconds(self, failures: int) -> float:
        """Delay before attempt ``failures + 1``. Doubles, then holds."""
        return min(
            self.degraded_retry_seconds * (2 ** max(failures - 1, 0)),
            self.degraded_retry_max_seconds,
        )

    def get_spot(self, instrument: Instrument) -> float:
        """Straight through to the provider; spot is never cached."""
        return self.provider.get_spot(instrument)

    @staticmethod
    def _age_hours(chain: OptionChain) -> float:
        """Hours since the chain was fetched. A naive ``asof`` is taken as UTC."""
        asof = chain.asof
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - asof).total_seconds() / 3600

    def _last_good(self, cached: OptionChain | None, instrument: Instrument) -> OptionChain | None:
        """The best snapshot to fall back on when a fetch is unusable.

        Reads through to the store even when ``force`` skipped the cache.
        Forcing means "do not trust the freshness window" -- it does not mean
        "throw away a good snapshot if the refetch turns out to be unusable",
        which is what the ↻ refetch button would otherwise do.
        """
        if cached is not None:
            return cached
        try:
            return self.store.load_usable(instrument)
        except Exception:
            return None

    def _as_degraded(self, chain: OptionChain, reason: str) -> OptionChain:
        """Mark a fallback chain stale, with the reason the UI shows. Mutates
        ``chain.meta`` in place and returns the same chain."""
        chain.meta.update(
            from_cache=True, stale=True,
            cache_age_hours=round(self._age_hours(chain), 2),
            degraded_reason=reason,
        )
        return chain

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None, force: bool = False) -> OptionChain:
        """The newest stored snapshot if it is fresh, else a new fetch, else a labelled fallback.

        In order:

        * A stored snapshot no older than ``max_age_hours`` and carrying open
          interest is returned as is (``from_cache``).
        * Inside the back-off after an empty-OI fetch, the provider is not
          asked again; the last usable snapshot is served instead.
        * Otherwise the provider is asked. A good chain is saved and returned.
          A failed fetch, or one without open interest, serves the last
          usable snapshot marked ``stale`` with a ``degraded_reason``; an
          empty-OI chain is never saved and starts the back-off.

        It raises only when the fetch fails and no usable snapshot exists.
        With no usable snapshot, an empty-OI chain is returned itself, with
        ``oi_unavailable`` set. ``force`` skips the freshness check and the
        back-off, but still falls back rather than discard a good snapshot.
        ``max_dte`` reaches the provider only on a fetch: a cached snapshot
        is returned whatever window it was fetched with.
        """
        cached = None if force else self.store.load(instrument)
        # A snapshot saved before this condition was detected can itself be
        # all zeros. Treat it as no cache at all, or it is served as good
        # until it ages out.
        if cached is not None and not _has_open_interest(cached):
            cached = None

        if cached is not None:
            age_h = self._age_hours(cached)
            if age_h <= self.max_age_hours:
                cached.meta.update(from_cache=True, stale=False, cache_age_hours=round(age_h, 2))
                return cached

        # Still inside the back-off from a chain that arrived without open
        # interest: do not ask again yet.
        held = self._degraded.get(instrument.key)
        if held is not None and not force and time.monotonic() < held[0]:
            fallback = self._last_good(None, instrument)
            return self._as_degraded(fallback, OI_ZERO_REASON) if fallback is not None else held[1]
        failures = held[2] if held is not None else 0

        try:
            chain = self.provider.get_chain(instrument, max_dte=max_dte)
        except Exception:
            # Degrade to the last good snapshot rather than to nothing. A
            # chain that is a few hours past its refresh window is still a
            # usable OI surface; a 502 is not. The staleness is flagged so
            # the UI's provenance panel can say so rather than imply freshness.
            fallback = self._last_good(cached, instrument)
            if fallback is None:
                raise
            return self._as_degraded(
                fallback, f"the {self.name} provider could not be reached"
            )

        # A chain whose open interest is entirely zero is a failed fetch that
        # arrives looking like a successful one. Every exposure measure is
        # proportional to OI, so the whole surface collapses to $0 -- which
        # renders as a confident zero, not as missing data. Observed live:
        # Schwab served 2,338 SPX contracts with openInterest 0 on every one
        # while volume was populated.
        if not _has_open_interest(chain):
            # Never let this overwrite a good snapshot: keep serving the last
            # one that had a real OI surface.
            chain.meta.update(from_cache=False, stale=True, oi_unavailable=True,
                              degraded_reason=OI_ZERO_REASON)
            failures += 1
            self._degraded[instrument.key] = (
                time.monotonic() + self._backoff_seconds(failures), chain, failures,
            )
            fallback = self._last_good(cached, instrument)
            if fallback is not None:
                return self._as_degraded(fallback, OI_ZERO_REASON)
            # Nothing better to serve. Return it, but never cache it to disk,
            # so the UI reports the exposure as unavailable rather than nil.
            return chain

        self._degraded.pop(instrument.key, None)
        self.store.save(chain, provider=self.name)
        chain.meta.update(from_cache=False, stale=False)
        return chain
