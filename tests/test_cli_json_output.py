"""Tests for JSON output serialization in CLI."""

import json
import pytest
from io import StringIO

from tradingagents.agents.schemas import FuturesDecision, FuturesSide
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


@pytest.mark.unit
class TestDecisionNotifyCard:
    """_build_decision_notify_card reads the structured decision + real
    gate outcome from state — never the rating string, never a hardcoded
    "pass"."""

    def _decision(self, **overrides):
        base = dict(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            entry_price=61500.0,
            stop_loss=60200.0,
            take_profit=64000.0,
            executive_summary="test summary",
            investment_thesis="test thesis",
            time_horizon="1-3 days",
        )
        base.update(overrides)
        return FuturesDecision(**base)

    def test_card_uses_structured_decision_and_gate_pass(self):
        from cli.main import _build_decision_notify_card

        state = {
            "final_decision_structured": self._decision(),
            "execution_intent": {"intent_id": "x"},
            "risk_gate_rejection_reason": None,
        }
        card = _build_decision_notify_card(state, "BTC-USD", "dryrun")
        assert "BTC-USD" in card
        assert "做多" in card
        assert "✅" in card
        assert "test summary" in card
        assert "test thesis" in card

    def test_card_reports_gate_rejection(self):
        from cli.main import _build_decision_notify_card

        state = {
            "final_decision_structured": self._decision(),
            "execution_intent": None,
            "risk_gate_rejection_reason": "leverage exceeds configured max_leverage",
        }
        card = _build_decision_notify_card(state, "BTC-USD", "dryrun")
        assert "🛑" in card
        assert "杠杆超过配置上限" in card

    def test_no_structured_decision_returns_none(self):
        from cli.main import _build_decision_notify_card

        assert _build_decision_notify_card({}, "BTC-USD", "dryrun") is None

    def test_dict_form_decision_accepted(self):
        from cli.main import _build_decision_notify_card

        state = {
            "final_decision_structured": self._decision().model_dump(),
            "execution_intent": {"intent_id": "x"},
        }
        card = _build_decision_notify_card(state, "ETH-USD", "testnet")
        assert "ETH-USD" in card
        assert "testnet" in card


@pytest.mark.unit
class TestPushDecisionCard:
    """_push_decision_card is the --notify wiring: in the manual-trading
    loop the card is the order ticket, so the happy path must actually
    hand the card to send_discord, and every skip/failure path must log
    rather than silently drop it."""

    _WEBHOOK = "https://discord.com/api/webhooks/123/abc"

    def _state(self):
        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            entry_price=61500.0,
            stop_loss=60200.0,
            take_profit=64000.0,
            executive_summary="test summary",
            investment_thesis="test thesis",
            time_horizon="1-3 days",
        )
        return {
            "final_decision_structured": decision,
            "execution_intent": {"intent_id": "x"},
            "risk_gate_rejection_reason": None,
        }

    def test_happy_path_really_hands_card_to_sender(self, monkeypatch):
        from cli.main import _push_decision_card
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        sent = []
        monkeypatch.setitem(DEFAULT_CONFIG, "discord_webhook_url", self._WEBHOOK)
        monkeypatch.setattr(
            notify_discord, "send_discord",
            lambda content, *, webhook_url=None: sent.append((content, webhook_url)) or True,
        )

        assert _push_decision_card(self._state(), "BTC-USD", "dryrun") is True
        assert len(sent) == 1
        card, url = sent[0]
        assert url == self._WEBHOOK
        assert "BTC-USD" in card
        assert "0.50%" in card
        assert "60,200" in card

    def test_unconfigured_webhook_skips_with_log_trail(self, monkeypatch, caplog):
        import logging
        from cli.main import _push_decision_card
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        called = []
        monkeypatch.setitem(DEFAULT_CONFIG, "discord_webhook_url", None)
        monkeypatch.setattr(
            notify_discord, "send_discord",
            lambda *a, **kw: called.append(1) or True,
        )

        with caplog.at_level(logging.WARNING, logger="cli.main"):
            assert _push_decision_card(self._state(), "BTC-USD", "dryrun") is False
        assert not called
        assert any("unconfigured" in r.message for r in caplog.records)

    def test_no_structured_decision_skips_with_log_trail(self, monkeypatch, caplog):
        import logging
        from cli.main import _push_decision_card
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        called = []
        monkeypatch.setitem(DEFAULT_CONFIG, "discord_webhook_url", self._WEBHOOK)
        monkeypatch.setattr(
            notify_discord, "send_discord",
            lambda *a, **kw: called.append(1) or True,
        )

        with caplog.at_level(logging.WARNING, logger="cli.main"):
            assert _push_decision_card({}, "BTC-USD", "dryrun") is False
        assert not called
        assert any("no structured decision" in r.message for r in caplog.records)

    def test_sender_exception_is_swallowed_but_logged(self, monkeypatch, caplog):
        import logging
        from cli.main import _push_decision_card
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        def boom(*a, **kw):
            raise RuntimeError("webhook exploded")

        monkeypatch.setitem(DEFAULT_CONFIG, "discord_webhook_url", self._WEBHOOK)
        monkeypatch.setattr(notify_discord, "send_discord", boom)

        with caplog.at_level(logging.WARNING, logger="cli.main"):
            assert _push_decision_card(self._state(), "BTC-USD", "dryrun") is False
        assert any("push failed" in r.message for r in caplog.records)

    def test_sender_false_propagates_as_failure(self, monkeypatch, caplog):
        import logging
        from cli.main import _push_decision_card
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        monkeypatch.setitem(DEFAULT_CONFIG, "discord_webhook_url", self._WEBHOOK)
        monkeypatch.setattr(
            notify_discord, "send_discord", lambda *a, **kw: False,
        )

        with caplog.at_level(logging.WARNING, logger="cli.main"):
            assert _push_decision_card(self._state(), "BTC-USD", "dryrun") is False
        assert any("push failed" in r.message for r in caplog.records)


@pytest.mark.unit
class TestRunAnalysisJson:
    """Run-level contract of the analyze_json main path.

    This is the manual-mode entry point: EXECUTOR_MODE must be pinned to
    dryrun *before* the graph is built (the graph resolves the venue at
    build time), the assembled LLM model ids must actually be servable,
    and stdout must carry a complete order ticket even when the Discord
    card is lost.
    """

    def _fake_graph_cls(self, captured, final_chunk=None, raise_in_stream=False):
        from tradingagents.agents.schemas import FuturesDecision, FuturesSide

        if final_chunk is None:
            decision = FuturesDecision(
                side=FuturesSide.LONG,
                leverage=2.0,
                position_size_pct=0.005,
                entry_price=61500.0,
                stop_loss=60200.0,
                take_profit=64000.0,
                executive_summary="test summary",
                investment_thesis="test thesis",
                time_horizon="1-3 days",
            )
            final_chunk = {
                "final_trade_decision": "**Side**: Long",
                "final_decision_structured": decision,
                "execution_intent": {"intent_id": "x"},
                "risk_gate_rejection_reason": None,
                "market_report": "report text",
            }

        class _Propagator:
            def create_initial_state(self, ticker, date):
                return {"trade_date": date}

            def get_graph_args(self, callbacks=None):
                return {"stream_mode": "values"}

        class _Compiled:
            def stream(self, init_state, **kwargs):
                if raise_in_stream:
                    raise RuntimeError("graph exploded")
                yield final_chunk

        class _FakeGraph:
            def __init__(self, analysts, config=None, debug=False, callbacks=None):
                import os as _os
                captured["env_mode"] = _os.environ.get("EXECUTOR_MODE")
                captured["config_mode"] = config.get("futures_executor_mode")
                captured["quick_think_llm"] = config.get("quick_think_llm")
                captured["deep_think_llm"] = config.get("deep_think_llm")
                captured["llm_provider"] = config.get("llm_provider")
                self.propagator = _Propagator()
                self.graph = _Compiled()

            def process_signal(self, full_signal):
                return "Buy"

        return _FakeGraph

    def _run(self, monkeypatch, tmp_path, captured, notify=False, **fake_kwargs):
        import cli.main as cli_main
        import tradingagents.futures.risk_state as risk_state

        monkeypatch.setattr(
            cli_main, "TradingAgentsGraph", self._fake_graph_cls(captured, **fake_kwargs)
        )
        monkeypatch.setattr(
            risk_state, "default_state_path", lambda: tmp_path / "risk_gate_state.jsonl"
        )
        return cli_main.run_analysis_json(notify=notify)

    def test_inherited_testnet_env_is_pinned_to_dryrun_before_graph_build(
        self, monkeypatch, tmp_path, capsys
    ):
        """cli forces EXECUTOR_MODE=dryrun (env + config) before the graph —
        and therefore the executor and mark-price venue — is constructed.
        An inherited EXECUTOR_MODE=testnet must never reach the analyze path."""
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")
        captured = {}

        rc = self._run(monkeypatch, tmp_path, captured)

        assert rc == 0
        assert captured["env_mode"] == "dryrun"
        assert captured["config_mode"] == "dryrun"

    def test_assembled_llm_models_are_servable_and_env_overridable(
        self, monkeypatch, tmp_path, capsys
    ):
        """The JSON-mode model defaults must be real catalog ids — a bare
        "gemini" 404s at the first LLM call and kills every analyze_json run.
        The documented TRADINGAGENTS_*_THINK_LLM overrides must still win."""
        from tradingagents.llm_clients.validators import validate_model

        monkeypatch.delenv("TRADINGAGENTS_QUICK_THINK_LLM", raising=False)
        monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)
        monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)
        captured = {}
        assert self._run(monkeypatch, tmp_path, captured) == 0
        assert validate_model(captured["llm_provider"], captured["quick_think_llm"])
        assert validate_model(captured["llm_provider"], captured["deep_think_llm"])

        monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "gemini-2.5-flash-lite")
        monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "gemini-2.5-pro")
        captured = {}
        assert self._run(monkeypatch, tmp_path, captured) == 0
        assert captured["quick_think_llm"] == "gemini-2.5-flash-lite"
        assert captured["deep_think_llm"] == "gemini-2.5-pro"

    def test_stdout_json_carries_structured_ticket_and_gate_outcome(
        self, monkeypatch, tmp_path, capsys
    ):
        """When the Discord card is lost the stdout JSON is the only order
        ticket left — it must carry the structured decision fields and the
        real gate outcome, not just the 5-tier rating string."""
        captured = {}
        rc = self._run(monkeypatch, tmp_path, captured)
        out = capsys.readouterr().out
        result = json.loads(out)

        assert rc == 0
        assert result["status"] == "success"
        assert result["decision"] == {"type": "str", "raw": "Buy"}
        ticket = result["decision_structured"]
        assert ticket["side"] == "Long"
        assert ticket["leverage"] == 2.0
        assert ticket["position_size_pct"] == 0.005
        assert ticket["stop_loss"] == 60200.0
        assert ticket["take_profit"] == 64000.0
        assert result["risk_gate"] == {"approved": True, "rejection_reason": None}

    def test_graph_failure_yields_error_json_and_exit_one(
        self, monkeypatch, tmp_path, capsys
    ):
        """Any crash must surface as status=error + exit 1 — never a
        misleading success payload."""
        captured = {}
        rc = self._run(monkeypatch, tmp_path, captured, raise_in_stream=True)
        result = json.loads(capsys.readouterr().out)

        assert rc == 1
        assert result["status"] == "error"
        assert "graph exploded" in result["error"]

    def test_notify_failure_leaves_stdout_and_exit_code_untouched(
        self, monkeypatch, tmp_path, capsys
    ):
        """--notify is fail-open at run level: a dead webhook must not
        corrupt the JSON contract or the exit code."""
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        monkeypatch.setitem(
            DEFAULT_CONFIG, "discord_webhook_url",
            "https://discord.com/api/webhooks/123/abc",
        )
        monkeypatch.setattr(notify_discord, "send_discord", lambda *a, **kw: False)
        captured = {}
        rc = self._run(monkeypatch, tmp_path, captured, notify=True)
        result = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert result["status"] == "success"
        assert result["decision_structured"]["side"] == "Long"
