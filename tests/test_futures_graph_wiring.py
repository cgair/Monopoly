"""Tests for the crypto-mode graph tail wiring.

Covers:
- ``create_mark_price_node`` behaviour: no-intent / limit-reuse / market-fetch.
- ``GraphSetup`` adds the futures tail only when ``config`` is provided.
"""

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.futures.market_data import create_mark_price_node


@pytest.mark.unit
class TestMarkPriceNode:
    def test_no_intent_yields_no_price(self):
        node = create_mark_price_node()
        assert node({"execution_intent": None}) == {"mark_price": None}
        assert node({}) == {"mark_price": None}

    def test_limit_order_reuses_entry_price_without_http(self):
        node = create_mark_price_node()
        intent = {"symbol": "BTC-USD", "entry_price": 64500.0}
        # No HTTP patch necessary — entry_price set short-circuits the fetch
        result = node({"execution_intent": intent})
        assert result == {"mark_price": 64500.0}

    def test_market_order_calls_fetch_mark_price(self):
        node = create_mark_price_node()
        intent = {"symbol": "ETH-USD", "entry_price": None}
        with patch(
            "tradingagents.futures.market_data.fetch_mark_price",
            return_value=3200.5,
        ) as mock_fetch:
            result = node({"execution_intent": intent})
        mock_fetch.assert_called_once_with("ETH-USD")
        assert result == {"mark_price": 3200.5}

    def test_market_order_with_fetch_failure_yields_none(self):
        node = create_mark_price_node()
        intent = {"symbol": "ETH-USD", "entry_price": None}
        with patch(
            "tradingagents.futures.market_data.fetch_mark_price",
            return_value=None,
        ):
            result = node({"execution_intent": intent})
        assert result == {"mark_price": None}


# ---------------------------------------------------------------------------
# GraphSetup futures-tail registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGraphSetupFuturesTail:
    def _build_setup(self, *, config):
        from tradingagents.graph.setup import GraphSetup
        from tradingagents.graph.conditional_logic import ConditionalLogic

        return GraphSetup(
            quick_thinking_llm=MagicMock(),
            deep_thinking_llm=MagicMock(),
            tool_nodes={
                "market": MagicMock(),
                "social": MagicMock(),
                "news": MagicMock(),
                "fundamentals": MagicMock(),
            },
            conditional_logic=ConditionalLogic(),
            config=config,
        )

    def test_config_provided_registers_futures_tail(self, tmp_path):
        setup = self._build_setup(config={
            "futures_risk_state_path": str(tmp_path / "state.jsonl"),
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
            "futures_starting_equity_usd": 1000.0,
        })
        graph = setup.setup_graph(selected_analysts=["market"])
        node_names = set(graph.nodes.keys())
        assert {"Risk Gate", "Mark Price", "Executor"}.issubset(node_names)

    def test_config_none_omits_futures_tail(self):
        setup = self._build_setup(config=None)
        graph = setup.setup_graph(selected_analysts=["market"])
        node_names = set(graph.nodes.keys())
        assert "Risk Gate" not in node_names
        assert "Executor" not in node_names

    def test_pm_connects_directly_to_risk_gate_when_futures_tail_enabled(self, tmp_path):
        """Crypto-only mode: PM feeds the Risk Gate unconditionally."""
        setup = self._build_setup(config={
            "futures_risk_state_path": str(tmp_path / "state.jsonl"),
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
        })
        graph = setup.setup_graph(selected_analysts=["market"])
        assert ("Portfolio Manager", "Risk Gate") in graph.edges
