"""Append-only JSONL event log for the futures risk gate.

The gate needs cross-run memory for two policies:

- **Daily drawdown halt** — cumulative realised P&L since 00:00 UTC must
  not exceed the configured loss threshold; once crossed, all new entries
  are blocked until the next UTC day.
- **Cooldown after stop-out** — after a position closes on its stop, no
  new entries for ``cooldown_after_loss_minutes``.

State is reconstructed by replaying events. A JSONL file is enough for
this scale (one ticker × two policies); SQLite would be over-engineered.

Event schema (all events carry ``type`` and ``ts``; ``ts`` is ISO-8601 UTC):

- ``{"type": "position_opened", "ts": ..., "intent_id": str, "symbol": str}``
- ``{"type": "position_closed", "ts": ..., "intent_id": str, "symbol": str,
       "pnl_usd": float, "outcome": "stop" | "tp" | "manual"}``
- ``{"type": "trade_skipped", "ts": ..., "symbol": str, "reason": str}``
   (informational; not consumed by gate policies)

Public API:

- :func:`append_event` — atomic append (one event per line).
- :func:`load_events` — parse the whole file; small enough to do every call.
- :func:`derive_state` — fold events into the policy-relevant snapshot.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class RiskGateSnapshot:
    """Snapshot of risk-gate-relevant state derived from the event log."""

    open_positions: int
    """Count of positions currently open (opened minus closed)."""

    daily_realised_pnl_usd: float
    """Sum of realised P&L from positions closed since 00:00 UTC today."""

    last_stop_loss_close_ts: Optional[datetime]
    """Timestamp of the most recent stop-out close, or None if never."""


def append_event(path: Path | str, event: dict) -> None:
    """Append a single JSON event to ``path``, creating parents as needed.

    Each event is one line written with a single ``write()`` to a file
    opened in append mode. On local POSIX filesystems an ``O_APPEND`` write
    of a small buffer is effectively atomic — the kernel resolves the file
    offset and copies the buffer under the inode lock — so concurrent
    appenders do not interleave partial lines. (This is a filesystem
    property, not the PIPE_BUF guarantee, which is about pipes/FIFOs.)
    Our events are a few hundred bytes, well within that.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_events(path: Path | str) -> list[dict]:
    """Return all events in chronological (file) order. Missing file → []."""
    p = Path(path)
    if not p.exists():
        return []
    events = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def derive_state(events: Iterable[dict], *, now: datetime) -> RiskGateSnapshot:
    """Fold events into the policy-relevant snapshot at time ``now``.

    ``now`` must be timezone-aware UTC. The daily-drawdown window is
    [today 00:00 UTC, now] — events outside this window contribute to
    ``open_positions`` and ``last_stop_loss_close_ts`` but not to
    ``daily_realised_pnl_usd``.
    """
    if now.tzinfo is None:
        raise ValueError("derive_state requires a timezone-aware 'now' (UTC)")

    today_start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    open_count = 0
    daily_pnl = 0.0
    last_stop_close_ts: Optional[datetime] = None

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "position_opened":
            open_count += 1
        elif ev_type == "position_closed":
            open_count -= 1
            ts = _parse_ts(ev["ts"])
            if ts >= today_start:
                daily_pnl += float(ev.get("pnl_usd", 0.0))
            if ev.get("outcome") == "stop":
                if last_stop_close_ts is None or ts > last_stop_close_ts:
                    last_stop_close_ts = ts
        # trade_skipped is informational; skip
    return RiskGateSnapshot(
        open_positions=max(0, open_count),
        daily_realised_pnl_usd=daily_pnl,
        last_stop_loss_close_ts=last_stop_close_ts,
    )


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; tolerates trailing 'Z'."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string (no microseconds)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_state_path() -> Path:
    """Return the default risk-gate state file path.

    Lives under the standard Monopoly results dir. Centralised here so
    Executor (Phase 4) and node wiring (Phase 5) point at the same file
    without re-deriving the path.
    """
    home = os.path.join(os.path.expanduser("~"), ".tradingagents")
    return Path(os.getenv("TRADINGAGENTS_RISK_GATE_STATE_PATH",
                          os.path.join(home, "risk_gate_state.jsonl")))
