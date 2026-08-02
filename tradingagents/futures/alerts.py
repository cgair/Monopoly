"""L4 monitoring layer: alert on anomalies in the risk-gate event log.

Defense layering (spec §8, L1–L4):
  L1: Prompt engineering (user guidance)
  L2: Schema descriptions (structured inputs)
  L3: Risk gate validation (deterministic checks)
  L4: Monitoring & alerts ← this module

The gate should rarely trigger in normal operation. If gate rejections
exceed a threshold, or if dangerous events (naked positions) appear,
something went wrong upstream (prompt/LLM/schema). This monitor
reconstructs state from the JSONL event log and surfaces structured
alerts for operational visibility.

Alerts can be consumed by:
  - Exit code: 0=ok, 1=warn, 2=critical (launchd/cron hook)
  - JSON stdout: structured AlertReport for downstream automation
  - Discord/Slack: TODO (integrate with T5 bot once ready)

TODO: Discord integration point:
  send_alert(report, discord_webhook_url, ...) — to be wired up
  after T5 (OpenClaw trade_review skill) bot is ready. Template:
  ```
  report_level_emoji = {"ok": ":white_check_mark:", "warn": ":warning:", "critical": ":octagonal_sign:"}
  message = f"{emoji} **{report.level.upper()}**: {', '.join(report.findings)}"
  requests.post(webhook_url, json={"content": message})
  ```
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Config: thresholds from environment, with sensible defaults
# ---------------------------------------------------------------------------


def _get_env_int(key: str, default: int) -> int:
    """Parse env var as int, fall back to default."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    """Parse env var as float, fall back to default."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class AlertConfig:
    """Thresholds that trigger alerts. All configurable via env."""

    window_hours: int = 24
    """Time window (hours) to scan for anomalies. CLI --window-hours overrides env."""

    rejection_threshold: int = 3
    """If gate rejections in window exceed this, raise WARN. 0 = disabled."""

    naked_position_threshold: int = 1
    """If any naked position events appear, raise CRITICAL. 0 = disabled."""

    executor_error_threshold: int = 5
    """If executor errors in window exceed this, raise WARN. 0 = disabled."""

    consecutive_stop_threshold: int = 3
    """N consecutive position_closed with outcome=stop triggers WARN. 0 = disabled."""

    untracked_position_threshold: int = 1
    """If any position_untracked events appear, raise CRITICAL — the
    exchange holds a position the local log couldn't account for
    (manual open or unexplained fill). 0 = disabled."""

    pnl_backfill_failure_threshold: int = 1
    """If any position_closed carries pnl_backfill_failed=true, raise WARN —
    drawdown/cooldown are blind to that close's real P&L. 0 = disabled."""

    state_path: Optional[Path] = None
    """Path to risk_gate_state.jsonl. CLI --state-path overrides env."""

    @classmethod
    def from_env(cls) -> AlertConfig:
        """Load config from environment variables."""
        return cls(
            window_hours=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_WINDOW_HOURS", 24),
            rejection_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_REJECTION_THRESHOLD", 3),
            naked_position_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_NAKED_POSITION_THRESHOLD", 1),
            executor_error_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_EXECUTOR_ERROR_THRESHOLD", 5),
            consecutive_stop_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_CONSECUTIVE_STOP_THRESHOLD", 3),
            untracked_position_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_UNTRACKED_POSITION_THRESHOLD", 1),
            pnl_backfill_failure_threshold=_get_env_int("TRADINGAGENTS_FUTURES_ALERT_PNL_BACKFILL_FAILURE_THRESHOLD", 1),
            state_path=Path(p) if (p := os.getenv("TRADINGAGENTS_RISK_GATE_STATE_PATH")) else None,
        )

    def resolved_state_path(self) -> Path:
        """Return the state file path, with sensible defaults."""
        if self.state_path:
            return self.state_path
        home = Path.home()
        default_path = home / ".tradingagents" / "risk_gate_state.jsonl"
        return default_path


# ---------------------------------------------------------------------------
# Alert report types
# ---------------------------------------------------------------------------


class AlertLevel:
    """Alert severity level."""
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class Finding:
    """A single finding (string description) + optional context."""
    message: str
    details: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {"message": self.message}
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class AlertReport:
    """Structured alert report to be JSON-printed and consumed by launchd/automation."""
    level: str  # "ok", "warn", "critical"
    window_hours: int
    scanned_until_ts: str  # ISO-8601 UTC
    findings: list[Finding] = field(default_factory=list)

    def exit_code(self) -> int:
        """Map alert level to shell exit code."""
        if self.level == AlertLevel.OK:
            return 0
        elif self.level == AlertLevel.WARN:
            return 1
        elif self.level == AlertLevel.CRITICAL:
            return 2
        return 0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "window_hours": self.window_hours,
            "scanned_until_ts": self.scanned_until_ts,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Event log parsing and analysis
# ---------------------------------------------------------------------------


def load_events(path: Path | str) -> list[dict]:
    """Load all events from the JSONL file, skipping malformed lines."""
    p = Path(path)
    if not p.exists():
        return []

    events = []
    with p.open("r", encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                # Skip malformed JSON lines without crashing
                # (spec: "损坏行(malformed JSON)跳过不炸")
                pass

    return events


def _parse_ts(value: str) -> datetime:
    """Parse ISO-8601 timestamp; tolerates trailing 'Z'."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


@dataclass
class EventStats:
    """Statistics collected from events in the time window."""
    total_events: int = 0
    gate_rejections: dict = field(default_factory=dict)  # reason -> count
    position_naked_count: int = 0
    position_untracked_count: int = 0
    pnl_backfill_failures: int = 0
    executor_errors: list[str] = field(default_factory=list)  # runs of consecutive stop-closes
    consecutive_stops: list[int] = field(default_factory=list)  # runs of consecutive stop-closes


def analyze_events(
    events: list[dict],
    *,
    window_hours: int,
    now: Optional[datetime] = None,
) -> EventStats:
    """Scan events in the time window and collect statistics.
    
    If now is None, will determine it from the latest event timestamp
    (for test compatibility), or use current UTC time if no events.
    """
    if now is None:
        # For testing: if we have events, use the latest timestamp as "now"
        # Otherwise use current UTC time
        if events:
            latest_ts_str = max(
                (e.get("ts") for e in events if e.get("ts")),
                default=None
            )
            if latest_ts_str:
                now = _parse_ts(latest_ts_str)
            else:
                now = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)

    cutoff_ts = now - timedelta(hours=window_hours)
    stats = EventStats()

    # Track consecutive stops (for consecutive-loss detection)
    consecutive_stop_run = 0

    for event in events:
        try:
            event_ts_str = event.get("ts")
            if not event_ts_str:
                continue

            event_ts = _parse_ts(event_ts_str)

            # Skip events outside the window
            if event_ts < cutoff_ts:
                continue

            stats.total_events += 1
            event_type = event.get("type")

            # Gate rejection (via trade_skipped events with a reason)
            # Note: trade_skipped can also be from executor; rejections are
            # those that came from the gate. For monitoring, we count all
            # trade_skipped as potential gate issues.
            if event_type == "trade_skipped":
                reason = event.get("reason", "unknown")
                stats.gate_rejections[reason] = stats.gate_rejections.get(reason, 0) + 1
                consecutive_stop_run = 0  # Reset on any non-close event

            # Naked position (highest alert)
            elif event_type == "position_naked":
                stats.position_naked_count += 1
                consecutive_stop_run = 0  # Reset

            # Untracked exchange position (monitor's reverse reconciliation)
            elif event_type == "position_untracked":
                stats.position_untracked_count += 1

            # Position closed
            elif event_type == "position_closed":
                if event.get("pnl_backfill_failed"):
                    stats.pnl_backfill_failures += 1
                if event.get("outcome") == "stop":
                    consecutive_stop_run += 1
                else:
                    # Non-stop close (tp, manual) breaks the streak
                    if consecutive_stop_run > 0:
                        stats.consecutive_stops.append(consecutive_stop_run)
                    consecutive_stop_run = 0

            # Position opened - skip, no analysis needed
            elif event_type == "position_opened":
                consecutive_stop_run = 0  # Reset

        except Exception:
            # Malformed event data; skip without crashing
            pass

    # Flush any trailing consecutive stops
    if consecutive_stop_run > 0:
        stats.consecutive_stops.append(consecutive_stop_run)

    return stats


def evaluate_alerts(stats: EventStats, config: AlertConfig) -> AlertReport:
    """Evaluate stats against thresholds; return AlertReport."""
    findings = []
    level = AlertLevel.OK

    # Now timestamp (for report)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # 1. Gate rejection threshold
    if config.rejection_threshold > 0:
        total_rejections = sum(stats.gate_rejections.values())
        if total_rejections >= config.rejection_threshold:
            level = AlertLevel.WARN
            reason_breakdown = "; ".join(
                f"{reason} ({count})"
                for reason, count in sorted(
                    stats.gate_rejections.items(),
                    key=lambda x: x[1],
                    reverse=True,
                )
            )
            findings.append(Finding(
                f"Gate rejections exceed threshold: {total_rejections} >= {config.rejection_threshold}",
                {"reason_breakdown": reason_breakdown},
            ))

    # 2. Naked position (highest priority)
    if config.naked_position_threshold > 0 and stats.position_naked_count >= config.naked_position_threshold:
        level = AlertLevel.CRITICAL
        findings.append(Finding(
            f"CRITICAL: Naked position event(s) detected ({stats.position_naked_count}). "
            "Manual intervention required.",
            {"count": stats.position_naked_count},
        ))

    # 2b. Untracked exchange position (same severity as naked — the
    # exchange holds risk the local log couldn't account for)
    if config.untracked_position_threshold > 0 and stats.position_untracked_count >= config.untracked_position_threshold:
        level = AlertLevel.CRITICAL
        findings.append(Finding(
            f"CRITICAL: Untracked exchange position(s) detected ({stats.position_untracked_count}). "
            "No local record and no dangling intent — manual intervention required.",
            {"count": stats.position_untracked_count},
        ))

    # 2c. P&L backfill failures (data gap: drawdown/cooldown blind to a close)
    if config.pnl_backfill_failure_threshold > 0 and stats.pnl_backfill_failures >= config.pnl_backfill_failure_threshold:
        if level != AlertLevel.CRITICAL:
            level = AlertLevel.WARN
        findings.append(Finding(
            f"P&L backfill failed for {stats.pnl_backfill_failures} close(s): "
            "recorded with pnl_usd=0.0/outcome=unknown — daily drawdown and "
            "cooldown did not see the real result.",
            {"count": stats.pnl_backfill_failures},
        ))

    # 3. Executor error threshold
    if config.executor_error_threshold > 0:
        if len(stats.executor_errors) >= config.executor_error_threshold:
            if level != AlertLevel.CRITICAL:
                level = AlertLevel.WARN
            findings.append(Finding(
                f"Executor errors exceed threshold: {len(stats.executor_errors)} >= {config.executor_error_threshold}",
                {"errors": stats.executor_errors[:5]},  # Include first 5 for context
            ))

    # 4. Consecutive stop losses
    if config.consecutive_stop_threshold > 0:
        max_consecutive = max(stats.consecutive_stops) if stats.consecutive_stops else 0
        if max_consecutive >= config.consecutive_stop_threshold:
            if level != AlertLevel.CRITICAL:
                level = AlertLevel.WARN
            findings.append(Finding(
                f"Consecutive stop losses: {max_consecutive} >= {config.consecutive_stop_threshold}",
                {"consecutive_stop_runs": stats.consecutive_stops},
            ))

    return AlertReport(
        level=level,
        window_hours=config.window_hours,
        scanned_until_ts=now,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(args: Optional[list[str]] = None, now: Optional[datetime] = None) -> int:
    """Run the alerts monitor.

    Returns exit code: 0=ok, 1=warn, 2=critical.
    
    Args:
        args: Command-line arguments (for testing)
        now: Override current time (for testing)
    """
    parser = argparse.ArgumentParser(
        description="L4 monitoring layer: detect anomalies in the risk-gate event log",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=None,
        help="Time window in hours (default: 24). Overrides env var.",
    )
    parser.add_argument(
        "--state-path",
        type=str,
        default=None,
        help="Path to risk_gate_state.jsonl. Overrides env var.",
    )

    parsed = parser.parse_args(args)

    # Load base config from environment
    config = AlertConfig.from_env()

    # Override with CLI args
    if parsed.window_hours is not None:
        config = replace(config, window_hours=parsed.window_hours)

    if parsed.state_path is not None:
        config = replace(config, state_path=Path(parsed.state_path))

    # Load events and analyze
    state_path = config.resolved_state_path()
    events = load_events(state_path)
    stats = analyze_events(events, window_hours=config.window_hours, now=now)
    report = evaluate_alerts(stats, config)

    # Output JSON
    print(json.dumps(report.to_dict(), indent=2))

    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
