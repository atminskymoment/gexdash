"""American exercise: a Cox-Ross-Rubinstein binomial, used to bound the error.

SPY and QQQ options are American on a dividend-paying ETF, so early exercise
has value that Black-Scholes does not price. The exposure engine still uses
the European greeks — a binomial per contract per spot tick is not viable when
``spot_solve`` alone does ~140 full chain re-pricings — but "it's an
approximation" is only an honest statement if you know how large the error is.

So this module exists to *measure* it. ``tests/test_american.py`` runs it
across a realistic SPY/QQQ grid and asserts a bound on the delta and gamma
error, which is the number quoted in the README and surfaced in the UI's
provenance panel.

Where the approximation is weakest is worth knowing: early exercise value
concentrates in deep ITM puts, which is exactly where put-wall detection
looks. Near the money and out of the money — where the overwhelming majority
of open interest sits — the two agree closely.
"""

from __future__ import annotations

import numpy as np

from .greeks import CALL, PUT

__all__ = [
    "binomial_american",
    "early_exercise_premium",
    "max_greek_error",
    "approximation_note",
]


# The two notes below quote bounds *measured* by this module, not assumed.
# They live here so the wording and the code that produces the number cannot
# drift apart. tests/test_american.py pins both.
_EQUITY_NOTE = (
    "American exercise priced with European greeks. On a dividend-paying "
    "underlying (q < r) the early-exercise premium concentrates in ITM puts: "
    "delta error within 0.02 inside +/-5% of spot, up to 0.05 at +/-10% and "
    "60 DTE."
)

_FUTURES_NOTE = (
    "American exercise priced with European greeks. On futures options the "
    "drift term is q = r, so the early-exercise premium is near-symmetric "
    "between ITM calls and ITM puts rather than put-concentrated, and the "
    "worst case is usually an ITM call. The error is smaller than the equity "
    "case: delta within 0.004 inside +/-5% of spot, up to 0.012 at +/-10% and "
    "60 DTE."
)


def approximation_note(instrument) -> str | None:
    """The disclosure string for an instrument, or None if European.

    Two notes, not one. The equity sentence ("concentrates in ITM puts") is a
    structural claim that is **false** for futures options: with q = r both
    sides carry premium. Emitting the equity wording for ES/NQ would state
    something measurably untrue.
    """
    if instrument.european:
        return None
    return _FUTURES_NOTE if instrument.is_future else _EQUITY_NOTE


def binomial_american(
    s: float,
    k: float,
    t: float,
    sigma: float,
    right: int,
    r: float = 0.0,
    q: float = 0.0,
    steps: int = 400,
) -> dict[str, float]:
    """Price an American option and read delta/gamma off the tree.

    Cox-Ross-Rubinstein. ``steps`` trades accuracy for time; 400 is ample for
    the error-bounding this is used for.
    """
    if t <= 0 or sigma <= 0:
        intrinsic = max(0.0, (s - k) if right > 0 else (k - s))
        return {"price": intrinsic, "delta": float(right if intrinsic > 0 else 0), "gamma": 0.0}

    dt = t / steps
    u = np.exp(sigma * np.sqrt(dt))
    d = 1.0 / u
    disc = np.exp(-r * dt)
    p = (np.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)  # guard extreme parameter combinations

    # Terminal asset prices: s * u^j * d^(steps-j)
    j = np.arange(steps + 1)
    prices = s * u**j * d ** (steps - j)
    values = np.maximum(0.0, (prices - k) if right > 0 else (k - prices))

    # Roll back, testing early exercise at every node.
    node_s = prices
    for step in range(steps - 1, -1, -1):
        node_s = node_s[1:] * d  # asset prices one level earlier
        values = disc * (p * values[1:] + (1 - p) * values[:-1])
        intrinsic = np.maximum(0.0, (node_s - k) if right > 0 else (k - node_s))
        values = np.maximum(values, intrinsic)

        if step == 2:
            v2, s2 = values.copy(), node_s.copy()
        if step == 1:
            v1, s1 = values.copy(), node_s.copy()

    price = float(values[0])

    # delta from the two step-1 nodes; gamma from the three step-2 nodes.
    delta = float((v1[1] - v1[0]) / (s1[1] - s1[0]))
    d_up = (v2[2] - v2[1]) / (s2[2] - s2[1])
    d_dn = (v2[1] - v2[0]) / (s2[1] - s2[0])
    gamma = float((d_up - d_dn) / (0.5 * (s2[2] - s2[0])))

    return {"price": price, "delta": delta, "gamma": gamma}


def early_exercise_premium(
    s: float, k: float, t: float, sigma: float, right: int, r: float, q: float, steps: int = 400
) -> float:
    """American price minus European price, in currency units."""
    from .greeks import black_scholes

    euro = float(
        black_scholes(
            np.array([s]), np.array([k]), np.array([t]), np.array([sigma]),
            np.array([right]), r=r, q=q,
        ).price[0]
    )
    return binomial_american(s, k, t, sigma, right, r, q, steps)["price"] - euro


def max_greek_error(
    spot: float,
    strikes: np.ndarray,
    t: float,
    sigma: float,
    r: float,
    q: float,
    steps: int = 300,
) -> dict[str, float]:
    """Worst-case delta/gamma disagreement across a strike grid.

    Returns the maxima plus where they occur, so the answer is actionable
    rather than a bare number.
    """
    from .greeks import black_scholes

    worst = {"delta": 0.0, "gamma": 0.0, "delta_at": None, "gamma_at": None}

    for k in strikes:
        for right in (CALL, PUT):
            amer = binomial_american(spot, float(k), t, sigma, right, r, q, steps)
            euro = black_scholes(
                np.array([spot]), np.array([k]), np.array([t]), np.array([sigma]),
                np.array([right]), r=r, q=q,
            )
            d_err = abs(amer["delta"] - float(euro.delta[0]))
            g_err = abs(amer["gamma"] - float(euro.gamma[0]))
            if d_err > worst["delta"]:
                worst["delta"] = d_err
                worst["delta_at"] = (float(k), "C" if right > 0 else "P")
            if g_err > worst["gamma"]:
                worst["gamma"] = g_err
                worst["gamma_at"] = (float(k), "C" if right > 0 else "P")

    return worst
