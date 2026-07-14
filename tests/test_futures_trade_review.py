"""Tests for trade review summarization."""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.futures.trade_review import (
    load_memory_log,
    analyze_trade_review,
    TradeReviewSummary,
)


@pytest.mark.unit
class TestLoadMemoryLog:
    def test_load_nonexistent_file(self):
        """Should return [] for missing file."""
        with TemporaryDirectory() as tmpdir:
            result = load_memory_log(Path(tmpdir))
            assert result == []

    def test_load_empty_file(self):
        """Should return [] for empty file."""
        with TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.jsonl"
            memory_file.touch()
            result = load_memory_log(Path(tmpdir))
            assert result == []

    def test_load_valid_jsonl(self):
        """Should parse valid JSONL entries."""
        with TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.jsonl"
            entries = [
                {"ts": "2026-07-14T10:00:00+00:00", "symbol": "BTCUSDT", "decision": "LONG"},
                {"ts": "2026-07-14T11:00:00+00:00", "symbol": "ETHUSDT", "decision": "SHORT"},
            ]
            with open(memory_file, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
            
            result = load_memory_log(Path(tmpdir))
            assert len(result) == 2
            assert result[0]["symbol"] == "BTCUSDT"
            assert result[1]["symbol"] == "ETHUSDT"

    def test_load_skip_malformed_lines(self):
        """Should skip malformed JSON lines."""
        with TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.jsonl"
            with open(memory_file, "w") as f:
                f.write('{"ts": "2026-07-14T10:00:00+00:00", "symbol": "BTCUSDT"}\n')
                f.write('this is not json\n')
                f.write('{"ts": "2026-07-14T11:00:00+00:00", "symbol": "ETHUSDT"}\n')
            
            result = load_memory_log(Path(tmpdir))
            assert len(result) == 2


@pytest.mark.unit
class TestAnalyzeTradeReview:
    def test_empty_logs(self):
        """Should handle missing logs gracefully."""
        with TemporaryDirectory() as tmpdir:
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=Path(tmpdir) / "risk_gate_state.jsonl",
            )
            assert summary.recent_decisions_count == 0
            assert summary.open_positions == 0
            assert summary.daily_pnl_usd == 0.0
            assert summary.gate_rejections == []

    def test_filter_by_symbol(self):
        """Should filter memory entries by symbol."""
        with TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc)
            memory_file = Path(tmpdir) / "memory.jsonl"
            
            entries = [
                {"ts": now.isoformat(), "symbol": "BTCUSDT", "decision": "LONG"},
                {"ts": now.isoformat(), "symbol": "ETHUSDT", "decision": "SHORT"},
            ]
            with open(memory_file, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
            
            # Filter by BTCUSDT only
            summary = analyze_trade_review(
                symbol="BTCUSDT",
                memory_dir=Path(tmpdir),
                risk_state_path=Path(tmpdir) / "risk_gate_state.jsonl",
            )
            assert summary.recent_decisions_count == 1
            assert summary.symbol == "BTCUSDT"

    def test_time_window_filter(self):
        """Should respect time window for recent decisions."""
        with TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc)
            old = now - timedelta(hours=25)
            memory_file = Path(tmpdir) / "memory.jsonl"
            
            entries = [
                {"ts": old.isoformat(), "symbol": "BTCUSDT", "decision": "LONG"},
                {"ts": now.isoformat(), "symbol": "BTCUSDT", "decision": "SHORT"},
            ]
            with open(memory_file, "w") as f:
                for entry in entries:
                    f.write(json.dumps(entry) + "\n")
            
            # 24-hour window should exclude old entry
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=Path(tmpdir) / "risk_gate_state.jsonl",
                time_window_hours=24,
            )
            assert summary.recent_decisions_count == 1

    def test_gate_rejection_stats(self):
        """Should aggregate rejection reasons."""
        with TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc)
            risk_file = Path(tmpdir) / "risk_gate_state.jsonl"
            
            events = [
                {
                    "type": "trade_skipped",
                    "ts": now.isoformat(),
                    "symbol": "BTCUSDT",
                    "reason": "max_positions_reached",
                },
                {
                    "type": "trade_skipped",
                    "ts": now.isoformat(),
                    "symbol": "BTCUSDT",
                    "reason": "max_positions_reached",
                },
                {
                    "type": "trade_skipped",
                    "ts": now.isoformat(),
                    "symbol": "BTCUSDT",
                    "reason": "daily_drawdown_halt",
                },
            ]
            with open(risk_file, "w") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=risk_file,
            )
            assert len(summary.gate_rejections) == 2
            # Find the rejection with count=2
            max_pos_rejection = next(
                (r for r in summary.gate_rejections if r["reason"] == "max_positions_reached"),
                None,
            )
            assert max_pos_rejection is not None
            assert max_pos_rejection["count"] == 2

    def test_return_type_is_serializable(self):
        """Should return dict that is JSON-serializable."""
        with TemporaryDirectory() as tmpdir:
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=Path(tmpdir) / "risk_gate_state.jsonl",
            )
            # Should not raise
            result_dict = summary.to_dict()
            json_str = json.dumps(result_dict)
            assert isinstance(json_str, str)

    def test_position_state_from_events(self):
        """Should derive open position count from events."""
        with TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc)
            risk_file = Path(tmpdir) / "risk_gate_state.jsonl"
            
            events = [
                {"type": "position_opened", "ts": now.isoformat(), "symbol": "BTCUSDT"},
                {"type": "position_opened", "ts": now.isoformat(), "symbol": "ETHUSDT"},
            ]
            with open(risk_file, "w") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=risk_file,
            )
            assert summary.open_positions == 2

    def test_pnl_calculation(self):
        """Should calculate daily realized P&L."""
        with TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc)
            risk_file = Path(tmpdir) / "risk_gate_state.jsonl"
            
            events = [
                {"type": "position_opened", "ts": now.isoformat(), "symbol": "BTCUSDT"},
                {
                    "type": "position_closed",
                    "ts": now.isoformat(),
                    "symbol": "BTCUSDT",
                    "pnl_usd": 150.50,
                    "outcome": "tp",
                },
                {
                    "type": "position_closed",
                    "ts": now.isoformat(),
                    "symbol": "ETHUSDT",
                    "pnl_usd": -50.25,
                    "outcome": "stop",
                },
            ]
            with open(risk_file, "w") as f:
                for event in events:
                    f.write(json.dumps(event) + "\n")
            
            summary = analyze_trade_review(
                memory_dir=Path(tmpdir),
                risk_state_path=risk_file,
            )
            # 150.50 + (-50.25) = 100.25
            assert summary.daily_pnl_usd == pytest.approx(100.25, abs=0.01)
