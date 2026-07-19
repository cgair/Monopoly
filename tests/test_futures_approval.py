"""Tests for the futures approval gate (staleness, metadata, decision reconstruction).

Covers the CLI's pre-execution approval logic without network or executor calls.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.agents.schemas import FuturesSide, FuturesDecision
from tradingagents.futures.approval import (
    ApprovalMetadata,
    check_staleness,
    parse_decision_json,
    reconstruct_intent_from_decision,
    write_trade_skipped_event,
)
from tradingagents.futures.risk_gate import ExecutionIntent
from tradingagents.futures.risk_state import load_events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _sample_decision_json() -> dict:
    """Return a sample decision dict from analyze_json output."""
    return {
        "timestamp": datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        "status": "success",
        "analysis": {
            "ticker": "BTC-USD",
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


def _base_config() -> dict:
    """Minimal Monopoly config for testing."""
    return {
        "futures_max_leverage": 3.0,
        "futures_per_trade_risk_pct": 0.01,
        "futures_daily_drawdown_halt_pct": 0.03,
        "futures_cooldown_after_loss_minutes": 60,
        "futures_max_concurrent_positions": 2,
        "futures_starting_equity_usd": 1000.0,
    }


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckStaleness:
    def test_fresh_decision_not_stale(self):
        now = datetime(2026, 7, 14, 12, 15, 0, tzinfo=timezone.utc)
        decision_ts = datetime(2026, 7, 14, 12, 10, 0, tzinfo=timezone.utc)
        is_stale, reason = check_staleness(
            decision_ts.isoformat(), now=now, approval_timeout_minutes=15
        )
        assert not is_stale
        assert reason is None

    def test_exactly_at_timeout_boundary_not_stale(self):
        now = datetime(2026, 7, 14, 12, 15, 0, tzinfo=timezone.utc)
        decision_ts = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        is_stale, reason = check_staleness(
            decision_ts.isoformat(), now=now, approval_timeout_minutes=15
        )
        assert not is_stale

    def test_just_past_timeout_boundary_is_stale(self):
        now = datetime(2026, 7, 14, 12, 15, 1, tzinfo=timezone.utc)
        decision_ts = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        is_stale, reason = check_staleness(
            decision_ts.isoformat(), now=now, approval_timeout_minutes=15
        )
        assert is_stale
        assert "approval timeout" in reason.lower()

    def test_very_old_decision_is_stale(self):
        now = datetime(2026, 7, 14, 18, 0, 0, tzinfo=timezone.utc)
        decision_ts = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        is_stale, reason = check_staleness(
            decision_ts.isoformat(), now=now, approval_timeout_minutes=15
        )
        assert is_stale

    def test_custom_timeout_respected(self):
        now = datetime(2026, 7, 14, 12, 35, 0, tzinfo=timezone.utc)
        decision_ts = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        # 35 minutes > 30 min timeout
        is_stale, reason = check_staleness(
            decision_ts.isoformat(), now=now, approval_timeout_minutes=30
        )
        assert is_stale

    def test_timestamp_with_z_suffix_parsed(self):
        now = datetime(2026, 7, 14, 12, 15, 0, tzinfo=timezone.utc)
        is_stale, reason = check_staleness(
            "2026-07-14T12:10:00Z", now=now, approval_timeout_minutes=15
        )
        assert not is_stale

    def test_requires_timezone_aware_now(self):
        now = datetime(2026, 7, 14, 12, 15, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            check_staleness("2026-07-14T12:10:00Z", now=now)


# ---------------------------------------------------------------------------
# Decision JSON parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseDecisionJson:
    def test_parses_full_analyze_json_output(self):
        decision_json = _sample_decision_json()
        decision, ts = parse_decision_json(decision_json)

        assert isinstance(decision, FuturesDecision)
        assert decision.side == FuturesSide.LONG
        assert decision.leverage == 2.0
        assert decision.position_size_pct == 0.01
        assert decision.entry_price == 64500.0
        assert decision.stop_loss == 62800.0
        assert decision.take_profit == 68000.0
        assert isinstance(ts, datetime)

    def test_parses_just_decision_dict(self):
        # A bare decision dict must carry its own timestamp — the top-level
        # analyze_json timestamp is not available in this shape.
        full_json = _sample_decision_json()
        decision_json = dict(full_json["decision"])
        decision_json["timestamp"] = full_json["timestamp"]
        decision, ts = parse_decision_json(decision_json)

        assert isinstance(decision, FuturesDecision)
        assert decision.side == FuturesSide.LONG

    def test_handles_short_side(self):
        decision_json = _sample_decision_json()
        decision_json["decision"]["side"] = "short"
        decision, _ = parse_decision_json(decision_json)
        assert decision.side == FuturesSide.SHORT

    def test_handles_flat_side(self):
        decision_json = _sample_decision_json()
        decision_json["decision"]["side"] = "Flat"
        decision, _ = parse_decision_json(decision_json)
        assert decision.side == FuturesSide.FLAT

    def test_handles_none_entry_price(self):
        decision_json = _sample_decision_json()
        decision_json["decision"]["entry_price"] = None
        decision, _ = parse_decision_json(decision_json)
        assert decision.entry_price is None

    def test_handles_none_take_profit(self):
        decision_json = _sample_decision_json()
        decision_json["decision"]["take_profit"] = None
        decision, _ = parse_decision_json(decision_json)
        assert decision.take_profit is None

    def test_raises_on_missing_required_fields(self):
        decision_json = _sample_decision_json()
        del decision_json["decision"]["leverage"]
        with pytest.raises(KeyError):
            parse_decision_json(decision_json)

    def test_missing_timestamp_fails_closed(self):
        # An undated decision must NOT default to "now" — that would make it
        # permanently fresh and bypass the approval-timeout staleness brake.
        decision_json = {"decision": dict(_sample_decision_json()["decision"])}
        decision_json["decision"].pop("timestamp", None)
        decision_json["decision"].pop("created_at", None)
        with pytest.raises(ValueError, match="timestamp"):
            parse_decision_json(decision_json)


# ---------------------------------------------------------------------------
# Intent reconstruction & gate re-evaluation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReconstructIntentFromDecision:
    def test_approved_decision_returns_intent(self, tmp_path):
        config = _base_config()
        config["futures_risk_state_path"] = str(tmp_path / "risk_state.jsonl")

        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.01,
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="",
            investment_thesis="",
            time_horizon="",
        )

        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        assert intent is not None
        assert reason is None
        assert intent.symbol == "BTC-USD"
        assert intent.side == FuturesSide.LONG
        assert intent.leverage == 2.0

    def test_rejected_by_gate_returns_reason(self, tmp_path):
        """Decision exceeds leverage cap."""
        config = _base_config()
        config["futures_max_leverage"] = 2.0
        config["futures_risk_state_path"] = str(tmp_path / "risk_state.jsonl")

        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=3.0,  # exceeds cap
            position_size_pct=0.01,
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="",
            investment_thesis="",
            time_horizon="",
        )

        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        assert intent is None
        assert reason is not None
        assert "leverage" in reason.lower()

    def test_gate_sees_flat_as_rejected(self, tmp_path):
        config = _base_config()
        config["futures_risk_state_path"] = str(tmp_path / "risk_state.jsonl")

        decision = FuturesDecision(
            side=FuturesSide.FLAT,
            leverage=2.0,
            position_size_pct=0.01,
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="",
            investment_thesis="",
            time_horizon="",
        )

        intent, reason = reconstruct_intent_from_decision(
            decision,
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=config,
            state_path=tmp_path / "risk_state.jsonl",
        )

        assert intent is None
        assert "flat" in reason.lower()


# ---------------------------------------------------------------------------
# Trade skipped event writing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWriteTradeSkippedEvent:
    def test_writes_event_without_approval_metadata(self, tmp_path):
        state_path = tmp_path / "risk_state.jsonl"
        write_trade_skipped_event(
            "test rejection reason",
            symbol="BTC-USD",
            state_path=state_path,
        )

        events = load_events(state_path)
        assert len(events) == 1
        assert events[0]["type"] == "trade_skipped"
        assert events[0]["symbol"] == "BTC-USD"
        assert events[0]["reason"] == "test rejection reason"
        assert "ts" in events[0]

    def test_writes_event_with_approval_metadata(self, tmp_path):
        state_path = tmp_path / "risk_state.jsonl"
        metadata = ApprovalMetadata(
            approved=False,
            approved_by="user@discord",
            approved_at="2026-07-14T12:15:00+00:00",
        )
        write_trade_skipped_event(
            "human rejected",
            symbol="BTC-USD",
            approval_metadata=metadata,
            state_path=state_path,
        )

        events = load_events(state_path)
        assert len(events) == 1
        assert events[0]["approval_by"] == "user@discord"
        assert events[0]["approval_at"] == "2026-07-14T12:15:00+00:00"
        assert events[0]["approval_decision"] == "rejected"

    def test_writes_approved_metadata_when_approved_true(self, tmp_path):
        state_path = tmp_path / "risk_state.jsonl"
        metadata = ApprovalMetadata(
            approved=True,
            approved_by="user@discord",
            approved_at="2026-07-14T12:15:00+00:00",
        )
        write_trade_skipped_event(
            "gate rejected after approval",
            symbol="BTC-USD",
            approval_metadata=metadata,
            state_path=state_path,
        )

        events = load_events(state_path)
        assert events[0]["approval_decision"] == "approved"


# ---------------------------------------------------------------------------
# Integration: approval metadata dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApprovalMetadata:
    def test_frozen_dataclass(self):
        metadata = ApprovalMetadata(
            approved=True,
            approved_by="alice",
            approved_at="2026-07-14T12:15:00+00:00",
        )
        with pytest.raises(AttributeError):
            metadata.approved = False

    def test_fields_captured_correctly(self):
        metadata = ApprovalMetadata(
            approved=False,
            approved_by="bob",
            approved_at="2026-07-14T12:16:00+00:00",
        )
        assert not metadata.approved
        assert metadata.approved_by == "bob"
        assert metadata.approved_at == "2026-07-14T12:16:00+00:00"
