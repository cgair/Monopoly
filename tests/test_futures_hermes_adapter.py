"""Tests for Hermes memory adapter (T7 feature)."""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from tradingagents.futures.hermes_memory_adapter import (
    export_hermes_memory,
    _parse_memory_log,
    _load_risk_events,
)


@pytest.fixture
def temp_dirs():
    """Create temporary directories for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        memory_dir = temp_path / "memory"
        memory_dir.mkdir(parents=True)
        risk_dir = temp_path / "risk"
        risk_dir.mkdir(parents=True)
        output_dir = temp_path / "output"
        output_dir.mkdir(parents=True)
        yield {
            "root": temp_path,
            "memory": memory_dir,
            "risk": risk_dir,
            "output": output_dir,
        }


class TestParseMemoryLog:
    """Test memory log parsing."""

    def test_parse_empty_log(self, temp_dirs):
        """Test parsing empty memory log."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        result = _parse_memory_log(memory_path)
        assert result == []

    def test_parse_missing_log(self, temp_dirs):
        """Test parsing non-existent memory log."""
        memory_path = temp_dirs["memory"] / "nonexistent.md"
        result = _parse_memory_log(memory_path)
        assert result == []

    def test_parse_single_entry(self, temp_dirs):
        """Test parsing single memory entry."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        content = """[2026-07-19 | BTC-USD | Buy | pending]

DECISION:
BTC showing strong momentum, RSI > 70. Recommend long entry at 2.0x leverage.

<!-- ENTRY_END -->
"""
        memory_path.write_text(content)
        result = _parse_memory_log(memory_path)

        assert len(result) == 1
        assert result[0]["date"] == "2026-07-19"
        assert result[0]["ticker"] == "BTC-USD"
        assert result[0]["rating"] == "Buy"
        assert "RSI" in result[0]["decision"]

    def test_parse_multiple_entries(self, temp_dirs):
        """Test parsing multiple memory entries."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        content = """[2026-07-19 | BTC-USD | Buy | +2.5% | +1.8% | 3d]

DECISION:
Long entry recommendation.

REFLECTION:
Good risk management.

<!-- ENTRY_END -->

[2026-07-18 | ETH-USD | Hold | pending]

DECISION:
ETH consolidating.

<!-- ENTRY_END -->
"""
        memory_path.write_text(content)
        result = _parse_memory_log(memory_path)

        assert len(result) == 2
        assert result[0]["ticker"] == "BTC-USD"
        assert result[1]["ticker"] == "ETH-USD"
        assert "Good risk management" in result[0]["reflection"]


class TestLoadRiskEvents:
    """Test risk event loading."""

    def test_load_empty_events(self, temp_dirs):
        """Test loading empty risk events."""
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        risk_path.write_text("")
        result = _load_risk_events(risk_path)
        assert result == []

    def test_load_missing_events(self, temp_dirs):
        """Test loading non-existent risk events."""
        risk_path = temp_dirs["risk"] / "nonexistent.jsonl"
        result = _load_risk_events(risk_path)
        assert result == []

    def test_load_trade_skipped_events(self, temp_dirs):
        """Test loading trade_skipped events."""
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        now = datetime.now(timezone.utc)
        events = [
            {
                "type": "trade_skipped",
                "ts": now.isoformat(),
                "symbol": "BTC-USD",
                "reason": "human rejected",
                "intent_id": "intent-123",
            },
            {
                "type": "position_opened",
                "ts": (now - timedelta(hours=1)).isoformat(),
                "symbol": "ETH-USD",
                "intent_id": "intent-456",
            },
        ]
        with open(risk_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        result = _load_risk_events(risk_path)
        assert len(result) == 2
        assert result[0]["type"] == "trade_skipped"
        assert result[0]["reason"] == "human rejected"
        assert result[1]["type"] == "position_opened"


class TestExportHermesMemory:
    """Test hermes memory export."""

    def test_export_empty_logs(self, temp_dirs):
        """Test exporting with empty logs."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        risk_path.write_text("")

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )

        assert Path(output_file).exists()
        content = Path(output_file).read_text()
        assert "# Trading Patterns & User Overrides" in content
        assert "No signal rejections in this period" in content

    def test_export_with_overrides(self, temp_dirs):
        """Test exporting with human rejections."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"

        now = datetime.now(timezone.utc)
        events = [
            {
                "type": "trade_skipped",
                "ts": now.isoformat(),
                "symbol": "BTC-USD",
                "reason": "human rejected",
                "intent_id": "intent-001",
            },
            {
                "type": "trade_skipped",
                "ts": (now - timedelta(hours=2)).isoformat(),
                "symbol": "ETH-USD",
                "reason": "insufficient margin",
                "intent_id": "intent-002",
            },
        ]
        with open(risk_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )

        content = Path(output_file).read_text()
        assert "human rejected" in content
        assert "BTC-USD" in content
        assert "ETH-USD" in content
        assert "Rejected Signals" in content

    def test_export_with_decisions(self, temp_dirs):
        """Test exporting with recent decisions."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        content = """[2026-07-19 | BTC-USD | Buy | pending]

DECISION:
Strong momentum signal. Recommend 2.0x leverage long entry.

<!-- ENTRY_END -->
"""
        memory_path.write_text(content)
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        risk_path.write_text("")

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )

        content = Path(output_file).read_text()
        assert "BTC-USD" in content
        assert "Buy" in content
        assert "Recent Decision Summary" in content

    def test_export_idempotent(self, temp_dirs):
        """Test that export is idempotent (no duplicate entries)."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"

        now = datetime.now(timezone.utc)
        event = {
            "type": "trade_skipped",
            "ts": now.isoformat(),
            "symbol": "BTC-USD",
            "reason": "human rejected",
            "intent_id": "intent-001",
        }
        with open(risk_path, "w") as f:
            f.write(json.dumps(event) + "\n")

        # First export
        output_file1 = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )
        content1 = Path(output_file1).read_text()

        # Second export (should be nearly identical, except for generated timestamp)
        output_file2 = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )
        content2 = Path(output_file2).read_text()

        # Count occurrences of the event in first export
        count_btc = content1.count("BTC-USD")
        assert count_btc >= 1  # Should appear at least once

        # Idempotency check: count of symbol should be same
        # (only the generated timestamp line differs)
        assert content1.count("BTC-USD") == content2.count("BTC-USD")
        assert content1.count("human rejected") == content2.count("human rejected")

    def test_export_lookback_filter(self, temp_dirs):
        """Test that lookback filter works correctly."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"

        now = datetime.now(timezone.utc)
        # Old event (outside lookback)
        old_event = {
            "type": "trade_skipped",
            "ts": (now - timedelta(hours=200)).isoformat(),
            "symbol": "OLD-USD",
            "reason": "gate rejected",
            "intent_id": "intent-old",
        }
        # Recent event (inside lookback)
        recent_event = {
            "type": "trade_skipped",
            "ts": (now - timedelta(hours=1)).isoformat(),
            "symbol": "BTC-USD",
            "reason": "human rejected",
            "intent_id": "intent-new",
        }

        risk_path.write_text(json.dumps(old_event) + "\n" + json.dumps(recent_event) + "\n")

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
            hours_lookback=168,  # 7 days
        )

        content = Path(output_file).read_text()
        assert "BTC-USD" in content  # Recent event should appear in override patterns
        assert "OLD-USD" not in content  # Old event should NOT appear in recent override patterns
        # But should appear in all-time gate rejection stats
        assert "gate rejected" in content

    def test_export_creates_output_dir(self, temp_dirs):
        """Test that export creates output directory if missing."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        risk_path.write_text("")

        # Non-existent output directory
        output_dir = temp_dirs["root"] / "nonexistent" / "nested" / "output"
        assert not output_dir.exists()

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=output_dir,
        )

        assert output_dir.exists()
        assert Path(output_file).exists()

    def test_export_rejection_stats(self, temp_dirs):
        """Test that all-time rejection stats are included."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"

        now = datetime.now(timezone.utc)
        events = [
            {
                "type": "trade_skipped",
                "ts": now.isoformat(),
                "symbol": "BTC-USD",
                "reason": "human rejected",
                "intent_id": f"intent-{i}",
            }
            for i in range(3)
        ]
        events.extend([
            {
                "type": "trade_skipped",
                "ts": (now - timedelta(hours=i)).isoformat(),
                "symbol": "ETH-USD",
                "reason": "insufficient margin",
                "intent_id": f"intent-{i+10}",
            }
            for i in range(2)
        ])

        with open(risk_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )

        content = Path(output_file).read_text()
        assert "Gate Rejection Patterns" in content
        assert "human rejected" in content and "3 times" in content
        assert "insufficient margin" in content and "2 times" in content

    def test_export_malformed_events(self, temp_dirs):
        """Test robustness against malformed events."""
        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"

        # Mix of valid and malformed events
        risk_path.write_text("")
        now = datetime.now(timezone.utc)

        valid_event = {
            "type": "trade_skipped",
            "ts": now.isoformat(),
            "symbol": "BTC-USD",
            "reason": "human rejected",
            "intent_id": "intent-001",
        }

        # Write to file manually to include malformed lines
        with open(risk_path, "w") as f:
            f.write(json.dumps(valid_event) + "\n")
            f.write("{invalid json\n")
            f.write('{"type": "trade_skipped", "ts": "bad-timestamp", "symbol": "ETH"}\n')

        # Should not raise; should skip malformed events
        output_file = export_hermes_memory(
            memory_log_path=memory_path,
            risk_state_path=risk_path,
            output_dir=temp_dirs["output"],
        )

        content = Path(output_file).read_text()
        assert "BTC-USD" in content
        assert Path(output_file).exists()

    def test_cli_main(self, temp_dirs, capsys):
        """Test CLI entry point."""
        from tradingagents.futures.hermes_memory_adapter import main
        import sys

        memory_path = temp_dirs["memory"] / "trading_memory.md"
        memory_path.write_text("")
        risk_path = temp_dirs["risk"] / "risk_gate_state.jsonl"
        risk_path.write_text("")

        # Mock sys.argv
        old_argv = sys.argv
        try:
            sys.argv = [
                "hermes_memory_adapter",
                "--output-dir",
                str(temp_dirs["output"]),
                "--memory-log-path",
                str(memory_path),
                "--risk-state-path",
                str(risk_path),
            ]
            main()
        finally:
            sys.argv = old_argv

        captured = capsys.readouterr()
        assert "✓ Exported to:" in captured.out
