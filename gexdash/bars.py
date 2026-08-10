"""Intraday OHLCV bars for the price panel.

This is the first thing in the project that needs a data source the two-method
provider protocol does not cover. It is kept **optional** on purpose:

* Everything the exposure engine produces works without it. The price panel is
  presentation.
* The Schwab futures path is already unverified for chains, and price history
  for ``/ES`` / ``/NQ`` will very likely fail the same way.

So a provider that cannot serve bars raises
:class:`~gexdash.providers.base.PriceHistoryUnavailable`, and the UI renders
the reason in place of the chart. A blank candle panel that looks like a
loading state, on a dashboard whose whole point is not fabricating data, would
be the wrong failure.

Note what does **not** belong here: exposure series. Levels drawn as horizontal
annotations on the price axis are legitimate; a GEX curve sharing that axis
would be the dual-axis anti-pattern this project deliberately avoids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from .instruments import Instrument

__all__ = ["PriceBars"]


@dataclass
class PriceBars:
    """A contiguous run of intraday bars, oldest first."""

    instrument: Instrument
    interval_minutes: int
    time: np.ndarray          # datetime64[s]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    meta: dict | None = None

    def __post_init__(self) -> None:
        n = len(self.time)
        for name in ("open", "high", "low", "close", "volume"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name} has length {len(getattr(self, name))}, expected {n}")

    def __len__(self) -> int:
        return len(self.time)

    @property
    def last(self) -> float:
        return float(self.close[-1])

    def vwap(self) -> np.ndarray:
        """Running session VWAP over the typical price.

        Cumulative from the first bar in the series, which is the convention
        traders read off a chart. If the series spans more than one session
        this is not a per-session VWAP -- callers should request a
        single-session lookback.
        """
        typical = (self.high + self.low + self.close) / 3.0
        vol = np.maximum(self.volume, 0.0)
        cum_vol = np.cumsum(vol)
        cum_pv = np.cumsum(typical * vol)
        # Before any volume prints, fall back to price rather than dividing by zero.
        return np.where(cum_vol > 0, cum_pv / np.maximum(cum_vol, 1e-9), typical)

    def session_range(self) -> tuple[float, float]:
        return float(self.low.min()), float(self.high.max())

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument.key,
            "interval_minutes": self.interval_minutes,
            "time": [str(t) for t in self.time],
            "open": self.open.tolist(),
            "high": self.high.tolist(),
            "low": self.low.tolist(),
            "close": self.close.tolist(),
            "volume": self.volume.tolist(),
            "vwap": self.vwap().tolist(),
            "session_low": self.session_range()[0],
            "session_high": self.session_range()[1],
            "meta": self.meta or {},
        }


def build_bars(
    instrument: Instrument,
    rows: list[dict],
    *,
    interval_minutes: int = 1,
    meta: dict | None = None,
) -> PriceBars:
    """Assemble bars from provider row dicts (``time``/``open``/…/``volume``)."""

    def to_time(v) -> np.datetime64:
        if isinstance(v, np.datetime64):
            return v.astype("datetime64[s]")
        if isinstance(v, datetime):
            return np.datetime64(v.replace(tzinfo=None), "s")
        if isinstance(v, (int, float)):  # epoch millis, as Schwab returns
            return np.datetime64(int(v // 1000), "s")
        return np.datetime64(str(v), "s")

    rows = sorted(rows, key=lambda r: to_time(r["time"]))
    return PriceBars(
        instrument=instrument,
        interval_minutes=interval_minutes,
        time=np.array([to_time(r["time"]) for r in rows], dtype="datetime64[s]"),
        open=np.array([float(r["open"]) for r in rows]),
        high=np.array([float(r["high"]) for r in rows]),
        low=np.array([float(r["low"]) for r in rows]),
        close=np.array([float(r["close"]) for r in rows]),
        volume=np.array([float(r.get("volume") or 0.0) for r in rows]),
        meta=meta or {},
    )
