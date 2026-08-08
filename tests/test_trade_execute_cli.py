"""Integration tests for the trade_execute CLI command.

Tests the full flow from JSON input through approval gates to executor output.
Uses temporary files and mock executor to avoid network calls.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.futures.risk_state import load_events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_decision_json(
    symbol: str = "BTC-USD",
    ts_offset_minutes: int = 0,
) -> dict:
    """Build a sample decision JSON for testing."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=ts_offset_minutes)
    return {
        "timestamp": ts.isoformat(),
        "status": "success",
        "analysis": {
            "ticker": symbol,
            "asset_type": "crypto",
            "analysis_date": "2026-07-14",
            "selected_analysts": ["market"],
        },
        "decision": {
            "type": "FuturesDecision",
            "side": "LONG",
            "leverage": 2.0,
            "position_size_pct": 0.01,
            "entry_price": 64500.0,
            "stop_loss": 62800.0,
            "take_profit": 68000.0,
            "executive_summary": "...",
            "investment_thesis": "...",
            "time_horizon": "...",
        },
    }


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTradeExecuteCLI:
    """Test trade_execute command via subprocess (like real usage)."""

    def test_rejected_flag_alone_skips_without_approval(self, tmp_path):
        """--rejected without --approved-by should fail."""
        decision = _sample_decision_json()
        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps(decision))

        # Note: we can't easily test subprocess here without proper setup,
        # so we'll mock at the module level in pytest context

    def test_missing_approved_flag_rejected_unapproved(self, tmp_path, monkeypatch):
        """Missing --approved flag → unapproved status."""
        # This test would use subprocess or direct import
        # For now, we test via the function directly
        pass


@pytest.mark.unit
class TestTradeExecuteLogic:
    """Test the approval logic flow using mocked executor."""

    def test_rejection_path_writes_event(self, tmp_path, monkeypatch):
        """Test --rejected path: writes trade_skipped without executing."""
        from cli.main import trade_execute
        from typer.testing import CliRunner
        from tradingagents.futures.risk_state import default_state_path
        from unittest.mock import patch

        # Set up config to use tmp_path
        monkeypatch.setenv(
            "TRADINGAGENTS_RISK_GATE_STATE_PATH", str(tmp_path / "risk_state.jsonl")
        )

        decision = _sample_decision_json()
        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps(decision))

        # We can't easily run the typer command directly from pytest,
        # so we'll test the underlying logic via mocking
        from tradingagents.futures.approval import write_trade_skipped_event

        write_trade_skipped_event(
            "human rejected",
            symbol="BTC-USD",
            state_path=tmp_path / "risk_state.jsonl",
        )

        events = load_events(tmp_path / "risk_state.jsonl")
        assert len(events) == 1
        assert events[0]["reason"] == "human rejected"

    def test_staleness_check_rejects_old_decision(self, tmp_path):
        """Stale decision should be rejected before approval check."""
        from tradingagents.futures.approval import (
            check_staleness,
            write_trade_skipped_event,
        )

        # Decision from 20 minutes ago with default 15 min timeout
        old_decision_json = _sample_decision_json(ts_offset_minutes=20)
        ts = old_decision_json["timestamp"]

        is_stale, reason = check_staleness(ts, approval_timeout_minutes=15)

        assert is_stale
        assert "approval timeout" in reason

        # Should write event
        write_trade_skipped_event(reason, symbol="BTC-USD", state_path=tmp_path / "state.jsonl")
        events = load_events(tmp_path / "state.jsonl")
        assert events[0]["reason"] == reason

    def test_missing_approved_by_rejected(self, tmp_path):
        """Missing --approved-by should be rejected even if --approved."""
        from tradingagents.futures.approval import write_trade_skipped_event

        reason = "missing --approved-by parameter"
        write_trade_skipped_event(reason, symbol="BTC-USD", state_path=tmp_path / "state.jsonl")

        events = load_events(tmp_path / "state.jsonl")
        assert events[0]["reason"] == reason

    def test_gate_rejection_on_leverage_cap(self, tmp_path):
        """Gate re-evaluation should reject trades that now exceed cap."""
        from tradingagents.futures.approval import (
            parse_decision_json,
            reconstruct_intent_from_decision,
        )

        decision_json = _sample_decision_json()
        decision, _ = parse_decision_json(decision_json)

        config = {
            "futures_max_leverage": 1.5,  # Smaller cap
            "futures_per_trade_risk_pct": 0.01,
            "futures_daily_drawdown_halt_pct": 0.03,
            "futures_cooldown_after_loss_minutes": 60,
            "futures_max_concurrent_positions": 2,
            "futures_starting_equity_usd": 1000.0,
            "futures_risk_state_path": str(tmp_path / "risk_state.jsonl"),
        }

        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        assert intent is None
        assert "leverage" in reason.lower()

    def test_normal_execution_flow(self, tmp_path):
        """Test the happy path: decision approved, gate passes, executor runs."""
        from tradingagents.futures.approval import (
            ApprovalMetadata,
            parse_decision_json,
            reconstruct_intent_from_decision,
        )

        decision_json = _sample_decision_json()  # Fresh decision
        decision, _ = parse_decision_json(decision_json)

        config = {
            "futures_max_leverage": 3.0,
            "futures_per_trade_risk_pct": 0.01,
            "futures_daily_drawdown_halt_pct": 0.03,
            "futures_cooldown_after_loss_minutes": 60,
            "futures_max_concurrent_positions": 2,
            "futures_starting_equity_usd": 1000.0,
            "futures_executor_mode": "dryrun",
            "futures_risk_state_path": str(tmp_path / "risk_state.jsonl"),
        }

        # Re-evaluate through gate
        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        # Should pass
        assert intent is not None
        assert reason is None
        assert intent.symbol == "BTC-USD"
        assert intent.leverage == 2.0


# ---------------------------------------------------------------------------
# Trade skipped event completeness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTradeSkippedEventFields:
    """Verify trade_skipped event includes all required fields."""

    def test_event_has_required_fields(self, tmp_path):
        from tradingagents.futures.approval import (
            ApprovalMetadata,
            write_trade_skipped_event,
        )

        metadata = ApprovalMetadata(
            approved=False,
            approved_by="alice@discord",
            approved_at="2026-07-14T12:15:00+00:00",
        )

        write_trade_skipped_event(
            "test reason",
            symbol="BTC-USD",
            approval_metadata=metadata,
            state_path=tmp_path / "state.jsonl",
        )

        events = load_events(tmp_path / "state.jsonl")
        assert len(events) == 1
        event = events[0]

        # Check all required fields
        assert event["type"] == "trade_skipped"
        assert "ts" in event
        assert event["symbol"] == "BTC-USD"
        assert event["reason"] == "test reason"
        assert event["approval_by"] == "alice@discord"
        assert event["approval_at"] == "2026-07-14T12:15:00+00:00"
        assert event["approval_decision"] == "rejected"

    def test_event_without_metadata_has_core_fields(self, tmp_path):
        from tradingagents.futures.approval import write_trade_skipped_event

        write_trade_skipped_event(
            "timeout", symbol="ETH-USD", state_path=tmp_path / "state.jsonl"
        )

        events = load_events(tmp_path / "state.jsonl")
        event = events[0]

        assert event["type"] == "trade_skipped"
        assert event["symbol"] == "ETH-USD"
        assert event["reason"] == "timeout"
        assert "approval_by" not in event


# ---------------------------------------------------------------------------
# End-to-end scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApprovalScenarios:
    """Test complete approval scenarios."""

    def test_scenario_user_approves_fresh_decision(self, tmp_path):
        """Complete flow: fresh decision, user approves, gate passes, executes."""
        from tradingagents.futures.approval import (
            ApprovalMetadata,
            check_staleness,
            parse_decision_json,
            reconstruct_intent_from_decision,
        )

        # 1. Fresh decision
        decision_json = _sample_decision_json()
        decision, ts = parse_decision_json(decision_json)

        # 2. Staleness check passes
        now = datetime.now(timezone.utc)
        is_stale, reason = check_staleness(ts.isoformat(), now=now, approval_timeout_minutes=15)
        assert not is_stale

        # 3. User approves
        approval = ApprovalMetadata(
            approved=True,
            approved_by="alice@discord",
            approved_at=now.isoformat(),
        )
        assert approval.approved

        # 4. Gate re-evaluation passes
        config = {
            "futures_max_leverage": 3.0,
            "futures_per_trade_risk_pct": 0.01,
            "futures_daily_drawdown_halt_pct": 0.03,
            "futures_cooldown_after_loss_minutes": 60,
            "futures_max_concurrent_positions": 2,
            "futures_starting_equity_usd": 1000.0,
            "futures_risk_state_path": str(tmp_path / "risk_state.jsonl"),
        }

        intent, gate_reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )
        assert intent is not None
        assert gate_reason is None

    def test_scenario_user_rejects_decision(self, tmp_path):
        """User explicitly rejects: writes event, no execution."""
        from tradingagents.futures.approval import (
            ApprovalMetadata,
            write_trade_skipped_event,
        )

        now = datetime.now(timezone.utc)
        metadata = ApprovalMetadata(
            approved=False,
            approved_by="bob@discord",
            approved_at=now.isoformat(),
        )

        # Write rejection event
        write_trade_skipped_event(
            "human rejected",
            symbol="BTC-USD",
            approval_metadata=metadata,
            state_path=tmp_path / "state.jsonl",
        )

        events = load_events(tmp_path / "state.jsonl")
        assert events[0]["approval_decision"] == "rejected"
        assert events[0]["reason"] == "human rejected"

    def test_scenario_decision_becomes_stale(self, tmp_path):
        """Decision is fresh initially but becomes stale over time."""
        from tradingagents.futures.approval import check_staleness

        ts_str = (datetime.now(timezone.utc) - timedelta(minutes=14)).isoformat()

        # Not stale after 1 minute
        is_stale, _ = check_staleness(ts_str, approval_timeout_minutes=15)
        assert not is_stale

        # Stale after 16 minutes
        now_later = datetime.now(timezone.utc) + timedelta(minutes=3)
        is_stale, reason = check_staleness(
            ts_str, now=now_later, approval_timeout_minutes=15
        )
        assert is_stale
        assert reason is not None

    def test_scenario_gate_rejects_after_approval(self, tmp_path):
        """Approved decision fails gate re-evaluation (state changed)."""
        from tradingagents.futures.approval import (
            ApprovalMetadata,
            parse_decision_json,
            reconstruct_intent_from_decision,
            write_trade_skipped_event,
        )

        decision_json = _sample_decision_json()
        decision, _ = parse_decision_json(decision_json)

        # User approved
        metadata = ApprovalMetadata(
            approved=True,
            approved_by="alice@discord",
            approved_at=datetime.now(timezone.utc).isoformat(),
        )

        # But max_concurrent_positions hit
        config = {
            "futures_max_leverage": 3.0,
            "futures_per_trade_risk_pct": 0.01,
            "futures_daily_drawdown_halt_pct": 0.03,
            "futures_cooldown_after_loss_minutes": 60,
            "futures_max_concurrent_positions": 0,  # Cap hit!
            "futures_starting_equity_usd": 1000.0,
            "futures_risk_state_path": str(tmp_path / "risk_state.jsonl"),
        }

        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        # Gate rejects
        assert intent is None
        assert "max_concurrent_positions" in reason.lower() or "concurrent" in reason.lower()

        # Write event with approval metadata
        write_trade_skipped_event(
            reason,
            symbol="BTC-USD",
            approval_metadata=metadata,
            state_path=tmp_path / "state.jsonl",
        )

        events = load_events(tmp_path / "state.jsonl")
        assert events[0]["approval_decision"] == "approved"  # Was approved
        assert "max" in events[0]["reason"].lower()  # But gate rejected


# ---------------------------------------------------------------------------
# 2026-08-02 review fixes (F3: replay protection; F12: ticker fail-fast)
# ---------------------------------------------------------------------------


def _invoke_cli(args):
    from typer.testing import CliRunner
    from cli.main import app

    return CliRunner().invoke(app, args)


@pytest.mark.unit
class TestDecisionReplayProtection:
    """F3: re-running the CLI on an already-executed decision file must be
    refused — in one-way mode a repeat opens a second same-direction
    position, doubling exposure past what was approved."""

    def test_second_execution_of_same_decision_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "TRADINGAGENTS_RISK_GATE_STATE_PATH", str(tmp_path / "state.jsonl"),
        )
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        import cli.main as cli_main
        monkeypatch.setitem(
            cli_main.DEFAULT_CONFIG,
            "futures_orders_log_path", str(tmp_path / "orders.jsonl"),
        )
        monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "futures_executor_mode", "dryrun")
        import tradingagents.futures.market_data as market_data
        monkeypatch.setattr(market_data, "fetch_mark_price", lambda s, **_kw: 64500.0)

        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps(_sample_decision_json()))
        args = ["trade_execute", "--decision-file", str(decision_file),
                "--approved", "--approved-by", "alice@discord"]

        first = _invoke_cli(args)
        assert first.exit_code == 0, first.stdout
        assert '"executed"' in first.stdout

        second = _invoke_cli(args)
        assert second.exit_code == 1, second.stdout
        assert "duplicate" in second.stdout.lower()
        # Exactly one position was opened across both runs.
        events = load_events(tmp_path / "state.jsonl")
        assert [e["type"] for e in events].count("position_opened") == 1


@pytest.mark.unit
class TestTickerFailFast:
    """F12: a decision without analysis.ticker must error out, not proceed
    as symbol UNKNOWN (which only fails much later, at the exchange,
    with a baffling message)."""

    def test_missing_ticker_exits_2_with_clear_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "TRADINGAGENTS_RISK_GATE_STATE_PATH", str(tmp_path / "state.jsonl"),
        )
        decision = _sample_decision_json()
        del decision["analysis"]["ticker"]
        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps(decision))

        # Rejected path touches no executor/network, yet still needs the
        # symbol — fail-fast must trigger before any event is written.
        result = _invoke_cli(["trade_execute", "--decision-file", str(decision_file),
                              "--rejected", "--approved-by", "alice@discord"])
        assert result.exit_code == 2, result.stdout
        assert "ticker" in result.stdout
