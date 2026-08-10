"""Market internals from the exchange breadth symbols.

thinkorswim (and the Schwab API behind it) publishes breadth as ordinary
quotable symbols, so a whole market-internals panel costs **one batched quote
request** rather than 500 per-constituent quotes.

Two things to be clear about before reading the numbers:

**This is exchange-wide breadth, not index-constituent breadth.** ``$ADVN`` /
``$DECN`` count every issue on the NYSE — roughly 3,000 lines including ETFs,
preferreds and closed-end funds — not the 503 members of the S&P 500. The
Nasdaq pair is the same for the Nasdaq composite versus the Nasdaq-100. Using
NYSE breadth alongside an SPX exposure surface is the standard trader
convention, and it is still a proxy: many S&P 500 members are Nasdaq-listed.
The UI names the exchange rather than the index for this reason.

**Every field is independently optional.** The exact tickers for the less
common internals vary, so an unresolved symbol degrades that one metric and is
reported by name — it never fails the panel. If a metric you want is missing,
the symbol in :data:`BREADTH_SYMBOLS` is the thing to correct.

Advancing and declining are fetched as *separate counts* rather than derived
from the net symbol, so the panel can show both sides. The net symbol is
fetched too and used as a **cross-check**: if ``advancing - declining`` does
not match it, the symbol mapping is wrong. That is the failure mode this
project keeps hitting — plausible numbers from a wrong input — so it is
checked rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

__all__ = ["BREADTH_SYMBOLS", "BreadthSnapshot", "build_snapshot", "symbols_for_family"]


#: Breadth symbol map, keyed by the exchange each instrument family trades
#: against. Edit here if a ticker does not resolve on your entitlement — the
#: payload reports which symbols failed by name.
BREADTH_SYMBOLS: dict[str, dict[str, str]] = {
    "NYSE": {
        "advancing": "$ADVN",
        "declining": "$DECN",
        "net": "$ADD",          # advancing - declining; used as a cross-check
        "up_volume": "$UVOL",
        "down_volume": "$DVOL",
        "tick": "$TICK",
        "trin": "$TRIN",
    },
    "NASDAQ": {
        "advancing": "$ADVQ",
        "declining": "$DECQ",
        "net": "$ADQDC",
        "up_volume": "$UVOLQ",
        "down_volume": "$DVOLQ",
        "tick": "$TICKQ",
        "trin": "$TRINQ",
    },
}

#: Which exchange's breadth belongs beside which instrument family. Keyed off
#: ``instruments.FAMILIES`` rather than a second instrument list.
FAMILY_EXCHANGE: dict[str, str] = {
    "S&P 500": "NYSE",
    "Nasdaq-100": "NASDAQ",
}


def symbols_for_family(family: str) -> tuple[str, dict[str, str]]:
    """(exchange, {metric: symbol}) for an instrument family."""
    exchange = FAMILY_EXCHANGE.get(family, "NYSE")
    return exchange, BREADTH_SYMBOLS[exchange]


@dataclass
class BreadthSnapshot:
    """One point-in-time reading of exchange breadth."""

    exchange: str
    asof: datetime

    advancing: float | None = None
    declining: float | None = None
    up_volume: float | None = None
    down_volume: float | None = None
    tick: float | None = None
    trin: float | None = None

    #: The exchange's own net figure, kept for the cross-check below.
    net_reported: float | None = None

    #: Symbols that did not resolve, by metric name.
    unresolved: dict[str, str] = field(default_factory=dict)
    #: Set when advancing - declining disagrees with the net symbol.
    reconciliation_warning: str | None = None

    # ------------------------------------------------------------------

    @property
    def total_issues(self) -> float | None:
        if self.advancing is None or self.declining is None:
            return None
        return self.advancing + self.declining

    @property
    def net(self) -> float | None:
        """Advancing minus declining, computed from the counts we display.

        Deliberately derived rather than taken from the net symbol: the panel
        shows both counts, so the net it displays must be the one those counts
        imply. ``net_reported`` is what the exchange publishes, and the two
        are compared in :func:`build_snapshot`.
        """
        if self.advancing is None or self.declining is None:
            return self.net_reported
        return self.advancing - self.declining

    @property
    def ad_ratio(self) -> float | None:
        if self.advancing is None or not self.declining:
            return None
        return self.advancing / self.declining

    @property
    def volume_ratio(self) -> float | None:
        if self.up_volume is None or not self.down_volume:
            return None
        return self.up_volume / self.down_volume

    @property
    def label(self) -> str:
        """POSITIVE / NEGATIVE / NEUTRAL from the A/D ratio."""
        r = self.ad_ratio
        if r is None:
            return "UNKNOWN"
        if r >= 1.5:
            return "POSITIVE"
        if r <= 1 / 1.5:
            return "NEGATIVE"
        return "NEUTRAL"

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "asof": self.asof.isoformat(),
            "advancing": self.advancing,
            "declining": self.declining,
            "total_issues": self.total_issues,
            "net": self.net,
            "net_reported": self.net_reported,
            "ad_ratio": self.ad_ratio,
            "up_volume": self.up_volume,
            "down_volume": self.down_volume,
            "volume_ratio": self.volume_ratio,
            "tick": self.tick,
            "trin": self.trin,
            "label": self.label,
            "unresolved": self.unresolved,
            "reconciliation_warning": self.reconciliation_warning,
            # Named so nobody reads this as S&P 500 / Nasdaq-100 membership.
            "scope": f"{self.exchange} exchange-wide issues, not index constituents",
        }


def build_snapshot(
    exchange: str,
    values: dict[str, float | None],
    *,
    unresolved: dict[str, str] | None = None,
    asof: datetime | None = None,
    tolerance: float = 2.0,
) -> BreadthSnapshot:
    """Assemble a snapshot and run the net cross-check.

    ``values`` maps metric name -> value; ``None`` means the symbol resolved
    but carried no usable figure.
    """
    snap = BreadthSnapshot(
        exchange=exchange,
        asof=asof or datetime.now(timezone.utc),
        advancing=values.get("advancing"),
        declining=values.get("declining"),
        up_volume=values.get("up_volume"),
        down_volume=values.get("down_volume"),
        tick=values.get("tick"),
        trin=values.get("trin"),
        net_reported=values.get("net"),
        unresolved=dict(unresolved or {}),
    )

    # Cross-check: the counts we show must imply the net the exchange
    # publishes. A mismatch means a symbol is mapped to the wrong series --
    # which would otherwise produce entirely plausible numbers.
    if snap.advancing is not None and snap.declining is not None and snap.net_reported is not None:
        derived = snap.advancing - snap.declining
        if abs(derived - snap.net_reported) > tolerance:
            snap.reconciliation_warning = (
                f"{exchange}: advancing - declining = {derived:,.0f} but the net symbol "
                f"reports {snap.net_reported:,.0f}. One of these symbols is mapped to the "
                f"wrong series -- check BREADTH_SYMBOLS[{exchange!r}]."
            )

    return snap
