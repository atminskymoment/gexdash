"""Instrument specifications.

Four things differ between these instruments and every one of them silently
corrupts exposure numbers if you get it wrong — the output stays smooth and
plausible, just scaled or shaped incorrectly:

1. **Multiplier.** SPX/NDX are x100, ES is x$50, NQ is x$20, and the ETFs are
   x100 shares. A wrong multiplier is a pure scale error, which is exactly the
   kind that never looks wrong.
2. **Pricing model.** Index options are Black-Scholes on the index with a
   dividend yield. Futures options are Black-76 on the *future*, expressed as
   Black-Scholes with q = r (an exact identity, so there is one code path).
3. **What "spot" means.** For futures the underlying in the greeks is the
   futures price, not the index. They differ by carry and the basis drifts.
4. **Exercise style.** SPX and NDX are European cash-settled index options.
   SPY and QQQ are *American* on a dividend-paying ETF. ES and NQ are CME
   options on futures, whose quarterly and serial contracts are *American*
   (their weeklies and EOM are European). See ``european`` below — that flag
   is read, not decorative.

Specs verified against the exchanges rather than from memory: CME (E-mini
Nasdaq-100 is $20 x index; quarterly options on E-mini futures are
American-style, weeklies European) and Nasdaq/Cboe (NDX is $100, European,
cash-settled, marketed on having no early-assignment risk).

Overriding exercise style
-------------------------
Every instrument's exercise style can be changed without editing this file::

    export GEXDASH_EXERCISE_OVERRIDES="NDX=american,NQ=european"

Useful when your chain is dominated by an expiry type whose rule differs from
the family default — CME weeklies being European is the realistic case.
A malformed entry, an unknown instrument, or an unrecognised style raises at
import: an override that silently does nothing is worse than a crash, because
you would go on believing it applied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instrument:
    key: str
    label: str
    multiplier: float
    #: "index" -> Black-Scholes with dividend yield; "future" -> Black-76
    model: str
    #: Typical listed strike interval near the money. Used for ATM rounding and
    #: by the mock generator — a display concern, not a contract-spec claim.
    strike_step: float
    #: Provider-side symbols. Schwab uses "$SPX"/"$NDX" for cash indices.
    quote_symbol: str
    chain_symbol: str
    #: How many strikes each side of ATM the charts show. The reference
    #: dashboard uses +/-30 for ES and +/-50 for NQ; it is per-instrument
    #: because strike density varies hugely (SPY lists $1 strikes on a ~670
    #: handle, NDX lists 25-point strikes on ~30,000).
    display_strikes: int = 30
    #: European exercise. False means American, where early exercise has value
    #: that Black-Scholes does not price. Read by ``exposure.compute`` and
    #: surfaced in the provenance panel — never silently ignored.
    #: Overridable at startup, see ``GEXDASH_EXERCISE_OVERRIDES`` below.
    european: bool = True
    #: Typical continuous dividend yield. Overridable per request; a default
    #: per instrument beats applying the S&P's yield to QQQ.
    default_dividend_yield: float = 0.013
    #: Sanity band for the underlying price, as a wrong-security tripwire.
    #:
    #: Not a forecast -- deliberately decades-wide. It exists because a
    #: mistyped or aliased root can resolve to a completely different
    #: security and be priced without complaint: Schwab's bare ``ES`` is
    #: Eversource Energy at ~$71, which as an "E-mini S&P 500 future" at
    #: x$50 with Black-76 would produce entirely plausible-looking garbage.
    plausible_spot: tuple[float, float] = (0.01, 1e9)

    @property
    def is_future(self) -> bool:
        return self.model == "future"

    @property
    def exercise_style(self) -> str:
        return "european" if self.european else "american"

    @property
    def note(self) -> str:
        """Short UI warning. Derived, not stored.

        A stored note would go stale the moment an exercise override flipped
        the instrument, leaving the strip claiming European on an American
        contract.
        """
        return "" if self.european else "American exercise; greeks use a European approximation."


# ---------------------------------------------------------------- S&P 500

SPX = Instrument(
    key="SPX",
    label="S&P 500 Index",
    multiplier=100.0,
    model="index",
    strike_step=5.0,
    quote_symbol="$SPX",
    chain_symbol="$SPX",
    display_strikes=30,
    default_dividend_yield=0.013,
    plausible_spot=(1000, 50000),
)

SPY = Instrument(
    key="SPY",
    label="SPDR S&P 500 ETF",
    multiplier=100.0,
    model="index",
    strike_step=1.0,
    quote_symbol="SPY",
    chain_symbol="SPY",
    display_strikes=40,
    european=False,  # American, physically settled
    default_dividend_yield=0.012,
    plausible_spot=(100, 5000),
)

#: UNVERIFIED AGAINST SCHWAB — see the note on the futures instruments below.
#:
#: SYMBOL WARNING: bare ``ES`` on Schwab is **Eversource Energy**, a NYSE
#: utility. It returns a perfectly valid option chain. ``plausible_spot``
#: below is the tripwire that stops it being priced as an E-mini.
#:
#: EXERCISE: American. CME's quarterly and serial options on E-mini S&P 500
#: futures are American-style; the weekly and end-of-month options are
#: European. We label the family by its quarterlies, which carry the bulk of
#: the open interest that drives an exposure surface. If your chain is
#: predominantly weeklies, override to european (see below).
ES = Instrument(
    key="ES",
    label="E-mini S&P 500 Future",
    multiplier=50.0,
    model="future",
    strike_step=5.0,
    quote_symbol="/ES",
    chain_symbol="/ES",
    display_strikes=30,
    european=False,
    plausible_spot=(1000, 50000),
)

# ------------------------------------------------------------- Nasdaq 100

#: EXERCISE: European, verified. Nasdaq and Cboe both specify NDX as
#: European-style and cash-settled, and market "no early assignment risk" as
#: a feature of the contract. Do not change this to American to match the CME
#: futures instruments — they are a different product with a different rule.
#: If you need it flipped for a specific reason, use the override rather than
#: editing this line.
NDX = Instrument(
    key="NDX",
    label="Nasdaq-100 Index",
    multiplier=100.0,
    model="index",
    strike_step=25.0,
    quote_symbol="$NDX",
    chain_symbol="$NDX",
    display_strikes=40,
    default_dividend_yield=0.007,
    plausible_spot=(5000, 200000),
)

QQQ = Instrument(
    key="QQQ",
    label="Invesco QQQ Trust",
    multiplier=100.0,
    model="index",
    strike_step=1.0,
    quote_symbol="QQQ",
    chain_symbol="QQQ",
    display_strikes=40,
    european=False,  # American, physically settled
    default_dividend_yield=0.005,
    plausible_spot=(100, 5000),
)

#: UNVERIFIED AGAINST SCHWAB. The exposure maths for the futures instruments
#: (Black-76, futures price as the underlying) is tested, but only against the
#: mock provider. ``/ES`` and ``/NQ`` are continuous-contract aliases rather
#: than tradeable contracts, and Schwab's chains endpoint documents equity and
#: index options — futures-options coverage there is undocumented. If they do
#: not resolve, use a dated root or route the futures legs to a CME-carrying
#: provider; the two-method provider interface takes one new class. Note
#: futures carry no OPRA fees; CME has its own entitlements.
#: EXERCISE: American, same rule as ES — CME quarterly and serial options on
#: E-mini Nasdaq-100 futures are American-style, weeklies and EOM are
#: European.
NQ = Instrument(
    key="NQ",
    label="E-mini Nasdaq-100 Future",
    multiplier=20.0,
    model="future",
    strike_step=10.0,
    quote_symbol="/NQ",
    chain_symbol="/NQ",
    display_strikes=50,
    european=False,
    plausible_spot=(5000, 200000),
)


#: Environment variable holding exercise-style overrides.
EXERCISE_OVERRIDE_ENV = "GEXDASH_EXERCISE_OVERRIDES"

_STYLES = {"american": False, "european": True}


def parse_exercise_overrides(raw: str | None) -> dict[str, bool]:
    """Parse ``"NDX=american,NQ=european"`` into ``{key: european}``.

    Raises on anything malformed rather than skipping it — see the module
    docstring on why a silently-ignored override is the worse failure.
    """
    if not raw or not raw.strip():
        return {}

    out: dict[str, bool] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"{EXERCISE_OVERRIDE_ENV}: expected 'SYMBOL=style' entries, got {chunk!r}"
            )
        key, _, style = chunk.partition("=")
        key, style = key.strip().upper(), style.strip().lower()
        if style not in _STYLES:
            raise ValueError(
                f"{EXERCISE_OVERRIDE_ENV}: unknown exercise style {style!r} for {key} "
                f"(expected one of {sorted(_STYLES)})"
            )
        out[key] = _STYLES[style]
    return out


def apply_exercise_overrides(
    registry: dict[str, Instrument], raw: str | None
) -> dict[str, Instrument]:
    """Return a registry with exercise styles overridden."""
    overrides = parse_exercise_overrides(raw)
    unknown = sorted(set(overrides) - set(registry))
    if unknown:
        raise ValueError(
            f"{EXERCISE_OVERRIDE_ENV}: unknown instrument(s) {unknown}; "
            f"known: {sorted(registry)}"
        )
    return {
        key: (replace(inst, european=overrides[key]) if key in overrides else inst)
        for key, inst in registry.items()
    }


REGISTRY: dict[str, Instrument] = apply_exercise_overrides(
    {inst.key: inst for inst in (SPX, SPY, ES, NDX, QQQ, NQ)},
    os.environ.get(EXERCISE_OVERRIDE_ENV),
)

#: Display grouping for the ticker selector.
FAMILIES: dict[str, list[str]] = {
    "S&P 500": ["SPX", "SPY", "ES"],
    "Nasdaq-100": ["NDX", "QQQ", "NQ"],
}


def get(key: str) -> Instrument:
    try:
        return REGISTRY[key.upper()]
    except KeyError:
        raise KeyError(f"unknown instrument {key!r}; known: {sorted(REGISTRY)}") from None


def all_instruments() -> list[Instrument]:
    return list(REGISTRY.values())
