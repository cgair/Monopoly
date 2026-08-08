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


# ---------------------------------------------------------------------------
# 2026-08-08 review, gap #1: Mark Price must run BEFORE Risk Gate so the
# gate can validate market-order stops against a reference price.
# ---------------------------------------------------------------------------


def _decision(**overrides):
    from tradingagents.agents.schemas import FuturesDecision, FuturesSide

    base = dict(
        side=FuturesSide.SHORT,
        leverage=2.0,
        position_size_pct=0.003,
        entry_price=None,
        stop_loss=68500.0,
        take_profit=62000.0,
        executive_summary="replay 2026-05-27 r0",
        investment_thesis="replay 2026-05-27 r0",
    )
    base.update(overrides)
    return FuturesDecision(**base)


@pytest.mark.unit
class TestMarkPriceNodeFromDecision:
    """New contract: the node reads ``final_decision_structured`` (it runs
    before the gate, so no intent exists yet)."""

    def test_market_decision_fetches_reference_price(self):
        node = create_mark_price_node()
        with patch(
            "tradingagents.futures.market_data.fetch_mark_price",
            return_value=75737.9,
        ) as mock_fetch:
            result = node({
                "final_decision_structured": _decision(),
                "company_of_interest": "BTC-USD",
            })
        mock_fetch.assert_called_once_with("BTC-USD")
        assert result == {"mark_price": 75737.9}

    def test_limit_decision_reuses_entry_price_without_http(self):
        node = create_mark_price_node()
        result = node({
            "final_decision_structured": _decision(entry_price=64500.0),
            "company_of_interest": "BTC-USD",
        })
        assert result == {"mark_price": 64500.0}

    def test_flat_decision_skips_fetch(self):
        from tradingagents.agents.schemas import FuturesSide

        node = create_mark_price_node()
        with patch(
            "tradingagents.futures.market_data.fetch_mark_price",
        ) as mock_fetch:
            result = node({
                "final_decision_structured": _decision(
                    side=FuturesSide.FLAT, leverage=None,
                    position_size_pct=None, stop_loss=None, take_profit=None,
                ),
                "company_of_interest": "BTC-USD",
            })
        mock_fetch.assert_not_called()
        assert result == {"mark_price": None}

    def test_no_structured_decision_yields_none(self):
        node = create_mark_price_node()
        assert node({"company_of_interest": "BTC-USD"}) == {"mark_price": None}


@pytest.mark.unit
class TestMarkPriceBeforeRiskGate:
    def _config(self, tmp_path):
        return {
            "futures_risk_state_path": str(tmp_path / "state.jsonl"),
            "futures_orders_log_path": str(tmp_path / "orders.jsonl"),
            "futures_starting_equity_usd": 1000.0,
        }

    def test_futures_tail_order_is_pm_markprice_gate_executor(self, tmp_path):
        from tradingagents.graph.setup import GraphSetup
        from tradingagents.graph.conditional_logic import ConditionalLogic

        setup = GraphSetup(
            quick_thinking_llm=MagicMock(),
            deep_thinking_llm=MagicMock(),
            tool_nodes={"market": MagicMock(), "social": MagicMock(),
                        "news": MagicMock(), "fundamentals": MagicMock()},
            conditional_logic=ConditionalLogic(),
            config=self._config(tmp_path),
        )
        graph = setup.setup_graph(selected_analysts=["market"])
        assert ("Portfolio Manager", "Mark Price") in graph.edges
        assert ("Mark Price", "Risk Gate") in graph.edges
        assert ("Risk Gate", "Executor") in graph.edges
