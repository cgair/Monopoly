"""Technical indicators on cached crypto bars.

Re-uses the upstream ``stockstats`` library against the OHLCV bars
already produced by ``crypto_binance``. Self-fetches the underlying
bars via ``_get_ohlcv`` so the LLM doesn't have to remember to call
``get_market_data`` first — if it does, the second call is just a
cache hit.

Two-layer pattern matches the rest of ``dataflows/`` (see
``docs/dataflows-decisions.md`` §3.0):

* ``_get_indicators`` returns a structured ``Response`` of (timestamp,
  value) pairs for tests / caching.
* ``get_indicators`` returns prompt-ready plaintext.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import stockstats

from . import crypto_binance
from .crypto_types import Meta, Response

logger = logging.getLogger(__name__)

# Indicator menu surfaced to the LLM. Names map directly onto
# stockstats's column-trigger syntax (sdf[name] forces evaluation).
SUPPORTED_INDICATORS: dict[str, str] = {
    # Moving averages
    "close_10_ema": "10 EMA — responsive short-term momentum",
    "close_50_sma": "50 SMA — medium-term trend",
    "close_200_sma": "200 SMA — long-term trend (golden/death cross)",
    # MACD family
    "macd": "MACD line — EMA12 - EMA26",
    "macds": "MACD signal — EMA9 of MACD line",
    "macdh": "MACD histogram — MACD minus signal",
    # Momentum
    "rsi": "RSI(14) — 70/30 overbought/oversold",
    # Volatility
    "boll": "Bollinger middle (20 SMA)",
    "boll_ub": "Bollinger upper band (mid + 2σ)",
    "boll_lb": "Bollinger lower band (mid - 2σ)",
    "atr": "ATR(14) — average true range, sizing stops",
    # Volume-weighted
    "vwma": "VWMA — volume-weighted moving average",
    "mfi": "MFI(14) — money flow index, momentum × volume",
}

# Fetch enough bars to seed lookback-heavy indicators (notably 200 SMA).
_INDICATOR_FETCH_BUFFER = 250


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _format_value(v) -> str:
    """Render a scalar indicator value; treats NaN as N/A."""
    try:
        if pd.isna(v):
            return "N/A"
    except (TypeError, ValueError):
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _get_indicators(symbol: str, indicator: str, interval: str = "1h",
                    look_back_bars: int = 100) -> Response[tuple]:
    """Compute ``indicator`` over the last ``look_back_bars`` bars.

    ``data`` is a list of ``(open_time_ms, value)`` tuples ordered
    oldest → newest. ``Meta.is_stale`` is propagated from the
    underlying bars fetch.
    """
    if indicator not in SUPPORTED_INDICATORS:
        return Response(data=[], meta=Meta(
            fetched_at=0, vendor="stockstats", ok=False,
            note=(f"unsupported indicator '{indicator}'; "
                  f"choose from {list(SUPPORTED_INDICATORS)}"),
        ))

    fetch_n = max(look_back_bars + 50, _INDICATOR_FETCH_BUFFER)
    bars = crypto_binance._get_ohlcv(symbol, interval, limit=fetch_n)
    if not bars.data:
        return Response(data=[], meta=Meta(
            fetched_at=bars.meta.fetched_at, vendor="stockstats",
            ok=False, note=f"no bars available: {bars.meta.note or 'unknown'}",
        ))

    df = pd.DataFrame([b.__dict__ for b in bars.data])
    sdf = stockstats.wrap(df)
    try:
        series = sdf[indicator]
    except (KeyError, ValueError, IndexError) as exc:
        return Response(data=[], meta=Meta(
            fetched_at=bars.meta.fetched_at, vendor="stockstats",
            ok=False, note=f"stockstats failed for {indicator}: {exc}",
        ))

    open_times = df["open_time"].tolist()
    pairs = list(zip(open_times, series.tolist()))[-look_back_bars:]

    return Response(data=pairs, meta=Meta(
        fetched_at=bars.meta.fetched_at, vendor="stockstats",
        ok=True, is_stale=bars.meta.is_stale, note=bars.meta.note,
    ))


def get_indicators(symbol: str, indicator: str, interval: str = "1h",
                   look_back_bars: int = 100) -> str:
    """Prompt-formatted indicator series.

    Returns a header block + a two-column timestamp/value table covering
    the most recent ``look_back_bars`` bars on the requested interval.
    """
    resp = _get_indicators(symbol, indicator, interval, look_back_bars)
    sym = symbol.upper()
    header = [
        f"# Technical indicator — {sym} {interval} · {indicator}",
        f"# Bars covered: {len(resp.data)} (requested {look_back_bars})",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    desc = SUPPORTED_INDICATORS.get(indicator)
    if desc:
        header.append(f"# Description: {desc}")
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no indicator data available for {sym} {interval} {indicator}>")
        return "\n".join(header)
    header.append("timestamp,value")
    for ts, val in resp.data:
        header.append(f"{_iso(ts)},{_format_value(val)}")
    return "\n".join(header)
