"""Futures order executor — pluggable dryrun | testnet.

Consumes an :class:`ExecutionIntent` approved by the risk gate and turns
it into either a logged JSON record (dryrun) or a live order on Binance
Futures Testnet. Selection is driven by ``EXECUTOR_MODE`` env var
(falls back to ``config["futures_executor_mode"]``, default ``dryrun``).

Validation-phase scope (per spec §3 Week 4): place the opening order
plus stop-loss / take-profit conditionals. Position monitoring,
closure events, and orphan-order cleanup are explicitly out of scope
— they get a dedicated phase once the closed-loop validation passes
on testnet. Closed positions therefore have to be reconciled into the
risk_state log out-of-band for now.

Sizing
------
The risk gate emits ``risk_pct`` (decimal fraction of equity to risk on
the trade). The executor converts to quantity using::

    risk_usd = risk_pct * equity_usd
    qty = risk_usd / |entry_price - stop_loss|

Leverage doesn't change qty — it changes how much margin is committed.
If required margin (``qty * entry_price / leverage``) exceeds available
equity, the order is rejected with a clear error rather than placed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from tradingagents.agents.schemas import FuturesSide
from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.futures.risk_gate import ExecutionIntent
from tradingagents.futures.risk_state import append_event, default_state_path, utcnow_iso


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of :meth:`ExecutorAdapter.place_order`."""

    success: bool
    intent_id: str
    mode: str
    """``"dryrun"`` or ``"testnet"``."""

    placed_at: str
    symbol: str
    side: str
    """``"BUY"`` or ``"SELL"`` — the Binance-side direction, not our internal Long/Short."""

    quantity: float
    notional_usd: float
    margin_required_usd: float
    order_id: Optional[str] = None
    avg_fill_price: Optional[float] = None
    stop_order_id: Optional[str] = None
    take_profit_order_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class ExecutorAdapter(Protocol):
    mode: str

    def place_order(
        self,
        intent: ExecutionIntent,
        *,
        equity_usd: float,
        mark_price: float,
    ) -> ExecutionResult: ...


# ---------------------------------------------------------------------------
# Sizing helpers (shared by both adapters)
# ---------------------------------------------------------------------------


def compute_sizing(
    intent: ExecutionIntent,
    *,
    equity_usd: float,
    mark_price: float,
) -> tuple[float, float, float, float]:
    """Return ``(reference_price, qty, notional_usd, margin_required_usd)``.

    ``reference_price`` is ``intent.entry_price`` when set (limit order)
    and ``mark_price`` otherwise (market). Quantity is computed from the
    risk budget and the stop distance — see module docstring.
    """
    reference_price = intent.entry_price if intent.entry_price is not None else mark_price
    risk_usd = intent.risk_pct * equity_usd
    risk_per_unit = abs(reference_price - intent.stop_loss)
    if risk_per_unit <= 0:
        raise ValueError("stop_loss equals entry — risk per unit is zero")
    qty = risk_usd / risk_per_unit
    notional = qty * reference_price
    margin_required = notional / intent.leverage
    return reference_price, qty, notional, margin_required


# ---------------------------------------------------------------------------
# Dryrun adapter
# ---------------------------------------------------------------------------


class DryRunExecutor:
    """Logs an order JSON to a file. No network. No state mutation.

    The orders log is a separate file from the risk-gate state log; the
    state log only records ``position_opened`` on successful execution
    (both dryrun and testnet emit this event, since the validation-mode
    risk-gate snapshot needs to know about in-flight positions even in
    dryrun).
    """

    mode = "dryrun"

    def __init__(self, orders_log_path: Path):
        self.orders_log_path = orders_log_path

    def place_order(
        self,
        intent: ExecutionIntent,
        *,
        equity_usd: float,
        mark_price: float,
    ) -> ExecutionResult:
        try:
            reference_price, qty, notional, margin = compute_sizing(
                intent, equity_usd=equity_usd, mark_price=mark_price,
            )
        except ValueError as e:
            return ExecutionResult(
                success=False,
                intent_id=intent.intent_id,
                mode=self.mode,
                placed_at=utcnow_iso(),
                symbol=intent.symbol,
                side=_intent_to_binance_side(intent.side),
                quantity=0.0,
                notional_usd=0.0,
                margin_required_usd=0.0,
                error=str(e),
            )

        if margin > equity_usd:
            return ExecutionResult(
                success=False,
                intent_id=intent.intent_id,
                mode=self.mode,
                placed_at=utcnow_iso(),
                symbol=intent.symbol,
                side=_intent_to_binance_side(intent.side),
                quantity=qty,
                notional_usd=notional,
                margin_required_usd=margin,
                error=(f"insufficient equity: margin required ${margin:.2f} > "
                       f"equity ${equity_usd:.2f}"),
            )

        record = {
            "ts": utcnow_iso(),
            "mode": self.mode,
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "binance_symbol": to_binance_symbol(intent.symbol),
            "side": _intent_to_binance_side(intent.side),
            "type": "MARKET" if intent.entry_price is None else "LIMIT",
            "quantity": qty,
            "reference_price": reference_price,
            "notional_usd": notional,
            "leverage": intent.leverage,
            "margin_required_usd": margin,
            "stop_loss": intent.stop_loss,
            "take_profit": intent.take_profit,
        }
        append_event(self.orders_log_path, record)

        return ExecutionResult(
            success=True,
            intent_id=intent.intent_id,
            mode=self.mode,
            placed_at=record["ts"],
            symbol=intent.symbol,
            side=record["side"],
            quantity=qty,
            notional_usd=notional,
            margin_required_usd=margin,
            order_id=f"dryrun-{intent.intent_id}",
            avg_fill_price=reference_price,
            raw=record,
        )


# ---------------------------------------------------------------------------
# Testnet adapter (Binance USDT-M Futures testnet)
# ---------------------------------------------------------------------------


class TestnetExecutor:
    # Prevent pytest from collecting this class as a test (name starts with Test).
    __test__ = False

    """Live testnet adapter using python-binance.

    Routing: ``Client(api_key, api_secret, testnet=True)`` directs all
    requests to ``testnet.binancefuture.com``. The library handles
    HMAC-SHA256 signing and ``recvWindow``; we only translate our
    intent into futures_*-API kwargs.

    Order shape per intent:
    1. ``futures_change_leverage`` for the symbol (idempotent).
    2. Opening order: ``MARKET`` if ``entry_price is None``, else ``LIMIT``.
    3. Stop-loss: ``STOP_MARKET`` with ``closePosition=True``.
    4. Take-profit (optional): ``TAKE_PROFIT_MARKET`` with ``closePosition=True``.

    Known gaps (deferred): orphan-order cleanup when one of stop / TP
    fills (Binance has no native futures-OCO), and position-closure
    event reconciliation back into the risk-state log.
    """

    mode = "testnet"

    def __init__(self, api_key: str, api_secret: str, *, client_cls=None):
        # Lazy import so dryrun-only runs do not require python-binance.
        if client_cls is None:
            from binance.client import Client  # type: ignore[import-untyped]
            client_cls = Client
        self.client = client_cls(api_key, api_secret, testnet=True)

    def place_order(
        self,
        intent: ExecutionIntent,
        *,
        equity_usd: float,
        mark_price: float,
    ) -> ExecutionResult:
        try:
            reference_price, qty, notional, margin = compute_sizing(
                intent, equity_usd=equity_usd, mark_price=mark_price,
            )
        except ValueError as e:
            return self._error_result(intent, error=str(e))

        if margin > equity_usd:
            return self._error_result(
                intent,
                error=(f"insufficient equity: margin required ${margin:.2f} > "
                       f"equity ${equity_usd:.2f}"),
                quantity=qty, notional_usd=notional, margin_required_usd=margin,
            )

        binance_symbol = to_binance_symbol(intent.symbol)
        binance_side = _intent_to_binance_side(intent.side)
        close_side = "SELL" if binance_side == "BUY" else "BUY"
        qty_rounded = self._round_qty(qty)

        try:
            self.client.futures_change_leverage(
                symbol=binance_symbol, leverage=int(round(intent.leverage)),
            )

            if intent.entry_price is None:
                open_resp = self.client.futures_create_order(
                    symbol=binance_symbol,
                    side=binance_side,
                    type="MARKET",
                    quantity=qty_rounded,
                )
            else:
                open_resp = self.client.futures_create_order(
                    symbol=binance_symbol,
                    side=binance_side,
                    type="LIMIT",
                    timeInForce="GTC",
                    quantity=qty_rounded,
                    price=str(intent.entry_price),
                )

            stop_resp = self.client.futures_create_order(
                symbol=binance_symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=str(intent.stop_loss),
                closePosition=True,
                workingType="MARK_PRICE",
            )

            tp_resp = None
            if intent.take_profit is not None:
                tp_resp = self.client.futures_create_order(
                    symbol=binance_symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=str(intent.take_profit),
                    closePosition=True,
                    workingType="MARK_PRICE",
                )

            return ExecutionResult(
                success=True,
                intent_id=intent.intent_id,
                mode=self.mode,
                placed_at=utcnow_iso(),
                symbol=intent.symbol,
                side=binance_side,
                quantity=qty_rounded,
                notional_usd=notional,
                margin_required_usd=margin,
                order_id=str(open_resp.get("orderId")),
                avg_fill_price=_safe_float(open_resp.get("avgPrice")),
                stop_order_id=str(stop_resp.get("orderId")),
                take_profit_order_id=str(tp_resp.get("orderId")) if tp_resp else None,
                raw={"open": open_resp, "stop": stop_resp, "take_profit": tp_resp},
            )
        except Exception as exc:  # binance.exceptions.BinanceAPIException, others
            return self._error_result(
                intent,
                error=f"{type(exc).__name__}: {exc}",
                quantity=qty, notional_usd=notional, margin_required_usd=margin,
            )

    def _error_result(self, intent: ExecutionIntent, *,
                      error: str,
                      quantity: float = 0.0,
                      notional_usd: float = 0.0,
                      margin_required_usd: float = 0.0) -> ExecutionResult:
        return ExecutionResult(
            success=False,
            intent_id=intent.intent_id,
            mode=self.mode,
            placed_at=utcnow_iso(),
            symbol=intent.symbol,
            side=_intent_to_binance_side(intent.side),
            quantity=quantity,
            notional_usd=notional_usd,
            margin_required_usd=margin_required_usd,
            error=error,
        )

    @staticmethod
    def _round_qty(qty: float) -> float:
        """Round to a sensible step size.

        Binance enforces per-symbol step sizes from the exchange info.
        For validation we keep a conservative 0.001 step that fits BTC
        and ETH; real-world this should consult ``/fapi/v1/exchangeInfo``.
        """
        step = 0.001
        return round(qty / step) * step


# ---------------------------------------------------------------------------
# Factory + LangGraph node
# ---------------------------------------------------------------------------


def create_executor(config: dict) -> ExecutorAdapter:
    """Build the executor adapter based on config / env.

    Precedence: ``EXECUTOR_MODE`` env var > ``config["futures_executor_mode"]``
    > default ``"dryrun"``.
    """
    mode = (os.getenv("EXECUTOR_MODE")
            or config.get("futures_executor_mode")
            or "dryrun").lower()
    if mode == "testnet":
        api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
        api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
        if not api_key or not api_secret:
            raise RuntimeError(
                "EXECUTOR_MODE=testnet but BINANCE_TESTNET_API_KEY / "
                "BINANCE_TESTNET_API_SECRET not set in env"
            )
        return TestnetExecutor(api_key, api_secret)
    if mode == "dryrun":
        orders_log = Path(config.get(
            "futures_orders_log_path",
            os.path.join(os.path.expanduser("~"), ".tradingagents", "orders.jsonl"),
        ))
        return DryRunExecutor(orders_log)
    raise ValueError(f"Unknown EXECUTOR_MODE: {mode!r} (expected 'dryrun' or 'testnet')")


def create_executor_node(config: dict):
    """LangGraph node wrapping :func:`create_executor`.

    Reads from state:
    - ``execution_intent``: dict (from risk gate). ``None`` → no-op.
    - ``equity_usd``: float; defaults to ``futures_starting_equity_usd``.
    - ``mark_price``: float; Phase 5 populates this from the data layer.

    Writes to state:
    - ``execution_result``: dict (ExecutionResult as dict) or ``None``.

    On successful placement, also appends a ``position_opened`` event to
    the risk-state JSONL so the next run's snapshot reflects the live
    position. Failures are logged as ``trade_skipped`` for audit.
    """
    executor = create_executor(config)
    starting_equity = float(config.get("futures_starting_equity_usd", 1000.0))
    state_path = Path(config.get("futures_risk_state_path") or default_state_path())

    def executor_node(state) -> dict:
        intent_dict = state.get("execution_intent")
        if intent_dict is None:
            return {"execution_result": None}

        intent = ExecutionIntent(
            intent_id=intent_dict["intent_id"],
            symbol=intent_dict["symbol"],
            side=FuturesSide(intent_dict["side"]) if isinstance(intent_dict["side"], str)
                 else intent_dict["side"],
            leverage=float(intent_dict["leverage"]),
            risk_pct=float(intent_dict["risk_pct"]),
            entry_price=intent_dict.get("entry_price"),
            stop_loss=float(intent_dict["stop_loss"]),
            take_profit=intent_dict.get("take_profit"),
            created_at=intent_dict["created_at"],
        )

        equity_usd = float(state.get("equity_usd", starting_equity))
        mark_price = state.get("mark_price")
        if mark_price is None:
            mark_price = intent.entry_price  # acceptable when entry is a known limit
        if mark_price is None:
            error = "mark_price unavailable for market-order sizing"
            append_event(state_path, {
                "type": "trade_skipped", "ts": utcnow_iso(),
                "symbol": intent.symbol, "reason": error,
            })
            return {"execution_result": {"success": False, "error": error,
                                         "intent_id": intent.intent_id}}

        result = executor.place_order(intent, equity_usd=equity_usd, mark_price=float(mark_price))

        if result.success:
            append_event(state_path, {
                "type": "position_opened",
                "ts": result.placed_at,
                "intent_id": intent.intent_id,
                "symbol": intent.symbol,
                "mode": result.mode,
                "order_id": result.order_id,
                "quantity": result.quantity,
                "notional_usd": result.notional_usd,
            })
        else:
            append_event(state_path, {
                "type": "trade_skipped", "ts": result.placed_at,
                "symbol": intent.symbol, "reason": result.error or "execution failed",
            })

        return {"execution_result": asdict(result)}

    return executor_node


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _intent_to_binance_side(side: FuturesSide) -> str:
    if side == FuturesSide.LONG:
        return "BUY"
    if side == FuturesSide.SHORT:
        return "SELL"
    raise ValueError(f"cannot place an order for side={side.value}")


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
