"""Position monitor — reconciles Binance and local state, both directions.

When a stop-loss or take-profit order triggers on Binance, the position is
closed on the exchange but the local risk_gate_state.jsonl does not
automatically record the closure. This creates two problems:

1. **Gate misfire** — the gate still thinks the position is open,
   so max_concurrent_positions rejects new entries.
2. **Orphan orders** — the unused stop or TP order (whichever didn't
   trigger) accumulates on the exchange, eventually causing -4130 errors.

The reverse direction matters too (T12): if the process died between the
exchange fill and the local ``position_opened`` append, the exchange holds
a position the JSONL knows nothing about. The executor's write-ahead
``order_submitted`` event marks these as *dangling intents* — the gate
blocks new entries until this monitor resolves them against live state.

**Design**: independent of LangGraph, designed for periodic execution
(launchd job or pre-step hook). Polls Binance testnet position information,
diffs against the JSONL in both directions, and:

- Writes a ``position_closed`` event for positions closed on the exchange,
  with real P&L backfilled from ``futures_income_history`` and the closure
  mode (stop / tp / manual / unknown) inferred from the opening event's
  stop/TP prices.
- Adopts exchange positions that match a dangling intent (``position_opened``
  with the original ``intent_id``), and flags ones that match nothing as
  ``position_untracked`` — both count toward the gate's concurrency cap;
  untracked ones additionally raise a critical alert (see futures/alerts.py).
- Dismisses dangling intents with no exchange position and no realized
  P&L (the submit never filled) via an intent-resolving ``trade_skipped``.
- Cancels orphaned Basic and algo orders.

Dryrun mode: reads JSONL, mocks exchange state, no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.futures.risk_state import (
    DEFAULT_DANGLING_INTENT_MINUTES,
    _parse_ts,
    append_event,
    default_state_path,
    load_events,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorResult:
    """Outcome of a monitor run."""

    success: bool
    positions_checked: int
    positions_closed: int
    """Count of positions reconciled into the JSONL."""

    orphan_basic_orders_cancelled: int
    orphan_algo_orders_pending: int
    """Algo orders that need T2 cancel API (deferred)."""

    positions_adopted: int = 0
    """Exchange positions matched to a dangling intent and recorded as
    ``position_opened`` with the original intent_id."""

    untracked_found: int = 0
    """Exchange positions with no local record and no dangling intent —
    recorded as ``position_untracked`` (manual attention required)."""

    dangling_dismissed: int = 0
    """Dangling intents resolved as never-filled (no exchange position,
    no realized P&L) via an intent-resolving ``trade_skipped``."""

    pnl_backfill_failures: int = 0
    """Closes recorded with pnl_usd=0.0 / outcome=unknown because the
    income-history fetch failed — the data gap is flagged on the event."""

    dangling_remaining: int = 0
    """Dangling intents this run could NOT resolve (income unavailable,
    overlap with a tracked position, or ambiguous adoption) — the gate
    stays closed until a human intervenes, so this must surface in the
    exit code and alerts, not just the log."""

    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Exchange adapter protocol (testable via mocking)
# ---------------------------------------------------------------------------


class ExchangeAdapter(Protocol):
    """Abstraction over exchange position fetch.

    Dryrun and testnet both implement this, allowing unit tests to
    inject a mock without network calls.
    """

    def get_open_positions(self) -> dict[str, dict]:
        """Fetch current open positions from Binance.

        Returns a dict keyed by symbol (e.g. ``"BTCUSDT"``), valued
        at position dicts containing:
        - ``symbol``: str
        - ``positionAmt``: float (may be negative for short)
        - ``entryPrice``: float

        Empty dict on no positions. Raises on network / auth errors.
        """
        ...

    def cancel_all_basic_orders(self, symbol: str) -> bool:
        """Cancel all Basic (non-algo) orders for ``symbol`` on the exchange.

        Returns ``True`` on success, ``False`` on failure. Failures are
        logged; the monitor continues processing other symbols.
        """
        ...

    def cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        """Cancel a specific algo order by ID.
        
        Parameters
        ----------
        symbol
            Trading symbol (e.g. ``"BTCUSDT"``).
        algo_id
            The algo order ID to cancel.
            
        Returns
        -------
        bool
            ``True`` on success, ``False`` on failure.
        """
        ...

    def cancel_all_algo_orders(self, symbol: str) -> int:
        """Cancel all algo orders for a symbol.

        Parameters
        ----------
        symbol
            Trading symbol (e.g. ``"BTCUSDT"``).

        Returns
        -------
        int
            Number of orders cancelled. Returns 0 on failure.
        """
        ...

    def get_realized_pnl(self, symbol: str, start_time_ms: int) -> Optional[float]:
        """Sum of realized P&L for ``symbol`` since ``start_time_ms``.

        Wraps ``futures_income_history`` (incomeType=REALIZED_PNL) on the
        live adapter. Returns ``None`` when the fetch fails — callers must
        degrade to pnl_usd=0.0 / outcome=unknown and flag the data gap
        (never silently).
        """
        ...



# ---------------------------------------------------------------------------
# Dryrun adapter (for testing)
# ---------------------------------------------------------------------------


class DryrunExchange:
    """Mock exchange that returns empty positions (already closed)."""

    mode = "dryrun"

    def __init__(self):
        self.cancelled_symbols: set[str] = set()

    def get_open_positions(self) -> dict[str, dict]:
        # In dryrun, all positions are already closed (empty state).
        return {}

    def cancel_all_basic_orders(self, symbol: str) -> bool:
        self.cancelled_symbols.add(symbol)
        return True

    def cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        self.cancelled_symbols.add(symbol)
        return True

    def cancel_all_algo_orders(self, symbol: str) -> int:
        self.cancelled_symbols.add(symbol)
        return 0  # Mock: no orders to cancel in dryrun

    def get_realized_pnl(self, symbol: str, start_time_ms: int) -> Optional[float]:
        # No real trades happen in dryrun; realized P&L is always zero.
        return 0.0



# ---------------------------------------------------------------------------
# Testnet adapter
# ---------------------------------------------------------------------------


class TestnetExchange:
    __test__ = False
    """Live testnet adapter using python-binance."""

    mode = "testnet"

    def __init__(self, api_key: str, api_secret: str, *, client_cls=None):
        if client_cls is None:
            from binance.client import Client  # type: ignore[import-untyped]
            client_cls = Client
        self.client = client_cls(api_key, api_secret, testnet=True)

    def get_open_positions(self) -> dict[str, dict]:
        """Fetch open positions via futures_position_information."""
        try:
            positions = self.client.futures_position_information()
            # Filter to positions with non-zero amount.
            return {
                p["symbol"]: p for p in positions if float(p.get("positionAmt", 0)) != 0
            }
        except Exception as exc:
            logger.error("Failed to fetch positions: %s", exc)
            raise

    def cancel_all_basic_orders(self, symbol: str) -> bool:
        """Cancel all Basic orders for symbol."""
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            logger.info("Cancelled all basic orders for %s", symbol)
            return True
        except Exception as exc:
            logger.error("Failed to cancel basic orders for %s: %s", symbol, exc)
            return False


    def cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        """Cancel a specific algo order (DELETE /fapi/v1/algoOrder).

        ``symbol`` is required by the endpoint alongside ``algoId``.
        Verified live on testnet 2026-07-18.
        """
        try:
            self.client.futures_cancel_algo_order(symbol=symbol, algoId=algo_id)
            logger.info("Cancelled algo order %d for %s", algo_id, symbol)
            return True
        except Exception as exc:
            logger.error("Failed to cancel algo order %d for %s: %s", algo_id, symbol, exc)
            return False

    def cancel_all_algo_orders(self, symbol: str) -> int:
        """Cancel all open algo orders for a symbol.

        Lists via GET /fapi/v1/openAlgoOrders to report an accurate count,
        then cancels in one shot via DELETE /fapi/v1/algoOpenOrders. Both
        are exposed by python-binance as ``futures_get_open_algo_orders`` /
        ``futures_cancel_all_algo_open_orders``. Verified live on testnet
        2026-07-18.
        """
        try:
            resp = self.client.futures_get_open_algo_orders(symbol=symbol)
            # Endpoint may return a bare list or {"total": n, "orders": [...]}.
            orders = resp.get("orders", resp) if isinstance(resp, dict) else resp
            if not orders:
                logger.info("No open algo orders for %s", symbol)
                return 0

            self.client.futures_cancel_all_algo_open_orders(symbol=symbol)
            logger.info("Cancelled %d algo orders for %s", len(orders), symbol)
            return len(orders)
        except Exception as exc:
            logger.error("Failed to cancel all algo orders for %s: %s", symbol, exc)
            return 0

    def get_realized_pnl(self, symbol: str, start_time_ms: int) -> Optional[float]:
        """Sum REALIZED_PNL income entries for ``symbol`` since ``start_time_ms``.

        With one position per symbol at a time (one-way mode, 2-symbol
        scope), the sum over the window since the open is exactly this
        position's realized P&L.
        """
        try:
            entries = self.client.futures_income_history(
                symbol=symbol,
                incomeType="REALIZED_PNL",
                startTime=start_time_ms,
                limit=1000,
            )
            return sum(float(e.get("income", 0.0)) for e in entries)
        except Exception as exc:
            logger.error("Failed to fetch income history for %s: %s", symbol, exc)
            return None



# ---------------------------------------------------------------------------
# Position reconciliation logic
# ---------------------------------------------------------------------------


def _symbol_from_state(state_ticker: str) -> str:
    """Convert state ticker (e.g. ``"BTC-USD"``) to Binance symbol."""
    return to_binance_symbol(state_ticker)


def _state_symbol_from_binance(binance_symbol: str) -> str:
    """Reverse of :func:`to_binance_symbol` (``"BTCUSDT"`` → ``"BTC-USD"``).

    Needed for exchange-only positions, which arrive keyed by Binance
    symbol but must be logged in the state-ticker convention.
    """
    s = binance_symbol.strip().upper()
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}-USD"
    return s


def _ts_to_ms(ts_str: str) -> int:
    return int(_parse_ts(ts_str).timestamp() * 1000)


_OUTCOME_PRICE_TOLERANCE = 0.01
"""Relative distance within which the derived close price must land on
the stop / TP to claim that outcome; farther than this from both → manual.
Capped per candidate at half the entry→trigger distance: with a tight
stop (e.g. 0.15% away), 1% of price dwarfs the whole stop distance and a
break-even manual close would otherwise be labelled a stop-out (arming
the cooldown for nothing). Half-distance keeps the claim meaningful —
the close must be nearer the trigger than the entry."""


def _infer_close_outcome(open_event: dict, pnl_usd: float) -> str:
    """Infer closure mode (stop / tp / manual / unknown) from real P&L.

    New-format opening events (T12) carry ``side`` / ``entry_price`` /
    ``quantity`` / ``stop_loss`` / ``take_profit``, so the close price can
    be derived from the realized P&L and nearest-neighbour matched against
    the stop / TP prices. Old-format events lack those fields — fall back
    to the P&L sign: a loss is treated as a stop-out (conservative: it
    arms the cooldown), a gain as a manual/TP close (indistinguishable),
    zero as unknown.
    """
    entry = open_event.get("entry_price")
    qty = open_event.get("quantity")
    side = open_event.get("side")
    stop = open_event.get("stop_loss")

    if entry and qty and side in ("BUY", "SELL") and stop:
        close = entry + pnl_usd / qty if side == "BUY" else entry - pnl_usd / qty
        candidates = [("stop", float(stop))]
        tp = open_event.get("take_profit")
        if tp:
            candidates.append(("tp", float(tp)))
        matched = []
        for outcome, price in candidates:
            tolerance = min(
                _OUTCOME_PRICE_TOLERANCE * abs(close),
                0.5 * abs(float(entry) - price),
            )
            if abs(close - price) <= tolerance:
                matched.append((abs(close - price), outcome))
        if matched:
            return min(matched)[1]
        return "manual"

    if pnl_usd < 0:
        return "stop"
    if pnl_usd > 0:
        return "manual"
    return "unknown"


def _backfill_close_pnl(
    exchange: ExchangeAdapter,
    binance_symbol: str,
    open_event: dict,
) -> tuple[float, str, bool]:
    """Return ``(pnl_usd, outcome, backfill_failed)`` for a detected close.

    Real P&L comes from the exchange income history over the window since
    the opening event. A failed fetch degrades to ``(0.0, "unknown",
    True)`` — the caller flags the event so the data gap is visible to
    the alerts layer instead of silently deflating the drawdown counter.
    """
    try:
        start_ms = _ts_to_ms(open_event["ts"])
    except (KeyError, ValueError):
        start_ms = 0
    pnl = exchange.get_realized_pnl(binance_symbol, start_ms)
    if pnl is None:
        logger.error(
            "P&L backfill failed for %s — recording pnl_usd=0.0/outcome=unknown; "
            "daily drawdown and cooldown are blind to this close",
            binance_symbol,
        )
        return 0.0, "unknown", True
    return pnl, _infer_close_outcome(open_event, pnl), False


def reconcile_positions(
    jsonl_path: Path | str,
    exchange: ExchangeAdapter,
    *,
    symbol_filter: Optional[list[str]] = None,
    now: Optional[datetime] = None,
    dangling_intent_minutes: float = DEFAULT_DANGLING_INTENT_MINUTES,
) -> MonitorResult:
    """Poll the exchange and reconcile positions with the JSONL, both ways.

    Parameters
    ----------
    jsonl_path
        Path to risk_gate_state.jsonl.
    exchange
        Exchange adapter (testnet or dryrun).
    symbol_filter
        If set, only reconcile these state symbols (e.g. ``["BTC-USD"]``).
        Useful for testing a single pair.
    now
        Current UTC time. Defaults to ``datetime.now(timezone.utc)``.
        Tests should pass an explicit value for determinism.
    dangling_intent_minutes
        Only ``order_submitted`` events older than this are reconciled;
        younger ones are assumed in-flight in a live run (the same
        threshold the gate uses, so monitor and gate agree on what
        "dangling" means).

    Three passes:

    1. **Forward** — locally open, gone on the exchange → ``position_closed``
       with real P&L from income history + outcome inference; cancel
       orphaned Basic/algo orders.
    2. **Reverse** — on the exchange, unknown locally → adopt into
       ``position_opened`` when a dangling intent matches the symbol,
       else record ``position_untracked`` (alerts layer escalates).
    3. **Dismissal** — dangling intents with no exchange position: if the
       income history shows realized P&L since the submit, the position
       opened *and* closed while we were blind → backfill an opened +
       closed pair; otherwise the submit never filled → resolve with an
       intent-carrying ``trade_skipped``. A failed income fetch leaves the
       intent dangling (gate stays closed) rather than guessing.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # Events written this run are stamped with ``now`` so tests injecting a
    # fixed clock get a consistent JSONL (write-time == reconcile-time).
    now_iso = now.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    # Load current state from JSONL. position_untracked counts as an open
    # marker: it holds a gate-concurrency slot and must not be re-reported
    # on every monitor run.
    events = load_events(jsonl_path)
    open_by_symbol: dict[str, dict] = {}  # state symbol -> last opening event
    submitted: dict[str, dict] = {}       # intent_id -> order_submitted event
    resolved_intents: set[str] = set()
    for ev in events:
        ev_type = ev.get("type")
        symbol = ev.get("symbol")
        intent_id = ev.get("intent_id")
        if ev_type == "order_submitted":
            if intent_id:
                submitted[intent_id] = ev
            continue
        if intent_id:
            resolved_intents.add(intent_id)
        if ev_type in ("position_opened", "position_untracked"):
            if symbol:
                open_by_symbol[symbol] = ev
        elif ev_type == "position_closed":
            if symbol in open_by_symbol:
                del open_by_symbol[symbol]

    cutoff = now - timedelta(minutes=dangling_intent_minutes)
    dangling: dict[str, dict] = {
        intent_id: ev for intent_id, ev in submitted.items()
        if intent_id not in resolved_intents and _parse_ts(ev["ts"]) <= cutoff
    }

    # Fetch live positions from the exchange.
    try:
        binance_positions = exchange.get_open_positions()
    except Exception as exc:
        return MonitorResult(
            success=False,
            positions_checked=0,
            positions_closed=0,
            orphan_basic_orders_cancelled=0,
            orphan_algo_orders_pending=0,
            error=f"exchange.get_open_positions() failed: {exc}",
        )

    # Determine which symbols to check.
    symbols_to_check = set(open_by_symbol.keys())
    if symbol_filter:
        symbols_to_check &= set(symbol_filter)

    positions_closed = 0
    orphan_basic_cancelled = 0
    orphan_algo_pending = 0
    positions_adopted = 0
    untracked_found = 0
    dangling_dismissed = 0
    pnl_backfill_failures = 0

    # ----- Pass 1: forward — locally open, closed on the exchange. -----
    for state_symbol in symbols_to_check:
        binance_symbol = _symbol_from_state(state_symbol)
        open_event = open_by_symbol[state_symbol]

        if binance_symbol not in binance_positions:
            pnl_usd, outcome, backfill_failed = _backfill_close_pnl(
                exchange, binance_symbol, open_event,
            )
            close_event = {
                "type": "position_closed",
                "ts": now_iso,
                "intent_id": open_event.get("intent_id", "unknown"),
                "symbol": state_symbol,
                "pnl_usd": pnl_usd,
                "outcome": outcome,
            }
            if backfill_failed:
                close_event["pnl_backfill_failed"] = True
                pnl_backfill_failures += 1
            append_event(jsonl_path, close_event)
            positions_closed += 1
            logger.info(
                "Reconciled closed position: %s (outcome=%s, pnl_usd=%.2f)",
                state_symbol, outcome, pnl_usd,
            )

            # Cancel orphaned orders for this symbol.
            # Basic orders (MARKET, LIMIT, STOP_MARKET/TAKE_PROFIT_MARKET on some systems)
            # can be cancelled via futures_cancel_all_open_orders. Algo orders
            # (STOP_MARKET/TAKE_PROFIT_MARKET on algo-order systems) go through
            # the adapter's algo-cancel endpoints.
            if exchange.cancel_all_basic_orders(binance_symbol):
                # TODO: count how many orders were actually cancelled
                # (Binance API doesn't return count; we'd need to fetch
                # open orders before/after or parse the response).
                orphan_basic_cancelled += 1
            else:
                # Log it but continue; if one symbol fails, check the others.
                logger.warning(
                    "Failed to cancel basic orders for %s; continuing",
                    binance_symbol,
                )

            # Algo orders: cancel via the exchange adapter.
            try:
                _cancel_orphaned_algo_orders(binance_symbol, exchange)
            except Exception as exc:
                # If algo cancellation fails, log it but don't block other symbols
                logger.warning(
                    "Failed to cancel algo orders for %s: %s; "
                    "manual intervention may be needed",
                    binance_symbol, exc,
                )

    # ----- Pass 2: reverse — on the exchange, unknown locally. -----
    local_binance_symbols = {_symbol_from_state(s) for s in open_by_symbol}
    for binance_symbol, position in binance_positions.items():
        if binance_symbol in local_binance_symbols:
            continue
        state_symbol = _state_symbol_from_binance(binance_symbol)
        if symbol_filter and state_symbol not in symbol_filter:
            continue

        position_amt = float(position.get("positionAmt", 0.0))
        entry_price = float(position.get("entryPrice", 0.0))
        position_side = "BUY" if position_amt > 0 else "SELL"

        # Adopt ONLY when exactly one dangling intent matches the symbol
        # AND its recorded side agrees with the live position. With
        # several candidates, guessing (e.g. "newest") can pair the
        # position with the wrong intent's stop/TP — corrupting the later
        # close-outcome inference — while the leftover intent parks in
        # the overlap branch of pass 3 forever. A side mismatch means the
        # position was reversed outside our control. Both cases fall
        # through to position_untracked + alert for a human to resolve.
        matches = []
        for intent_id, sub_ev in dangling.items():
            try:
                if _symbol_from_state(sub_ev.get("symbol", "")) != binance_symbol:
                    continue
            except ValueError:
                continue
            matches.append(intent_id)

        match_id = None
        if len(matches) == 1:
            if dangling[matches[0]].get("side") == position_side:
                match_id = matches[0]
            else:
                logger.error(
                    "Dangling intent %s for %s has side %s but the exchange "
                    "position is %s — refusing adoption, manual review required",
                    matches[0], binance_symbol,
                    dangling[matches[0]].get("side"), position_side,
                )
        elif len(matches) > 1:
            logger.error(
                "%d dangling intents (%s) match exchange position %s — "
                "ambiguous, refusing adoption; manual review required",
                len(matches), ", ".join(matches), binance_symbol,
            )

        if match_id is not None:
            sub_ev = dangling.pop(match_id)
            append_event(jsonl_path, {
                "type": "position_opened",
                "ts": now_iso,
                "intent_id": match_id,
                "symbol": sub_ev.get("symbol", state_symbol),
                "adopted": True,
                "side": position_side,
                "entry_price": entry_price,
                "quantity": abs(position_amt),
                "stop_loss": sub_ev.get("stop_loss"),
                "take_profit": sub_ev.get("take_profit"),
            })
            positions_adopted += 1
            logger.warning(
                "Adopted exchange position %s from dangling intent %s — "
                "protective stop/TP placement is unverified, check the exchange",
                binance_symbol, match_id,
            )
        else:
            append_event(jsonl_path, {
                "type": "position_untracked",
                "ts": now_iso,
                "symbol": state_symbol,
                "binance_symbol": binance_symbol,
                "quantity": position_amt,
                "entry_price": entry_price,
            })
            untracked_found += 1
            logger.error(
                "UNTRACKED position on exchange: %s (amt=%s, entry=%s) — "
                "no local record and no dangling intent; manual intervention required",
                binance_symbol, position_amt, entry_price,
            )

    # ----- Pass 3: dismiss dangling intents with no exchange position. -----
    for intent_id, sub_ev in list(dangling.items()):
        try:
            binance_symbol = _symbol_from_state(sub_ev.get("symbol", ""))
        except ValueError:
            binance_symbol = None
        if binance_symbol and binance_symbol in binance_positions:
            # Exchange has a position for this symbol but it's already
            # locally tracked — whether the dangling order contributed to
            # it is undecidable here. Leave the intent dangling (gate stays
            # closed) for the operator.
            logger.error(
                "Dangling intent %s for %s overlaps an already-tracked exchange "
                "position — cannot auto-reconcile, manual review required",
                intent_id, binance_symbol,
            )
            continue

        pnl = None
        if binance_symbol:
            pnl = exchange.get_realized_pnl(binance_symbol, _ts_to_ms(sub_ev["ts"]))
        if pnl is None:
            logger.error(
                "Cannot verify dangling intent %s (%s): income history "
                "unavailable — leaving it dangling, gate stays closed",
                intent_id, sub_ev.get("symbol"),
            )
            continue

        if pnl != 0.0:
            # The order filled and the position already closed while we
            # were blind — backfill the full open/close pair so drawdown
            # and cooldown see the loss.
            outcome = _infer_close_outcome(sub_ev, pnl)
            append_event(jsonl_path, {
                "type": "position_opened",
                "ts": now_iso,
                "intent_id": intent_id,
                "symbol": sub_ev.get("symbol"),
                "adopted": True,
                "side": sub_ev.get("side"),
                "stop_loss": sub_ev.get("stop_loss"),
                "take_profit": sub_ev.get("take_profit"),
            })
            append_event(jsonl_path, {
                "type": "position_closed",
                "ts": now_iso,
                "intent_id": intent_id,
                "symbol": sub_ev.get("symbol"),
                "pnl_usd": pnl,
                "outcome": outcome,
            })
            positions_closed += 1
            logger.warning(
                "Dangling intent %s reconciled as opened-and-closed "
                "(pnl_usd=%.2f, outcome=%s)", intent_id, pnl, outcome,
            )
        else:
            append_event(jsonl_path, {
                "type": "trade_skipped",
                "ts": now_iso,
                "intent_id": intent_id,
                "symbol": sub_ev.get("symbol"),
                "reason": ("dangling intent reconciled: no exchange position or "
                           "realized pnl since submit — order presumed never filled"),
            })
            logger.info("Dismissed dangling intent %s (never filled)", intent_id)
        dangling_dismissed += 1
        del dangling[intent_id]

    return MonitorResult(
        success=True,
        positions_checked=len(symbols_to_check),
        positions_closed=positions_closed,
        orphan_basic_orders_cancelled=orphan_basic_cancelled,
        orphan_algo_orders_pending=orphan_algo_pending,
        positions_adopted=positions_adopted,
        untracked_found=untracked_found,
        dangling_dismissed=dangling_dismissed,
        pnl_backfill_failures=pnl_backfill_failures,
        dangling_remaining=len(dangling),
    )


def _cancel_orphaned_algo_orders(binance_symbol: str, exchange: ExchangeAdapter) -> None:
    """Cancel orphaned algo orders (STOP_MARKET / TAKE_PROFIT_MARKET).

    Calls exchange.cancel_all_algo_orders() which is implemented in T2.
    See: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Cancel-Algo-Order
    """
    try:
        count = exchange.cancel_all_algo_orders(binance_symbol)
        logger.info("Cancelled %d algo orders for %s", count, binance_symbol)
    except Exception as exc:
        logger.error("Algo order cancellation failed for %s: %s", binance_symbol, exc)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_monitor(config: dict, *, client_cls=None) -> ExchangeAdapter:
    """Build the monitor's exchange adapter based on config / env.

    Precedence: ``MONITOR_MODE`` env var > ``config["futures_monitor_mode"]``
    > default ``"dryrun"``.
    """
    import os

    mode = (
        os.getenv("MONITOR_MODE")
        or config.get("futures_monitor_mode")
        or "dryrun"
    ).lower()

    if mode == "testnet":
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError(
                "MONITOR_MODE=testnet but BINANCE_TESTNET_API_KEY / "
                "BINANCE_TESTNET_API_SECRET not set in env"
            )
        return TestnetExchange(api_key, api_secret, client_cls=client_cls)
    if mode == "dryrun":
        return DryrunExchange()
    raise ValueError(f"Unknown MONITOR_MODE: {mode!r} (expected 'dryrun' or 'testnet')")


# ---------------------------------------------------------------------------
# CLI entry point (launchd job / manual reconciliation runs)
# ---------------------------------------------------------------------------


def main(args: Optional[list] = None) -> int:
    """Run one reconciliation pass and print a JSON summary.

    ``python -m tradingagents.futures.position_monitor [--state-path P]``
    Mode comes from ``MONITOR_MODE`` (dryrun default; testnet needs
    BINANCE_TESTNET_API_KEY/SECRET). Exit codes: 0 = clean, 1 = attention
    needed (untracked positions / P&L data gaps / left-dangling intents),
    2 = run failed. Suitable as a launchd hook next to futures.alerts.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="Reconcile Binance positions with the local risk-state JSONL",
    )
    parser.add_argument(
        "--state-path", type=str, default=None,
        help="Path to risk_gate_state.jsonl (default: standard state path)",
    )
    parsed = parser.parse_args(args)

    state_path = Path(parsed.state_path) if parsed.state_path else default_state_path()
    exchange = create_monitor({})
    result = reconcile_positions(state_path, exchange)

    print(_json.dumps({
        "mode": getattr(exchange, "mode", "unknown"),
        "state_path": str(state_path),
        **{k: getattr(result, k) for k in (
            "success", "positions_checked", "positions_closed",
            "positions_adopted", "untracked_found", "dangling_dismissed",
            "pnl_backfill_failures", "dangling_remaining", "error",
        )},
    }, indent=2))

    if not result.success:
        return 2
    if result.untracked_found or result.pnl_backfill_failures or result.dangling_remaining:
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
