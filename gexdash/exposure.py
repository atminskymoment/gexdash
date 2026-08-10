"""Dealer exposure surface and derived levels.

Scaling conventions, stated once and applied everywhere
-------------------------------------------------------
Raw analytic greeks come out of :mod:`gexdash.greeks` in per-1.0 units. The
conversions to the units traders actually quote all live here so the choices
are visible in one place:

===========  ==========================================  ==============================
Metric       Definition                                  Reads as
===========  ==========================================  ==============================
GEX          gamma * OI * mult * S^2 * 0.01              $ delta gained per +1% in spot
DEX          delta * OI * mult * S                       $ delta
VANX         vanna * OI * mult * S / 100                 $ delta gained per +1 vol point
CEX          charm * OI * mult * S / 365                 $ delta gained per day elapsed
VEX          vega  * OI * mult / 100                     $ P&L per +1 vol point
THEX         theta * OI * mult / 365                     $ P&L per day elapsed
===========  ==========================================  ==============================

The dealer sign convention
--------------------------
Nobody publishes dealer inventory. The near-universal convention -- dealers
long calls, short puts -- is an *assumption* about who is on the other side of
customer flow. It moves the gamma flip level more than any data-quality issue
will, so it is a config knob (:class:`ExposureConfig.dealer_sign`) and the
active choice is reported in the output.

One consequence worth knowing before you read the DEX tile. The convention is
designed for *gamma*, which is positive for calls and puts alike and so needs
an external sign to express dealer positioning. Applied uniformly it also
implies dealers are long every call and short every put -- a synthetic long
against the entire chain -- so aggregate DEX comes out very large and
positive by construction. That is the assumption being honest about itself,
not a bug. For the conventional "open-interest weighted net delta" reading,
where put deltas net against call deltas, use ``dealer_sign="all_long"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .american import approximation_note
from .chain import OptionChain

DealerSign = Literal["long_calls_short_puts", "short_calls_long_puts", "all_long"]
FlipMethod = Literal["spot_solve", "cumulative"]

#: Named expiry windows, in calendar days. ``None`` means no upper bound.
EXPIRY_WINDOWS: dict[str, float | None] = {
    "0dte": 1.0,
    "7dte": 7.0,
    "30dte": 30.0,
    "60dte": 60.0,
    "all": None,
}


@dataclass
class ExposureConfig:
    """Every modelling choice that moves the numbers, in one place."""

    #: Which expiries feed the aggregate. "Total GEX" is meaningless without
    #: this, and it moves the result more than any other single choice.
    expiry_window: str = "30dte"
    dealer_sign: DealerSign = "long_calls_short_puts"
    flip_method: FlipMethod = "spot_solve"
    risk_free_rate: float = 0.042
    #: ``None`` means "use the instrument's own default", which is almost
    #: always what you want -- applying the S&P's yield to QQQ is simply wrong.
    dividend_yield: float | None = None
    #: Half-width of the spot grid the flip solver searches, as a fraction.
    flip_search_span: float = 0.15

    #: Contracts at or below this many days to expiry are dropped entirely.
    #:
    #: Settled contracts carry no exposure, but the analytic greeks are
    #: singular at T=0 -- gamma and charm both blow up as 1/sqrt(T) and 1/T.
    #: Clamping (as the greeks module does, to avoid nan) leaves a *finite but
    #: enormous* value that silently dominates every aggregate: on a real
    #: chain one settled strike contributed 2x the entire CEX total. So they
    #: are excluded here rather than clamped.
    #:
    #: The default of 0.0 drops only genuinely expired contracts. Raise it
    #: (e.g. 1/24 for one hour) if you want to suppress the final-hour 0DTE
    #: gamma spike, which is real but numerically violent.
    min_dte_days: float = 0.0

    def effective_dividend_yield(self, instrument) -> float:
        """Resolve q: explicit override, else the instrument's own default.

        Futures ignore this entirely -- Black-76 sets q = r -- but resolving
        it here keeps the reported diagnostics honest for every instrument.
        """
        if self.dividend_yield is not None:
            return self.dividend_yield
        return instrument.default_dividend_yield

    def max_dte(self) -> float | None:
        try:
            return EXPIRY_WINDOWS[self.expiry_window]
        except KeyError:
            raise KeyError(
                f"unknown expiry_window {self.expiry_window!r}; "
                f"known: {sorted(EXPIRY_WINDOWS)}"
            ) from None


@dataclass
class Level:
    """A price level with the exposure that produced it."""

    name: str
    strike: float | None
    value: float | None = None
    distance: float | None = None       # points from spot, signed
    distance_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "strike": self.strike,
            "value": self.value,
            "distance": self.distance,
            "distance_pct": self.distance_pct,
        }


@dataclass
class ExposureResult:
    spot: float
    instrument: str
    contracts: int
    strikes: np.ndarray

    # Per-strike net dealer exposure, aligned with ``strikes``.
    gex: np.ndarray
    dex: np.ndarray
    vanx: np.ndarray
    cex: np.ndarray
    vex: np.ndarray
    thex: np.ndarray

    # Per-strike splits for the histograms.
    call_gex: np.ndarray
    put_gex: np.ndarray
    call_volume: np.ndarray
    put_volume: np.ndarray
    call_oi: np.ndarray
    put_oi: np.ndarray

    totals: dict = field(default_factory=dict)
    levels: dict[str, Level] = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    regime: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "spot": self.spot,
            "instrument": self.instrument,
            "contracts": self.contracts,
            "strikes": self.strikes.tolist(),
            "gex": self.gex.tolist(),
            "dex": self.dex.tolist(),
            "vanx": self.vanx.tolist(),
            "cex": self.cex.tolist(),
            "vex": self.vex.tolist(),
            "thex": self.thex.tolist(),
            "call_gex": self.call_gex.tolist(),
            "put_gex": self.put_gex.tolist(),
            "call_volume": self.call_volume.tolist(),
            "put_volume": self.put_volume.tolist(),
            "call_oi": self.call_oi.tolist(),
            "put_oi": self.put_oi.tolist(),
            "totals": self.totals,
            "levels": {k: v.to_dict() for k, v in self.levels.items()},
            "diagnostics": self.diagnostics,
            "regime": self.regime,
        }


def _dealer_weights(right: np.ndarray, convention: DealerSign) -> np.ndarray:
    if convention == "long_calls_short_puts":
        return np.where(right > 0, 1.0, -1.0)
    if convention == "short_calls_long_puts":
        return np.where(right > 0, -1.0, 1.0)
    if convention == "all_long":
        return np.ones_like(right)
    raise ValueError(f"unknown dealer_sign {convention!r}")


def _per_contract_exposure(
    chain: OptionChain, cfg: ExposureConfig, spot: float | None = None
) -> dict[str, np.ndarray]:
    """Signed dealer exposure per contract, in the units documented above."""
    s = chain.spot if spot is None else float(spot)
    q = cfg.effective_dividend_yield(chain.instrument)
    g = chain.greeks(spot=s, r=cfg.risk_free_rate, q=q)

    mult = chain.instrument.multiplier
    oi = chain.open_interest
    w = _dealer_weights(chain.right, cfg.dealer_sign)
    base = w * oi * mult

    return {
        "gex": base * g.gamma * s * s * 0.01,
        "dex": base * g.delta * s,
        "vanx": base * g.vanna * s / 100.0,
        "cex": base * g.charm * s / 365.0,
        "vex": base * g.vega / 100.0,
        "thex": base * g.theta / 365.0,
    }


def total_gex_at_spot(chain: OptionChain, cfg: ExposureConfig, spot: float) -> float:
    """Aggregate dealer GEX if spot were ``spot``.

    Holds each contract's IV and time-to-expiry fixed -- a sticky-strike
    assumption. That is the standard simplification for flip solving, but it
    is a real one: in a selloff the surface repositions and the true flip
    moves relative to what this predicts.
    """
    return float(_per_contract_exposure(chain, cfg, spot)["gex"].sum())


def gamma_flip(
    chain: OptionChain, cfg: ExposureConfig, per_strike_gex: np.ndarray, strikes: np.ndarray
) -> tuple[float | None, str]:
    """Locate the zero-gamma level.

    Two defensible definitions, and they do not agree:

    ``spot_solve`` (default)
        Root-find over spot: the price at which *total* dealer GEX would be
        zero, re-pricing every contract's gamma at each candidate. This is
        the quantity people mean when they say "below the flip, dealers are
        short gamma".

    ``cumulative``
        The strike at which the running sum of per-strike GEX crosses zero.
        Cheaper, and it falls straight out of a groupby, but it answers a
        different question -- it ignores that gamma itself moves with spot.
    """
    if cfg.flip_method == "cumulative":
        order = np.argsort(strikes)
        ks, cum = strikes[order], np.cumsum(per_strike_gex[order])
        sign_change = np.where(np.sign(cum[:-1]) != np.sign(cum[1:]))[0]
        if len(sign_change) == 0:
            return None, "cumulative"
        i = sign_change[0]
        y0, y1 = cum[i], cum[i + 1]
        if y1 == y0:
            return float(ks[i]), "cumulative"
        return float(ks[i] - y0 * (ks[i + 1] - ks[i]) / (y1 - y0)), "cumulative"

    # spot_solve
    span = cfg.flip_search_span
    grid = np.linspace(chain.spot * (1 - span), chain.spot * (1 + span), 121)
    curve = np.array([total_gex_at_spot(chain, cfg, s) for s in grid])

    crossings = np.where(np.sign(curve[:-1]) != np.sign(curve[1:]))[0]
    if len(crossings) == 0:
        return None, "spot_solve"

    # Nearest crossing to current spot -- with multiple flips the relevant
    # one is the one price would reach first.
    mids = 0.5 * (grid[crossings] + grid[crossings + 1])
    i = crossings[int(np.argmin(np.abs(mids - chain.spot)))]

    lo, hi = grid[i], grid[i + 1]
    f_lo = curve[i]
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        f_mid = total_gex_at_spot(chain, cfg, mid)
        if np.sign(f_mid) == np.sign(f_lo):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return float(0.5 * (lo + hi)), "spot_solve"


def max_pain(chain: OptionChain, expiry: np.datetime64 | None = None) -> tuple[float | None, np.datetime64 | None]:
    """Strike minimising total intrinsic payout to option holders.

    Computed for a *single* expiry. Pooling strikes across expiries produces
    a number with no interpretation, so when ``expiry`` is omitted this uses
    the nearest expiry with open interest and returns which one it used.
    """
    if len(chain) == 0:
        return None, None

    if expiry is None:
        candidates = [e for e in chain.expiries() if chain.open_interest[chain.expiry == e].sum() > 0]
        if not candidates:
            return None, None
        expiry = candidates[0]

    mask = chain.expiry == expiry
    if not mask.any():
        return None, expiry

    k = chain.strike[mask]
    oi = chain.open_interest[mask]
    is_call = chain.right[mask] > 0

    settle = np.unique(k)
    # payout[i] = sum over contracts of intrinsic value at settle[i]
    diff = settle[:, None] - k[None, :]
    call_pay = np.where(is_call[None, :], np.maximum(diff, 0.0), 0.0)
    put_pay = np.where(~is_call[None, :], np.maximum(-diff, 0.0), 0.0)
    total = ((call_pay + put_pay) * oi[None, :]).sum(axis=1)

    return float(settle[int(np.argmin(total))]), expiry


def gamma_regime(total_gex: float, spot: float, flip: float | None) -> dict:
    """Classify the dealer gamma regime from GEX sign and spot vs the flip.

    This is **our own rule**, not a reproduction of any commercial
    dashboard's proprietary label, and the UI says so where it is displayed.
    It is derived entirely from tier-A quantities, so unlike the vendor
    labels it is reproducible and auditable:

    * **Long gamma** (GEX > 0) -- dealers hedge *against* the move: they sell
      into strength and buy weakness. Associated with suppressed realised
      volatility and price pinning toward large-gamma strikes.
    * **Short gamma** (GEX < 0) -- dealers hedge *with* the move, amplifying
      it. Associated with trend extension and higher realised volatility.
    * The **flip** is where total GEX would cross zero, so which side of it
      spot sits on says whether the regime is entrenched or fragile.

    None of this is a prediction. It is a restatement of the exposure numbers
    in words, and it inherits every assumption behind them -- above all the
    dealer sign convention, which decides the sign of GEX in the first place.
    """
    long_gamma = total_gex > 0
    if flip is None:
        side = "no flip in range"
        entrenched = True
    else:
        above = spot > flip
        side = "above flip" if above else "below flip"
        # Long gamma above the flip, or short gamma below it, is the
        # self-consistent case; the mixed cases sit near a regime boundary.
        entrenched = above == long_gamma

    if long_gamma and entrenched:
        label, gloss = "SUPPRESSED", "Dealers dampen moves — mean reversion toward walls"
    elif long_gamma:
        label, gloss = "TRANSITIONAL", "Long gamma but below the flip — regime is fragile"
    elif entrenched:
        label, gloss = "AMPLIFIED", "Dealers hedge with the move — trends extend"
    else:
        label, gloss = "TRANSITIONAL", "Short gamma but above the flip — regime is fragile"

    return {
        "label": label,
        "gloss": gloss,
        "gamma_sign": "long" if long_gamma else "short",
        "position": side,
        "entrenched": entrenched,
        "derivation": "gexdash rule: sign(GEX) x (spot vs gamma flip)",
    }


def compute(chain: OptionChain, cfg: ExposureConfig | None = None) -> ExposureResult:
    """Full tier-A exposure surface and level set."""
    cfg = cfg or ExposureConfig()

    full = chain
    in_window = chain.filter_by_dte(cfg.max_dte())

    # Drop settled contracts before anything else -- see min_dte_days.
    live_mask = in_window.dte > cfg.min_dte_days
    expired_count = int(len(in_window) - live_mask.sum())
    had_zero_dte = bool(np.any(in_window.dte < 1.0))
    chain = in_window.subset(live_mask)
    spot = chain.spot

    if len(chain) == 0:
        raise ValueError(
            f"no live contracts within expiry window {cfg.expiry_window!r} "
            f"({len(full)} in chain, {len(in_window)} in window, "
            f"{expired_count} expired at or below {cfg.min_dte_days} DTE)"
        )

    per = _per_contract_exposure(chain, cfg)

    strikes, inv = np.unique(chain.strike, return_inverse=True)
    n = len(strikes)

    def by_strike(values: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
        v = values if mask is None else np.where(mask, values, 0.0)
        return np.bincount(inv, weights=v, minlength=n)

    calls = chain.is_call
    puts = chain.is_put

    gex = by_strike(per["gex"])
    result = ExposureResult(
        spot=spot,
        instrument=chain.instrument.key,
        contracts=len(chain),
        strikes=strikes,
        gex=gex,
        dex=by_strike(per["dex"]),
        vanx=by_strike(per["vanx"]),
        cex=by_strike(per["cex"]),
        vex=by_strike(per["vex"]),
        thex=by_strike(per["thex"]),
        call_gex=by_strike(per["gex"], calls),
        put_gex=by_strike(per["gex"], puts),
        call_volume=by_strike(chain.volume, calls),
        put_volume=by_strike(chain.volume, puts),
        call_oi=by_strike(chain.open_interest, calls),
        put_oi=by_strike(chain.open_interest, puts),
    )

    # ---------------- totals ----------------
    call_oi_sum = float(chain.open_interest[calls].sum())
    put_oi_sum = float(chain.open_interest[puts].sum())
    call_vol_sum = float(chain.volume[calls].sum())
    put_vol_sum = float(chain.volume[puts].sum())

    result.totals = {
        "gex": float(per["gex"].sum()),
        "dex": float(per["dex"].sum()),
        "vanx": float(per["vanx"].sum()),
        "cex": float(per["cex"].sum()),
        "vex": float(per["vex"].sum()),
        "thex": float(per["thex"].sum()),
        "call_oi": call_oi_sum,
        "put_oi": put_oi_sum,
        "call_volume": call_vol_sum,
        "put_volume": put_vol_sum,
        "pc_oi_ratio": (put_oi_sum / call_oi_sum) if call_oi_sum else None,
        "pc_volume_ratio": (put_vol_sum / call_vol_sum) if call_vol_sum else None,
    }

    # ---------------- levels ----------------
    def level(name: str, k: float | None, v: float | None) -> Level:
        if k is None:
            return Level(name, None)
        return Level(name, float(k), None if v is None else float(v), float(k - spot), float((k - spot) / spot * 100.0))

    # Walls are the largest single-strike gamma concentrations on each side,
    # measured on absolute exposure so the dealer-sign convention cannot flip
    # which strike is selected.
    call_wall_i = int(np.argmax(np.abs(result.call_gex))) if np.any(result.call_gex) else None
    put_wall_i = int(np.argmax(np.abs(result.put_gex))) if np.any(result.put_gex) else None
    max_pos_i = int(np.argmax(gex)) if len(gex) else None
    max_neg_i = int(np.argmin(gex)) if len(gex) else None

    levels: dict[str, Level] = {}
    if call_wall_i is not None:
        levels["call_wall"] = level("Call Wall", strikes[call_wall_i], result.call_gex[call_wall_i])
    if put_wall_i is not None:
        levels["put_wall"] = level("Put Wall", strikes[put_wall_i], result.put_gex[put_wall_i])
    if max_pos_i is not None:
        levels["max_pos_gex"] = level("Max +GEX", strikes[max_pos_i], gex[max_pos_i])
    if max_neg_i is not None:
        levels["max_neg_gex"] = level("Max -GEX", strikes[max_neg_i], gex[max_neg_i])

    flip_price, flip_method = gamma_flip(chain, cfg, gex, strikes)
    levels["gamma_flip"] = level("Gamma Flip", flip_price, None)

    mp_strike, mp_expiry = max_pain(chain)
    levels["max_pain"] = level("Max Pain", mp_strike, None)

    # Highest option volume by strike -- the "volume POC" tile. This is
    # cumulative session volume from the chain snapshot, not a classified
    # trade tape.
    total_vol = result.call_volume + result.put_volume
    if total_vol.sum() > 0:
        poc_i = int(np.argmax(total_vol))
        levels["volume_poc"] = level("Volume POC", strikes[poc_i], total_vol[poc_i])

    result.levels = levels
    result.regime = gamma_regime(result.totals["gex"], spot, flip_price)

    # ---------------- diagnostics ----------------
    front = chain.expiries()[0] if len(chain.expiries()) else None
    abs_gex_total = float(np.abs(per["gex"]).sum())
    front_share = None
    if front is not None and abs_gex_total > 0:
        front_mask = chain.expiry == front
        front_share = float(np.abs(per["gex"][front_mask]).sum() / abs_gex_total * 100.0)

    # Share of gross gamma sitting in contracts expiring today. This is the
    # "GEX concentration" tile: high means the surface is dominated by
    # contracts that will not exist tomorrow.
    zero_dte_mask = chain.dte < 1.0
    zero_dte_share = (
        float(np.abs(per["gex"][zero_dte_mask]).sum() / abs_gex_total * 100.0)
        if abs_gex_total > 0
        else None
    )

    result.diagnostics = {
        "expiry_window": cfg.expiry_window,
        "expiry_window_days": cfg.max_dte(),
        "min_dte_days": cfg.min_dte_days,
        "expired_excluded": expired_count,
        # Mirrors the reference dashboard's "0DTE EXPIRED" state: the chain
        # listed contracts expiring today, and every one of them has settled.
        "zero_dte_expired": bool(had_zero_dte and not np.any(zero_dte_mask)),
        "zero_dte_gex_share_pct": zero_dte_share,
        "dealer_sign": cfg.dealer_sign,
        "flip_method": flip_method,
        "risk_free_rate": cfg.risk_free_rate,
        "dividend_yield": cfg.effective_dividend_yield(chain.instrument),
        "dividend_yield_source": "override" if cfg.dividend_yield is not None else "instrument default",
        # Exercise style is read, not decorative. SPY/QQQ are American and the
        # greeks here are European -- stated rather than buried.
        "exercise_style": chain.instrument.exercise_style,
        # Per-model: the equity and futures cases differ structurally, not
        # just in magnitude. See american.approximation_note.
        "greeks_approximation": approximation_note(chain.instrument),
        "pricing_model": "black76" if chain.instrument.is_future else "black_scholes",
        "multiplier": chain.instrument.multiplier,
        "contracts_in_window": len(chain),
        "contracts_in_chain": len(full),
        "expiries_in_window": len(chain.expiries()),
        "front_expiry": str(front) if front is not None else None,
        "front_expiry_gex_share_pct": front_share,
        "max_pain_expiry": str(mp_expiry) if mp_expiry is not None else None,
        "oi_asof": chain.oi_asof.isoformat() if chain.oi_asof else None,
        "asof": chain.asof.isoformat(),
        "min_dte": float(chain.dte.min()),
        "max_dte": float(chain.dte.max()),
    }

    return result
