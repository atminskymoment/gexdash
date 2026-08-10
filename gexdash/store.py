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
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------

    def save(self, chain: OptionChain, provider: str, day: date | None = None) -> None:
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

    def load(self, instrument: Instrument | str, day: date | None = None) -> OptionChain | None:
        key = instrument if isinstance(instrument, str) else instrument.key
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
        when = when or datetime.now(timezone.utc)
        # Bucketed to the minute: at a 60s server TTL this is one row per
        # minute per exchange, and re-fetches inside a bucket overwrite
        # rather than accumulating duplicates.
        bucket = when.replace(second=0, microsecond=0).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO breadth_sample (exchange, sampled_at, advancing, declining, net) "
            "VALUES (?,?,?,?,?)",
            (exchange, bucket, advancing, declining, net),
        )
        self._conn.commit()

    def breadth_history(self, exchange: str, limit: int = 240) -> list[dict]:
        """Most recent samples, oldest first."""
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
        key = instrument if isinstance(instrument, str) else instrument.key
        rows = self._conn.execute(
            "SELECT snapshot_day FROM chain_snapshot WHERE instrument = ? ORDER BY snapshot_day DESC",
            (key,),
        ).fetchall()
        return [date.fromisoformat(r[0]) for r in rows]


class CachedProvider:
    """Wraps a provider so the chain is fetched at most once per day.

    Spot is *not* cached -- that is the live half of tier A.
    """

    def __init__(self, provider, store: ChainStore | None = None, max_age_hours: float = 12.0) -> None:
        self.provider = provider
        self.store = store or ChainStore()
        self.max_age_hours = max_age_hours
        self.name = getattr(provider, "name", "unknown")

    def get_spot(self, instrument: Instrument) -> float:
        return self.provider.get_spot(instrument)

    @staticmethod
    def _age_hours(chain: OptionChain) -> float:
        asof = chain.asof
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - asof).total_seconds() / 3600

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None, force: bool = False) -> OptionChain:
        cached = None if force else self.store.load(instrument)

        if cached is not None:
            age_h = self._age_hours(cached)
            if age_h <= self.max_age_hours:
                cached.meta.update(from_cache=True, stale=False, cache_age_hours=round(age_h, 2))
                return cached

        try:
            chain = self.provider.get_chain(instrument, max_dte=max_dte)
        except Exception:
            # Degrade to the last good snapshot rather than to nothing. A
            # chain that is a few hours past its refresh window is still a
            # usable OI surface; a 502 is not. The staleness is flagged so
            # the UI's provenance panel can say so rather than imply freshness.
            if cached is None:
                raise
            cached.meta.update(
                from_cache=True, stale=True, cache_age_hours=round(self._age_hours(cached), 2)
            )
            return cached

        self.store.save(chain, provider=self.name)
        chain.meta.update(from_cache=False, stale=False)
        return chain
