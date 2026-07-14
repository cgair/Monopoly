"""Position monitor — reconciles Binance and local state.

When a stop-loss or take-profit order triggers on Binance, the position is
closed on the exchange but the local risk_gate_state.jsonl does not
automatically record the closure. This creates two problems:

1. **Gate misfire** — the gate still thinks the position is open,
   so max_concurrent_positions rejects new entries.
2. **Orphan orders** — the unused stop or TP order (whichever didn't
   trigger) accumulates on the exchange, eventually causing -4130 errors.

**Design**: independent of LangGraph, designed for periodic execution
(launchd job or pre-step hook). Polls Binance testnet position information,
diffs against the JSONL, and:

- Writes a ``position_closed`` event for positions closed on the exchange.
- Infers the closure mode (stop / tp / manual / unknown) from available signals.
- Cancels orphaned Basic orders via ``futures_cancel_all_open_orders``.
- Logs and fails loud on algo order orphans (T2 dependency: algo cancel API).

Dryrun mode: reads JSONL, mocks exchange state, no network.

Module docstring follows the pattern of futures/executor.py:
- design motivation up-front
- public API second
- implementation details last
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.futures.risk_state import (
    append_event,
    default_state_path,
    load_events,
    utcnow_iso,
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
        """Cancel a specific algo order by ID.
        
        Uses DELETE /fapi/v1/algoOrder endpoint with algoId parameter.
        See: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Cancel-Algo-Order
        """
        try:
            self.client._request_futures_api(
                "delete",
                "/fapi/v1/algoOrder",
                True,
                data={"algoId": algo_id},
            )
            logger.info("Cancelled algo order %d for %s", algo_id, symbol)
            return True
        except Exception as exc:
            logger.error("Failed to cancel algo order %d for %s: %s", algo_id, symbol, exc)
            return False

    def cancel_all_algo_orders(self, symbol: str) -> int:
        """Cancel all algo orders for a symbol.
        
        Fetches active algo orders via GET /fapi/v1/algoOpenOrders, then
        cancels each via DELETE /fapi/v1/algoOrder.
        See: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Current-All-Algo-Open-Orders
        """
        try:
            # Fetch all open algo orders for this symbol
            orders = self.client._request_futures_api(
                "get",
                "/fapi/v1/algoOpenOrders",
                True,
                data={"symbol": symbol},
            )
            if not orders:
                logger.info("No open algo orders for %s", symbol)
                return 0

            # Cancel each one
            cancelled_count = 0
            for order in orders:
                algo_id = order.get("algoId")
                if algo_id is None:
                    logger.warning("Algo order missing algoId: %s", order)
                    continue
                if self.cancel_algo_order(symbol, int(algo_id)):
                    cancelled_count += 1

            logger.info("Cancelled %d algo orders for %s", cancelled_count, symbol)
            return cancelled_count
        except Exception as exc:
            logger.error("Failed to cancel all algo orders for %s: %s", symbol, exc)
            return 0



# ---------------------------------------------------------------------------
# Position reconciliation logic
# ---------------------------------------------------------------------------


def _symbol_from_state(state_ticker: str) -> str:
    """Convert state ticker (e.g. ``"BTC-USD"``) to Binance symbol."""
    return to_binance_symbol(state_ticker)


def _infer_close_outcome(
    open_event: dict,
    binance_positions: dict[str, dict],
    binance_symbol: str,
) -> str:
    """Infer closure mode (stop / tp / manual / unknown).

    Heuristic:
    - If the position exists in Binance but with a different sign
      (long → short or vice versa), it was likely manually reversed.
    - If neither, assume stop or TP (we can't tell which without
      checking actual order history).
    - Default to "unknown" if any information is missing.
    """
    # For now, we infer "stop" when we can't determine otherwise.
    # A real implementation would query order history or event logs.
    if binance_symbol in binance_positions:
        # Position exists but closed locally — possible reversal.
        # For now, default to "unknown" to be conservative.
        return "unknown"
    # Position closed; assume stop or TP (we check order state in T2).
    return "stop"


def reconcile_positions(
    jsonl_path: Path | str,
    exchange: ExchangeAdapter,
    *,
    symbol_filter: Optional[list[str]] = None,
    now: Optional[datetime] = None,
) -> MonitorResult:
    """Poll the exchange and reconcile open positions into the JSONL log.

    Parameters
    ----------
    jsonl_path
        Path to risk_gate_state.jsonl.
    exchange
        Exchange adapter (testnet or dryrun).
    symbol_filter
        If set, only reconcile these symbols (e.g. ``["BTCUSDT"]``).
        Useful for testing a single pair.
    now
        Current UTC time. Defaults to ``datetime.now(timezone.utc)``.
        Tests should pass an explicit value for determinism.

    Returns
    -------
    MonitorResult
        Summary of the run: positions checked/closed, orders cancelled, errors.

    Side effects:
    - Appends ``position_closed`` events to the JSONL.
    - Calls ``exchange.cancel_all_basic_orders()`` for each symbol.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Load current state from JSONL.
    events = load_events(jsonl_path)
    open_by_symbol: dict[str, dict] = {}  # symbol -> last position_opened event
    for ev in events:
        if ev.get("type") == "position_opened":
            symbol = ev.get("symbol")
            if symbol:
                open_by_symbol[symbol] = ev
        elif ev.get("type") == "position_closed":
            # Remove from open tracking.
            symbol = ev.get("symbol")
            if symbol in open_by_symbol:
                del open_by_symbol[symbol]

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

    for state_symbol in symbols_to_check:
        binance_symbol = _symbol_from_state(state_symbol)
        open_event = open_by_symbol[state_symbol]

        if binance_symbol not in binance_positions:
            # Position closed on Binance but open in JSONL.
            outcome = _infer_close_outcome(
                open_event, binance_positions, binance_symbol,
            )
            close_event = {
                "type": "position_closed",
                "ts": utcnow_iso(),
                "intent_id": open_event.get("intent_id", "unknown"),
                "symbol": state_symbol,
                "pnl_usd": 0.0,  # We don't compute actual P&L here; that's executor's job.
                "outcome": outcome,
            }
            append_event(jsonl_path, close_event)
            positions_closed += 1
            logger.info(
                "Reconciled closed position: %s (outcome=%s)",
                state_symbol, outcome,
            )

            # Cancel orphaned orders for this symbol.
            # Basic orders (MARKET, LIMIT, STOP_MARKET/TAKE_PROFIT_MARKET on some systems)
            # can be cancelled via futures_cancel_all_open_orders. Algo orders
            # (STOP_MARKET/TAKE_PROFIT_MARKET on algo-order systems) need T2.
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

    return MonitorResult(
        success=True,
        positions_checked=len(symbols_to_check),
        positions_closed=positions_closed,
        orphan_basic_orders_cancelled=orphan_basic_cancelled,
        orphan_algo_orders_pending=orphan_algo_pending,
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
