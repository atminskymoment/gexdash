"""Plain-language reading of the exposure surface.

Computed server-side for the same reason the greeks and indicators are: it
can be tested. Prose assembled in the browser is prose nobody checks, and the
failure mode here is worse than a wrong number -- a confident sentence is
believed in a way a chart is not.

Three rules this module holds to:

1. **Restate, never predict.** Every sentence is a consequence of the numbers
   in front of it. "Dealers sell into strength" describes a hedging mechanic
   implied by positive gamma; it does not say the market will fall.
2. **Carry the assumptions with the claim.** The dealer sign convention
   decides the *sign* of GEX and DEX, so any statement about direction is
   downstream of an assumption the user chose in a dropdown. Where a reading
   is an artefact of that choice, it says so next to the reading rather than
   in a footnote.
3. **No magnitude claims in absolute currency.** "$8B of gamma" means nothing
   without knowing the instrument -- SPY and a single name are orders apart.
   Everything comparative here is expressed relative to spot, so it transfers
   between instruments.
"""

from __future__ import annotations

__all__ = ["build", "PROXIMITY_AT", "PROXIMITY_NEAR"]

#: Distance to a level, as a percentage of spot, within which price is
#: treated as sitting *at* that level rather than merely near it.
#:
#: 0.35% is roughly a quiet day's range for a broad index, so inside it the
#: level is reachable without anything unusual happening. Not a claim about
#: any particular instrument's volatility -- it is a labelling threshold, and
#: the exact distance is always printed beside the label so the reader can
#: disagree with it.
PROXIMITY_AT = 0.35

#: Beyond this, a level is far enough away to be scenery rather than a
#: constraint on today's trade.
PROXIMITY_NEAR = 2.0


def _pct_from(spot: float, level: float | None) -> float | None:
    if level is None or not spot:
        return None
    return (level - spot) / spot * 100.0


def _proximity(distance_pct: float | None) -> str:
    if distance_pct is None:
        return "unknown"
    d = abs(distance_pct)
    if d <= PROXIMITY_AT:
        return "at"
    if d <= PROXIMITY_NEAR:
        return "near"
    return "far"


def _reading(key, label, value, tone, meaning, caveat=None) -> dict:
    return {
        "key": key, "label": label, "value": value,
        "tone": tone, "meaning": meaning, "caveat": caveat,
    }


def _gex_reading(gex: float) -> dict:
    if gex > 0:
        return _reading(
            "gex", "Net GEX", gex, "pos",
            "Dealers are long gamma, so they hedge against the move — selling "
            "strength and buying weakness. That damps realised volatility and "
            "pulls price toward the large-gamma strikes.",
        )
    if gex < 0:
        return _reading(
            "gex", "Net GEX", gex, "neg",
            "Dealers are short gamma, so they hedge with the move — selling "
            "weakness and buying strength. That extends trends and raises "
            "realised volatility, and gaps travel further before they meet "
            "opposing flow.",
        )
    return _reading(
        "gex", "Net GEX", gex, "flat",
        "Net gamma is flat: no consistent damping or amplifying pressure from "
        "dealer hedging at this spot.",
    )


def _dex_reading(dex: float, dealer_sign: str) -> dict:
    direction = "long" if dex > 0 else "short" if dex < 0 else "flat"
    meaning = (
        f"Aggregate dealer delta is {direction}. Hedging that position means "
        f"trading the underlying {'against' if dex > 0 else 'with'} further "
        "moves in it."
        if dex else "Aggregate dealer delta nets to zero."
    )
    caveat = None
    if dealer_sign in ("long_calls_short_puts", "short_calls_long_puts"):
        caveat = (
            "This is dominated by the dealer sign convention you selected, "
            "which assigns a side to every contract rather than measuring one. "
            "The level is largely an artefact of that choice — its change "
            "through the day is the part worth reading."
        )
    return _reading("dex", "Net DEX", dex, "pos" if dex > 0 else "neg" if dex else "flat",
                    meaning, caveat)


def _vanna_reading(vanx: float) -> dict:
    if vanx > 0:
        meaning = (
            "Dealer delta rises as implied volatility rises. A vol spike "
            "mechanically adds buying and a vol collapse removes it, so this "
            "flow tends to support a market whose vol is falling."
        )
    elif vanx < 0:
        meaning = (
            "Dealer delta falls as implied volatility rises, so a vol spike "
            "mechanically adds selling — the flow reinforces a shock rather "
            "than cushioning it."
        )
    else:
        meaning = "Dealer delta is insensitive to a change in implied volatility."
    return _reading("vanx", "Net vanna", vanx,
                    "pos" if vanx > 0 else "neg" if vanx else "flat", meaning)


def _charm_reading(cex: float) -> dict:
    if cex > 0:
        meaning = (
            "Dealer delta grows as time passes, so the passage of time alone "
            "generates buying. It concentrates into expiry and is largest on "
            "expiry days."
        )
    elif cex < 0:
        meaning = (
            "Dealer delta decays as time passes, so the passage of time alone "
            "generates selling. It concentrates into expiry and is largest on "
            "expiry days."
        )
    else:
        meaning = "Dealer delta is not decaying materially with time."
    return _reading("cex", "Net charm", cex,
                    "pos" if cex > 0 else "neg" if cex else "flat", meaning)


def _level_readings(spot: float, levels: dict) -> list[dict]:
    out = []

    flip = (levels.get("gamma_flip") or {}).get("strike")
    flip_pct = _pct_from(spot, flip)
    if flip is None:
        out.append(_reading(
            "gamma_flip", "Gamma flip", None, "flat",
            "No zero-gamma crossing inside the strike range, so the current "
            "regime has no nearby boundary to fail at.",
        ))
    else:
        prox = _proximity(flip_pct)
        # flip_pct is (level - spot)/spot, so a negative value puts the level
        # *below* spot. Getting this backwards inverts the regime description
        # while leaving a sentence that still reads perfectly well.
        where = "below" if flip_pct < 0 else "above"
        if prox == "at":
            meaning = (
                f"Spot is sitting on the flip ({abs(flip_pct):.2f}% away). This is "
                "the least stable configuration on the board: the sign of dealer "
                "gamma — and so whether hedging damps or amplifies — changes on a "
                "move of less than half a percent."
            )
        elif prox == "near":
            meaning = (
                f"The flip is {abs(flip_pct):.2f}% {where} spot — within reach on "
                "an ordinary session, so the current regime should be treated as "
                "conditional rather than settled."
            )
        else:
            meaning = (
                f"The flip is {abs(flip_pct):.2f}% {where} spot, far enough that "
                "the current regime holds unless something moves price a long way."
            )
        out.append(_reading("gamma_flip", "Gamma flip", flip, "flat", meaning))

    call_wall = (levels.get("call_wall") or {}).get("strike")
    put_wall = (levels.get("put_wall") or {}).get("strike")
    if call_wall is not None and put_wall is not None:
        cw, pw = _pct_from(spot, call_wall), _pct_from(spot, put_wall)
        out.append(_reading(
            "walls", "Put / call wall", [put_wall, call_wall], "flat",
            f"The heaviest gamma sits at {put_wall:g} ({pw:+.2f}%) and "
            f"{call_wall:g} ({cw:+.2f}%). While dealers are long gamma these act "
            "as the edges price is hedged back into; they are where flow is "
            "concentrated, not a floor and ceiling anyone is defending.",
        ))

    max_pain = (levels.get("max_pain") or {}).get("strike")
    if max_pain is not None:
        mp = _pct_from(spot, max_pain)
        out.append(_reading(
            "max_pain", "Max pain", max_pain, "flat",
            f"{max_pain:g}, {abs(mp):.2f}% {'above' if mp > 0 else 'below'} spot — "
            "the strike where the most open interest expires worthless. It is an "
            "accounting property of the OI distribution, not a magnet, and it "
            "only bites near expiry.",
        ))

    return out


def _pc_reading(totals: dict) -> dict | None:
    ratio = totals.get("pc_oi_ratio")
    if ratio is None:
        return None
    if ratio > 1.3:
        meaning = ("Put open interest heavily outweighs call. Typical of index "
                   "hedging demand rather than a directional bet, and it is the "
                   "normal state for a broad index.")
    elif ratio < 0.7:
        meaning = ("Call open interest outweighs put — unusual for an index, and "
                   "worth checking against upcoming events before reading it as "
                   "positioning.")
    else:
        meaning = "Put and call open interest are close to balanced."
    return _reading("pc_oi", "P/C open interest", ratio, "flat", meaning)


def _synthesis(spot, totals, levels, regime, dealer_sign) -> list[str]:
    """Two or three sentences tying the separate readings together."""
    gex = totals.get("gex") or 0.0
    long_gamma = gex > 0
    flip = (levels.get("gamma_flip") or {}).get("strike")
    flip_pct = _pct_from(spot, flip)
    prox = _proximity(flip_pct)

    lines = []

    if long_gamma and prox == "far":
        lines.append(
            "Taken together: a damped tape. Dealer hedging leans against moves, "
            "and the regime boundary is not close, so the base case is range "
            "behaviour between the walls with rallies and dips both sold into."
        )
    elif long_gamma:
        lines.append(
            "Taken together: damped, but conditionally. Hedging currently leans "
            "against moves, and the flip is close enough that losing it would "
            "reverse that — the same flow that is suppressing this range would "
            "start extending moves instead."
        )
    elif not long_gamma and prox == "far":
        lines.append(
            "Taken together: an unstable tape. Dealer hedging reinforces "
            "direction rather than fading it, and spot is well clear of the "
            "boundary where that would change, so moves should be expected to "
            "travel further than the recent range suggests."
        )
    else:
        lines.append(
            "Taken together: unstable but near the boundary. Hedging currently "
            "amplifies moves, and reclaiming the flip would flip that to "
            "suppression — expect the character of the tape to change, not just "
            "its direction, if spot crosses."
        )

    call_wall = (levels.get("call_wall") or {}).get("strike")
    put_wall = (levels.get("put_wall") or {}).get("strike")
    if call_wall is not None and put_wall is not None and long_gamma:
        lines.append(
            f"The {put_wall:g}–{call_wall:g} band is where that hedging is "
            "concentrated; it is the most likely place for movement to stall, "
            "and a decisive move through either edge removes the flow that has "
            "been containing it."
        )

    cex = totals.get("cex") or 0.0
    if cex:
        lines.append(
            "Charm adds a "
            + ("bid" if cex > 0 else "drag")
            + " that builds with the clock rather than with price, so it shows up "
            "as late-session drift and is strongest into expiry."
        )

    if dealer_sign in ("long_calls_short_puts", "short_calls_long_puts"):
        lines.append(
            "All of the above inherits the dealer sign convention: it is an "
            "assumption about who holds which side, not a measurement. Flip it "
            "in the dropdown and the directional readings invert."
        )
    return lines


def _caveats(payload: dict) -> list[str]:
    out = []
    diag = payload.get("diagnostics") or {}
    meta = payload.get("instrument_meta") or {}

    oi_asof = diag.get("oi_asof")
    out.append(
        f"Open interest is the OCC snapshot for {oi_asof or 'an unknown date'} — "
        "published once daily. Everything here is that fixed OI surface "
        "re-priced against live spot, so positioning opened today is not in it."
        if oi_asof else
        "Open interest is a once-daily OCC figure re-priced against live spot."
    )

    window = diag.get("expiry_window")
    if window:
        out.append(f"Only contracts inside the {window} expiry window are included.")

    if diag.get("greeks_approximation"):
        out.append(str(diag["greeks_approximation"]))
    elif meta.get("note"):
        out.append(str(meta["note"]))

    if payload.get("stale"):
        out.append(
            "This chain is stale — see the banner above for why and for what is "
            "being shown instead."
        )
    return out


def build(payload: dict) -> dict | None:
    """Assemble the reading. ``None`` when there is nothing to read.

    Takes the assembled API payload rather than an ``ExposureResult`` so that
    what is interpreted is exactly what the client was sent -- there is no
    second path that could describe different numbers from the ones on screen.
    """
    if payload.get("oi_unavailable"):
        return None

    spot = payload.get("spot") or 0.0
    totals = payload.get("totals") or {}
    levels = payload.get("levels") or {}
    regime = payload.get("regime") or {}
    dealer_sign = (payload.get("diagnostics") or {}).get("dealer_sign", "")

    readings = [
        _gex_reading(totals.get("gex") or 0.0),
        _dex_reading(totals.get("dex") or 0.0, dealer_sign),
        _vanna_reading(totals.get("vanx") or 0.0),
        _charm_reading(totals.get("cex") or 0.0),
    ]
    readings += _level_readings(spot, levels)
    pc = _pc_reading(totals)
    if pc:
        readings.append(pc)

    return {
        "headline": regime.get("label") or "—",
        "headline_gloss": regime.get("gloss") or "",
        "readings": readings,
        "synthesis": _synthesis(spot, totals, levels, regime, dealer_sign),
        "caveats": _caveats(payload),
        "derivation": (
            "Derived from the exposure numbers on this page by a fixed rule set "
            "(gexdash/interpretation.py). Not a forecast, and not a vendor signal."
        ),
    }
