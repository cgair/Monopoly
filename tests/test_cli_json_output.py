"""Tests for JSON output serialization in CLI."""

import json
import pytest
from io import StringIO

from tradingagents.agents.schemas import FuturesDecision, FuturesSide, PortfolioDecision
from cli.json_output import (
    serialize_decision,
    serialize_risk_snapshot,
    build_analysis_result,
    output_json,
)
from tradingagents.futures.risk_state import RiskGateSnapshot
from datetime import datetime, timezone


@pytest.mark.unit
class TestSerializeDecision:
    def test_serialize_futures_decision_long(self):
        """Should serialize FuturesDecision (Long) to dict."""
        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="Bullish setup",
            investment_thesis="Technical breakout",
            time_horizon="2-5 days",
        )
        result = serialize_decision(decision)

        assert result["type"] == "FuturesDecision"
        assert result["side"] == "Long"
        assert result["leverage"] == 2.0
        assert result["position_size_pct"] == 0.005
        assert result["stop_loss"] == 62800.0
        assert result["take_profit"] == 68000.0
        assert result["executive_summary"] == "Bullish setup"
        assert result["investment_thesis"] == "Technical breakout"

    def test_serialize_futures_decision_short(self):
        """Should serialize FuturesDecision (Short) to dict."""
        decision = FuturesDecision(
            side=FuturesSide.SHORT,
            leverage=1.5,
            position_size_pct=0.003,
            stop_loss=66500.0,
            take_profit=60000.0,
            executive_summary="Bearish reversal",
            investment_thesis="Resistance failed",
        )
        result = serialize_decision(decision)

        assert result["type"] == "FuturesDecision"
        assert result["side"] == "Short"
        assert result["leverage"] == 1.5

    def test_serialize_futures_decision_flat(self):
        """Should serialize FuturesDecision (Flat/no trade) to dict."""
        decision = FuturesDecision(
            side=FuturesSide.FLAT,
            leverage=None,
            position_size_pct=None,
            stop_loss=None,
            take_profit=None,
            executive_summary="Wait for clarity",
            investment_thesis="Insufficient signal",
        )
        result = serialize_decision(decision)

        assert result["type"] == "FuturesDecision"
        assert result["side"] == "Flat"
        assert result["leverage"] is None
        assert result["position_size_pct"] is None

    def test_serialized_decision_is_json_safe(self):
        """Serialized decision should be JSON-serializable."""
        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            stop_loss=62800.0,
            executive_summary="test",
            investment_thesis="test",
        )
        result = serialize_decision(decision)
        # Should not raise
        json_str = json.dumps(result)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert parsed["side"] == "Long"


@pytest.mark.unit
class TestSerializeRiskSnapshot:
    def test_serialize_risk_snapshot(self):
        """Should serialize RiskGateSnapshot to dict."""
        now = datetime.now(timezone.utc)
        snapshot = RiskGateSnapshot(
            open_positions=2,
            daily_realised_pnl_usd=150.75,
            last_stop_loss_close_ts=now,
        )
        result = serialize_risk_snapshot(snapshot)

        assert result["open_positions"] == 2
        assert result["daily_realised_pnl_usd"] == 150.75
        assert result["last_stop_loss_close_ts"] == now.isoformat()

    def test_serialize_risk_snapshot_no_stop(self):
        """Should handle None last_stop_loss_close_ts."""
        snapshot = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=0.0,
            last_stop_loss_close_ts=None,
        )
        result = serialize_risk_snapshot(snapshot)

        assert result["open_positions"] == 0
        assert result["last_stop_loss_close_ts"] is None

    def test_serialize_none_snapshot(self):
        """Should return empty dict for None."""
        result = serialize_risk_snapshot(None)
        assert result == {}

    def test_serialized_snapshot_is_json_safe(self):
        """Serialized snapshot should be JSON-serializable."""
        snapshot = RiskGateSnapshot(
            open_positions=1,
            daily_realised_pnl_usd=-50.25,
            last_stop_loss_close_ts=datetime.now(timezone.utc),
        )
        result = serialize_risk_snapshot(snapshot)
        # Should not raise
        json_str = json.dumps(result, default=str)
        assert isinstance(json_str, str)


@pytest.mark.unit
class TestBuildAnalysisResult:
    def test_build_success_result(self):
        """Should build a complete analysis result with FuturesDecision."""
        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            stop_loss=62800.0,
            executive_summary="Bullish",
            investment_thesis="Technicals",
        )
        final_state = {
            "market_report": "Technical analysis...",
            "sentiment_report": "Bullish signals...",
            "investment_plan": "Plan...",
            "final_trade_decision": "Decision...",
        }
        selections = {
            "ticker": "BTCUSDT",
            "analysis_date": "2026-07-14",
            "analysts": ["market", "social"],
        }

        result = build_analysis_result(
            final_state,
            selections,
            decision,
            "crypto",
        )

        assert result["status"] == "success"
        assert result["error"] is None
        assert result["analysis"]["ticker"] == "BTCUSDT"
        assert result["analysis"]["asset_type"] == "crypto"
        assert result["decision"]["type"] == "FuturesDecision"
        assert result["decision"]["side"] == "Long"

    def test_build_error_result(self):
        """Should build an error result."""
        result = build_analysis_result(
            {},
            {"ticker": "BTCUSDT", "analysis_date": "2026-07-14"},
            None,
            "crypto",
            error="LLM API failed",
        )

        assert result["status"] == "error"
        assert result["error"] == "LLM API failed"

    def test_built_result_is_json_safe(self):
        """Built result should be JSON-serializable."""
        decision = FuturesDecision(
            side=FuturesSide.FLAT,
            executive_summary="Wait",
            investment_thesis="No signal",
        )
        result = build_analysis_result(
            {"final_trade_decision": "No trade"},
            {"ticker": "BTC-USD", "analysis_date": "2026-07-14"},
            decision,
            "crypto",
        )

        # Should not raise
        json_str = json.dumps(result, default=str)
        parsed = json.loads(json_str)
        assert parsed["status"] == "success"


@pytest.mark.unit
class TestOutputJson:
    def test_output_json_compact(self, capsys):
        """Should output compact JSON to stdout."""
        data = {"key": "value", "number": 42}
        output_json(data)

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["key"] == "value"
        assert parsed["number"] == 42

    def test_output_json_pretty(self, capsys):
        """Should output pretty JSON when requested."""
        data = {"key": "value", "nested": {"item": "test"}}
        output_json(data, pretty=True)

        captured = capsys.readouterr()
        # Pretty output should have newlines and indentation
        assert "\n" in captured.out
        assert "  " in captured.out
        parsed = json.loads(captured.out)
        assert parsed["nested"]["item"] == "test"

    def test_output_json_handles_complex_types(self, capsys):
        """Should use default=str for complex types."""
        from datetime import datetime, timezone
        data = {"timestamp": datetime.now(timezone.utc)}
        output_json(data)  # Should not raise

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert isinstance(parsed["timestamp"], str)
