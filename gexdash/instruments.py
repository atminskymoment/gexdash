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
import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Instrument:
    """Specification of one underlying: every field that changes the
    exposure numbers if it is wrong (see the module docstring).

    ``key`` is the symbol the API and UI use; ``label`` is the display name.
    Frozen: exercise overrides produce modified copies in :data:`REGISTRY`,
    so look instruments up with :func:`get` rather than through the module
    constants.
    """

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
    #: Rolls on the CME equity-index quarterly calendar (the Monday of expiry
    #: week), so ``futures.back_adjust`` applies. True for ES and NQ only: a
    #: typed future such as /CL rolls monthly on its own calendar.
    quarterly_rolls: bool = False
    #: Built by :func:`resolve` for a typed symbol that is not registered,
    #: from stated defaults rather than a verified contract spec.
    generic: bool = False
    #: Why exposure cannot be computed for this instrument, or None when it
    #: can. Set for typed futures, whose contract multiplier is unknown.
    exposure_unavailable: str | None = None

    @property
    def is_future(self) -> bool:
        """True for options on futures, priced with Black-76 on the futures price."""
        return self.model == "future"

    @property
    def exercise_style(self) -> str:
        """``"european"`` or ``"american"``, as reported in the payload."""
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
    quarterly_rolls=True,
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
    quarterly_rolls=True,
)


# --------------------------------------------------------------- Crude oil

#: Crude-oil ETF. Optionable, so it carries a full instrument spec rather than
#: a chart-only symbol: every path that serves bars keys on ``Instrument``.
#: EXERCISE: American, physically settled -- an equity-style ETF option.
#: No distribution policy to speak of, so the dividend default is zero; the
#: fund's roll yield is in the price, not a cash dividend.
USO = Instrument(
    key="USO",
    label="United States Oil Fund",
    multiplier=100.0,
    model="index",
    # $0.50 strikes near the money on a ~$75 handle; +/-15 of them spans
    # about 20% of spot, the same order as the equity ETFs' grids.
    strike_step=0.5,
    quote_symbol="USO",
    chain_symbol="USO",
    display_strikes=15,
    european=False,
    default_dividend_yield=0.0,
    plausible_spot=(5, 1000),
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
    {inst.key: inst for inst in (SPX, SPY, ES, NDX, QQQ, NQ, USO)},
    os.environ.get(EXERCISE_OVERRIDE_ENV),
)

#: Display grouping for the ticker selector.
FAMILIES: dict[str, list[str]] = {
    "S&P 500": ["SPX", "SPY", "ES"],
    "Nasdaq-100": ["NDX", "QQQ", "NQ"],
    "Crude oil": ["USO"],
}


def get(key: str) -> Instrument:
    """The registered instrument for ``key``, case-insensitive.

    This is the copy with ``GEXDASH_EXERCISE_OVERRIDES`` applied; the module
    constants (``SPX``, ``ES`` ...) never carry overrides. An unknown key
    raises KeyError listing the known ones.
    """
    try:
        return REGISTRY[key.upper()]
    except KeyError:
        raise KeyError(f"unknown instrument {key!r}; known: {sorted(REGISTRY)}") from None


def all_instruments() -> list[Instrument]:
    """Every registered instrument, overrides applied, in registry order."""
    return list(REGISTRY.values())


# ----------------------------------------------------------- typed symbols

#: A symbol a user may type: an equity or ETF ("AAPL", "BRK.B"), a cash index
#: ("$RUT") or a future ("/CL"). Letters first, then letters, digits or dots.
#: Anything else is refused rather than sent to a provider.
SYMBOL_PATTERN = re.compile(r"^[$/]?[A-Z][A-Z0-9.]{0,11}$")

_GENERIC: dict[str, Instrument] = {}


def _generic(symbol: str) -> Instrument:
    """Stated default specs for an unregistered symbol. See :func:`resolve`."""
    if symbol.startswith("/"):
        return Instrument(
            key=symbol, label=f"{symbol} (future; generic specs)", multiplier=1.0, model="future",
            strike_step=0.0, quote_symbol=symbol, chain_symbol=symbol, european=False,
            default_dividend_yield=0.0, plausible_spot=(1e-4, 1e9), generic=True,
            exposure_unavailable=(
                f"exposure needs the contract multiplier, which gexdash does not know for {symbol}; "
                "ES and NQ are the futures it carries specs for. The price charts work."),
        )
    if symbol.startswith("$"):
        return Instrument(
            key=symbol, label=f"{symbol} (cash index; generic specs)", multiplier=100.0,
            model="index", strike_step=0.0, quote_symbol=symbol, chain_symbol=symbol,
            european=True, default_dividend_yield=0.0, plausible_spot=(1e-4, 1e9), generic=True,
        )
    return Instrument(
        key=symbol, label=f"{symbol} (equity or ETF; generic specs)", multiplier=100.0,
        model="index", strike_step=0.0, quote_symbol=symbol, chain_symbol=symbol,
        european=False, default_dividend_yield=0.0, plausible_spot=(1e-4, 1e9), generic=True,
    )


def resolve(symbol: str) -> Instrument:
    """The instrument for anything a user may type.

    A registered key (``SPX``) or a registered provider symbol (``/ES``,
    ``$SPX``) returns that verified spec, overrides applied. Any other
    well-formed symbol (:data:`SYMBOL_PATTERN`) gets generic specs, stated
    in its label and the payload rather than verified:

    * ``AAPL``, ``BRK.B``: an equity or ETF. x100, American exercise (the
      greeks are European, as for SPY), no dividend unless one is passed.
    * ``$RUT``: a cash index. x100, European, cash-settled. Not right for an
      index whose options are priced off its futures, such as VIX: Black-
      Scholes on the spot index gives wrong greeks there.
    * ``/CL``: a future. Its contract multiplier is unknown, so exposure is
      refused (``exposure_unavailable``) and only bars are served, without
      roll adjustment.

    The same symbol always returns the same object. A malformed symbol raises
    KeyError, as :func:`get` does for an unknown key; :func:`get` itself stays
    strict, for code that must only ever see registered specs.
    """
    key = (symbol or "").strip().upper()
    if key in REGISTRY:
        return REGISTRY[key]
    for inst in REGISTRY.values():
        if key in (inst.quote_symbol.upper(), inst.chain_symbol.upper()):
            return inst
    if not SYMBOL_PATTERN.match(key):
        raise KeyError(
            f"{symbol!r} is not a symbol gexdash can look up: letters first, then letters, digits "
            "or dots, with $ for a cash index or / for a future (AAPL, BRK.B, $RUT, /CL)")
    if key not in _GENERIC:
        _GENERIC[key] = _generic(key)
    return _GENERIC[key]
