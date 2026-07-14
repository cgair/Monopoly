"""Trade review summarization — reads memory logs and risk_gate_state.jsonl.

Produces machine-readable JSON summaries of recent decisions, open positions,
and gate rejection statistics for OpenClaw skill integration.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any
from collections import defaultdict

from tradingagents.futures.risk_state import load_events, derive_state, default_state_path


@dataclass
class GateRejectionStats:
    """Rejection reason frequency over time window."""
    reason: str
    count: int
    first_occurrence_ts: str
    last_occurrence_ts: str


@dataclass
class TradeReviewSummary:
    """JSON-serializable trade review summary."""
    timestamp: str
    symbol: Optional[str]
    time_window_hours: int
    recent_decisions_count: int
    open_positions: int
    daily_pnl_usd: float
    last_stop_close_ts: Optional[str]
    gate_rejections: list[dict]  # list of GateRejectionStats as dicts
    recent_gate_rejections: list[dict]  # raw events within window

    def to_dict(self) -> dict:
        """Convert to JSON-serializable dict."""
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "time_window_hours": self.time_window_hours,
            "recent_decisions_count": self.recent_decisions_count,
            "open_positions": self.open_positions,
            "daily_pnl_usd": self.daily_pnl_usd,
            "last_stop_close_ts": self.last_stop_close_ts,
            "gate_rejections": self.gate_rejections,
            "recent_gate_rejections": self.recent_gate_rejections,
        }


def load_memory_log(memory_dir: Path | str) -> list[dict]:
    """Load and parse memory.jsonl if it exists.
    
    Returns a list of decoded memory log entries (one per line).
    If file doesn't exist, returns [].
    """
    p = Path(memory_dir) if isinstance(memory_dir, str) else memory_dir
    memory_file = p / "memory.jsonl" if p.is_dir() else p
    
    if not memory_file.exists():
        return []
    
    entries = []
    try:
        with open(memory_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # Skip malformed lines
    except (IOError, OSError):
        pass
    
    return entries


def analyze_trade_review(
    symbol: Optional[str] = None,
    time_window_hours: int = 24,
    memory_dir: Optional[Path | str] = None,
    risk_state_path: Optional[Path | str] = None,
) -> TradeReviewSummary:
    """Analyze trade review data from memory log and risk_gate_state.jsonl.
    
    Args:
        symbol: Optional ticker to filter by (e.g., "BTCUSDT"). If None, analyze all.
        time_window_hours: Look back this many hours for recent decisions/rejections.
        memory_dir: Path to memory log directory. Defaults to ~/.tradingagents/memory/
        risk_state_path: Path to risk_gate_state.jsonl. Defaults to ~/.tradingagents/risk_gate_state.jsonl
    
    Returns:
        TradeReviewSummary with recent decisions, positions, and rejection stats.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=time_window_hours)
    
    if memory_dir is None:
        home = Path.home()
        memory_dir = home / ".tradingagents" / "memory"
    if risk_state_path is None:
        risk_state_path = default_state_path()
    
    # Load memory log
    memory_entries = load_memory_log(memory_dir)
    
    # Load risk gate state
    risk_events = load_events(risk_state_path)
    
    # Filter memory entries to the time window
    recent_decisions = []
    for entry in memory_entries:
        try:
            entry_ts_str = entry.get("ts")
            if entry_ts_str:
                entry_ts = datetime.fromisoformat(entry_ts_str.replace("Z", "+00:00"))
                if entry_ts >= cutoff:
                    if symbol is None or entry.get("symbol") == symbol:
                        recent_decisions.append(entry)
        except (ValueError, KeyError):
            continue
    
    # Filter risk gate events to the time window
    recent_rejections = []
    rejection_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "first_ts": None, "last_ts": None}
    )
    
    for event in risk_events:
        try:
            event_ts_str = event.get("ts")
            if event_ts_str:
                event_ts = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))
                if event.get("type") == "trade_skipped":
                    reason = event.get("reason", "unknown")
                    rejection_stats[reason]["count"] += 1
                    if rejection_stats[reason]["first_ts"] is None:
                        rejection_stats[reason]["first_ts"] = event_ts_str
                    rejection_stats[reason]["last_ts"] = event_ts_str
                    
                    if event_ts >= cutoff and (symbol is None or event.get("symbol") == symbol):
                        recent_rejections.append(event)
        except (ValueError, KeyError):
            continue
    
    # Build rejection stats list
    gate_rejections = [
        {
            "reason": reason,
            "count": stats["count"],
            "first_occurrence_ts": stats["first_ts"],
            "last_occurrence_ts": stats["last_ts"],
        }
        for reason, stats in rejection_stats.items()
    ]
    
    # Derive current risk state
    snapshot = derive_state(risk_events, now=now)
    
    return TradeReviewSummary(
        timestamp=now.isoformat(),
        symbol=symbol,
        time_window_hours=time_window_hours,
        recent_decisions_count=len(recent_decisions),
        open_positions=snapshot.open_positions,
        daily_pnl_usd=snapshot.daily_realised_pnl_usd,
        last_stop_close_ts=(
            snapshot.last_stop_loss_close_ts.isoformat()
            if snapshot.last_stop_loss_close_ts
            else None
        ),
        gate_rejections=gate_rejections,
        recent_gate_rejections=recent_rejections,
    )
