"""Pure directional scoring: how right was the *direction*, at each holding period?

:mod:`~tradingagents.backtest.score` races the decision's own stop and
target, which answers "what would this trade have returned as written".
That measure cannot be compared across holding periods: the longer the
period, the more likely the race has already ended, so a 720h column and
a 6h column would be describing different populations of trades.

This module strips the tactics away. It enters at the same instant and
price as the market-entry counterfactual in ``score``, then simply holds
to the h-hour mark and reports the signed move. No stop, no target, no
early exit — so the only thing being measured is whether the direction
was right, and for how long it stayed right.

The gap between the two views is itself a result: a direction that pays
at 168h but loses under the race says the stop, not the analysis, is
what lost the money.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .score import _fetch_klines, _normalize_side
from tradingagents.agents.utils.symbol_utils import to_binance_symbol

# The holding periods under test: 6h through 30 days.
HORIZONS: tuple[int, ...] = (6, 24, 72, 168, 336, 720)

_HOUR_MS = 3_600_000


@dataclass
class HorizonPoint:
    """The trade's state at one holding period, held blind."""

    horizon_hours: int
    exit_ts: int | None
    exit_price: float | None
    return_pct: float | None   # signed in the trade's direction; + = direction was right
    mfe_pct: float | None      # best excursion in favour, within the period
    mae_pct: float | None      # worst excursion against, within the period
    covered: bool              # bars actually reach the period; False = not measurable


@dataclass
class DirectionalOutcome:
    symbol: str
    side: str
    decision_ts: int
    entry_ts: int | None
    entry_price: float | None
    points: dict[int, HorizonPoint] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def fetch_window_bars(symbol: str, decision_ts: int, hours: int, interval: str = "5m") -> list[tuple]:
    """5-minute bars covering ``[decision_ts, decision_ts + hours]``.

    Shared by both scorers so the race and the blind hold see exactly the
    same price series and the same entry.
    """
    return _fetch_klines(
        to_binance_symbol(symbol), decision_ts, decision_ts + hours * _HOUR_MS, interval=interval
    )


def _bar_step(bars: list[tuple]) -> int:
    """Bar spacing in ms, inferred from the series (5m unless told otherwise)."""
    if len(bars) < 2:
        return 300_000
    return min(b[0] - a[0] for a, b in zip(bars, bars[1:]) if b[0] > a[0])


def directional_returns(
    *,
    symbol: str,
    side: str,
    decision_ts: int,
    horizons: tuple[int, ...] | list[int] = HORIZONS,
    bars: list | None = None,
) -> DirectionalOutcome:
    """Hold blind from ``decision_ts`` and report the signed move at each horizon.

    Args:
        symbol: "BTC-USD" or "BTCUSDT" form.
        side: "LONG"/"SHORT"/"BUY"/"SELL", case-insensitive.
        decision_ts: decision instant (ms epoch UTC).
        horizons: holding periods in hours.
        bars: ``(open_time_ms, open, high, low, close)`` tuples. Fetched
            for the longest horizon when omitted.

    Returns:
        A :class:`DirectionalOutcome`; points whose horizon the bars do
        not reach are returned with ``covered=False`` and no numbers,
        never with a silently truncated one.
    """
    side = _normalize_side(side)
    horizons = tuple(sorted(set(int(h) for h in horizons)))
    if not horizons or horizons[0] <= 0:
        raise ValueError("horizons must be positive hours")

    if bars is None:
        bars = fetch_window_bars(symbol, decision_ts, horizons[-1])
    bars = sorted(
        (int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])) for b in bars
    )

    out = DirectionalOutcome(
        symbol=symbol, side=side, decision_ts=decision_ts, entry_ts=None, entry_price=None
    )

    after = [b for b in bars if b[0] > decision_ts]
    if not after:
        out.notes.append("no bars after the decision instant")
        for h in horizons:
            out.points[h] = HorizonPoint(h, None, None, None, None, None, covered=False)
        return out

    entry_bar = after[0]
    out.entry_ts, out.entry_price = entry_bar[0], entry_bar[1]
    entry = out.entry_price

    step = _bar_step(bars)
    last_open = after[-1][0]
    sign = 1.0 if side == "LONG" else -1.0

    for h in horizons:
        target = decision_ts + h * _HOUR_MS
        window = [b for b in after if b[0] <= target]
        # One bar of slack: the bar opening at the horizon has not closed yet.
        if not window or last_open < target - step:
            out.points[h] = HorizonPoint(h, None, None, None, None, None, covered=False)
            continue

        exit_bar = window[-1]
        exit_price = exit_bar[4]
        ret = sign * (exit_price - entry) / entry * 100.0

        best = max(b[2] for b in window) if side == "LONG" else min(b[3] for b in window)
        worst = min(b[3] for b in window) if side == "LONG" else max(b[2] for b in window)
        mfe = max(sign * (best - entry) / entry * 100.0, 0.0)
        mae = max(-sign * (worst - entry) / entry * 100.0, 0.0)

        out.points[h] = HorizonPoint(
            horizon_hours=h,
            exit_ts=exit_bar[0],
            exit_price=exit_price,
            return_pct=ret,
            mfe_pct=mfe,
            mae_pct=mae,
            covered=True,
        )

    return out
