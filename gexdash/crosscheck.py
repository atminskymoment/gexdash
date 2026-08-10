"""Diff our greeks against the provider's.

Why this exists
---------------
Vanna and charm are not published by any retail provider, so we compute them
ourselves. Nothing downstream cross-checks them: a wrong risk-free rate or
dividend yield produces a VANX and CEX profile that is smooth, plausible, and
wrong, and no test of *our* code would catch it because our code would be
doing exactly what it was told.

Delta and gamma, however, the provider does publish. Since all six greeks come
out of the same Black-Scholes call with the same r and q, agreement on delta
and gamma is evidence the inputs are right, which is the only handle we have
on the two we cannot verify directly.

Run it against a live chain::

    python -m gexdash.crosscheck --symbol SPX --provider schwab

A mean absolute delta error above ~0.01 usually means the rate or dividend
assumption is off, not that the model is wrong.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import numpy as np

from .chain import OptionChain


@dataclass
class CrossCheckReport:
    contracts: int
    compared: int
    delta_mae: float | None
    delta_max: float | None
    gamma_mae: float | None
    gamma_max: float | None
    gamma_rel_mae: float | None
    rate: float
    dividend: float

    @property
    def ok(self) -> bool:
        """Loose thresholds -- providers round, and quote asynchronously."""
        if self.delta_mae is None:
            return False
        return self.delta_mae < 0.01 and (self.gamma_rel_mae is None or self.gamma_rel_mae < 0.05)

    def __str__(self) -> str:
        if self.compared == 0:
            return "no provider greeks available to compare against"
        return (
            f"compared {self.compared}/{self.contracts} contracts "
            f"at r={self.rate:.4f} q={self.dividend:.4f}\n"
            f"  delta  MAE {self.delta_mae:.6f}   max {self.delta_max:.6f}\n"
            f"  gamma  MAE {self.gamma_mae:.8f}   max {self.gamma_max:.8f}"
            f"   relative MAE {self.gamma_rel_mae:.4%}\n"
            f"  verdict: {'PASS' if self.ok else 'MISMATCH -- check r / q assumptions'}"
        )


def cross_check(chain: OptionChain, *, r: float = 0.042, q: float | None = None) -> CrossCheckReport:
    """Compare self-computed delta/gamma against the provider's.

    ``q = None`` resolves the instrument's own default, exactly as
    :class:`~gexdash.exposure.ExposureConfig` does. Hardcoding a yield here
    would mean the tool built to catch a wrong dividend was itself supplying
    one -- on QQQ (0.5%) a fixed 1.3% would report a mismatch the engine
    never actually has.
    """
    if q is None:
        q = chain.instrument.default_dividend_yield
    if chain.provider_delta is None and chain.provider_gamma is None:
        return CrossCheckReport(len(chain), 0, None, None, None, None, None, r, q)

    ours = chain.greeks(r=r, q=q)

    # Only compare contracts where the provider actually supplied a value and
    # the contract is live and has a usable IV.
    usable = (chain.iv > 0) & (chain.dte > 0)
    if chain.provider_delta is not None:
        usable &= np.isfinite(chain.provider_delta)
    if chain.provider_gamma is not None:
        usable &= np.isfinite(chain.provider_gamma)

    n = int(usable.sum())
    if n == 0:
        return CrossCheckReport(len(chain), 0, None, None, None, None, None, r, q)

    d_mae = d_max = g_mae = g_max = g_rel = None

    if chain.provider_delta is not None:
        err = np.abs(ours.delta[usable] - chain.provider_delta[usable])
        d_mae, d_max = float(err.mean()), float(err.max())

    if chain.provider_gamma is not None:
        theirs = chain.provider_gamma[usable]
        err = np.abs(ours.gamma[usable] - theirs)
        g_mae, g_max = float(err.mean()), float(err.max())
        scale = np.maximum(np.abs(theirs), 1e-12)
        g_rel = float(np.mean(err / scale))

    return CrossCheckReport(len(chain), n, d_mae, d_max, g_mae, g_max, g_rel, r, q)


def fit_rate(chain: OptionChain, *, q: float | None = None, lo: float = -0.02, hi: float = 0.12) -> float:
    """Find the risk-free rate that best reproduces the provider's deltas.

    Useful for calibration: if this lands far from the rate you configured,
    your configured rate is the thing that is wrong.
    """
    if chain.provider_delta is None:
        raise ValueError("chain carries no provider deltas to fit against")
    if q is None:
        q = chain.instrument.default_dividend_yield

    grid = np.linspace(lo, hi, 141)
    errs = [cross_check(chain, r=float(r), q=q).delta_mae or np.inf for r in grid]
    return float(grid[int(np.argmin(errs))])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbol", default="SPX")
    ap.add_argument("--provider", default=None, help="mock | schwab (default: $GEXDASH_PROVIDER)")
    ap.add_argument("--rate", type=float, default=0.042)
    ap.add_argument(
        "--dividend", type=float, default=None,
        help="dividend yield; omit to use the instrument default",
    )
    ap.add_argument("--fit-rate", action="store_true", help="also solve for the best-fit rate")
    args = ap.parse_args(argv)

    from . import instruments
    from .providers import build_provider

    inst = instruments.get(args.symbol)
    chain = build_provider(args.provider).get_chain(inst, max_dte=60)

    report = cross_check(chain, r=args.rate, q=args.dividend)
    print(report)

    if report.compared == 0:
        print(
            "\nThis provider does not supply greeks, so there is nothing to check against.\n"
            "The Schwab chain endpoint does -- run with --provider schwab.",
            file=sys.stderr,
        )
        return 0

    if args.fit_rate:
        print(f"\n  best-fit rate: {fit_rate(chain, q=args.dividend):.4f} (configured {args.rate:.4f})")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
