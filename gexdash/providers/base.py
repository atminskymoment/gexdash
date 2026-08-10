"""Provider interface.

Tier A needs exactly two things from a data source:

* one option chain per day (open interest is an OCC once-daily figure, so
  pulling it more often buys nothing), and
* a live spot price.

Keeping the surface this small is deliberate: it means swapping Schwab for
ThetaData or Databento later is a single class, and it keeps the door open to
the entitlement change that comes with going from personal to redistributed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..chain import OptionChain
from ..instruments import Instrument


@runtime_checkable
class ChainProvider(Protocol):
    """Source of option chains and spot prices."""

    name: str

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None) -> OptionChain:
        """Fetch the current chain, including OCC open interest."""
        ...

    def get_spot(self, instrument: Instrument) -> float:
        """Fetch the current underlying price."""
        ...


class ProviderError(RuntimeError):
    """Raised when a provider cannot serve a request."""


class NotAuthenticated(ProviderError):
    """Raised when credentials are missing or the token chain has expired."""
