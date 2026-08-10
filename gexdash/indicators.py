"""RSI, MACD and ADX/DI.

Computed server-side rather than in the browser for the same reason the
greeks are: they can be tested. An indicator that is subtly wrong looks
exactly like one that is right, and the classic error -- using a simple
moving average where Wilder specified his own smoothing -- produces a curve
that tracks the correct one closely enough to pass a glance and diverge
whenever it matters.

**Wilder smoothing is not an EMA with the same period.** Wilder's average is
``s[i] = s[i-1] + (x[i] - s[i-1]) / n``, i.e. an EMA with ``alpha = 1/n``,
whereas a conventional n-period EMA uses ``alpha = 2/(n+1)``. A 14-period
Wilder average behaves like a 27-period EMA. RSI, ATR and ADX all use
Wilder; MACD uses conventional EMAs. Mixing them is the single most common
way these come out wrong.

Every series returned is the same length as the input, ``nan``-padded at the
front where there is not yet enough history — never silently truncated, so
indicator values stay aligned with their bars.
"""

from __future__ import annotations

import numpy as np

__all__ = ["wilder", "ema", "sma", "rsi", "macd", "adx", "compute_all"]


def ema(values: np.ndarray, length: int) -> np.ndarray:
    """Conventional EMA, alpha = 2/(n+1). Seeded with the first value."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) == 0 or length < 1:
        return out

    alpha = 2.0 / (length + 1.0)
    acc = values[0]
    out[0] = acc
    for i in range(1, len(values)):
        acc = alpha * values[i] + (1 - alpha) * acc
        out[i] = acc
    return out


def sma(values: np.ndarray, length: int) -> np.ndarray:
    """Simple moving average, nan until the window fills.

    nan-aware: RSI is undefined for its own warm-up, and a plain rolling mean
    would poison the first `length` averages with those nans instead of
    simply not existing yet.
    """
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if length < 1 or len(values) < length:
        return out
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        if np.all(np.isfinite(window)):
            out[i] = window.mean()
    return out


def wilder(values: np.ndarray, length: int) -> np.ndarray:
    """Wilder's smoothing, alpha = 1/n, seeded with the first n-mean.

    Deliberately distinct from :func:`ema`. Using an EMA here would make RSI
    and ADX systematically too responsive.
    """
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < length or length < 1:
        return out

    acc = float(np.mean(values[:length]))
    out[length - 1] = acc
    for i in range(length, len(values)):
        acc += (values[i] - acc) / length
        out[i] = acc
    return out


def rsi(close: np.ndarray, length: int = 14) -> np.ndarray:
    """Wilder's RSI. Bounded [0, 100]."""
    close = np.asarray(close, dtype=float)
    out = np.full(close.shape, np.nan)
    if len(close) < length + 1:
        return out

    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    # The first delta is a fabricated zero from `prepend`; drop it so the
    # seed average covers real changes only.
    avg_gain = wilder(gain[1:], length)
    avg_loss = wilder(loss[1:], length)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss > 0, avg_gain / avg_loss, np.inf)
        values = np.where(np.isfinite(rs), 100.0 - 100.0 / (1.0 + rs), 100.0)
    # An all-gain window has no downside at all: RSI is 100 by definition,
    # not nan from a zero divisor.
    values = np.where(np.isnan(avg_gain), np.nan, values)

    out[1:] = values
    return out


def macd(
    close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, np.ndarray]:
    """MACD line, signal and histogram. Conventional EMAs, not Wilder."""
    close = np.asarray(close, dtype=float)
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return {"macd": line, "signal": sig, "histogram": line - sig}


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14
) -> dict[str, np.ndarray]:
    """Wilder's ADX with +DI / -DI.

    Directional movement is *exclusive*: only the larger of the two moves
    counts on any bar, and neither counts when the bar is an inside bar.
    Awarding both is a common slip that inflates ADX.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    nan = np.full(n, np.nan)
    if n < length * 2:
        return {"adx": nan, "di_plus": nan.copy(), "di_minus": nan.copy()}

    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])

    # Drop the fabricated first bar before smoothing, as with RSI.
    atr = wilder(tr[1:], length)
    sm_plus = wilder(plus_dm[1:], length)
    sm_minus = wilder(minus_dm[1:], length)

    with np.errstate(divide="ignore", invalid="ignore"):
        di_plus = np.where(atr > 0, 100.0 * sm_plus / atr, 0.0)
        di_minus = np.where(atr > 0, 100.0 * sm_minus / atr, 0.0)
        denom = di_plus + di_minus
        dx = np.where(denom > 0, 100.0 * np.abs(di_plus - di_minus) / denom, 0.0)

    dx = np.where(np.isnan(atr), np.nan, dx)
    adx_vals = wilder(dx[~np.isnan(dx)], length) if np.any(~np.isnan(dx)) else np.array([])

    out_adx = np.full(n, np.nan)
    if len(adx_vals):
        # Re-seat the ADX series at the offset where DX first existed.
        first_dx = int(np.argmax(~np.isnan(dx)))
        out_adx[1 + first_dx : 1 + first_dx + len(adx_vals)] = adx_vals

    out_plus, out_minus = np.full(n, np.nan), np.full(n, np.nan)
    out_plus[1:] = np.where(np.isnan(atr), np.nan, di_plus)
    out_minus[1:] = np.where(np.isnan(atr), np.nan, di_minus)
    return {"adx": out_adx, "di_plus": out_plus, "di_minus": out_minus}


def _clean(values: np.ndarray) -> list:
    """JSON-safe: nan becomes null so the client can break the line."""
    return [None if not np.isfinite(v) else round(float(v), 6) for v in values]


def compute_all(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    rsi_length: int = 14,
    rsi_ma_length: int = 20,
    adx_length: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> dict:
    """Every indicator, JSON-ready and aligned with the input bars."""
    m = macd(close, macd_fast, macd_slow, macd_signal)
    a = adx(high, low, close, adx_length)
    rsi_values = rsi(close, rsi_length)
    return {
        "rsi": _clean(rsi_values),
        "rsi_ma": _clean(sma(rsi_values, rsi_ma_length)),
        "macd": _clean(m["macd"]),
        "macd_signal": _clean(m["signal"]),
        "macd_histogram": _clean(m["histogram"]),
        "adx": _clean(a["adx"]),
        "di_plus": _clean(a["di_plus"]),
        "di_minus": _clean(a["di_minus"]),
        "params": {
            "rsi_length": rsi_length,
            "rsi_ma_length": rsi_ma_length,
            "adx_length": adx_length,
            "macd": [macd_fast, macd_slow, macd_signal],
            "smoothing": "Wilder for RSI/ADX, conventional EMA for MACD",
        },
    }
