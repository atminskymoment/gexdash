"""Option chain container.

Held as parallel numpy arrays rather than a list of objects so the whole
chain re-prices array-wise on every spot tick. A ~2000 contract chain is
normal for SPX once you include several expiries.

The chain carries the provider's own delta/gamma when supplied. We do not use
them for exposure -- we recompute everything so vanna and charm come from the
same model -- but keeping them makes the cross-check in
``tests/test_crosscheck.py`` possible, which is the only thing standing
between a wrong rate assumption and silently wrong VANX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np

from .greeks import CALL, PUT, Greeks, black76_params, black_scholes
from .instruments import Instrument

# Options expire at settlement, not midnight. SPX AM-settled options settle on
# the open; PM-settled (SPXW) and ES settle at the close. Using a single
# close-ish convention is adequate for exposure work but matters for 0DTE
# theta, so it is named rather than hidden as a magic number.
SETTLEMENT_HOUR_UTC = 20.0  # 16:00 ET
YEAR_DAYS = 365.0


@dataclass
class OptionChain:
    """A point-in-time option chain, vectorized."""

    instrument: Instrument
    spot: float
    asof: datetime

    strike: np.ndarray
    expiry: np.ndarray          # datetime64[s]
    right: np.ndarray           # +1 call, -1 put
    iv: np.ndarray              # decimal, 0.20 == 20%
    open_interest: np.ndarray
    volume: np.ndarray

    # Optional provider-supplied greeks, for cross-checking only.
    provider_delta: np.ndarray | None = None
    provider_gamma: np.ndarray | None = None

    #: Set by providers to record when the OI snapshot was published by OCC.
    #: OI is a once-daily figure; surfacing its age is the honest thing to do.
    oi_asof: date | None = None

    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        n = len(self.strike)
        for name in ("expiry", "right", "iv", "open_interest", "volume"):
            arr = getattr(self, name)
            if len(arr) != n:
                raise ValueError(f"{name} has length {len(arr)}, expected {n}")

    def __len__(self) -> int:
        return len(self.strike)

    # ------------------------------------------------------------------
    # Derived quantities
    # ------------------------------------------------------------------

    @property
    def time_to_expiry(self) -> np.ndarray:
        """Years to expiry, from ``asof``, floored at zero."""
        now = np.datetime64(self.asof.replace(tzinfo=None), "s")
        seconds = (self.expiry - now) / np.timedelta64(1, "s")
        return np.maximum(seconds / (YEAR_DAYS * 86400.0), 0.0)

    @property
    def dte(self) -> np.ndarray:
        """Calendar days to expiry (may be fractional)."""
        return self.time_to_expiry * YEAR_DAYS

    @property
    def is_call(self) -> np.ndarray:
        return self.right > 0

    @property
    def is_put(self) -> np.ndarray:
        return self.right < 0

    def expiries(self) -> np.ndarray:
        """Unique expiries, ascending."""
        return np.unique(self.expiry)

    def greeks(self, spot: float | None = None, *, r: float = 0.0, q: float = 0.0) -> Greeks:
        """Re-price the whole chain, optionally at a hypothetical spot.

        Passing ``spot`` is how the gamma-flip solver walks the price axis.
        Note this holds each contract's IV fixed -- a sticky-strike
        assumption. Real surfaces move; see ``exposure.gamma_flip``.
        """
        s = self.spot if spot is None else spot
        kwargs = black76_params(r) if self.instrument.is_future else {"r": r, "q": q}
        return black_scholes(
            np.full(len(self), float(s)),
            self.strike,
            self.time_to_expiry,
            self.iv,
            self.right,
            **kwargs,
        )

    def filter_by_dte(self, max_dte: float | None, min_dte: float = 0.0) -> "OptionChain":
        """Return a new chain restricted to an expiry window."""
        d = self.dte
        mask = d >= min_dte
        if max_dte is not None:
            mask &= d <= max_dte
        return self.subset(mask)

    def subset(self, mask: np.ndarray) -> "OptionChain":
        def take(arr):
            return None if arr is None else arr[mask]

        return OptionChain(
            instrument=self.instrument,
            spot=self.spot,
            asof=self.asof,
            strike=self.strike[mask],
            expiry=self.expiry[mask],
            right=self.right[mask],
            iv=self.iv[mask],
            open_interest=self.open_interest[mask],
            volume=self.volume[mask],
            provider_delta=take(self.provider_delta),
            provider_gamma=take(self.provider_gamma),
            oi_asof=self.oi_asof,
            meta=dict(self.meta),
        )

    def with_spot(self, spot: float) -> "OptionChain":
        """Same contracts, new spot. Cheap -- arrays are shared, not copied."""
        return OptionChain(
            instrument=self.instrument,
            spot=float(spot),
            asof=self.asof,
            strike=self.strike,
            expiry=self.expiry,
            right=self.right,
            iv=self.iv,
            open_interest=self.open_interest,
            volume=self.volume,
            provider_delta=self.provider_delta,
            provider_gamma=self.provider_gamma,
            oi_asof=self.oi_asof,
            meta=self.meta,
        )


def build_chain(
    instrument: Instrument,
    spot: float,
    rows: list[dict],
    *,
    asof: datetime | None = None,
    oi_asof: date | None = None,
    meta: dict | None = None,
) -> OptionChain:
    """Assemble a chain from provider row dicts.

    Each row needs ``strike``, ``expiry`` (datetime/date/ISO string),
    ``right`` ("C"/"P" or +/-1), ``iv``, ``open_interest``, ``volume``, and
    optionally ``delta`` / ``gamma`` from the provider.
    """
    asof = asof or datetime.now(timezone.utc)

    def to_expiry(v) -> np.datetime64:
        if isinstance(v, np.datetime64):
            return v.astype("datetime64[s]")
        if isinstance(v, datetime):
            return np.datetime64(v.replace(tzinfo=None), "s")
        if isinstance(v, date):
            return np.datetime64(datetime(v.year, v.month, v.day), "s") + np.timedelta64(
                int(SETTLEMENT_HOUR_UTC * 3600), "s"
            )
        return np.datetime64(str(v), "s")

    def to_right(v) -> int:
        if isinstance(v, str):
            return CALL if v.upper().startswith("C") else PUT
        return CALL if float(v) > 0 else PUT

    has_delta = all("delta" in row and row["delta"] is not None for row in rows) if rows else False
    has_gamma = all("gamma" in row and row["gamma"] is not None for row in rows) if rows else False

    return OptionChain(
        instrument=instrument,
        spot=float(spot),
        asof=asof,
        strike=np.array([float(r["strike"]) for r in rows]),
        expiry=np.array([to_expiry(r["expiry"]) for r in rows], dtype="datetime64[s]"),
        right=np.array([to_right(r["right"]) for r in rows], dtype=float),
        iv=np.array([float(r.get("iv") or 0.0) for r in rows]),
        open_interest=np.array([float(r.get("open_interest") or 0.0) for r in rows]),
        volume=np.array([float(r.get("volume") or 0.0) for r in rows]),
        provider_delta=np.array([float(r["delta"]) for r in rows]) if has_delta else None,
        provider_gamma=np.array([float(r["gamma"]) for r in rows]) if has_gamma else None,
        oi_asof=oi_asof,
        meta=meta or {},
    )
