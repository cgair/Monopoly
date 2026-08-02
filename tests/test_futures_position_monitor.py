"""Tests for the futures position monitor.

Covers:
- Dryrun adapter (mock exchange, no network).
- Testnet adapter with mocked Binance client.
- Position reconciliation logic: open → closed detection, event logging.
- Orphan order cancellation (basic and algo, with T2 fallback).
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from tradingagents.futures.position_monitor import (
    DryrunExchange,
    MonitorResult,
    TestnetExchange,
    create_monitor,
    reconcile_positions,
)
from tradingagents.futures.risk_state import load_events


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_jsonl(tmp_path: Path):
    """Temporary JSONL state file."""
    return tmp_path / "risk_gate_state.jsonl"


@pytest.fixture
def dryrun_exchange() -> DryrunExchange:
    """Dryrun exchange instance."""
    return DryrunExchange()


def mock_binance_client() -> Mock:
    """Mock Binance client."""
    return Mock()


# ---------------------------------------------------------------------------
# DryrunExchange tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryrunExchange:
    def test_returns_empty_positions(self):
        ex = DryrunExchange()
        assert ex.get_open_positions() == {}

    def test_cancel_all_basic_orders_returns_true(self):
        ex = DryrunExchange()
        assert ex.cancel_all_basic_orders("BTCUSDT") is True
        assert "BTCUSDT" in ex.cancelled_symbols

    def test_mode_string(self):
        ex = DryrunExchange()
        assert ex.mode == "dryrun"


# ---------------------------------------------------------------------------
# TestnetExchange tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTestnetExchange:
    def test_init_with_provided_client_cls(self):
        mock_cls = Mock()
        mock_client = Mock()
        mock_cls.return_value = mock_client
        ex = TestnetExchange("key", "secret", client_cls=mock_cls)
        assert ex.client == mock_client
        mock_cls.assert_called_once_with("key", "secret", testnet=True)

    def test_get_open_positions_filters_zero_amounts(self):
        mock_client = Mock()
        mock_client.futures_position_information.return_value = [
            {"symbol": "BTCUSDT", "positionAmt": "1.5"},
            {"symbol": "ETHUSDT", "positionAmt": "0"},  # Filtered out
            {"symbol": "DOGEUSDT", "positionAmt": "-0.5"},  # Included (short)
        ]
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)
        positions = ex.get_open_positions()
        assert "BTCUSDT" in positions
        assert "DOGEUSDT" in positions
        assert "ETHUSDT" not in positions

    def test_get_open_positions_handles_network_error(self):
        mock_client = Mock()
        mock_client.futures_position_information.side_effect = Exception("Network error")
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)
        with pytest.raises(Exception, match="Network error"):
            ex.get_open_positions()

    def test_cancel_all_basic_orders_success(self):
        mock_client = Mock()
        mock_client.futures_cancel_all_open_orders.return_value = {"orders": []}
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)
        assert ex.cancel_all_basic_orders("BTCUSDT") is True
        mock_client.futures_cancel_all_open_orders.assert_called_once_with(symbol="BTCUSDT")

    def test_cancel_all_basic_orders_failure(self):
        mock_client = Mock()
        mock_client.futures_cancel_all_open_orders.side_effect = Exception("API error")
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)
        assert ex.cancel_all_basic_orders("BTCUSDT") is False

    def test_mode_string(self):
        mock_client = Mock()
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)
        assert ex.mode == "testnet"


# ---------------------------------------------------------------------------
# Position reconciliation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReconcilePositions:
    def test_no_open_positions_in_jsonl(self, temp_jsonl: Path):
        """Empty JSONL → no positions to reconcile."""
        ex = DryrunExchange()
        result = reconcile_positions(temp_jsonl, ex, now=NOW)
        assert result.success
        assert result.positions_checked == 0
        assert result.positions_closed == 0

    def test_position_still_open_on_exchange(self, temp_jsonl: Path):
        """Position open locally and on exchange → no action."""
        from tradingagents.futures.risk_state import append_event

        # Manually create a position_opened event
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )

        # Mock exchange returns the position still open
        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {"BTCUSDT": {"symbol": "BTCUSDT"}}
        mock_ex.cancel_all_basic_orders.return_value = True

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.success
        assert result.positions_checked == 1
        assert result.positions_closed == 0  # Still open, nothing to close

    def test_position_closed_on_exchange(self, temp_jsonl: Path):
        """Position open locally but closed on exchange → write position_closed event."""
        from tradingagents.futures.risk_state import append_event

        # Create a position_opened event
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )

        # Mock exchange returns empty (position closed); income history
        # reports a loss → sign-based inference (old-format event) → stop.
        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.get_realized_pnl.return_value = -25.0

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.success
        assert result.positions_checked == 1
        assert result.positions_closed == 1
        assert result.orphan_basic_orders_cancelled == 1

        # Verify position_closed event was written with the real P&L
        events = load_events(temp_jsonl)
        closed_events = [e for e in events if e.get("type") == "position_closed"]
        assert len(closed_events) == 1
        assert closed_events[0]["symbol"] == "BTC-USD"
        assert closed_events[0]["intent_id"] == "intent-1"
        assert closed_events[0]["pnl_usd"] == -25.0
        assert closed_events[0]["outcome"] == "stop"  # loss → conservative stop

        # Verify cancel was called
        mock_ex.cancel_all_basic_orders.assert_called_once_with("BTCUSDT")

    def test_multiple_positions_reconciled(self, temp_jsonl: Path):
        """Multiple open positions, some closed on exchange."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:01:00+00:00",
                "intent_id": "intent-2",
                "symbol": "ETH-USD",
            },
        )

        # Mock exchange: BTC closed, ETH still open
        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {"ETHUSDT": {"symbol": "ETHUSDT"}}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.get_realized_pnl.return_value = 0.0

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.success
        assert result.positions_checked == 2
        assert result.positions_closed == 1  # Only BTC

        # Verify BTC was recorded closed
        events = load_events(temp_jsonl)
        closed_events = [e for e in events if e.get("type") == "position_closed"]
        assert len(closed_events) == 1
        assert closed_events[0]["symbol"] == "BTC-USD"

    def test_position_already_closed_in_jsonl(self, temp_jsonl: Path):
        """Position was opened then closed in the JSONL → not checked again."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )
        append_event(
            temp_jsonl,
            {
                "type": "position_closed",
                "ts": "2026-06-15T10:05:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
                "pnl_usd": 50.0,
                "outcome": "tp",
            },
        )

        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.positions_checked == 0  # BTC was already closed

    def test_symbol_filter_restricts_check(self, temp_jsonl: Path):
        """symbol_filter restricts which symbols are reconciled."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:01:00+00:00",
                "intent_id": "intent-2",
                "symbol": "ETH-USD",
            },
        )

        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.get_realized_pnl.return_value = 0.0

        # Only check BTC
        result = reconcile_positions(
            temp_jsonl,
            mock_ex,
            symbol_filter=["BTC-USD"],
            now=NOW,
        )

        assert result.positions_checked == 1  # Only BTC
        assert result.positions_closed == 1

        # Verify only BTC's orders were cancelled
        mock_ex.cancel_all_basic_orders.assert_called_once_with("BTCUSDT")

    def test_exchange_error_handled(self, temp_jsonl: Path):
        """Exchange error is caught and returned in result."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )

        mock_ex = Mock()
        mock_ex.get_open_positions.side_effect = Exception("Network error")

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert not result.success
        assert "Network error" in result.error

    def test_cancel_all_basic_orders_failure_continues(self, temp_jsonl: Path):
        """If cancel_all_basic_orders fails for one symbol, continue processing others."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:01:00+00:00",
                "intent_id": "intent-2",
                "symbol": "ETH-USD",
            },
        )

        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        # BTC cancel succeeds, ETH cancel fails
        mock_ex.cancel_all_basic_orders.side_effect = [True, False]
        mock_ex.get_realized_pnl.return_value = 0.0

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        # Both positions should be recorded closed despite the cancel failure
        assert result.success
        assert result.positions_checked == 2
        assert result.positions_closed == 2
        assert result.orphan_basic_orders_cancelled == 1

    def test_creates_monitor_dryrun_mode(self):
        """create_monitor returns DryrunExchange by default."""
        ex = create_monitor({})
        assert isinstance(ex, DryrunExchange)

    def test_creates_monitor_testnet_mode_with_keys(self):
        """create_monitor returns TestnetExchange when mode is testnet."""
        import os

        # Set testnet keys in environment
        os.environ["BINANCE_TESTNET_API_KEY"] = "test-key"
        os.environ["BINANCE_TESTNET_API_SECRET"] = "test-secret"
        try:
            ex = create_monitor(
                {"futures_monitor_mode": "testnet"},
                client_cls=lambda *a, **kw: Mock(),
            )
            assert isinstance(ex, TestnetExchange)
        finally:
            os.environ.pop("BINANCE_TESTNET_API_KEY", None)
            os.environ.pop("BINANCE_TESTNET_API_SECRET", None)

    def test_create_monitor_testnet_missing_keys_raises(self):
        """create_monitor raises if testnet mode but keys missing."""
        import os

        os.environ.pop("BINANCE_TESTNET_API_KEY", None)
        os.environ.pop("BINANCE_TESTNET_API_SECRET", None)
        with pytest.raises(RuntimeError, match="API_KEY"):
            create_monitor({"futures_monitor_mode": "testnet"})

    def test_reconcile_positions_utcnow_default(self, temp_jsonl: Path):
        """reconcile_positions uses datetime.now(timezone.utc) by default."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )

        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.get_realized_pnl.return_value = 0.0

        result = reconcile_positions(temp_jsonl, mock_ex)  # No explicit now

        assert result.success
        # The event was written; we can't check exact timestamp without
        # introspecting the JSONL, but the fact that it succeeded is enough.


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReconcileIntegration:
    def test_full_workflow_from_open_to_closed(self, temp_jsonl: Path):
        """Full workflow: open position, then reconcile when closed."""
        from tradingagents.futures.risk_state import append_event, derive_state

        # Simulate a position that was opened
        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
                "order_id": "order-123",
                "quantity": 0.01,
                "notional_usd": 600.0,
            },
        )

        # Verify gate sees the open position
        events = load_events(temp_jsonl)
        snapshot = derive_state(events, now=NOW)
        assert snapshot.open_positions == 1

        # Now simulate the position closing on Binance (returns empty)
        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.get_realized_pnl.return_value = 0.0

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.success
        assert result.positions_closed == 1

        # Verify gate no longer sees the position
        events = load_events(temp_jsonl)
        snapshot = derive_state(events, now=NOW)
        assert snapshot.open_positions == 0


# ---------------------------------------------------------------------------
# Algo order cancellation tests (T2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAlgoOrderCancellation:
    def test_dryrun_cancel_algo_order(self):
        """DryrunExchange.cancel_algo_order returns True."""
        ex = DryrunExchange()
        assert ex.cancel_algo_order("BTCUSDT", 12345) is True
        assert "BTCUSDT" in ex.cancelled_symbols

    def test_dryrun_cancel_all_algo_orders(self):
        """DryrunExchange.cancel_all_algo_orders returns 0."""
        ex = DryrunExchange()
        count = ex.cancel_all_algo_orders("BTCUSDT")
        assert count == 0
        assert "BTCUSDT" in ex.cancelled_symbols

    def test_testnet_cancel_algo_order_success(self):
        """TestnetExchange.cancel_algo_order delegates to the native client method."""
        mock_client = Mock()
        mock_client.futures_cancel_algo_order.return_value = {"code": 200}
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        result = ex.cancel_algo_order("BTCUSDT", 12345)

        assert result is True
        mock_client.futures_cancel_algo_order.assert_called_once_with(
            symbol="BTCUSDT", algoId=12345,
        )

    def test_testnet_cancel_algo_order_failure(self):
        """TestnetExchange.cancel_algo_order returns False on error."""
        mock_client = Mock()
        mock_client.futures_cancel_algo_order.side_effect = Exception("API error")
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        result = ex.cancel_algo_order("BTCUSDT", 12345)

        assert result is False

    def test_testnet_cancel_all_algo_orders_empty(self):
        """TestnetExchange.cancel_all_algo_orders returns 0 and skips the delete when no orders."""
        mock_client = Mock()
        mock_client.futures_get_open_algo_orders.return_value = []
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        count = ex.cancel_all_algo_orders("BTCUSDT")

        assert count == 0
        mock_client.futures_get_open_algo_orders.assert_called_once_with(symbol="BTCUSDT")
        mock_client.futures_cancel_all_algo_open_orders.assert_not_called()

    def test_testnet_cancel_all_algo_orders_multiple(self):
        """TestnetExchange.cancel_all_algo_orders cancels via one delete-all call."""
        mock_client = Mock()
        mock_client.futures_get_open_algo_orders.return_value = [
            {"algoId": 111, "symbol": "BTCUSDT", "side": "SELL"},
            {"algoId": 222, "symbol": "BTCUSDT", "side": "SELL"},
        ]
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        count = ex.cancel_all_algo_orders("BTCUSDT")

        assert count == 2
        mock_client.futures_cancel_all_algo_open_orders.assert_called_once_with(
            symbol="BTCUSDT",
        )

    def test_testnet_cancel_all_algo_orders_dict_response(self):
        """The list endpoint may wrap orders as {"total": n, "orders": [...]}."""
        mock_client = Mock()
        mock_client.futures_get_open_algo_orders.return_value = {
            "total": 1,
            "orders": [{"algoId": 111, "symbol": "BTCUSDT"}],
        }
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        count = ex.cancel_all_algo_orders("BTCUSDT")

        assert count == 1
        mock_client.futures_cancel_all_algo_open_orders.assert_called_once()

    def test_testnet_cancel_all_algo_orders_api_failure(self):
        """TestnetExchange.cancel_all_algo_orders returns 0 on any API failure."""
        mock_client = Mock()
        mock_client.futures_get_open_algo_orders.side_effect = Exception("Network error")
        ex = TestnetExchange("key", "secret", client_cls=lambda *a, **kw: mock_client)

        count = ex.cancel_all_algo_orders("BTCUSDT")

        assert count == 0

    def test_reconcile_calls_cancel_all_algo_orders(self, temp_jsonl: Path):
        """reconcile_positions calls cancel_all_algo_orders for closed positions."""
        from tradingagents.futures.risk_state import append_event

        append_event(
            temp_jsonl,
            {
                "type": "position_opened",
                "ts": "2026-06-15T10:00:00+00:00",
                "intent_id": "intent-1",
                "symbol": "BTC-USD",
            },
        )

        # Mock exchange with algo cancel support
        mock_ex = Mock()
        mock_ex.get_open_positions.return_value = {}
        mock_ex.cancel_all_basic_orders.return_value = True
        mock_ex.cancel_all_algo_orders.return_value = 1  # 1 algo order cancelled
        mock_ex.get_realized_pnl.return_value = 0.0

        result = reconcile_positions(temp_jsonl, mock_ex, now=NOW)

        assert result.success
        assert result.positions_closed == 1
        # Since algo cancel succeeds, no pending algo orders
        assert result.orphan_algo_orders_pending == 0
        
        # Verify cancel_all_algo_orders was called
        mock_ex.cancel_all_algo_orders.assert_called_once_with("BTCUSDT")
