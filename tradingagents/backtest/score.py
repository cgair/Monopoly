"""Decision-outcome scorer: score a trading decision against real subsequent price action.

Quantifies how well a specific entry tactic (limit/market at a given
price) performed relative to:
  - Its own parameters (stop-loss, take-profit)
  - A market-entry counterfactual (same direction, market entry)

No look-ahead concerns: the scorer is only called on bars AFTER the
decision timestamp. The market data input is trusted to be clean
(correct timestamps, no gaps, aligned to decision timezone).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

import requests

from tradingagents.agents.utils.symbol_utils import to_binance_symbol

logger = logging.getLogger(__name__)


_BINANCE_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
_KLINE_FETCH_TIMEOUT = 5.0
_KLINE_BATCH_LIMIT = 1500


def _normalize_side(side: str) -> str:
    """Accept "LONG", "SHORT", "BUY", "SELL", case-insensitive."""
    normalized = side.strip().upper()
    if normalized in ("BUY", "LONG"):
        return "LONG"
    elif normalized in ("SELL", "SHORT"):
        return "SHORT"
    else:
        raise ValueError(f"side must be LONG/SHORT/BUY/SELL, got: {side}")


def _fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    interval: str = "5m",
) -> list[tuple]:
    """Fetch 5-minute klines from Binance for [start_ms, end_ms], returning (open_time_ms, open, high, low, close).

    Paginates if needed (1500 limit per request). Returns sorted by open_time_ms.

    Args:
        symbol: Binance symbol (e.g. "BTCUSDT").
        start_ms: start timestamp (ms, inclusive).
        end_ms: end timestamp (ms, inclusive).
        interval: kline interval (default "5m").

    Returns:
        List of (open_time_ms, open, high, low, close) tuples.

    Raises:
        Exception: on network error (will be caught by caller).
    """
    bars = []
    current_start = start_ms

    while current_start < end_ms:
        try:
            response = requests.get(
                _BINANCE_KLINES_URL,
                params={
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": current_start,
                    "endTime": end_ms,
                    "limit": _KLINE_BATCH_LIMIT,
                },
                timeout=_KLINE_FETCH_TIMEOUT,
            )
            response.raise_for_status()
            batch = response.json()

            if not batch:
                break

            for kline in batch:
                open_time_ms = int(kline[0])
                open_price = float(kline[1])
                high_price = float(kline[2])
                low_price = float(kline[3])
                close_price = float(kline[4])
                bars.append((open_time_ms, open_price, high_price, low_price, close_price))

            if len(batch) < _KLINE_BATCH_LIMIT:
                break

            current_start = int(batch[-1][0]) + 1

        except Exception as exc:
            logger.error(
                "Failed to fetch klines for %s [%d, %d]: %s",
                symbol,
                start_ms,
                end_ms,
                exc,
            )
            raise

    return sorted(bars, key=lambda x: x[0])


@dataclass
class ScoredOutcome:
    """Outcome of a scored decision over a set of bars."""

    symbol: str
    side: str  # "LONG" | "SHORT"
    decision_ts: int  # ms epoch UTC
    entry_price: float | None  # limit/target entry, None if market
    stop_loss: float
    take_profit: float

    # fill simulation
    filled: bool
    fill_ts: int | None
    fill_price: float | None
    closest_approach: float | None  # best price reached toward an unfilled limit
    miss_by_pct: float | None  # how far the unfilled limit missed, in %

    # outcome from the actual fill
    outcome: str | None  # "take_profit" | "stop_loss" | "open" | "ambiguous"
    exit_ts: int | None
    exit_price: float | None
    r_multiple: float | None
    pnl_pct: float | None
    hold_hours: float | None
    mfe_pct: float | None  # max favorable excursion from fill, %
    mae_pct: float | None  # max adverse excursion from fill, %

    # counterfactual: market entry at decision time
    market_entry_price: float
    market_outcome: str
    market_r_multiple: float | None
    market_pnl_pct: float | None
    market_hold_hours: float | None

    notes: list[str]


def score_decision(
    *,
    symbol: str,
    side: str,
    decision_ts: int,
    entry_price: float | None,
    stop_loss: float,
    take_profit: float,
    horizon_hours: int = 240,
    bars: list | None = None,
) -> ScoredOutcome:
    """Score a trading decision against subsequent price action.

    Args:
        symbol: "BTC-USD" or "BTCUSDT" form.
        side: "LONG", "SHORT", "BUY", or "SELL" (case-insensitive).
        decision_ts: decision timestamp (ms, UTC).
        entry_price: limit/target entry price, or None for market entry.
        stop_loss: stop-loss level.
        take_profit: take-profit level.
        horizon_hours: max duration to look for fill/outcome (default 240 = 10 days).
        bars: optional list of (open_time_ms, open, high, low, close) tuples.
              If None, fetches from Binance.

    Returns:
        ScoredOutcome with fill/outcome details and market counterfactual.

    Raises:
        ValueError: on invalid inputs (symbol, side, bars format, etc.).
        Exception: on network error (if bars not provided).
    """
    side = _normalize_side(side)
    notes = []

    if not symbol:
        raise ValueError("symbol is required")
    if stop_loss <= 0 or take_profit <= 0:
        raise ValueError("stop_loss and take_profit must be positive")
    if side == "LONG":
        if take_profit <= stop_loss:
            raise ValueError("for LONG: take_profit must be > stop_loss")
    else:  # SHORT
        if take_profit >= stop_loss:
            raise ValueError("for SHORT: take_profit must be < stop_loss")

    try:
        binance_symbol = to_binance_symbol(symbol)
    except ValueError as e:
        raise ValueError(f"Invalid symbol '{symbol}': {e}")

    # Fetch or validate bars
    horizon_ms = horizon_hours * 60 * 60 * 1000
    end_ts = decision_ts + horizon_ms

    if bars is None:
        bars = _fetch_klines(binance_symbol, decision_ts, end_ts)
    else:
        # Validate bar format
        if not isinstance(bars, list):
            raise ValueError("bars must be a list of tuples/objects")
        normalized_bars = []
        for bar in bars:
            if isinstance(bar, (tuple, list)):
                if len(bar) < 5:
                    raise ValueError("each bar tuple must have >= 5 elements")
                normalized_bars.append(
                    (int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]))
                )
            elif hasattr(bar, "__getitem__"):
                normalized_bars.append(
                    (int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]))
                )
            else:
                raise ValueError("bars must be tuples, lists, or subscriptable objects")
        bars = normalized_bars

    if not bars:
        notes.append("insufficient bars")
        return ScoredOutcome(
            symbol=symbol,
            side=side,
            decision_ts=decision_ts,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            filled=False,
            fill_ts=None,
            fill_price=None,
            closest_approach=None,
            miss_by_pct=None,
            outcome=None,
            exit_ts=None,
            exit_price=None,
            r_multiple=None,
            pnl_pct=None,
            hold_hours=None,
            mfe_pct=None,
            mae_pct=None,
            market_entry_price=0.0,
            market_outcome="insufficient_bars",
            market_r_multiple=None,
            market_pnl_pct=None,
            market_hold_hours=None,
            notes=notes,
        )

    # Simulate fill
    filled, fill_ts, fill_price, closest_approach, miss_by_pct = _simulate_fill(
        side, entry_price, bars, decision_ts
    )

    if not filled:
        notes.append("limit never filled")

    # Score the limit-entry trade (if filled)
    if filled:
        outcome, exit_ts, exit_price, r_mult, pnl, hold_h, mfe, mae = _score_outcome(
            side, fill_price, stop_loss, take_profit, fill_ts, bars
        )
        if outcome == "ambiguous":
            notes.append("ambiguous same-bar tp/sl")
        if outcome == "open":
            notes.append("horizon reached with position open")
    else:
        outcome = None
        exit_ts = None
        exit_price = None
        r_mult = None
        pnl = None
        hold_h = None
        mfe = None
        mae = None

    # Score the market-entry counterfactual
    market_entry_price, market_filled_ts, market_filled = _market_entry_price_and_ts(
        bars, decision_ts
    )
    if market_filled:
        (
            market_outcome,
            market_exit_ts,
            market_exit_price,
            market_r_mult,
            market_pnl,
            market_hold_h,
            _,
            _,
        ) = _score_outcome(side, market_entry_price, stop_loss, take_profit, market_filled_ts, bars)
    else:
        market_outcome = "no_data"
        market_exit_ts = None
        market_exit_price = None
        market_r_mult = None
        market_pnl = None
        market_hold_h = None

    return ScoredOutcome(
        symbol=symbol,
        side=side,
        decision_ts=decision_ts,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        filled=filled,
        fill_ts=fill_ts,
        fill_price=fill_price,
        closest_approach=closest_approach,
        miss_by_pct=miss_by_pct,
        outcome=outcome,
        exit_ts=exit_ts,
        exit_price=exit_price,
        r_multiple=r_mult,
        pnl_pct=pnl,
        hold_hours=hold_h,
        mfe_pct=mfe,
        mae_pct=mae,
        market_entry_price=market_entry_price,
        market_outcome=market_outcome,
        market_r_multiple=market_r_mult,
        market_pnl_pct=market_pnl,
        market_hold_hours=market_hold_h,
        notes=notes,
    )


def _simulate_fill(
    side: str,
    entry_price: float | None,
    bars: list[tuple],
    decision_ts: int,
) -> tuple[bool, int | None, float | None, float | None, float | None]:
    """Simulate fill for a limit or market entry.

    Returns: (filled, fill_ts, fill_price, closest_approach, miss_by_pct)
    """
    if entry_price is None:
        first_bar = next((b for b in bars if b[0] > decision_ts), None)
        if first_bar is None:
            return False, None, None, None, None
        market_ts = first_bar[0]
        market_price = first_bar[1]  # open
        return True, market_ts, market_price, None, None

    closest_approach = None
    for bar in bars:
        bar_ts, bar_open, bar_high, bar_low, bar_close = bar
        if bar_ts <= decision_ts:
            continue

        if side == "LONG":
            if bar_low <= entry_price:
                return True, bar_ts, entry_price, None, None
            if closest_approach is None or bar_low < closest_approach:
                closest_approach = bar_low
        else:  # SHORT
            if bar_high >= entry_price:
                return True, bar_ts, entry_price, None, None
            if closest_approach is None or bar_high > closest_approach:
                closest_approach = bar_high

    if closest_approach is not None:
        miss_by_pct = abs(entry_price - closest_approach) / entry_price * 100
    else:
        miss_by_pct = None

    return False, None, None, closest_approach, miss_by_pct


def _market_entry_price_and_ts(bars: list[tuple], decision_ts: int) -> tuple[float, int, bool]:
    """Get the market entry price (first bar open after decision) and its timestamp.

    Returns: (market_entry_price, fill_ts, filled)
    """
    for bar in bars:
        bar_ts = bar[0]
        if bar_ts > decision_ts:
            return bar[1], bar_ts, True
    return 0.0, 0, False


def _score_outcome(
    side: str,
    fill_price: float,
    stop_loss: float,
    take_profit: float,
    fill_ts: int,
    bars: list[tuple],
) -> tuple[str | None, int | None, float | None, float | None, float | None, float | None, float | None, float | None]:
    """Race stop/tp levels from fill onwards, compute metrics.

    Returns: (outcome, exit_ts, exit_price, r_multiple, pnl_pct, hold_hours, mfe_pct, mae_pct)
    """
    best_price = fill_price
    worst_price = fill_price
    exit_ts = None
    exit_price = None
    outcome = None
    tp_hit = False
    sl_hit = False
    last_bar_after_fill = None

    for bar in bars:
        bar_ts, bar_open, bar_high, bar_low, bar_close = bar
        if bar_ts <= fill_ts:
            continue

        last_bar_after_fill = bar

        if side == "LONG":
            if bar_high >= take_profit and not tp_hit:
                tp_hit = True
            if bar_low <= stop_loss and not sl_hit:
                sl_hit = True
            if bar_low < worst_price:
                worst_price = bar_low
            if bar_high > best_price:
                best_price = bar_high
        else:  # SHORT
            if bar_low <= take_profit and not tp_hit:
                tp_hit = True
            if bar_high >= stop_loss and not sl_hit:
                sl_hit = True
            if bar_high > worst_price:
                worst_price = bar_high
            if bar_low < best_price:
                best_price = bar_low

        if tp_hit and sl_hit:
            outcome = "ambiguous"
            exit_ts = bar_ts
            exit_price = bar_close
            break
        elif tp_hit:
            outcome = "take_profit"
            exit_ts = bar_ts
            exit_price = take_profit
            break
        elif sl_hit:
            outcome = "stop_loss"
            exit_ts = bar_ts
            exit_price = stop_loss
            break

    if outcome is None:
        outcome = "open"
        if last_bar_after_fill:
            exit_ts = last_bar_after_fill[0]
            exit_price = last_bar_after_fill[4]  # close
        else:
            exit_price = fill_price

    r_multiple = _compute_r_multiple(side, fill_price, exit_price, stop_loss)
    pnl_pct = _compute_pnl_pct(side, fill_price, exit_price)
    hold_hours = (exit_ts - fill_ts) / (60 * 60 * 1000) if exit_ts else None
    mfe_pct = _compute_mfe_pct(side, fill_price, best_price)
    mae_pct = _compute_mae_pct(side, fill_price, worst_price)

    return outcome, exit_ts, exit_price, r_multiple, pnl_pct, hold_hours, mfe_pct, mae_pct


def _compute_r_multiple(
    side: str, entry: float, exit_price: float, stop_loss: float
) -> float | None:
    """Compute r-multiple: how many 'risk units' did we make/lose?

    For LONG: (exit - entry) / (entry - sl)
    For SHORT: (entry - exit) / (sl - entry)
    """
    if side == "LONG":
        risk = entry - stop_loss
        if risk <= 0:
            return None
        return (exit_price - entry) / risk
    else:  # SHORT
        risk = stop_loss - entry
        if risk <= 0:
            return None
        return (entry - exit_price) / risk


def _compute_pnl_pct(side: str, entry: float, exit_price: float) -> float | None:
    """Signed percent move in the direction of the trade."""
    if side == "LONG":
        return ((exit_price - entry) / entry) * 100
    else:  # SHORT
        return ((entry - exit_price) / entry) * 100


def _compute_mfe_pct(side: str, entry: float, best_price: float) -> float | None:
    """Max favorable excursion from entry, as a positive percentage."""
    if side == "LONG":
        if best_price <= entry:
            return 0.0
        return ((best_price - entry) / entry) * 100
    else:  # SHORT
        if best_price >= entry:
            return 0.0
        return ((entry - best_price) / entry) * 100


def _compute_mae_pct(side: str, entry: float, worst_price: float) -> float | None:
    """Max adverse excursion from entry, as a positive percentage."""
    if side == "LONG":
        if worst_price >= entry:
            return 0.0
        return ((entry - worst_price) / entry) * 100
    else:  # SHORT
        if worst_price <= entry:
            return 0.0
        return ((worst_price - entry) / entry) * 100
