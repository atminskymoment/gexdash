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

from ..bars import PriceBars
from ..breadth import BreadthSnapshot
from ..chain import OptionChain
from ..instruments import Instrument


@runtime_checkable
class ChainProvider(Protocol):
    """Source of option chains and spot prices.

    These two methods are mandatory: everything the exposure engine produces
    depends on them and nothing else.
    """

    name: str

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None) -> OptionChain:
        """Fetch the current chain, including OCC open interest."""
        ...

    def get_spot(self, instrument: Instrument) -> float:
        """Fetch the current underlying price."""
        ...


@runtime_checkable
class PriceHistoryProvider(Protocol):
    """**Optional** capability: intraday OHLCV bars for the price panel.

    Kept separate from :class:`ChainProvider` rather than bolted onto it. The
    price panel is presentation — the exposure surface does not need bars —
    and a provider that cannot serve them must be able to say so without
    failing the whole dashboard. Check with ``isinstance(p, PriceHistoryProvider)``
    and be ready for :class:`PriceHistoryUnavailable` even when it matches.
    """

    def get_price_history(
        self, instrument: Instrument, *, interval_minutes: int = 1, lookback_hours: float = 7.0
    ) -> PriceBars:
        """Fetch recent bars, oldest first.

        Raise :class:`PriceHistoryUnavailable` when this provider cannot serve
        history for this instrument — that is an expected outcome, not a bug.
        """
        ...


@runtime_checkable
class BreadthProvider(Protocol):
    """**Optional** capability: exchange market-internals (advance/decline).

    Same contract as :class:`PriceHistoryProvider` — the exposure surface does
    not need breadth, so a provider that cannot serve it says so and the panel
    states the reason.
    """

    def get_breadth(self, exchange: str) -> BreadthSnapshot:
        """Fetch breadth for ``"NYSE"`` or ``"NASDAQ"``.

        Raise :class:`BreadthUnavailable` when unsupported — an expected
        outcome, not a bug.
        """
        ...


class ProviderError(RuntimeError):
    """Raised when a provider cannot serve a request."""


class NotAuthenticated(ProviderError):
    """Raised when credentials are missing or the token chain has expired."""


class BreadthUnavailable(ProviderError):
    """This provider cannot serve market internals.

    Expected, not a failure: the panel renders the reason. Note the breadth
    symbols are *exchange* series, so an entitlement that covers option chains
    does not necessarily cover them.
    """


class PriceHistoryUnavailable(ProviderError):
    """This provider cannot serve bars for this instrument.

    An expected outcome, not a failure: the UI renders the reason where the
    chart would go. A blank candle panel that reads as "still loading" would
    be the wrong result on a dashboard whose point is not fabricating data.
    """
