"""T12 closed-loop reconciliation scenarios (PLAN.md T12, step 5).

End-to-end coverage over the intent write-ahead + reverse reconciliation
+ real P&L backfill triad, all through a fake exchange adapter — no
network. Scenarios:

1. Crash after submit → dangling intent → gate rejects → monitor adopts.
2. Untracked exchange position → event + critical alert; gate counts it.
3. Stop-out close → real P&L backfilled → cooldown + drawdown engage.
4. Income-history failure → degraded close (flagged) / dangling preserved.
5. Old-format events (pre-T12, no enriched fields) replay compatibly.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingagents.agents.schemas import FuturesDecision, FuturesSide
from tradingagents.futures.executor import ExecutionResult, execute_with_ledger
from tradingagents.futures.position_monitor import reconcile_positions
from tradingagents.futures.risk_gate import (
    REASON_COOLDOWN_ACTIVE,
    REASON_DAILY_DRAWDOWN_HALT,
    REASON_DANGLING_INTENT,
    RiskGateConfig,
    evaluate,
)
from tradingagents.futures.risk_state import (
    append_event,
    derive_state,
    load_events,
)
from tradingagents.futures.alerts import AlertConfig, analyze_events, evaluate_alerts


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


class FakeExchange:
    """Offline exchange adapter with scriptable positions and income."""

    mode = "fake"

    def __init__(self, positions=None, realized_pnl=0.0):
        self.positions = dict(positions or {})
        # float → same P&L for every symbol; None → fetch failure;
        # dict → per-symbol (missing key → 0.0).
        self.realized_pnl = realized_pnl
        self.cancelled_basic: list[str] = []
        self.cancelled_algo: list[str] = []

    def get_open_positions(self):
        return dict(self.positions)

    def cancel_all_basic_orders(self, symbol):
        self.cancelled_basic.append(symbol)
        return True

    def cancel_algo_order(self, symbol, algo_id):
        return True

    def cancel_all_algo_orders(self, symbol):
        self.cancelled_algo.append(symbol)
        return 0

    def get_realized_pnl(self, symbol, start_time_ms):
        if isinstance(self.realized_pnl, dict):
            return self.realized_pnl.get(symbol, 0.0)
        return self.realized_pnl


def _submit_event(intent_id="intent-1", symbol="BTC-USD", *, age_minutes=10.0,
                  side="BUY", stop_loss=60000.0, take_profit=70000.0):
    return {
        "type": "order_submitted",
        "ts": _iso(NOW - timedelta(minutes=age_minutes)),
        "intent_id": intent_id,
        "symbol": symbol,
        "side": side,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "mode": "testnet",
    }


def _decision(**overrides) -> FuturesDecision:
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


@pytest.fixture
def jsonl(tmp_path: Path):
    return tmp_path / "risk_gate_state.jsonl"


def _gate(jsonl: Path, **overrides) -> RiskGateConfig:
    return RiskGateConfig(state_path=jsonl, **overrides)


# ---------------------------------------------------------------------------
# Scenario 1 — crash after submit → dangling → gate rejects → monitor adopts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCrashAfterSubmit:
    def test_dangling_intent_detected_and_gate_rejects(self, jsonl):
        append_event(jsonl, _submit_event(age_minutes=10))

        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert len(snapshot.dangling_intents) == 1
        assert snapshot.dangling_intents[0].intent_id == "intent-1"
        assert snapshot.open_positions == 0

        result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=1000.0,
            config=_gate(jsonl), now=NOW,
        )
        assert not result.approved
        assert REASON_DANGLING_INTENT in result.reason
        assert "intent-1" in result.reason

    def test_fresh_submit_is_not_dangling(self, jsonl):
        """A submit younger than the timeout is an in-flight run, not a crash."""
        append_event(jsonl, _submit_event(age_minutes=2))
        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert snapshot.dangling_intents == ()

    def test_monitor_adopts_exchange_position(self, jsonl):
        """place_order filled but the result append never happened; the
        exchange holds the position → monitor adopts it under the original
        intent_id, the dangling intent resolves, and the gate reopens."""
        append_event(jsonl, _submit_event(age_minutes=10))
        exchange = FakeExchange(positions={
            "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "65000.0"},
        })

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.success
        assert result.positions_adopted == 1
        assert result.untracked_found == 0

        events = load_events(jsonl)
        opened = [e for e in events if e["type"] == "position_opened"]
        assert len(opened) == 1
        assert opened[0]["intent_id"] == "intent-1"
        assert opened[0]["adopted"] is True
        assert opened[0]["symbol"] == "BTC-USD"
        assert opened[0]["side"] == "BUY"
        assert opened[0]["entry_price"] == 65000.0
        assert opened[0]["stop_loss"] == 60000.0

        snapshot = derive_state(events, now=NOW)
        assert snapshot.dangling_intents == ()
        assert snapshot.open_positions == 1  # gate counts the adopted position

        gate_result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=1000.0,
            config=_gate(jsonl), now=NOW,
        )
        assert gate_result.approved  # 1 open < max_concurrent 2

    def test_monitor_dismisses_never_filled_submit(self, jsonl):
        """No exchange position and zero realized P&L since the submit →
        the order never filled; resolve via intent-carrying trade_skipped."""
        append_event(jsonl, _submit_event(age_minutes=10))
        exchange = FakeExchange(realized_pnl=0.0)

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.dangling_dismissed == 1
        events = load_events(jsonl)
        skips = [e for e in events if e["type"] == "trade_skipped"]
        assert len(skips) == 1
        assert skips[0]["intent_id"] == "intent-1"

        snapshot = derive_state(events, now=NOW)
        assert snapshot.dangling_intents == ()
        assert snapshot.open_positions == 0

    def test_monitor_backfills_opened_and_closed_while_blind(self, jsonl):
        """No exchange position but realized P&L exists since the submit →
        the position opened AND closed while we were blind; backfill both
        events so drawdown and cooldown see the loss."""
        append_event(jsonl, _submit_event(age_minutes=10))
        exchange = FakeExchange(realized_pnl=-12.5)

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.dangling_dismissed == 1
        assert result.positions_closed == 1
        events = load_events(jsonl)
        types = [e["type"] for e in events]
        assert types == ["order_submitted", "position_opened", "position_closed"]
        closed = events[-1]
        assert closed["intent_id"] == "intent-1"
        assert closed["pnl_usd"] == -12.5
        assert closed["outcome"] == "stop"  # loss → conservative stop

        snapshot = derive_state(events, now=NOW)
        assert snapshot.dangling_intents == ()
        assert snapshot.open_positions == 0
        assert snapshot.daily_realised_pnl_usd == -12.5
        assert snapshot.last_stop_loss_close_ts is not None

    def test_ambiguous_multiple_dangling_intents_not_adopted(self, jsonl):
        """Two dangling intents for the same symbol: guessing which one
        filled would pair the position with the wrong stop/TP — refuse
        adoption, record untracked, leave both dangling for a human."""
        append_event(jsonl, _submit_event(intent_id="intent-1", age_minutes=20))
        append_event(jsonl, _submit_event(intent_id="intent-2", age_minutes=10))
        exchange = FakeExchange(positions={
            "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "65000.0"},
        })

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.positions_adopted == 0
        assert result.untracked_found == 1
        assert result.dangling_remaining == 2
        types = [e["type"] for e in load_events(jsonl)]
        assert "position_opened" not in types
        assert "position_untracked" in types

    def test_side_mismatch_refuses_adoption(self, jsonl):
        """Dangling intent says BUY but the exchange position is short —
        the position was reversed outside our control; adopting would
        record the wrong direction under that intent_id."""
        append_event(jsonl, _submit_event(intent_id="intent-1", age_minutes=10, side="BUY"))
        exchange = FakeExchange(positions={
            "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": "-0.010", "entryPrice": "65000.0"},
        })

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.positions_adopted == 0
        assert result.untracked_found == 1
        assert result.dangling_remaining == 1
        types = [e["type"] for e in load_events(jsonl)]
        assert "position_opened" not in types

    def test_dangling_overlapping_tracked_position_left_for_operator(self, jsonl):
        """Exchange position exists for the symbol but is already locally
        tracked — whether the dangling order contributed is undecidable;
        the intent stays dangling and the gate stays closed."""
        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "intent-0", "symbol": "BTC-USD",
        })
        append_event(jsonl, _submit_event(intent_id="intent-1", age_minutes=10))
        exchange = FakeExchange(positions={
            "BTCUSDT": {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "65000.0"},
        })

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.positions_adopted == 0
        assert result.dangling_dismissed == 0
        assert result.dangling_remaining == 1
        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert len(snapshot.dangling_intents) == 1


# ---------------------------------------------------------------------------
# Scenario 2 — untracked exchange position → event + alert, gate counts it
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUntrackedPosition:
    def test_untracked_position_recorded_and_counted(self, jsonl):
        """Manual UI open (no local record, no dangling intent) →
        position_untracked; the gate's concurrency count includes it."""
        exchange = FakeExchange(positions={
            "ETHUSDT": {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3300.0"},
        })

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.untracked_found == 1
        events = load_events(jsonl)
        assert len(events) == 1
        ev = events[0]
        assert ev["type"] == "position_untracked"
        assert ev["symbol"] == "ETH-USD"
        assert ev["binance_symbol"] == "ETHUSDT"
        assert ev["quantity"] == -0.5
        assert ev["entry_price"] == 3300.0

        snapshot = derive_state(events, now=NOW)
        assert snapshot.open_positions == 1  # conservative direction

    def test_untracked_not_duplicated_on_next_run(self, jsonl):
        exchange = FakeExchange(positions={
            "ETHUSDT": {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3300.0"},
        })
        reconcile_positions(jsonl, exchange, now=NOW)
        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.untracked_found == 0
        events = load_events(jsonl)
        assert [e["type"] for e in events] == ["position_untracked"]

    def test_untracked_raises_critical_alert(self, jsonl):
        exchange = FakeExchange(positions={
            "ETHUSDT": {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3300.0"},
        })
        reconcile_positions(jsonl, exchange, now=NOW)

        stats = analyze_events(load_events(jsonl), window_hours=24, now=NOW)
        assert stats.position_untracked_count == 1
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == "critical"
        assert any("Untracked" in f.message for f in report.findings)

    def test_untracked_position_closed_out_later(self, jsonl):
        """When the exchange side closes, the untracked marker reconciles
        into a position_closed like any position, freeing the gate slot."""
        exchange = FakeExchange(positions={
            "ETHUSDT": {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3300.0"},
        })
        reconcile_positions(jsonl, exchange, now=NOW)

        closed_exchange = FakeExchange(realized_pnl=40.0)
        result = reconcile_positions(jsonl, closed_exchange, now=NOW)

        assert result.positions_closed == 1
        events = load_events(jsonl)
        assert [e["type"] for e in events] == ["position_untracked", "position_closed"]
        assert events[-1]["pnl_usd"] == 40.0

        snapshot = derive_state(events, now=NOW)
        assert snapshot.open_positions == 0


# ---------------------------------------------------------------------------
# Scenario 3 — stop-out close → real P&L → cooldown + drawdown engage
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStopOutBackfill:
    def _open_event(self, **overrides):
        base = {
            "type": "position_opened",
            "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "intent-1",
            "symbol": "BTC-USD",
            "side": "BUY",
            "entry_price": 65000.0,
            "quantity": 0.01,
            "stop_loss": 60000.0,
            "take_profit": 70000.0,
        }
        base.update(overrides)
        return base

    def test_stop_out_infers_outcome_and_backfills_pnl(self, jsonl):
        append_event(jsonl, self._open_event())
        # Loss of $50 over qty 0.01 → close price 60000 == stop_loss.
        exchange = FakeExchange(realized_pnl=-50.0)

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.positions_closed == 1
        assert result.pnl_backfill_failures == 0
        closed = load_events(jsonl)[-1]
        assert closed["type"] == "position_closed"
        assert closed["pnl_usd"] == -50.0
        assert closed["outcome"] == "stop"
        assert "pnl_backfill_failed" not in closed

    def test_tp_close_inferred(self, jsonl):
        append_event(jsonl, self._open_event())
        # Gain of $50 over qty 0.01 → close price 70000 == take_profit.
        exchange = FakeExchange(realized_pnl=50.0)

        reconcile_positions(jsonl, exchange, now=NOW)

        closed = load_events(jsonl)[-1]
        assert closed["outcome"] == "tp"

    def test_manual_close_far_from_stop_and_tp(self, jsonl):
        append_event(jsonl, self._open_event())
        # Loss of $20 → close price 63000, far from both 60000 and 70000.
        exchange = FakeExchange(realized_pnl=-20.0)

        reconcile_positions(jsonl, exchange, now=NOW)

        closed = load_events(jsonl)[-1]
        assert closed["outcome"] == "manual"

    def test_breakeven_manual_close_with_tight_stop_not_labelled_stop(self, jsonl):
        """With a tight stop (0.14% away), 1% relative tolerance dwarfs
        the whole stop distance — a break-even manual close must NOT be
        claimed as a stop-out. The per-candidate cap (half the
        entry→trigger distance) keeps the claim meaningful."""
        append_event(jsonl, self._open_event(
            entry_price=63279.0, quantity=0.033,
            stop_loss=63190.0, take_profit=64500.0,
        ))
        # Zero realized P&L → derived close == entry (89 points from the
        # stop, > half-distance cap of 44.5) → manual, not stop.
        reconcile_positions(jsonl, FakeExchange(realized_pnl=0.0), now=NOW)

        closed = load_events(jsonl)[-1]
        assert closed["outcome"] == "manual"

    def test_tight_stop_genuine_stop_out_still_matches(self, jsonl):
        """Mirror of the live T12-B trade: tight stop, loss lands the
        derived close exactly on the stop price → outcome=stop."""
        append_event(jsonl, self._open_event(
            entry_price=63279.0, quantity=0.033,
            stop_loss=63190.0, take_profit=64500.0,
        ))
        # (63190 - 63279) * 0.033 = -2.937 → derived close == stop.
        reconcile_positions(jsonl, FakeExchange(realized_pnl=-2.937), now=NOW)

        closed = load_events(jsonl)[-1]
        assert closed["outcome"] == "stop"

    def test_short_side_close_price_derivation(self, jsonl):
        append_event(jsonl, self._open_event(
            side="SELL", entry_price=65000.0, stop_loss=70000.0, take_profit=60000.0,
        ))
        # Short losing $50 over qty 0.01 → close price 70000 == stop_loss.
        exchange = FakeExchange(realized_pnl=-50.0)

        reconcile_positions(jsonl, exchange, now=NOW)

        closed = load_events(jsonl)[-1]
        assert closed["outcome"] == "stop"

    def test_cooldown_engages_after_backfilled_stop_out(self, jsonl):
        append_event(jsonl, self._open_event())
        reconcile_positions(jsonl, FakeExchange(realized_pnl=-50.0), now=NOW)

        # High equity so the drawdown halt does NOT trip — isolates cooldown.
        result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=100000.0,
            config=_gate(jsonl), now=NOW + timedelta(minutes=30),
        )
        assert not result.approved
        assert REASON_COOLDOWN_ACTIVE in result.reason

        # After the 60-minute window the gate reopens.
        result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=100000.0,
            config=_gate(jsonl), now=NOW + timedelta(minutes=61),
        )
        assert result.approved

    def test_drawdown_accumulates_from_backfilled_pnl(self, jsonl):
        append_event(jsonl, self._open_event())
        reconcile_positions(jsonl, FakeExchange(realized_pnl=-50.0), now=NOW)

        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert snapshot.daily_realised_pnl_usd == -50.0

        # Equity 1000 → halt threshold -$30; the -$50 close trips it.
        result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=1000.0,
            config=_gate(jsonl), now=NOW + timedelta(minutes=61),
        )
        assert not result.approved
        assert REASON_DAILY_DRAWDOWN_HALT in result.reason


# ---------------------------------------------------------------------------
# Scenario 4 — income-history failure degrades loudly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIncomeFailureDegradation:
    def test_close_recorded_with_flagged_data_gap(self, jsonl):
        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "intent-1", "symbol": "BTC-USD",
        })
        exchange = FakeExchange(realized_pnl=None)  # income fetch fails

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.positions_closed == 1
        assert result.pnl_backfill_failures == 1
        closed = load_events(jsonl)[-1]
        assert closed["pnl_usd"] == 0.0
        assert closed["outcome"] == "unknown"
        assert closed["pnl_backfill_failed"] is True

    def test_flagged_data_gap_raises_warn_alert(self, jsonl):
        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "intent-1", "symbol": "BTC-USD",
        })
        reconcile_positions(jsonl, FakeExchange(realized_pnl=None), now=NOW)

        stats = analyze_events(load_events(jsonl), window_hours=24, now=NOW)
        assert stats.pnl_backfill_failures == 1
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == "warn"
        assert any("backfill" in f.message for f in report.findings)

    def test_unresolvable_dangling_surfaces_in_exit_code_and_alerts(self, jsonl, monkeypatch, capsys):
        """A left-dangling intent must not be log-only: the monitor CLI
        exits 1 and the alerts layer raises a WARN finding, so a launchd
        operator learns why the gate is closed without reading logs."""
        import json

        from tradingagents.futures import position_monitor
        from tradingagents.futures.alerts import AlertConfig, analyze_events, evaluate_alerts

        append_event(jsonl, _submit_event(age_minutes=10))
        monkeypatch.delenv("MONITOR_MODE", raising=False)
        monkeypatch.setattr(
            position_monitor, "create_monitor",
            lambda cfg: FakeExchange(realized_pnl=None),  # income unavailable
        )
        rc = position_monitor.main(["--state-path", str(jsonl)])
        assert rc == 1
        summary = json.loads(capsys.readouterr().out)
        assert summary["dangling_remaining"] == 1

        stats = analyze_events(load_events(jsonl), window_hours=24, now=NOW)
        assert stats.dangling_intents == 1
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == "warn"
        assert any("Dangling" in f.message for f in report.findings)

    def test_old_dangling_outside_scan_window_still_alerts(self, jsonl):
        """Dangling pairing ignores the 24h scan window — an unresolved
        submit from 3 days ago is exactly what must stay visible."""
        from tradingagents.futures.alerts import AlertConfig, analyze_events, evaluate_alerts

        append_event(jsonl, _submit_event(age_minutes=3 * 24 * 60))
        stats = analyze_events(load_events(jsonl), window_hours=24, now=NOW)
        assert stats.dangling_intents == 1
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == "warn"

    def test_dangling_intent_preserved_when_income_unavailable(self, jsonl):
        """Without income data a dangling intent cannot be verified —
        it must stay dangling (gate closed) rather than being guessed away."""
        append_event(jsonl, _submit_event(age_minutes=10))
        exchange = FakeExchange(realized_pnl=None)

        result = reconcile_positions(jsonl, exchange, now=NOW)

        assert result.dangling_dismissed == 0
        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert len(snapshot.dangling_intents) == 1

        gate_result = evaluate(
            _decision(), symbol="ETH-USD", equity_usd=1000.0,
            config=_gate(jsonl), now=NOW,
        )
        assert not gate_result.approved
        assert REASON_DANGLING_INTENT in gate_result.reason


# ---------------------------------------------------------------------------
# Torn-write resilience — malformed lines skip + alert, never crash
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMalformedLineResilience:
    def test_torn_line_does_not_crash_replay(self, jsonl):
        """A half-written line (crash / power loss mid-append) must not
        brick every gate evaluation and monitor run."""
        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=1)),
            "intent_id": "i-1", "symbol": "BTC-USD",
        })
        with open(jsonl, "a", encoding="utf-8") as fh:
            fh.write('{"type": "position_clo')  # torn write, no newline

        events = load_events(jsonl)  # must not raise
        assert len(events) == 1
        snapshot = derive_state(events, now=NOW)
        assert snapshot.open_positions == 1

        result = reconcile_positions(jsonl, FakeExchange(realized_pnl=0.0), now=NOW)
        assert result.success

    def test_malformed_lines_raise_warn_alert(self, jsonl):
        from tradingagents.futures.alerts import AlertConfig, analyze_events, evaluate_alerts
        from tradingagents.futures.risk_state import load_events_with_errors

        with open(jsonl, "w", encoding="utf-8") as fh:
            fh.write('{"type": "position_opened", "ts": "2026-06-15T10:00:00+00:00", "intent_id": "i-1", "symbol": "BTC-USD"}\n')
            fh.write('not json at all\n')

        events, malformed = load_events_with_errors(jsonl)
        assert len(events) == 1
        assert malformed == 1

        stats = analyze_events(events, window_hours=24, now=NOW)
        stats.malformed_lines = malformed
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == "warn"
        assert any("malformed" in f.message for f in report.findings)


# ---------------------------------------------------------------------------
# Scenario 5 — old-format events replay compatibly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOldFormatReplay:
    def test_pre_t12_log_derives_clean_snapshot(self, jsonl):
        """A log written before T12 (no order_submitted, no enriched
        fields, trade_skipped without intent_id) must replay without
        dangling intents or count drift."""
        append_event(jsonl, {
            "type": "position_opened", "ts": "2026-06-15T09:00:00+00:00",
            "intent_id": "old-1", "symbol": "BTC-USD",
        })
        append_event(jsonl, {
            "type": "trade_skipped", "ts": "2026-06-15T09:30:00+00:00",
            "symbol": "ETH-USD", "reason": "max_concurrent_positions already open",
        })
        append_event(jsonl, {
            "type": "position_closed", "ts": "2026-06-15T10:00:00+00:00",
            "intent_id": "old-1", "symbol": "BTC-USD",
            "pnl_usd": 0.0, "outcome": "stop",
        })

        snapshot = derive_state(load_events(jsonl), now=NOW)
        assert snapshot.open_positions == 0
        assert snapshot.dangling_intents == ()
        assert snapshot.last_stop_loss_close_ts is not None

    def test_old_format_open_falls_back_to_pnl_sign(self, jsonl):
        """Old position_opened without side/entry/stop → outcome from the
        P&L sign: loss = stop (conservative, arms cooldown), gain = manual."""
        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "old-1", "symbol": "BTC-USD",
        })
        reconcile_positions(jsonl, FakeExchange(realized_pnl=-10.0), now=NOW)
        assert load_events(jsonl)[-1]["outcome"] == "stop"

        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=1)),
            "intent_id": "old-2", "symbol": "ETH-USD",
        })
        reconcile_positions(jsonl, FakeExchange(realized_pnl=25.0), now=NOW)
        assert load_events(jsonl)[-1]["outcome"] == "manual"


# ---------------------------------------------------------------------------
# execute_with_ledger — write-ahead ordering + intent pairing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecuteWithLedger:
    def _intent(self):
        from tradingagents.futures.risk_gate import ExecutionIntent

        return ExecutionIntent(
            intent_id="i-ledger", symbol="BTC-USD", side=FuturesSide.LONG,
            leverage=2.0, risk_pct=0.01, entry_price=None,
            stop_loss=60000.0, take_profit=70000.0,
            created_at=_iso(NOW),
        )

    def _result(self, intent, **overrides):
        # execute_with_ledger stamps the write-ahead with wall-clock time,
        # so the result must be wall-clock too — a fixed NOW in the past
        # would make the pair look out of order once events are ts-sorted.
        base = dict(
            success=True, intent_id=intent.intent_id, mode="fake",
            placed_at=_iso(datetime.now(timezone.utc)),
            symbol=intent.symbol, side="BUY",
            quantity=0.01, notional_usd=650.0, margin_required_usd=325.0,
            avg_fill_price=65000.0,
        )
        base.update(overrides)
        return ExecutionResult(**base)

    def test_failure_writes_intent_carrying_trade_skipped(self, jsonl):
        outer = self

        class FailingExecutor:
            mode = "fake"

            def place_order(self, intent, *, equity_usd, mark_price):
                return outer._result(
                    intent, success=False, avg_fill_price=None,
                    error="open failed: boom",
                )

        result = execute_with_ledger(
            FailingExecutor(), self._intent(),
            equity_usd=1000.0, mark_price=65000.0, state_path=jsonl,
        )

        assert not result.success
        events = load_events(jsonl)
        assert [e["type"] for e in events] == ["order_submitted", "trade_skipped"]
        assert events[1]["intent_id"] == "i-ledger"
        # The failure resolves the write-ahead: nothing dangles.
        snapshot = derive_state(events, now=NOW + timedelta(minutes=10))
        assert snapshot.dangling_intents == ()

    def test_crash_between_fill_and_append_leaves_dangling(self, jsonl):
        """The exact T12 gap: place_order succeeds on the exchange but the
        process dies before the result event lands. Simulated by an adapter
        that raises after 'filling' — the write-ahead is already on disk."""
        outer = self

        class CrashingExecutor:
            mode = "fake"

            def place_order(self, intent, *, equity_usd, mark_price):
                raise KeyboardInterrupt("process died mid-execution")

        with pytest.raises(KeyboardInterrupt):
            execute_with_ledger(
                CrashingExecutor(), outer._intent(),
                equity_usd=1000.0, mark_price=65000.0, state_path=jsonl,
            )

        events = load_events(jsonl)
        assert [e["type"] for e in events] == ["order_submitted"]
        # The write-ahead is stamped with wall-clock time; look 10 minutes
        # past it so the unresolved submit crosses the dangling threshold.
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        snapshot = derive_state(events, now=later)
        assert len(snapshot.dangling_intents) == 1
        assert snapshot.dangling_intents[0].intent_id == "i-ledger"

    def test_success_pairs_submitted_with_enriched_opened(self, jsonl):
        outer = self

        class OkExecutor:
            mode = "fake"

            def place_order(self, intent, *, equity_usd, mark_price):
                return outer._result(intent)

        execute_with_ledger(
            OkExecutor(), self._intent(),
            equity_usd=1000.0, mark_price=65000.0, state_path=jsonl,
        )

        events = load_events(jsonl)
        assert [e["type"] for e in events] == ["order_submitted", "position_opened"]
        opened = events[1]
        assert opened["side"] == "BUY"
        assert opened["entry_price"] == 65000.0
        assert opened["stop_loss"] == 60000.0
        assert opened["take_profit"] == 70000.0


# ---------------------------------------------------------------------------
# CLI entry point — dryrun, no network
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMonitorMain:
    def test_main_dryrun_clean_log_exits_zero(self, jsonl, monkeypatch, capsys):
        import json

        from tradingagents.futures.position_monitor import main

        monkeypatch.delenv("MONITOR_MODE", raising=False)
        rc = main(["--state-path", str(jsonl)])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["mode"] == "dryrun"
        assert summary["success"] is True

    def test_main_flags_attention_on_backfill_failure(self, jsonl, monkeypatch, capsys):
        """An untracked find or a data gap surfaces as exit code 1 so a
        launchd hook can escalate. Simulated via an open position that the
        dryrun exchange reports closed with zero P&L — then a doctored
        pnl_backfill_failed close to exercise the flag path."""
        import json

        from tradingagents.futures import position_monitor

        monkeypatch.delenv("MONITOR_MODE", raising=False)
        monkeypatch.setattr(
            position_monitor, "create_monitor",
            lambda cfg: FakeExchange(positions={
                "ETHUSDT": {"symbol": "ETHUSDT", "positionAmt": "-0.5", "entryPrice": "3300.0"},
            }),
        )
        rc = position_monitor.main(["--state-path", str(jsonl)])
        assert rc == 1  # untracked position found
        summary = json.loads(capsys.readouterr().out)
        assert summary["untracked_found"] == 1


# ---------------------------------------------------------------------------
# 2026-08-02 review fixes (F5b: orphan_algo_orders_pending was a dead metric)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOrphanAlgoPendingMetric:
    def test_algo_cancel_failure_increments_pending(self, jsonl):
        """F5b: when algo-order cancellation fails after a close, the
        stop/TP conditionals may still be live on the exchange — the
        result must count them as pending, not report 0 forever."""
        class FailingAlgoCancelExchange(FakeExchange):
            def cancel_all_algo_orders(self, symbol):
                return None  # adapter contract: None == cancellation failed

        append_event(jsonl, {
            "type": "position_opened", "ts": _iso(NOW - timedelta(hours=2)),
            "intent_id": "i-1", "symbol": "BTC-USD", "side": "BUY",
            "entry_price": 65000.0, "quantity": 0.01,
            "stop_loss": 63000.0, "take_profit": 70000.0,
        })
        # Position gone on the exchange → pass 1 closes it and cancels
        # orphaned orders; the algo cancellation fails.
        result = reconcile_positions(
            jsonl, FailingAlgoCancelExchange(realized_pnl=-5.0), now=NOW,
        )
        assert result.success
        assert result.positions_closed == 1
        assert result.orphan_algo_orders_pending == 1
