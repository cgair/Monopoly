"""Tests for the futures risk-gate JSONL event log + state derivation."""

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.futures.risk_state import (
    append_event,
    derive_state,
    load_events,
    utcnow_iso,
)


@pytest.mark.unit
class TestAppendAndLoad:
    def test_append_creates_file_and_parents(self, tmp_path):
        path = tmp_path / "nested" / "risk_gate_state.jsonl"
        append_event(path, {"type": "trade_skipped", "ts": utcnow_iso(),
                            "symbol": "BTC-USD", "reason": "test"})
        assert path.exists()
        events = load_events(path)
        assert len(events) == 1
        assert events[0]["type"] == "trade_skipped"

    def test_append_is_one_event_per_line(self, tmp_path):
        path = tmp_path / "state.jsonl"
        for i in range(3):
            append_event(path, {"type": "trade_skipped", "ts": utcnow_iso(),
                                "symbol": "BTC-USD", "reason": f"r{i}"})
        text = path.read_text()
        assert text.count("\n") == 3
        events = load_events(path)
        assert [ev["reason"] for ev in events] == ["r0", "r1", "r2"]

    def test_load_missing_file_returns_empty(self, tmp_path):
        assert load_events(tmp_path / "does_not_exist.jsonl") == []

    def test_load_skips_blank_lines(self, tmp_path):
        path = tmp_path / "state.jsonl"
        path.write_text('{"type":"trade_skipped","ts":"2026-05-31T00:00:00+00:00",'
                        '"symbol":"BTC-USD","reason":"x"}\n\n\n')
        assert len(load_events(path)) == 1


# ---------------------------------------------------------------------------
# derive_state — the heart of cross-run policy enforcement
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


@pytest.mark.unit
class TestDeriveState:
    def test_empty_event_log_yields_zero_state(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        snap = derive_state([], now=now)
        assert snap.open_positions == 0
        assert snap.daily_realised_pnl_usd == 0.0
        assert snap.last_stop_loss_close_ts is None

    def test_open_position_counted(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_opened", "ts": _ts(now - timedelta(hours=2)),
             "intent_id": "a", "symbol": "BTC-USD"},
        ]
        snap = derive_state(events, now=now)
        assert snap.open_positions == 1

    def test_open_then_close_nets_to_zero(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_opened", "ts": _ts(now - timedelta(hours=2)),
             "intent_id": "a", "symbol": "BTC-USD"},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
             "intent_id": "a", "symbol": "BTC-USD", "pnl_usd": -12.5, "outcome": "stop"},
        ]
        snap = derive_state(events, now=now)
        assert snap.open_positions == 0
        # Same UTC day → contributes to daily_realised_pnl_usd
        assert snap.daily_realised_pnl_usd == pytest.approx(-12.5)
        # Outcome=stop → last_stop_loss_close_ts populated
        assert snap.last_stop_loss_close_ts is not None

    def test_close_outside_today_does_not_count_to_daily_pnl(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            # Yesterday's close
            {"type": "position_closed", "ts": _ts(now - timedelta(days=1, hours=2)),
             "intent_id": "old", "symbol": "BTC-USD", "pnl_usd": -50.0, "outcome": "stop"},
            # Today's close
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
             "intent_id": "new", "symbol": "ETH-USD", "pnl_usd": -10.0, "outcome": "stop"},
        ]
        snap = derive_state(events, now=now)
        # Open count went negative on paper; derive_state clamps at 0
        assert snap.open_positions == 0
        assert snap.daily_realised_pnl_usd == pytest.approx(-10.0)

    def test_last_stop_loss_close_tracks_most_recent(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=6)),
             "intent_id": "a", "symbol": "BTC-USD", "pnl_usd": -5.0, "outcome": "stop"},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
             "intent_id": "b", "symbol": "BTC-USD", "pnl_usd": -3.0, "outcome": "stop"},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=2)),
             "intent_id": "c", "symbol": "ETH-USD", "pnl_usd": -1.0, "outcome": "tp"},
        ]
        snap = derive_state(events, now=now)
        # Latest stop-out is b (1 hour ago)
        assert snap.last_stop_loss_close_ts == datetime(2026, 5, 31, 11, 0, tzinfo=timezone.utc)

    def test_trade_skipped_events_are_informational_only(self):
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=1)),
             "symbol": "BTC-USD", "reason": "leverage over cap"},
        ]
        snap = derive_state(events, now=now)
        # trade_skipped does not affect any derived field
        assert snap.open_positions == 0
        assert snap.daily_realised_pnl_usd == 0.0
        assert snap.last_stop_loss_close_ts is None

    def test_naive_now_rejected(self):
        with pytest.raises(ValueError):
            derive_state([], now=datetime(2026, 5, 31, 12, 0))  # no tzinfo
