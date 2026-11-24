"""gexdash — tier-A dealer exposure for SPX, SPY, ES, NDX, QQQ, NQ and USO,
with a technical chart for QQQ, SPY, USO, ES and NQ.

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

# Load .env BEFORE anything else imports, because instruments.py reads
# os.environ at import time to resolve exercise overrides; loading later would
# leave them bound to the wrong values. (GEXDASH_HOME is looked up on each
# call, by store.default_db_path and the Schwab token path.)
from .config import load_dotenv as _load_dotenv  # noqa: E402

_DOTENV_APPLIED = _load_dotenv()

from . import chain, exposure, greeks, instruments  # noqa: F401,E402

__all__ = ["chain", "exposure", "greeks", "instruments", "__version__"]
