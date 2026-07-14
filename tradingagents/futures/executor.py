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
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from tradingagents.agents.schemas import FuturesSide
from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.futures.risk_gate import ExecutionIntent
from tradingagents.futures.risk_state import append_event, default_state_path, utcnow_iso

logger = logging.getLogger(__name__)


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
    position_naked: bool = False
    """True iff an opening order filled but the protective stop failed AND the
    automatic unwind also failed — the position is live on the exchange with
    no stop. Operators must intervene manually. The executor node escalates
    this to a distinct ``position_naked`` event in the risk-state log."""


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

    # Stop-direction sanity against the actual reference price. The risk
    # gate only validates stop side when a limit ``entry_price`` is set
    # (it has no price feed); for market entries the first concrete price
    # is here. A wrong-side stop on a market order would open the position
    # and then have Binance reject the STOP_MARKET with -2021 ("would
    # immediately trigger"), leaving a naked position — so we fail before
    # placing anything. Raised as ValueError so both adapters' existing
    # error paths surface it as success=False.
    if intent.side == FuturesSide.LONG and intent.stop_loss >= reference_price:
        raise ValueError(
            f"long stop_loss {intent.stop_loss} >= reference price {reference_price} "
            f"— stop must be below entry"
        )
    if intent.side == FuturesSide.SHORT and intent.stop_loss <= reference_price:
        raise ValueError(
            f"short stop_loss {intent.stop_loss} <= reference price {reference_price} "
            f"— stop must be above entry"
        )

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
    3. Stop-loss: ``STOP_MARKET`` with ``reduceOnly=True`` and explicit ``quantity``.
    4. Take-profit (optional): ``TAKE_PROFIT_MARKET`` with ``reduceOnly=True``
       and explicit ``quantity``.

    Why ``reduceOnly + quantity`` instead of ``closePosition=True``: the
    latter is rejected by Binance with code -4130 in some account states
    ("An open stop or take profit order with GTE and closePosition in
    the direction is existing") even when no such order actually exists.
    The reduceOnly + quantity pattern is universally accepted and
    quantitatively equivalent — both close exactly the opened qty.

    Sanity-checking: every order response is required to carry an
    identifier — ``orderId`` for regular orders, ``algoId`` for the
    conditional (algo) orders Binance uses for STOP_MARKET /
    TAKE_PROFIT_MARKET on some account configurations. We accept
    either, but raise loudly if neither is present so the outer
    try/except returns ``success=False``. Earlier silent failures
    left positions naked.

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
        # Validation-phase guard: a LIMIT entry needs a fill-then-protect
        # monitor we have not built yet — Binance rejects a reduceOnly
        # stop/TP while the position is still unfilled, so the protective
        # orders cannot attach at placement time and the limit could fill
        # naked. Reject loudly until the position monitor lands (spec §8).
        if intent.entry_price is not None:
            return self._error_result(
                intent,
                error=("LIMIT entry disabled in validation phase — reduceOnly "
                       "stop/TP cannot attach before the limit fills; use market entry"),
            )

        try:
            # The raw notional/margin are recomputed below from the rounded
            # qty + floored leverage, so only the reference price and qty are
            # taken from compute_sizing here.
            reference_price, qty, _notional, _margin = compute_sizing(
                intent, equity_usd=equity_usd, mark_price=mark_price,
            )
        except ValueError as e:
            return self._error_result(intent, error=str(e))

        # Round DOWN to the step size and floor leverage to an int. Both must
        # match what Binance actually receives, so margin is recomputed from
        # the rounded values before the equity check — otherwise the check
        # could pass on a theoretical (smaller) margin while the real order
        # commits more. Flooring qty also guarantees the check is conservative.
        leverage_int = max(1, int(intent.leverage))
        qty_rounded = self._round_qty(qty)
        if qty_rounded <= 0:
            return self._error_result(
                intent,
                error=(f"quantity {qty:.8f} rounds to zero at step {self._STEP} — "
                       f"risk budget too small for this price"),
            )
        notional_rounded = qty_rounded * reference_price
        margin_required = notional_rounded / leverage_int

        if margin_required > equity_usd:
            return self._error_result(
                intent,
                error=(f"insufficient equity: margin required ${margin_required:.2f} > "
                       f"equity ${equity_usd:.2f}"),
                quantity=qty_rounded,
                notional_usd=notional_rounded,
                margin_required_usd=margin_required,
            )

        binance_symbol = to_binance_symbol(intent.symbol)
        binance_side = _intent_to_binance_side(intent.side)
        close_side = "SELL" if binance_side == "BUY" else "BUY"

        # Phase 1 — leverage + opening order. A failure here leaves nothing
        # on the book, so we just report it.
        try:
            self.client.futures_change_leverage(
                symbol=binance_symbol, leverage=leverage_int,
            )
            open_resp = self.client.futures_create_order(
                symbol=binance_symbol,
                side=binance_side,
                type="MARKET",
                quantity=qty_rounded,
            )
            open_order_id = _extract_order_id(open_resp, "open")
        except Exception as exc:  # binance.exceptions.BinanceAPIException, others
            return self._error_result(
                intent,
                error=f"open failed: {type(exc).__name__}: {exc}",
                quantity=qty_rounded,
                notional_usd=notional_rounded,
                margin_required_usd=margin_required,
            )

        # Phase 2 — protective orders. The position now EXISTS. Any failure
        # here leaves it naked, so we attempt a market unwind before
        # returning. If the unwind also fails, flag position_naked so the
        # caller escalates for manual intervention.
        try:
            stop_resp = self.client.futures_create_order(
                symbol=binance_symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=str(intent.stop_loss),
                quantity=qty_rounded,
                reduceOnly=True,
                workingType="MARK_PRICE",
            )
            stop_order_id = _extract_order_id(stop_resp, "stop_loss")

            tp_resp = None
            tp_order_id = None
            if intent.take_profit is not None:
                tp_resp = self.client.futures_create_order(
                    symbol=binance_symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=str(intent.take_profit),
                    quantity=qty_rounded,
                    reduceOnly=True,
                    workingType="MARK_PRICE",
                )
                tp_order_id = _extract_order_id(tp_resp, "take_profit")
        except Exception as exc:  # protective order failed — position is naked
            unwound, unwind_err = self._try_unwind(
                binance_symbol, close_side, qty_rounded,
            )
            if unwound:
                return self._error_result(
                    intent,
                    error=(f"protective order failed, position unwound: "
                           f"{type(exc).__name__}: {exc}"),
                    quantity=qty_rounded,
                    notional_usd=notional_rounded,
                    margin_required_usd=margin_required,
                )
            return self._error_result(
                intent,
                error=(f"protective order failed AND unwind failed — POSITION NAKED, "
                       f"manual intervention required. stop_err={type(exc).__name__}: {exc}; "
                       f"unwind_err={unwind_err}"),
                quantity=qty_rounded,
                notional_usd=notional_rounded,
                margin_required_usd=margin_required,
                position_naked=True,
            )

        return ExecutionResult(
            success=True,
            intent_id=intent.intent_id,
            mode=self.mode,
            placed_at=utcnow_iso(),
            symbol=intent.symbol,
            side=binance_side,
            quantity=qty_rounded,
            notional_usd=notional_rounded,
            margin_required_usd=margin_required,
            order_id=open_order_id,
            avg_fill_price=_safe_float(open_resp.get("avgPrice")),
            stop_order_id=stop_order_id,
            take_profit_order_id=tp_order_id,
            raw={"open": open_resp, "stop": stop_resp, "take_profit": tp_resp},
        )

    def _try_unwind(
        self, binance_symbol: str, close_side: str, qty: float,
    ) -> tuple[bool, Optional[str]]:
        """Best-effort market close of a just-opened position whose stop failed.

        Returns ``(True, None)`` on success, ``(False, error_str)`` if the
        unwind order itself raised — in which case the position is naked and
        the caller must escalate.
        """
        try:
            self.client.futures_create_order(
                symbol=binance_symbol,
                side=close_side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True,
            )
            return True, None
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _error_result(self, intent: ExecutionIntent, *,
                      error: str,
                      quantity: float = 0.0,
                      notional_usd: float = 0.0,
                      margin_required_usd: float = 0.0,
                      position_naked: bool = False) -> ExecutionResult:
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
            position_naked=position_naked,
        )

    _STEP = 0.001
    """Quantity step size. Binance enforces per-symbol step sizes from the
    exchange info; for validation we keep a conservative 0.001 that fits BTC
    and ETH. Real-world this should consult ``/fapi/v1/exchangeInfo``."""

    @classmethod
    def _round_qty(cls, qty: float) -> float:
        """Floor ``qty`` to the step size.

        Flooring (not rounding) keeps the committed quantity at or below the
        risk-budgeted size, so the margin check computed from the rounded qty
        is never undershot by a round-up.
        """
        return math.floor(qty / cls._STEP) * cls._STEP


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
        elif result.position_naked:
            # The opening order filled but the stop failed AND the unwind
            # failed: a live, unprotected position is on the exchange. Record
            # it as opened (so the gate's concurrent-position count includes
            # it and blocks new entries) AND raise a distinct naked alert for
            # the operator. Do NOT log trade_skipped — that would undercount.
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
            append_event(state_path, {
                "type": "position_naked", "ts": result.placed_at,
                "intent_id": intent.intent_id, "symbol": intent.symbol,
                "reason": result.error or "naked position — manual intervention required",
            })
            logger.error(
                "NAKED POSITION on %s (intent %s): %s",
                intent.symbol, intent.intent_id, result.error,
            )
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


def _extract_order_id(response: Any, label: str) -> str:
    """Return the order identifier from a ``futures_create_order`` response.

    Binance returns ``orderId`` for plain orders (MARKET / LIMIT) and
    ``algoId`` for conditional / algo orders (STOP_MARKET /
    TAKE_PROFIT_MARKET on accounts where conditionals are routed to
    the algo-order system — observed on Futures Testnet 2026-06-02).
    Either is treated as a valid identifier; both can be passed back
    to Binance's cancel endpoints. If neither is present, raise so
    the outer try/except returns ``success=False`` — silent failures
    left positions naked before this guard existed.
    """
    if not isinstance(response, dict):
        raise RuntimeError(f"{label} order response not a dict: {response!r}")
    for key in ("orderId", "algoId"):
        if key in response:
            return str(response[key])
    raise RuntimeError(
        f"{label} order response missing orderId/algoId: {response!r}"
    )
