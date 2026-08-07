"""Point-in-time replay: make the data layer answer as of a past instant.

The production fetchers in ``dataflows.crypto_binance`` are hardwired to
"latest N samples relative to now". Rather than thread an ``as_of``
parameter through code that is already review-hardened and testnet
verified, this module swaps the four private fetchers (plus the
auxiliary ratio renderer) for archive-backed equivalents inside a
context manager. The public prompt formatters are untouched, so an
analyst sees a byte-identical report shape — only the data moves.

Two rules make the replay trustworthy, and both are enforced rather
than documented:

* **Closed bars only.** A bar is visible only once ``close_time <=
  as_of``. The bar in progress at the cutoff has not happened yet.
* **No live egress.** ``_http_get`` is replaced by a raiser, so any
  path that still reaches for a live endpoint fails loudly instead of
  quietly leaking the future into the prompt.

Only the market-data path is reconstructible. News, Reddit and the
newsflash relay have no usable archive, so a replay must select the
market analyst alone; the leak guard turns any other choice into an
immediate error rather than a silently contaminated run.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from tradingagents.dataflows import crypto_binance as cb
from tradingagents.dataflows.crypto_types import (
    LongShortRatio, Meta, OpenInterest, Response,
)

from . import vision

logger = logging.getLogger(__name__)


class LookAheadError(RuntimeError):
    """Raised when replay code attempts to reach live data."""


@dataclass
class _Clock:
    as_of: int          # ms epoch UTC — the instant being replayed
    symbol: str


_clock: _Clock | None = None
_memo: dict = {}


def _need_clock() -> _Clock:
    if _clock is None:
        raise LookAheadError("point-in-time fetcher called outside a replay context")
    return _clock


def _iso(ms: int) -> str:
    return cb._iso(ms)


# ---------------------------------------------------------------------------
# archive access, memoised per replay instant
# ---------------------------------------------------------------------------

def _bars(symbol: str, interval: str, count: int) -> list:
    """The ``count`` most recent bars closed at or before the cutoff."""
    clk = _need_clock()
    step = vision.INTERVAL_MS.get(interval, 3_600_000)
    key = ("bars", symbol, interval, clk.as_of)
    span = max(count + 60, 240)
    cached = _memo.get(key)
    if cached is None or len(cached) < count:
        start = clk.as_of - span * step
        cached = vision.klines(symbol, interval, start, clk.as_of)
        _memo[key] = cached
    return [b for b in cached if b.close_time <= clk.as_of][-count:]


def _metric_rows(symbol: str, interval: str, count: int) -> list[vision.MetricRow]:
    """Interval-aligned OI/ratio snapshots taken at or before the cutoff.

    Binance samples these as instantaneous snapshots, so selecting the
    5-minute rows that land on an interval boundary reproduces what the
    ``period=<interval>`` endpoint would have returned.
    """
    clk = _need_clock()
    step = vision.INTERVAL_MS.get(interval, 3_600_000)
    key = ("metrics", symbol, clk.as_of)
    span = max(count + 10, 120)
    cached = _memo.get(key)
    if cached is None:
        cached = vision.metrics(symbol, clk.as_of - span * step, clk.as_of)
        _memo[key] = cached
    aligned = [m for m in cached if m.timestamp % step == 0 and m.timestamp <= clk.as_of]
    return aligned[-count:]


def _taker(symbol: str, interval: str, count: int) -> list[tuple[int, float, float]]:
    """``(open_time, taker_buy_vol, total_vol)`` for closed bars at the cutoff."""
    clk = _need_clock()
    step = vision.INTERVAL_MS.get(interval, 3_600_000)
    key = ("taker", symbol, interval, clk.as_of)
    cached = _memo.get(key)
    if cached is None:
        cached = vision.taker_buy_volume(
            symbol, interval, clk.as_of - max(count + 10, 60) * step, clk.as_of)
        _memo[key] = cached
    rows = [(ts, v[0], v[1]) for ts, v in sorted(cached.items())
            if ts + step - 1 <= clk.as_of]
    return rows[-count:]


# ---------------------------------------------------------------------------
# replacements for the private fetchers
# ---------------------------------------------------------------------------

def _pit_ohlcv(symbol: str, interval: str, *, limit: int = 200) -> Response:
    clk = _need_clock()
    bars = _bars(symbol.upper(), interval, limit)
    return Response(
        data=bars,
        meta=Meta(fetched_at=clk.as_of, vendor="binance-archive", ok=bool(bars),
                  note=None if bars else "no archived bars at this cutoff"),
    )


def _pit_funding(symbol: str, *, limit: int = 100) -> Response:
    clk = _need_clock()
    key = ("funding", symbol.upper(), clk.as_of)
    items = _memo.get(key)
    if items is None:
        items = vision.funding(symbol.upper(), clk.as_of - (limit + 10) * 8 * 3_600_000, clk.as_of)
        _memo[key] = items
    items = [f for f in items if f.funding_time <= clk.as_of][-limit:]
    return Response(
        data=items,
        meta=Meta(fetched_at=clk.as_of, vendor="binance-archive", ok=bool(items),
                  note=None if items else "no archived funding at this cutoff"),
    )


def _pit_open_interest(symbol: str, interval: str, *, limit: int = 100) -> Response:
    clk = _need_clock()
    sym = symbol.upper()
    rows = _metric_rows(sym, interval, limit)
    data = [OpenInterest(symbol=sym, interval=interval, timestamp=m.timestamp, oi=m.open_interest)
            for m in rows]
    return Response(
        data=data,
        meta=Meta(fetched_at=clk.as_of, vendor="binance-archive", ok=bool(data),
                  note=None if data else "no archived open interest at this cutoff"),
    )


def _pit_long_short(symbol: str, interval: str, *, limit: int = 50) -> Response:
    clk = _need_clock()
    sym = symbol.upper()
    rows = _metric_rows(sym, interval, limit)
    data = []
    for m in rows:
        r = m.account_ratio
        # The archive stores only the ratio; the endpoint also reports the
        # two shares, which are recoverable because they sum to 1.
        long_share = r / (1.0 + r) if r > 0 else 0.0
        data.append(LongShortRatio(
            id=cb._ls_id(sym, interval, m.timestamp),
            symbol=sym, interval=interval, timestamp=m.timestamp,
            long_account=round(long_share, 4),
            short_account=round(1.0 - long_share, 4) if r > 0 else 0.0,
            ratio=r,
        ))
    return Response(
        data=data,
        meta=Meta(fetched_at=clk.as_of, vendor="binance-archive", ok=bool(data),
                  note=None if data else "no archived long/short data at this cutoff"),
    )


def _pit_aux_lines(path: str, label: str, columns: str, symbol: str,
                   interval: str, rows: int) -> list[str]:
    sym = symbol.upper()
    if "taker" in path.lower():
        tk = _taker(sym, interval, rows)
        if not tk:
            return [f"## {label}", "# Note: no archived taker volume at this cutoff"]
        lines = [f"## {label} (latest {len(tk)})", columns]
        for ts, buy, total in tk:
            sell = max(total - buy, 0.0)
            ratio = buy / sell if sell else 0.0
            lines.append(f"{_iso(ts)},{ratio:.4f},{buy},{round(sell, 4)}")
        return lines

    metric_rows = _metric_rows(sym, interval, rows)
    if not metric_rows:
        return [f"## {label}", "# Note: no archived ratio data at this cutoff"]
    lines = [f"## {label} (latest {len(metric_rows)})", columns]
    for m in metric_rows:
        r = m.top_ratio
        long_share = r / (1.0 + r) if r > 0 else 0.0
        lines.append(f"{_iso(m.timestamp)},{round(long_share, 4)},"
                     f"{round(1.0 - long_share, 4) if r > 0 else 0.0},{r}")
    return lines


def _blocked_http(path: str, params: dict):
    raise LookAheadError(
        f"live Binance call to {path} during point-in-time replay — "
        "this would leak data from after the cutoff"
    )


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def point_in_time(as_of_ms: int, symbol: str = "BTCUSDT"):
    """Serve the market-data layer as of ``as_of_ms`` and block live egress."""
    global _clock
    if _clock is not None:
        raise RuntimeError("point_in_time contexts cannot be nested")

    originals = {
        "_get_ohlcv": cb._get_ohlcv,
        "_get_funding_rate": cb._get_funding_rate,
        "_get_open_interest": cb._get_open_interest,
        "_get_long_short_ratio": cb._get_long_short_ratio,
        "_aux_ratio_lines": cb._aux_ratio_lines,
        "_http_get": cb._http_get,
    }
    _clock = _Clock(as_of=as_of_ms, symbol=symbol.upper())
    _memo.clear()

    cb._get_ohlcv = _pit_ohlcv
    cb._get_funding_rate = _pit_funding
    cb._get_open_interest = _pit_open_interest
    cb._get_long_short_ratio = _pit_long_short
    cb._aux_ratio_lines = _pit_aux_lines
    cb._http_get = _blocked_http
    try:
        yield _clock
    finally:
        for name, fn in originals.items():
            setattr(cb, name, fn)
        _clock = None
        _memo.clear()
