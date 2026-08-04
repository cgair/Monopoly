"""Tests for the futures executor (dryrun + testnet adapters + factory + node).

Testnet tests inject a mock Binance Client class so no network is hit.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.schemas import FuturesSide
from tradingagents.futures.executor import (
    DryRunExecutor,
    TestnetExecutor,
    compute_sizing,
    create_executor,
    create_executor_node,
)
from tradingagents.futures.risk_gate import ExecutionIntent
from tradingagents.futures.risk_state import load_events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _intent_long(**overrides) -> ExecutionIntent:
    base = dict(
        intent_id="test-intent-1",
        symbol="BTC-USD",
        side=FuturesSide.LONG,
        leverage=2.0,
        risk_pct=0.01,
        entry_price=64500.0,
        stop_loss=62800.0,
        take_profit=68000.0,
        created_at="2026-05-31T12:00:00+00:00",
    )
    base.update(overrides)
    return ExecutionIntent(**base)


def _intent_short_market(**overrides) -> ExecutionIntent:
    base = dict(
        intent_id="test-intent-2",
        symbol="ETH-USD",
        side=FuturesSide.SHORT,
        leverage=2.0,
        risk_pct=0.01,
        entry_price=None,        # market entry
        stop_loss=3300.0,
        take_profit=None,
        created_at="2026-05-31T12:00:00+00:00",
    )
    base.update(overrides)
    return ExecutionIntent(**base)


def _intent_long_market(**overrides) -> ExecutionIntent:
    """Market-entry long with a take-profit. Testnet rejects LIMIT entries in
    the validation phase, so testnet tests use this market shape."""
    base = dict(
        intent_id="test-intent-3",
        symbol="BTC-USD",
        side=FuturesSide.LONG,
        leverage=2.0,
        risk_pct=0.01,
        entry_price=None,        # market entry
        stop_loss=62800.0,
        take_profit=68000.0,
        created_at="2026-05-31T12:00:00+00:00",
    )
    base.update(overrides)
    return ExecutionIntent(**base)


# ---------------------------------------------------------------------------
# compute_sizing — pure math
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeSizing:
    def test_long_with_limit_entry(self):
        # entry 64500, stop 62800 → risk_per_unit = 1700
        # risk_usd = 1% * $1000 = $10; qty = 10/1700 ≈ 0.00588
        # notional = qty * 64500 ≈ 379.4; margin = notional / 2 ≈ 189.7
        intent = _intent_long()
        ref, qty, notional, margin = compute_sizing(
            intent, equity_usd=1000.0, mark_price=64600.0,
        )
        assert ref == 64500.0  # limit takes precedence over mark
        assert qty == pytest.approx(10.0 / 1700.0)
        assert notional == pytest.approx(qty * 64500.0)
        assert margin == pytest.approx(notional / 2.0)

    def test_market_entry_uses_mark_price(self):
        intent = _intent_short_market()
        ref, qty, notional, margin = compute_sizing(
            intent, equity_usd=1000.0, mark_price=3200.0,
        )
        # short: mark 3200, stop 3300 → risk_per_unit = 100; risk_usd = 10
        # qty = 10/100 = 0.1; notional = 0.1 * 3200 = 320; margin = 160
        assert ref == 3200.0
        assert qty == pytest.approx(0.1)
        assert notional == pytest.approx(320.0)
        assert margin == pytest.approx(160.0)

    def test_zero_risk_distance_raises(self):
        # stop == entry: now caught by the stop-direction check (a long stop
        # at entry is not strictly below entry) before the zero-distance guard.
        intent = _intent_long(stop_loss=64500.0)  # equals entry
        with pytest.raises(ValueError, match="stop must be below entry"):
            compute_sizing(intent, equity_usd=1000.0, mark_price=64500.0)

    def test_market_long_stop_above_mark_raises(self):
        # Market entry: the reference price is the mark. A long stop at or
        # above the mark would open then get its STOP_MARKET rejected (-2021),
        # leaving a naked position — compute_sizing rejects it up front.
        intent = _intent_long_market(stop_loss=65000.0)  # above mark 64600
        with pytest.raises(ValueError, match="stop must be below entry"):
            compute_sizing(intent, equity_usd=1000.0, mark_price=64600.0)

    def test_market_short_stop_below_mark_raises(self):
        intent = _intent_short_market(stop_loss=3100.0)  # below mark 3200
        with pytest.raises(ValueError, match="stop must be above entry"):
            compute_sizing(intent, equity_usd=1000.0, mark_price=3200.0)


# ---------------------------------------------------------------------------
# DryRunExecutor
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunExecutor:
    def test_records_order_to_jsonl(self, tmp_path):
        log = tmp_path / "orders.jsonl"
        ex = DryRunExecutor(log)
        result = ex.place_order(_intent_long(), equity_usd=1000.0, mark_price=64600.0)
        assert result.success
        assert result.mode == "dryrun"
        assert result.symbol == "BTC-USD"
        assert result.side == "BUY"
        assert result.order_id.startswith("dryrun-")
        assert result.quantity > 0
        # JSONL contains one record with the expected shape
        lines = log.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["mode"] == "dryrun"
        assert record["binance_symbol"] == "BTCUSDT"
        assert record["side"] == "BUY"
        assert record["type"] == "LIMIT"
        assert record["take_profit"] == 68000.0

    def test_market_order_recorded_as_market_type(self, tmp_path):
        log = tmp_path / "orders.jsonl"
        ex = DryRunExecutor(log)
        result = ex.place_order(_intent_short_market(), equity_usd=1000.0, mark_price=3200.0)
        assert result.success
        record = json.loads(log.read_text().strip())
        assert record["type"] == "MARKET"
        assert record["side"] == "SELL"

    def test_insufficient_equity_rejected_without_log_entry(self, tmp_path):
        log = tmp_path / "orders.jsonl"
        ex = DryRunExecutor(log)
        # leverage=1, equity=$100, very tight stop → massive qty / margin
        intent = _intent_long(leverage=1.0, entry_price=64500.0, stop_loss=64499.0)
        result = ex.place_order(intent, equity_usd=100.0, mark_price=64500.0)
        assert not result.success
        assert "insufficient equity" in result.error
        # No JSON line written when rejected
        assert not log.exists() or log.read_text() == ""

    def test_flat_side_rejected(self, tmp_path):
        log = tmp_path / "orders.jsonl"
        ex = DryRunExecutor(log)
        # FuturesSide.FLAT cannot be placed; the helper raises before we get
        # to the orders log
        intent = ExecutionIntent(
            intent_id="i", symbol="BTC-USD", side=FuturesSide.FLAT,
            leverage=1.0, risk_pct=0.01, entry_price=64500.0,
            stop_loss=62800.0, take_profit=None,
            created_at="2026-05-31T12:00:00+00:00",
        )
        with pytest.raises(ValueError):
            ex.place_order(intent, equity_usd=1000.0, mark_price=64500.0)


# ---------------------------------------------------------------------------
# TestnetExecutor (with mocked Client)
# ---------------------------------------------------------------------------


def _mock_binance_client_class():
    """Return a Client-class-shaped MagicMock that records calls."""
    client = MagicMock()
    client.futures_change_leverage.return_value = {"leverage": 2}
    client.futures_create_order.side_effect = [
        {"orderId": 1111, "avgPrice": "64500.5", "status": "FILLED"},
        {"orderId": 2222, "status": "NEW"},          # stop
        {"orderId": 3333, "status": "NEW"},          # take-profit
    ]
    cls = MagicMock(return_value=client)
    return cls, client


@pytest.mark.unit
class TestTestnetExecutor:
    def test_places_three_orders_with_correct_kwargs(self):
        cls, client = _mock_binance_client_class()
        ex = TestnetExecutor("k", "s", client_cls=cls)
        # Client constructed with testnet=True and an HTTP timeout
        cls.assert_called_once_with(
            "k", "s", testnet=True, requests_params={"timeout": 10},
        )
        result = ex.place_order(_intent_long_market(), equity_usd=1000.0, mark_price=64600.0)
        assert result.success
        assert result.mode == "testnet"
        assert result.order_id == "1111"
        assert result.stop_order_id == "2222"
        assert result.take_profit_order_id == "3333"
        # Leverage set once for this symbol
        client.futures_change_leverage.assert_called_once_with(
            symbol="BTCUSDT", leverage=2,
        )
        # 3 orders placed: open MARKET, stop, TP
        assert client.futures_create_order.call_count == 3
        first_call = client.futures_create_order.call_args_list[0].kwargs
        assert first_call["symbol"] == "BTCUSDT"
        assert first_call["side"] == "BUY"
        assert first_call["type"] == "MARKET"
        assert "price" not in first_call
        stop_call = client.futures_create_order.call_args_list[1].kwargs
        assert stop_call["type"] == "STOP_MARKET"
        assert stop_call["side"] == "SELL"
        # Use reduceOnly + quantity instead of closePosition=True (see executor docstring)
        assert stop_call["reduceOnly"] is True
        assert "closePosition" not in stop_call
        assert stop_call["quantity"] == client.futures_create_order.call_args_list[0].kwargs["quantity"]
        assert stop_call["stopPrice"] == "62800.0"
        tp_call = client.futures_create_order.call_args_list[2].kwargs
        assert tp_call["type"] == "TAKE_PROFIT_MARKET"
        assert tp_call["reduceOnly"] is True
        assert "closePosition" not in tp_call

    def test_market_entry_omits_take_profit_when_none(self):
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1, "avgPrice": "3200.0"},
            {"orderId": 2},
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_short_market(), equity_usd=1000.0, mark_price=3200.0)
        assert result.success
        assert result.take_profit_order_id is None
        # 2 calls only: open MARKET + stop
        assert client.futures_create_order.call_count == 2
        first = client.futures_create_order.call_args_list[0].kwargs
        assert first["type"] == "MARKET"
        assert first["side"] == "SELL"

    def test_api_error_returns_failure_result(self):
        cls, client = _mock_binance_client_class()
        client.futures_change_leverage.side_effect = RuntimeError("API: -2015 invalid key")
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_long_market(), equity_usd=1000.0, mark_price=64600.0)
        assert not result.success
        assert "RuntimeError" in result.error
        assert "invalid key" in result.error
        # Leverage/open is Phase 1: nothing on the book, so not naked.
        assert result.position_naked is False

    def test_silent_failure_with_missing_order_id_is_caught(self):
        """Regression: stop_resp missing orderId/algoId must surface as success=False.

        Live-fire validation on 2026-06-02 hit a case where Binance
        returned a non-error response without orderId for the stop
        order, and the executor reported success=True with a naked
        position. The fix requires every order response to carry an
        identifier — this test pins that behaviour. Here the open filled,
        so the executor unwinds the position (a 3rd order) before reporting.
        """
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5"},   # open: ok
            {"status": "weird-no-orderId"},              # stop: no identifier → raises
            {"orderId": 9999, "status": "FILLED"},       # unwind: market close ok
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_long_market(), equity_usd=1000.0, mark_price=64600.0)
        assert not result.success
        assert "stop_loss" in result.error
        assert "unwound" in result.error
        assert result.position_naked is False

    def test_algo_id_accepted_for_conditional_orders(self):
        """Regression: conditional orders return ``algoId`` instead of ``orderId``.

        Observed on Futures Testnet 2026-06-02. The executor accepts
        either identifier and stores it as the order id string.
        """
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5"},                   # open
            {"algoId": 1000000093875506, "algoType": "CONDITIONAL"},    # stop
            {"algoId": 1000000093875507, "algoType": "CONDITIONAL"},    # tp
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_long_market(), equity_usd=1000.0, mark_price=64600.0)
        assert result.success
        assert result.order_id == "1111"
        assert result.stop_order_id == "1000000093875506"
        assert result.take_profit_order_id == "1000000093875507"

    def test_limit_entry_rejected_in_validation_phase(self):
        """M2: LIMIT entry is disabled on testnet until the position monitor
        exists — reduceOnly stop/TP cannot attach before the limit fills."""
        cls, client = _mock_binance_client_class()
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_long(), equity_usd=1000.0, mark_price=64600.0)
        assert not result.success
        assert "LIMIT entry disabled" in result.error
        # Nothing was sent to Binance.
        client.futures_create_order.assert_not_called()

    def test_stop_failure_unwind_failure_flags_naked(self):
        """H1: open fills, stop raises, AND unwind raises → position_naked=True."""
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5"},   # open ok
            RuntimeError("API: -1001 stop rejected"),    # stop raises
            RuntimeError("API: -1001 unwind rejected"),  # unwind raises too
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(_intent_long_market(), equity_usd=1000.0, mark_price=64600.0)
        assert not result.success
        assert result.position_naked is True
        assert "NAKED" in result.error

    def test_leverage_floored_and_margin_recomputed(self):
        """M4: leverage 2.7 → sent as 2 (floored); qty floored to step; the
        margin check and reported margin use the rounded values."""
        cls, client = _mock_binance_client_class()
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(
            _intent_long_market(leverage=2.7), equity_usd=1000.0, mark_price=64600.0,
        )
        assert result.success
        client.futures_change_leverage.assert_called_once_with(
            symbol="BTCUSDT", leverage=2,
        )
        # qty floored to 0.001 step; reported margin == notional / floored leverage
        assert result.quantity == pytest.approx(round(result.quantity, 3))
        assert result.margin_required_usd == pytest.approx(
            result.notional_usd / 2.0
        )


# ---------------------------------------------------------------------------
# 2026-08-02 review fixes (F2 / F9 / F14 / F5a)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReviewFixes20260802:
    def test_client_constructed_with_request_timeout(self):
        """F2: python-binance defaults to NO HTTP timeout — a stalled
        network call would hang the order placement indefinitely, leaving
        a possibly-live position unmonitored until the next monitor run.
        The real Client must be built with requests_params carrying a
        timeout."""
        cls, _ = _mock_binance_client_class()
        TestnetExecutor("k", "s", client_cls=cls)
        kwargs = cls.call_args.kwargs
        assert kwargs.get("testnet") is True
        requests_params = kwargs.get("requests_params") or {}
        assert requests_params.get("timeout"), (
            "Client built without requests_params timeout — order calls "
            "can hang forever on a stalled connection"
        )

    def test_partial_fill_sizes_protection_off_executed_qty(self):
        """F9: a partially filled market open must size the protective
        stop/TP (and the recorded quantity) off executedQty, not the
        requested qty — an over-sized reduceOnly order gets rejected,
        leaving the position naked."""
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5", "status": "FILLED",
             "executedQty": "0.002"},                 # open: partial fill
            {"orderId": 2222, "status": "NEW"},       # stop
            {"orderId": 3333, "status": "NEW"},       # take-profit
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(
            _intent_long_market(), equity_usd=1000.0, mark_price=64600.0,
        )
        assert result.success
        requested = client.futures_create_order.call_args_list[0].kwargs["quantity"]
        assert requested > 0.002  # sanity: this scenario IS a partial fill
        stop_call = client.futures_create_order.call_args_list[1].kwargs
        tp_call = client.futures_create_order.call_args_list[2].kwargs
        assert stop_call["quantity"] == 0.002
        assert tp_call["quantity"] == 0.002
        assert result.quantity == 0.002

    def test_partial_fill_unwind_uses_executed_qty(self):
        """F9: the emergency unwind after a failed stop must also close
        the executed quantity — unwinding the requested qty with
        reduceOnly on a partial fill is over-sized and gets rejected."""
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5", "status": "FILLED",
             "executedQty": "0.002"},                 # open: partial fill
            RuntimeError("API: -1001 stop rejected"),  # stop raises
            {"orderId": 9999, "status": "FILLED"},     # unwind
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(
            _intent_long_market(), equity_usd=1000.0, mark_price=64600.0,
        )
        assert not result.success
        unwind_call = client.futures_create_order.call_args_list[2].kwargs
        assert unwind_call["quantity"] == 0.002

    def test_unwind_rejected_status_flags_naked(self):
        """F14: an unwind order that comes back with an explicit rejection
        status did NOT close the position — treating any non-raising
        response as success would hide a naked position."""
        cls, client = _mock_binance_client_class()
        client.futures_create_order.side_effect = [
            {"orderId": 1111, "avgPrice": "64500.5", "executedQty": "0.005"},
            RuntimeError("API: -1001 stop rejected"),      # stop raises
            {"orderId": 9999, "status": "REJECTED"},       # unwind rejected
        ]
        ex = TestnetExecutor("k", "s", client_cls=cls)
        result = ex.place_order(
            _intent_long_market(), equity_usd=1000.0, mark_price=64600.0,
        )
        assert not result.success
        assert result.position_naked is True

    def test_failed_execution_trade_skipped_carries_executor_origin(self, tmp_path):
        """F5a: the alerts layer distinguishes executor failures from gate
        rejections via origin=executor on trade_skipped — without the
        field the executor_error_threshold alert can never fire."""
        from tradingagents.futures.executor import execute_with_ledger

        state = tmp_path / "state.jsonl"
        ex = DryRunExecutor(tmp_path / "orders.jsonl")
        # Tight stop + tiny equity → insufficient-equity executor failure.
        intent = _intent_long(leverage=1.0, entry_price=64500.0, stop_loss=64499.0)
        result = execute_with_ledger(
            ex, intent, equity_usd=100.0, mark_price=64500.0, state_path=state,
        )
        assert not result.success
        skipped = [e for e in load_events(state) if e["type"] == "trade_skipped"]
        assert skipped and skipped[0].get("origin") == "executor"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateExecutor:
    def test_default_is_dryrun(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        ex = create_executor({"futures_orders_log_path": str(tmp_path / "orders.jsonl")})
        assert ex.mode == "dryrun"
        assert isinstance(ex, DryRunExecutor)

    def test_env_overrides_config(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EXECUTOR_MODE", "dryrun")
        ex = create_executor({"futures_executor_mode": "testnet",
                              "futures_orders_log_path": str(tmp_path / "o.jsonl")})
        assert ex.mode == "dryrun"

    def test_testnet_requires_keys(self, monkeypatch):
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")
        monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="BINANCE_TESTNET_API_KEY"):
            create_executor({})

    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.setenv("EXECUTOR_MODE", "real-money-yolo")
        with pytest.raises(ValueError, match="Unknown EXECUTOR_MODE"):
            create_executor({})


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecutorNode:
    def test_no_intent_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        node = create_executor_node({
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
            "futures_risk_state_path": str(tmp_path / "state.jsonl"),
        })
        out = node({"execution_intent": None})
        assert out["execution_result"] is None

    def test_successful_intent_writes_position_opened(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        state_path = tmp_path / "state.jsonl"
        node = create_executor_node({
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
            "futures_risk_state_path": str(state_path),
            "futures_starting_equity_usd": 1000.0,
        })
        intent_dict = {
            "intent_id": "i-1",
            "symbol": "BTC-USD",
            "side": "Long",
            "leverage": 2.0,
            "risk_pct": 0.01,
            "entry_price": 64500.0,
            "stop_loss": 62800.0,
            "take_profit": 68000.0,
            "created_at": "2026-05-31T12:00:00+00:00",
        }
        out = node({
            "execution_intent": intent_dict,
            "equity_usd": 1000.0,
            "mark_price": 64600.0,
        })
        assert out["execution_result"]["success"] is True
        events = load_events(state_path)
        # Write-ahead: order_submitted lands BEFORE the result event.
        assert [e["type"] for e in events] == ["order_submitted", "position_opened"]
        assert events[0]["intent_id"] == "i-1"
        assert events[0]["symbol"] == "BTC-USD"
        assert events[0]["stop_loss"] == 62800.0
        opened = events[1]
        assert opened["symbol"] == "BTC-USD"
        assert opened["intent_id"] == "i-1"
        # Enriched fields for the monitor's close-outcome inference.
        assert opened["side"] == "BUY"
        assert opened["entry_price"] == 64500.0
        assert opened["stop_loss"] == 62800.0
        assert opened["take_profit"] == 68000.0

    def test_naked_result_logs_position_opened_and_naked(self, tmp_path, monkeypatch):
        """H1: when the adapter reports position_naked, the node must record a
        position_opened (so the gate counts the live position) AND a distinct
        position_naked alert — never a bare trade_skipped, which would
        undercount concurrent positions."""
        import tradingagents.futures.executor as executor_module
        from tradingagents.futures.executor import ExecutionResult

        class _NakedExecutor:
            mode = "testnet"

            def place_order(self, intent, *, equity_usd, mark_price):
                return ExecutionResult(
                    success=False, intent_id=intent.intent_id, mode="testnet",
                    placed_at="2026-05-31T12:00:00+00:00", symbol=intent.symbol,
                    side="BUY", quantity=0.005, notional_usd=323.0,
                    margin_required_usd=161.5,
                    error="POSITION NAKED", position_naked=True,
                )

        monkeypatch.setattr(executor_module, "create_executor", lambda cfg: _NakedExecutor())
        state_path = tmp_path / "state.jsonl"
        node = create_executor_node({"futures_risk_state_path": str(state_path)})
        intent_dict = {
            "intent_id": "i-naked", "symbol": "BTC-USD", "side": "Long",
            "leverage": 2.0, "risk_pct": 0.01, "entry_price": None,
            "stop_loss": 62800.0, "take_profit": None,
            "created_at": "2026-05-31T12:00:00+00:00",
        }
        out = node({"execution_intent": intent_dict, "equity_usd": 1000.0, "mark_price": 64600.0})
        assert out["execution_result"]["position_naked"] is True
        types = [e["type"] for e in load_events(state_path)]
        assert "position_opened" in types
        assert "position_naked" in types
        assert "trade_skipped" not in types

    def test_market_order_without_mark_price_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        state_path = tmp_path / "state.jsonl"
        node = create_executor_node({
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
            "futures_risk_state_path": str(state_path),
            "futures_starting_equity_usd": 1000.0,
        })
        intent_dict = {
            "intent_id": "i-2",
            "symbol": "ETH-USD",
            "side": "Short",
            "leverage": 2.0,
            "risk_pct": 0.01,
            "entry_price": None,    # market — needs mark_price
            "stop_loss": 3300.0,
            "take_profit": None,
            "created_at": "2026-05-31T12:00:00+00:00",
        }
        out = node({"execution_intent": intent_dict})   # no mark_price in state
        assert out["execution_result"]["success"] is False
        assert "mark_price" in out["execution_result"]["error"]
        events = load_events(state_path)
        assert events[0]["type"] == "trade_skipped"
