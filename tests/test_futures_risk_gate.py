"""Tests for the futures risk gate evaluation logic.

Targets ``evaluate`` directly with synthesised snapshots so file I/O
stays out of the test path; one end-to-end node test exercises
``create_risk_gate_node`` with a temp state file.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.agents.schemas import FuturesDecision, FuturesSide
from tradingagents.futures.risk_gate import (
    REASON_COOLDOWN_ACTIVE,
    REASON_DAILY_DRAWDOWN_HALT,
    REASON_DANGLING_INTENT,
    REASON_FLAT,
    REASON_LEVERAGE_BELOW_MIN,
    REASON_LEVERAGE_OVER_CAP,
    REASON_MAX_POSITIONS,
    REASON_MISSING_LEVERAGE,
    REASON_MISSING_RISK_PCT,
    REASON_MISSING_STOP,
    REASON_RISK_NON_POSITIVE,
    REASON_RISK_OVER_CAP,
    REASON_STOP_NON_POSITIVE,
    REASON_STOP_WRONG_SIDE,
    RiskGateConfig,
    create_risk_gate_node,
    evaluate,
    from_config,
)
from tradingagents.futures.risk_state import RiskGateSnapshot


NOW = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)


def _empty_snapshot() -> RiskGateSnapshot:
    return RiskGateSnapshot(
        open_positions=0,
        daily_realised_pnl_usd=0.0,
        last_stop_loss_close_ts=None,
    )


def _ok_long(**overrides) -> FuturesDecision:
    base = dict(
        side=FuturesSide.LONG,
        leverage=2.0,
        position_size_pct=0.01,
        entry_price=64500.0,
        stop_loss=62800.0,
        take_profit=68000.0,
        executive_summary="ok",
        investment_thesis="ok",
    )
    base.update(overrides)
    return FuturesDecision(**base)


def _ok_short(**overrides) -> FuturesDecision:
    base = dict(
        side=FuturesSide.SHORT,
        leverage=2.0,
        position_size_pct=0.01,
        entry_price=64500.0,
        stop_loss=66500.0,
        take_profit=60000.0,
        executive_summary="ok",
        investment_thesis="ok",
    )
    base.update(overrides)
    return FuturesDecision(**base)


# ---------------------------------------------------------------------------
# RiskGateConfig + from_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFromConfig:
    def test_defaults_when_keys_missing(self):
        cfg = from_config({})
        assert cfg.max_leverage == 3.0
        assert cfg.per_trade_risk_pct == 0.01
        assert cfg.daily_drawdown_halt_pct == 0.03
        assert cfg.cooldown_after_loss_minutes == 60
        assert cfg.max_concurrent_positions == 2

    def test_overrides_applied(self):
        cfg = from_config({
            "futures_max_leverage": 5.0,
            "futures_per_trade_risk_pct": 0.005,
            "futures_daily_drawdown_halt_pct": 0.05,
            "futures_cooldown_after_loss_minutes": 120,
            "futures_max_concurrent_positions": 3,
        })
        assert cfg.max_leverage == 5.0
        assert cfg.per_trade_risk_pct == 0.005
        assert cfg.daily_drawdown_halt_pct == 0.05
        assert cfg.cooldown_after_loss_minutes == 120
        assert cfg.max_concurrent_positions == 3


# ---------------------------------------------------------------------------
# evaluate() — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateHappy:
    def test_long_approved(self):
        result = evaluate(
            _ok_long(),
            symbol="BTC-USD",
            equity_usd=1000.0,
            config=RiskGateConfig(),
            now=NOW,
            snapshot=_empty_snapshot(),
        )
        assert result.approved
        assert result.intent is not None
        assert result.intent.symbol == "BTC-USD"
        assert result.intent.side == FuturesSide.LONG
        assert result.intent.leverage == 2.0
        assert result.intent.risk_pct == 0.01
        assert result.reason is None

    def test_short_approved(self):
        result = evaluate(
            _ok_short(),
            symbol="ETH-USD",
            equity_usd=1000.0,
            config=RiskGateConfig(),
            now=NOW,
            snapshot=_empty_snapshot(),
        )
        assert result.approved
        assert result.intent.side == FuturesSide.SHORT

    def test_intent_has_unique_uuid(self):
        snap = _empty_snapshot()
        r1 = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=RiskGateConfig(), now=NOW, snapshot=snap)
        r2 = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=RiskGateConfig(), now=NOW, snapshot=snap)
        assert r1.intent.intent_id != r2.intent.intent_id


# ---------------------------------------------------------------------------
# evaluate() — rejections
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateRejections:
    def test_flat_rejected_with_flat_reason(self):
        d = FuturesDecision(
            side=FuturesSide.FLAT,
            executive_summary="sit out",
            investment_thesis="conviction absent",
        )
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_FLAT
        assert result.intent is None

    def test_missing_leverage(self):
        d = _ok_long(leverage=None)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_MISSING_LEVERAGE

    def test_missing_risk_pct(self):
        d = _ok_long(position_size_pct=None)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_MISSING_RISK_PCT

    def test_missing_stop_loss(self):
        d = _ok_long(stop_loss=None)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_MISSING_STOP

    def test_leverage_over_cap(self):
        d = _ok_long(leverage=5.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(max_leverage=3.0),
                          now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_LEVERAGE_OVER_CAP in result.reason

    def test_risk_pct_over_cap(self):
        d = _ok_long(position_size_pct=0.05)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(per_trade_risk_pct=0.01),
                          now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_RISK_OVER_CAP in result.reason

    def test_leverage_zero_rejected(self):
        # leverage=0 would pass every upper-bound check and then divide-by-zero
        # in the executor's sizing. The gate rejects it as below the 1x floor.
        d = _ok_long(leverage=0.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_LEVERAGE_BELOW_MIN in result.reason

    def test_leverage_below_one_rejected(self):
        d = _ok_long(leverage=0.5)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_LEVERAGE_BELOW_MIN in result.reason

    def test_negative_risk_pct_rejected(self):
        d = _ok_long(position_size_pct=-0.005)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_RISK_NON_POSITIVE in result.reason

    def test_zero_risk_pct_rejected(self):
        d = _ok_long(position_size_pct=0.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_RISK_NON_POSITIVE in result.reason

    def test_non_positive_stop_rejected(self):
        d = _ok_long(stop_loss=0.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert REASON_STOP_NON_POSITIVE in result.reason

    def test_long_stop_above_entry_rejected(self):
        d = _ok_long(entry_price=64500.0, stop_loss=64600.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_STOP_WRONG_SIDE

    def test_short_stop_below_entry_rejected(self):
        d = _ok_short(entry_price=64500.0, stop_loss=64400.0)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert not result.approved
        assert result.reason == REASON_STOP_WRONG_SIDE

    def test_stop_side_skipped_when_entry_market(self):
        # entry_price=None → market order. The gate cannot validate stop side
        # without a price feed, so it lets it through; the executor re-checks
        # against the live mark price before placing any order (see
        # compute_sizing) so a wrong-side market stop is still caught.
        d = _ok_long(entry_price=None)
        result = evaluate(d, symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=_empty_snapshot())
        assert result.approved

    def test_daily_drawdown_halt_blocks_new_entries(self):
        # Equity 1000, halt threshold -3% = -30. Snapshot at -35 should halt.
        snap = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=-35.0,
            last_stop_loss_close_ts=None,
        )
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(daily_drawdown_halt_pct=0.03),
                          now=NOW, snapshot=snap)
        assert not result.approved
        assert REASON_DAILY_DRAWDOWN_HALT in result.reason

    def test_daily_drawdown_within_threshold_passes(self):
        snap = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=-20.0,   # within -30 threshold
            last_stop_loss_close_ts=None,
        )
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(daily_drawdown_halt_pct=0.03),
                          now=NOW, snapshot=snap)
        assert result.approved

    def test_cooldown_active_after_recent_stop_out(self):
        snap = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=0.0,
            last_stop_loss_close_ts=NOW - timedelta(minutes=30),
        )
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(cooldown_after_loss_minutes=60),
                          now=NOW, snapshot=snap)
        assert not result.approved
        assert REASON_COOLDOWN_ACTIVE in result.reason

    def test_cooldown_expired_allows_entry(self):
        snap = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=0.0,
            last_stop_loss_close_ts=NOW - timedelta(minutes=90),
        )
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(cooldown_after_loss_minutes=60),
                          now=NOW, snapshot=snap)
        assert result.approved

    def test_max_concurrent_positions_blocks_new_entry(self):
        snap = RiskGateSnapshot(
            open_positions=2,
            daily_realised_pnl_usd=0.0,
            last_stop_loss_close_ts=None,
        )
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(max_concurrent_positions=2),
                          now=NOW, snapshot=snap)
        assert not result.approved
        assert REASON_MAX_POSITIONS in result.reason

    def test_dangling_intent_blocks_new_entry(self):
        from tradingagents.futures.risk_state import DanglingIntent

        snap = RiskGateSnapshot(
            open_positions=0,
            daily_realised_pnl_usd=0.0,
            last_stop_loss_close_ts=None,
            dangling_intents=(DanglingIntent(
                intent_id="i-dangling", symbol="BTC-USD",
                submitted_ts=NOW - timedelta(minutes=10),
            ),),
        )
        result = evaluate(_ok_long(), symbol="ETH-USD", equity_usd=1000.0,
                          config=RiskGateConfig(), now=NOW, snapshot=snap)
        assert not result.approved
        assert REASON_DANGLING_INTENT in result.reason
        assert "i-dangling" in result.reason


# ---------------------------------------------------------------------------
# Node factory: state writes + state-dict shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRiskGateNode:
    def test_missing_structured_decision_logs_skip(self, tmp_path):
        state_path = tmp_path / "state.jsonl"
        node = create_risk_gate_node({
            "futures_risk_state_path": str(state_path),
            "futures_starting_equity_usd": 1000.0,
        })
        out = node({"company_of_interest": "BTC-USD"})
        assert out["execution_intent"] is None
        assert "no structured" in out["risk_gate_rejection_reason"]
        # trade_skipped event was logged
        from tradingagents.futures.risk_state import load_events
        events = load_events(state_path)
        assert len(events) == 1
        assert events[0]["type"] == "trade_skipped"
        assert events[0]["symbol"] == "BTC-USD"

    def test_approved_emits_intent_dict(self, tmp_path):
        state_path = tmp_path / "state.jsonl"
        node = create_risk_gate_node({
            "futures_risk_state_path": str(state_path),
            "futures_starting_equity_usd": 1000.0,
        })
        out = node({
            "company_of_interest": "BTC-USD",
            "final_decision_structured": _ok_long(),
            "equity_usd": 1000.0,
        })
        assert out["execution_intent"] is not None
        assert out["execution_intent"]["symbol"] == "BTC-USD"
        assert out["execution_intent"]["leverage"] == 2.0
        assert out["risk_gate_rejection_reason"] is None
        # Approved path does NOT log trade_skipped
        from tradingagents.futures.risk_state import load_events
        assert load_events(state_path) == []

    def test_rejected_logs_skip(self, tmp_path):
        state_path = tmp_path / "state.jsonl"
        node = create_risk_gate_node({
            "futures_risk_state_path": str(state_path),
            "futures_max_leverage": 1.0,
            "futures_starting_equity_usd": 1000.0,
        })
        out = node({
            "company_of_interest": "BTC-USD",
            "final_decision_structured": _ok_long(leverage=2.0),
            "equity_usd": 1000.0,
        })
        assert out["execution_intent"] is None
        assert REASON_LEVERAGE_OVER_CAP in out["risk_gate_rejection_reason"]
        from tradingagents.futures.risk_state import load_events
        events = load_events(state_path)
        assert len(events) == 1
        assert events[0]["type"] == "trade_skipped"


# ---------------------------------------------------------------------------
# Macro-event window block (optional, default off)
# ---------------------------------------------------------------------------

def _macro_event(hours_from_now: float, *, country="USD", impact="High"):
    from tradingagents.dataflows.econ_calendar import EconEvent
    return EconEvent(title="FOMC Statement", country=country, impact=impact,
                     when=NOW + timedelta(hours=hours_from_now))


@pytest.mark.unit
def test_macro_block_disabled_by_default_ignores_events():
    result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=RiskGateConfig(), now=NOW,
                      snapshot=_empty_snapshot(),
                      macro_events=[_macro_event(1.0)])
    assert result.approved is True


@pytest.mark.unit
def test_macro_block_rejects_inside_window():
    from tradingagents.futures.risk_gate import REASON_MACRO_EVENT_WINDOW
    result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=RiskGateConfig(macro_block_hours=6.0), now=NOW,
                      snapshot=_empty_snapshot(),
                      macro_events=[_macro_event(2.0)])
    assert result.approved is False
    assert REASON_MACRO_EVENT_WINDOW in result.reason
    assert "FOMC Statement" in result.reason


@pytest.mark.unit
def test_macro_block_passes_outside_window_or_low_impact():
    cfg = RiskGateConfig(macro_block_hours=6.0)
    outside = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                       config=cfg, now=NOW, snapshot=_empty_snapshot(),
                       macro_events=[_macro_event(12.0)])
    assert outside.approved is True
    medium = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=cfg, now=NOW, snapshot=_empty_snapshot(),
                      macro_events=[_macro_event(2.0, impact="Medium")])
    assert medium.approved is True


@pytest.mark.unit
def test_from_config_reads_macro_block_hours():
    cfg = from_config({"futures_macro_block_hours": 4.5})
    assert cfg.macro_block_hours == 4.5
    assert from_config({}).macro_block_hours == 0.0


@pytest.mark.unit
def test_from_config_reads_dangling_intent_minutes():
    cfg = from_config({"futures_dangling_intent_minutes": 15})
    assert cfg.dangling_intent_minutes == 15.0
    assert from_config({}).dangling_intent_minutes == 5.0


# ---------------------------------------------------------------------------
# 2026-08-02 review fixes (F10: macro calendar failure must leave a trace)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_calendar_failure_flags_result_and_logs(monkeypatch, caplog):
    """F10: with macro_block_hours enabled, a calendar-source exception
    silently disabled the macro fence. Fail-open stays (the hard ceilings
    are the real floor) but the failure must be visible: a warning log +
    macro_check_failed on the result."""
    import logging

    import tradingagents.dataflows.econ_calendar as cal

    def boom(*args, **kwargs):
        raise RuntimeError("calendar source down")

    monkeypatch.setattr(cal, "upcoming_high_impact", boom)
    with caplog.at_level(logging.WARNING, logger="tradingagents.futures.risk_gate"):
        result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                          config=RiskGateConfig(macro_block_hours=6.0), now=NOW,
                          snapshot=_empty_snapshot())
    assert result.approved is True  # fail-open by design
    assert result.macro_check_failed is True
    assert any("macro" in rec.message.lower() for rec in caplog.records)


@pytest.mark.unit
def test_macro_check_failed_defaults_false():
    result = evaluate(_ok_long(), symbol="BTC-USD", equity_usd=1000.0,
                      config=RiskGateConfig(), now=NOW,
                      snapshot=_empty_snapshot())
    assert result.approved is True
    assert result.macro_check_failed is False
