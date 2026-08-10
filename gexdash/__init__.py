"""gexdash — tier-A dealer exposure for SPX and ES.

Computes the exposure surface that a once-daily open-interest snapshot can
honestly support: GEX, DEX, VANX, CEX, VEX and THEX by strike, plus call and
put walls, the gamma flip, max pain and volume POC — all re-priced against
live spot on every tick.

What it deliberately does not do is guess at the things that need a live
trade feed. Open interest is published once per day by the OCC; there is no
live OI feed at any price. Anything derived from intraday flow is surfaced as
explicitly unavailable rather than estimated.
"""

__version__ = "0.1.0"

from . import chain, exposure, greeks, instruments  # noqa: F401

__all__ = ["chain", "exposure", "greeks", "instruments", "__version__"]
