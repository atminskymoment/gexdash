"""Vectorized option greeks.

One Black-Scholes core serves both instruments we care about:

    SPX / SPXW  -> European index options.  Pass the index level as `s` and the
                   continuous dividend yield as `q`.
    ES options  -> Options on the E-mini future.  Black-76 is *identical* to
                   Black-Scholes with ``q = r`` and ``s = F`` (the futures
                   price), because F = S*exp((r-q)T) collapses the drift term.
                   ``black76_params`` builds that substitution for you.

Keeping a single code path means a sign error can only be made once, and
``tests/test_greeks.py`` pins every partial against a finite difference.

Everything is numpy-vectorized over contracts. A ~2000-contract chain
re-prices in well under a millisecond, so this can sit under a spot stream.

Conventions
-----------
All greeks are returned in *raw* analytic units (per 1.0 of the underlying,
per 1.0 of vol, per 1.0 year). Scaling to the units traders quote -- vega per
vol point, theta per day -- happens in ``exposure.py`` so that the scaling
choice is visible in one place rather than baked in here.

``charm`` is d(delta)/dt with t running *forward* in calendar time, i.e.
-d(delta)/dT. A call struck ATM has negative charm: its delta decays toward
0.5 as expiry approaches.
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "norm_cdf",
    "norm_pdf",
    "d1_d2",
    "black_scholes",
    "black76_params",
    "Greeks",
    "CALL",
    "PUT",
]

CALL = 1
PUT = -1

# scipy is not a dependency. np.vectorize over math.erf clocks ~0.14ms for
# 2000 elements, which is far below the latency budget for a spot tick.
_erf = np.vectorize(math.erf, otypes=[float])

_SQRT2 = math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)

# Below these thresholds the analytic greeks are singular (division by
# sigma*sqrt(T)). We clamp rather than emit inf/nan so that an expired or
# zero-bid contract degrades to "no exposure" instead of poisoning a sum.
_MIN_T = 1.0 / (365.0 * 24.0 * 60.0)  # one minute, in years
_MIN_SIGMA = 1e-6


def norm_cdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF, vectorized."""
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / _SQRT2))


def norm_pdf(x: np.ndarray) -> np.ndarray:
    """Standard normal PDF, vectorized."""
    x = np.asarray(x, dtype=float)
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


def d1_d2(
    s: np.ndarray,
    k: np.ndarray,
    t: np.ndarray,
    r: np.ndarray,
    q: np.ndarray,
    sigma: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (d1, d2, sigma*sqrt(t)) with degenerate inputs clamped."""
    s = np.asarray(s, dtype=float)
    k = np.asarray(k, dtype=float)
    t = np.maximum(np.asarray(t, dtype=float), _MIN_T)
    r = np.asarray(r, dtype=float)
    q = np.asarray(q, dtype=float)
    sigma = np.maximum(np.asarray(sigma, dtype=float), _MIN_SIGMA)

    sqrt_t = np.sqrt(t)
    vol_t = sigma * sqrt_t
    d1 = (np.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    d2 = d1 - vol_t
    return d1, d2, vol_t


class Greeks:
    """Container for a vectorized greek calculation.

    Attributes are numpy arrays aligned with the input contract arrays.
    """

    __slots__ = (
        "price",
        "delta",
        "gamma",
        "vega",
        "theta",
        "vanna",
        "charm",
        "d1",
        "d2",
    )

    def __init__(self, **kw) -> None:
        for name in self.__slots__:
            setattr(self, name, kw[name])

    def as_dict(self) -> dict[str, np.ndarray]:
        """All nine arrays by attribute name, ``d1`` and ``d2`` included."""
        return {name: getattr(self, name) for name in self.__slots__}


def black_scholes(
    s: np.ndarray,
    k: np.ndarray,
    t: np.ndarray,
    sigma: np.ndarray,
    right: np.ndarray,
    r: float | np.ndarray = 0.0,
    q: float | np.ndarray = 0.0,
) -> Greeks:
    """Price and greeks for European options.

    Parameters
    ----------
    s : underlying level. For futures options pass the futures price F.
    k : strike.
    t : time to expiry in years.
    sigma : implied volatility as a decimal (0.20 == 20%).
    right : +1 for calls, -1 for puts (see ``CALL`` / ``PUT``).
    r : risk-free rate, continuously compounded.
    q : continuous dividend yield. Pass ``q = r`` for Black-76 (futures).

    Returns
    -------
    Greeks
        ``delta`` is per 1.0 of underlying, ``vega`` per 1.0 of vol
        (not per vol point), ``theta`` and ``charm`` per year.
    """
    s_a = np.asarray(s, dtype=float)
    k_a = np.asarray(k, dtype=float)
    t_a = np.maximum(np.asarray(t, dtype=float), _MIN_T)
    sig = np.maximum(np.asarray(sigma, dtype=float), _MIN_SIGMA)
    right_a = np.asarray(right, dtype=float)
    r_a = np.asarray(r, dtype=float)
    q_a = np.asarray(q, dtype=float)

    d1, d2, vol_t = d1_d2(s_a, k_a, t_a, r_a, q_a, sig)
    sqrt_t = np.sqrt(t_a)

    disc_q = np.exp(-q_a * t_a)
    disc_r = np.exp(-r_a * t_a)
    pdf_d1 = norm_pdf(d1)

    # N(w*d1) and N(w*d2) where w = +1 for calls, -1 for puts. This is the
    # standard trick that collapses the call and put formulas into one.
    n_w_d1 = norm_cdf(right_a * d1)
    n_w_d2 = norm_cdf(right_a * d2)

    price = right_a * (s_a * disc_q * n_w_d1 - k_a * disc_r * n_w_d2)

    # delta: call = e^{-qT} N(d1); put = e^{-qT} (N(d1) - 1)
    delta = disc_q * (norm_cdf(d1) - np.where(right_a > 0, 0.0, 1.0))

    # gamma and vega are right-independent
    gamma = disc_q * pdf_d1 / (s_a * vol_t)
    vega = s_a * disc_q * pdf_d1 * sqrt_t

    # theta = dV/dt (calendar time forward), so it is negative for long
    # premium in the usual case.
    theta = (
        -(s_a * disc_q * pdf_d1 * sig) / (2.0 * sqrt_t)
        + right_a * q_a * s_a * disc_q * n_w_d1
        - right_a * r_a * k_a * disc_r * n_w_d2
    )

    # vanna = d(delta)/d(sigma) = d2V/dSdSigma. Right-independent.
    vanna = -disc_q * pdf_d1 * d2 / sig

    # charm = d(delta)/dt, t forward. Right-dependent only through the
    # dividend term.
    common = disc_q * pdf_d1 * (2.0 * (r_a - q_a) * t_a - d2 * vol_t) / (2.0 * t_a * vol_t)
    charm = right_a * q_a * disc_q * n_w_d1 - common

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        vanna=vanna,
        charm=charm,
        d1=d1,
        d2=d2,
    )


def black76_params(r: float | np.ndarray) -> dict[str, float | np.ndarray]:
    """Keyword args that turn :func:`black_scholes` into Black-76.

    Options on futures discount at r but carry no dividend drift, which is
    exactly Black-Scholes with q = r. Use with the *futures* price as ``s``::

        g = black_scholes(f, k, t, iv, right, **black76_params(r))
    """
    return {"r": r, "q": r}


def implied_vol(
    target_price: np.ndarray,
    s: np.ndarray,
    k: np.ndarray,
    t: np.ndarray,
    right: np.ndarray,
    r: float | np.ndarray = 0.0,
    q: float | np.ndarray = 0.0,
    *,
    tol: float = 1e-6,
    max_iter: int = 64,
) -> np.ndarray:
    """Solve for implied vol by bisection.

    Only needed when a provider returns prices without IV. Bisection rather
    than Newton because vega collapses for deep wings, and a robust-but-slower
    solve beats a fast one that diverges on the 5-delta strikes that matter
    most for wall detection.
    """
    target = np.asarray(target_price, dtype=float)
    lo = np.full(target.shape, 1e-4)
    hi = np.full(target.shape, 5.0)

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        price = black_scholes(s, k, t, mid, right, r=r, q=q).price
        too_high = price > target
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)
        if np.all(hi - lo < tol):
            break

    return 0.5 * (lo + hi)
