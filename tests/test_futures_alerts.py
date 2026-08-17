"""Tests for the L4 monitoring/alerts layer (tradingagents.futures.alerts)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import pytest

from tradingagents.futures.alerts import (
    AlertLevel,
    AlertConfig,
    Finding,
    AlertReport,
    load_events,
    analyze_events,
    evaluate_alerts,
    main,
)


# ---------------------------------------------------------------------------
# Utilities for test JSONL construction
# ---------------------------------------------------------------------------


def _ts(dt: datetime) -> str:
    """Format datetime as ISO-8601 UTC string (no microseconds)."""
    return dt.replace(microsecond=0).isoformat()


def _append_event(path: Path, event: dict) -> None:
    """Append a single event to the JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Test: load_events (robustness)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadEvents:
    """Tests for load_events: parsing JSONL, handling malformed data."""

    def test_load_empty_file_returns_empty_list(self, tmp_path):
        """Missing file should return []."""
        path = tmp_path / "does_not_exist.jsonl"
        assert load_events(path) == []

    def test_load_skips_blank_lines(self, tmp_path):
        """Blank lines should be skipped."""
        path = tmp_path / "state.jsonl"
        path.write_text('{"type":"trade_skipped","ts":"2026-05-31T00:00:00+00:00"}\n\n\n')
        events = load_events(path)
        assert len(events) == 1

    def test_load_skips_malformed_json_lines(self, tmp_path):
        """Malformed JSON lines should be skipped without crashing."""
        path = tmp_path / "state.jsonl"
        path.write_text(
            '{"type":"trade_skipped","ts":"2026-05-31T00:00:00+00:00"}\n'
            'NOT VALID JSON HERE\n'
            '{"type":"position_naked","ts":"2026-05-31T01:00:00+00:00"}\n'
        )
        events = load_events(path)
        assert len(events) == 2
        assert events[0]["type"] == "trade_skipped"
        assert events[1]["type"] == "position_naked"


# ---------------------------------------------------------------------------
# Test: analyze_events (stats collection)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAnalyzeEvents:
    """Tests for event analysis and statistics collection."""

    def test_empty_events_yields_zero_stats(self):
        """Empty event list should give zero stats."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        stats = analyze_events([], window_hours=24, now=now)
        assert stats.total_events == 0
        assert stats.gate_rejections == {}
        assert stats.position_naked_count == 0
        assert stats.executor_errors == []
        assert stats.consecutive_stops == []

    def test_gate_rejection_counted_and_grouped_by_reason(self):
        """Gate rejections should be counted and grouped by reason."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=1)),
             "reason": "daily drawdown halt active until next UTC day"},
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=2)),
             "reason": "daily drawdown halt active until next UTC day"},
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=3)),
             "reason": "leverage exceeds configured max_leverage"},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        assert stats.gate_rejections["daily drawdown halt active until next UTC day"] == 2
        assert stats.gate_rejections["leverage exceeds configured max_leverage"] == 1

    def test_naked_position_events_counted(self):
        """Naked position events should be counted."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_naked", "ts": _ts(now - timedelta(hours=1)),
             "reason": "stop failed after entry"},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        assert stats.position_naked_count == 1

    def test_events_outside_window_excluded(self):
        """Events older than the window should be excluded."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            # Outside window (older than 24h)
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=25)),
             "reason": "old rejection"},
            # Inside window
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=1)),
             "reason": "recent rejection"},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        assert stats.total_events == 1
        assert "old rejection" not in stats.gate_rejections
        assert stats.gate_rejections["recent rejection"] == 1

    def test_consecutive_stops_detected(self):
        """Consecutive position_closed with outcome=stop should be tracked."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=3)),
             "outcome": "stop", "pnl_usd": -50.0},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=2)),
             "outcome": "stop", "pnl_usd": -30.0},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
             "outcome": "stop", "pnl_usd": -20.0},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        # Three consecutive stops should be in the list
        assert stats.consecutive_stops == [3]

    def test_consecutive_stops_broken_by_non_stop(self):
        """Non-stop closes should break consecutive stop runs."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=4)),
             "outcome": "stop", "pnl_usd": -50.0},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=3)),
             "outcome": "stop", "pnl_usd": -30.0},
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=2)),
             "outcome": "tp", "pnl_usd": 100.0},  # ← breaks the streak
            {"type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
             "outcome": "stop", "pnl_usd": -20.0},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        # Should see two runs: [2, 1]
        assert sorted(stats.consecutive_stops) == [1, 2]

    def test_event_without_timestamp_skipped(self):
        """Events without a 'ts' field should be skipped gracefully."""
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "trade_skipped"},  # ← missing 'ts'
            {"type": "trade_skipped", "ts": _ts(now - timedelta(hours=1)),
             "reason": "valid event"},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        # Should only count the valid event
        assert stats.total_events == 1


# ---------------------------------------------------------------------------
# Test: evaluate_alerts (threshold evaluation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateAlerts:
    """Tests for alert threshold evaluation."""

    def test_clean_log_yields_ok(self):
        """Empty stats with thresholds > 0 should yield OK."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats()
        config = AlertConfig(rejection_threshold=3, naked_position_threshold=1)
        report = evaluate_alerts(stats, config)
        assert report.level == AlertLevel.OK
        assert len(report.findings) == 0

    def test_rejection_threshold_exceeded_yields_warn(self):
        """Gate rejections >= threshold should yield WARN."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats(
            gate_rejections={
                "leverage exceeds configured max_leverage": 2,
                "daily drawdown halt active until next UTC day": 1,
            }
        )
        config = AlertConfig(rejection_threshold=3)
        report = evaluate_alerts(stats, config)
        assert report.level == AlertLevel.WARN
        assert len(report.findings) > 0
        finding = report.findings[0]
        assert "Gate rejections exceed threshold" in finding.message

    def test_naked_position_yields_critical(self):
        """Any naked position event should yield CRITICAL."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats(position_naked_count=1)
        config = AlertConfig(naked_position_threshold=1)
        report = evaluate_alerts(stats, config)
        assert report.level == AlertLevel.CRITICAL
        assert len(report.findings) > 0
        finding = report.findings[0]
        assert "CRITICAL" in finding.message
        assert "Naked position" in finding.message

    def test_consecutive_stops_threshold_exceeded_yields_warn(self):
        """Consecutive stops >= threshold should yield WARN."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats(consecutive_stops=[2, 3, 1])
        config = AlertConfig(consecutive_stop_threshold=3)
        report = evaluate_alerts(stats, config)
        assert report.level == AlertLevel.WARN
        assert len(report.findings) > 0
        finding = report.findings[0]
        assert "Consecutive stop losses" in finding.message

    def test_zero_threshold_disables_check(self):
        """Zero threshold should disable that check."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats(
            gate_rejections={"reason": 100},  # Way over any normal threshold
            position_naked_count=10,
        )
        config = AlertConfig(
            rejection_threshold=0,  # Disabled
            naked_position_threshold=0,  # Disabled
        )
        report = evaluate_alerts(stats, config)
        assert report.level == AlertLevel.OK
        assert len(report.findings) == 0

    def test_multiple_findings_accumulated(self):
        """Multiple threshold violations should accumulate findings."""
        from tradingagents.futures.alerts import EventStats
        stats = EventStats(
            gate_rejections={"reason": 5},  # >= 3
            consecutive_stops=[4],  # >= 3
        )
        config = AlertConfig(rejection_threshold=3, consecutive_stop_threshold=3)
        report = evaluate_alerts(stats, config)
        assert len(report.findings) >= 2


# ---------------------------------------------------------------------------
# Test: end-to-end with fixture JSONL
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEndToEnd:
    """End-to-end tests with fixture JSONL files."""

    def test_clean_jsonl_exits_zero(self, tmp_path):
        """Clean log (no alerts) should return exit code 0."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        # Append some normal events
        _append_event(path, {
            "type": "position_opened", "ts": _ts(now - timedelta(hours=2)),
            "intent_id": "pos1", "symbol": "BTC-USD"
        })
        _append_event(path, {
            "type": "position_closed", "ts": _ts(now - timedelta(hours=1)),
            "intent_id": "pos1", "symbol": "BTC-USD",
            "pnl_usd": 50.0, "outcome": "tp"  # ← healthy tp, not stop
        })

        # Run main() with CLI args and explicit now
        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)
        assert exit_code == 0

    def test_rejection_threshold_exceeded_exits_one(self, tmp_path):
        """Excessive rejections should return exit code 1."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        for i in range(4):  # 4 rejections > default threshold of 3
            _append_event(path, {
                "type": "trade_skipped", "ts": _ts(now - timedelta(hours=i)),
                "reason": "leverage exceeds configured max_leverage"
            })

        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)
        assert exit_code == 1

    def test_naked_position_exits_two(self, tmp_path):
        """Any naked position should return exit code 2."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        _append_event(path, {
            "type": "position_naked", "ts": _ts(now - timedelta(hours=1)),
            "reason": "stop failed"
        })

        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)
        assert exit_code == 2

    def test_consecutive_stop_losses_triggers_alert(self, tmp_path):
        """Three consecutive stops should trigger WARN."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        for i in range(3):
            _append_event(path, {
                "type": "position_closed", "ts": _ts(now - timedelta(hours=i)),
                "outcome": "stop", "pnl_usd": -50.0
            })

        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)
        assert exit_code == 1  # WARN

    def test_window_filtering_excludes_old_events(self, tmp_path):
        """Events older than window should not trigger alerts."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        # Add old event outside 24h window
        _append_event(path, {
            "type": "trade_skipped", "ts": _ts(now - timedelta(hours=30)),
            "reason": "leverage exceeds configured max_leverage"
        })

        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)
        assert exit_code == 0  # Should be OK (event outside window)

    def test_output_json_structure(self, tmp_path, capsys):
        """Output should be valid JSON with expected structure."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        _append_event(path, {
            "type": "trade_skipped", "ts": _ts(now - timedelta(hours=1)),
            "reason": "test reason"
        })

        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        assert output["level"] in ["ok", "warn", "critical"]
        assert output["window_hours"] == 24
        assert "scanned_until_ts" in output
        assert isinstance(output["findings"], list)

    def test_env_var_window_hours(self, tmp_path, monkeypatch, capsys):
        """CLI --window-hours should override env var."""
        path = tmp_path / "state.jsonl"
        now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)

        # Set env to 12 hours
        monkeypatch.setenv("TRADINGAGENTS_FUTURES_ALERT_WINDOW_HOURS", "12")

        # Add event just outside 12h but inside 24h
        _append_event(path, {
            "type": "trade_skipped", "ts": _ts(now - timedelta(hours=13)),
            "reason": "test"
        })

        # Using 24h window should see the event
        exit_code = main([
            "--window-hours", "24",
            "--state-path", str(path),
        ], now=now)

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["window_hours"] == 24


# ---------------------------------------------------------------------------
# Test: AlertReport dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAlertReport:
    """Tests for AlertReport dataclass."""

    def test_exit_code_mapping(self):
        """Exit codes should map correctly to alert levels."""
        assert AlertReport(level=AlertLevel.OK, window_hours=24, scanned_until_ts="now").exit_code() == 0
        assert AlertReport(level=AlertLevel.WARN, window_hours=24, scanned_until_ts="now").exit_code() == 1
        assert AlertReport(level=AlertLevel.CRITICAL, window_hours=24, scanned_until_ts="now").exit_code() == 2

    def test_to_dict_serialization(self):
        """Report should serialize to valid dict."""
        finding = Finding("Test finding", {"detail": "value"})
        report = AlertReport(
            level=AlertLevel.WARN,
            window_hours=24,
            scanned_until_ts="2026-05-31T12:00:00+00:00",
            findings=[finding],
        )
        data = report.to_dict()
        assert data["level"] == AlertLevel.WARN
        assert data["window_hours"] == 24
        assert len(data["findings"]) == 1
        assert data["findings"][0]["message"] == "Test finding"
        assert data["findings"][0]["details"]["detail"] == "value"


# ---------------------------------------------------------------------------
# Test: Finding dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFinding:
    """Tests for Finding dataclass."""

    def test_finding_with_details(self):
        """Finding should include optional details."""
        f = Finding("msg", {"key": "value"})
        d = f.to_dict()
        assert d["message"] == "msg"
        assert d["details"]["key"] == "value"

    def test_finding_without_details(self):
        """Finding should omit details key if None."""
        f = Finding("msg", None)
        d = f.to_dict()
        assert d["message"] == "msg"
        assert "details" not in d


# ---------------------------------------------------------------------------
# 2026-08-02 review fixes (F5a: executor_errors was a dead metric)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExecutorErrorMetric:
    """F5a: analyze_events never populated executor_errors, so the
    executor_error_threshold alert could never fire — consecutive
    executor crashes reported OK forever."""

    def test_executor_origin_skips_counted_as_executor_errors(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        events = []
        for i in range(5):
            events.append({
                "type": "trade_skipped",
                "ts": _ts(now - timedelta(minutes=30 - i)),
                "symbol": "BTC-USD",
                "reason": f"open failed: ConnectionError #{i}",
                "origin": "executor",
            })
        stats = analyze_events(events, window_hours=24, now=now)
        assert len(stats.executor_errors) == 5
        report = evaluate_alerts(stats, AlertConfig())
        assert report.level == AlertLevel.WARN
        assert any("Executor errors" in f.message for f in report.findings)

    def test_gate_rejections_not_counted_as_executor_errors(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        events = [
            {"type": "trade_skipped", "ts": _ts(now - timedelta(minutes=10)),
             "symbol": "BTC-USD", "reason": "leverage exceeds configured max_leverage"},
        ]
        stats = analyze_events(events, window_hours=24, now=now)
        assert stats.executor_errors == []


# ---------------------------------------------------------------------------
# Test: --notify integration (fail-open property)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAlertsNotifyIntegration:
    """Tests for --notify flag in alerts.py."""

    def test_notify_flag_not_sent_when_webhook_unconfigured(self, tmp_path):
        """Alert with --notify but no webhook should not crash."""
        # Create a minimal event log
        state_file = tmp_path / "state.jsonl"
        state_file.write_text('{"event": "gate_rejected", "ts": "2026-08-09T00:00:00Z", "reason": "test"}\n')

        # Run with --notify but no webhook configured
        result = main(
            args=[
                "--state-path", str(state_file),
                "--notify",
            ],
            now=datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
        )

        # Should still return success (0 for clean log)
        assert result == 0

    def test_notify_flag_parsed_without_error(self, tmp_path):
        """--notify flag should parse without error."""
        state_file = tmp_path / "state.jsonl"
        state_file.write_text('')

        # Should not raise
        result = main(
            args=["--state-path", str(state_file), "--notify"],
            now=datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert result == 0


# ---------------------------------------------------------------------------
# Test: fail-open isolation (hard constraint from §5.5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAlertsFailOpenIsolation:
    """§5.5 hard constraint: a failing push changes neither exit code nor stdout.

    These tests arm the push path for real — webhook configured, a
    ``position_naked`` event driving the report to critical, dedup
    answering "send" — and then blow up inside it. The exit code must
    still be exactly ``AlertReport.exit_code()`` (2 here): launchd's
    escalation depends on it, and a push failure that flipped it would
    mask the very alert it was trying to deliver.
    """

    NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)

    def _critical_log(self, tmp_path):
        state_file = tmp_path / "state.jsonl"
        state_file.write_text(
            '{"type": "position_naked", '
            '"ts": "' + _ts(self.NOW - timedelta(hours=1)) + '", '
            '"symbol": "BTCUSDT"}\n'
        )
        return state_file

    def _arm_push_path(self, monkeypatch):
        from tradingagents.default_config import DEFAULT_CONFIG
        import tradingagents.notify.discord as notify_discord

        monkeypatch.setitem(
            DEFAULT_CONFIG, "discord_webhook_url",
            "https://discord.com/api/webhooks/123/test",
        )
        # Bypass the real dedup file (~/.tradingagents/) in tests.
        monkeypatch.setattr(notify_discord, "should_send_alert", lambda *a, **k: True)
        monkeypatch.setattr(notify_discord, "record_alert_sent", lambda *a, **k: None)
        return notify_discord

    def test_critical_exit_2_when_sender_raises(self, tmp_path, monkeypatch):
        notify_discord = self._arm_push_path(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(notify_discord, "send_discord", boom)
        result = main(
            args=["--state-path", str(self._critical_log(tmp_path)), "--notify"],
            now=self.NOW,
        )
        assert result == 2

    def test_critical_exit_2_when_formatting_raises(self, tmp_path, monkeypatch):
        notify_discord = self._arm_push_path(monkeypatch)

        def boom(*args, **kwargs):
            raise ValueError("bad template input")

        monkeypatch.setattr(notify_discord, "format_alert_card", boom)
        result = main(
            args=["--state-path", str(self._critical_log(tmp_path)), "--notify"],
            now=self.NOW,
        )
        assert result == 2

    def test_stdout_identical_when_push_fails(self, tmp_path, monkeypatch, capsys):
        state_file = self._critical_log(tmp_path)

        baseline_code = main(args=["--state-path", str(state_file)], now=self.NOW)
        baseline_out = capsys.readouterr().out

        notify_discord = self._arm_push_path(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr(notify_discord, "send_discord", boom)
        notify_code = main(
            args=["--state-path", str(state_file), "--notify"], now=self.NOW,
        )
        notify_out = capsys.readouterr().out

        assert notify_code == baseline_code == 2
        assert notify_out == baseline_out


if __name__ == "__main__":
    sys.exit(main())
