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

- ``{"type": "order_submitted", "ts": ..., "intent_id": str, "symbol": str,
       "side": str, "stop_loss": float, "take_profit": float | null}``
   Write-ahead record appended **before** the executor calls
   ``place_order()`` (dryrun included, so the event stream is uniform).
   Every submit must be resolved by a later result event carrying the
   same ``intent_id`` (``position_opened`` / ``trade_skipped`` /
   ``position_naked`` / ``position_closed``). A submit with no result
   after ``dangling_intent_minutes`` means the process died between the
   exchange fill and the local append — the position may be live on the
   exchange with the local log blind to it. Such *dangling intents* make
   the gate reject new entries until the position monitor reconciles.
- ``{"type": "position_opened", "ts": ..., "intent_id": str, "symbol": str,
       "side": "BUY" | "SELL", "entry_price": float,
       "stop_loss": float, "take_profit": float | null}``
   (side/price fields added for T12 outcome inference; events written
   before then lack them — replay treats missing fields as unknown)
- ``{"type": "position_untracked", "ts": ..., "symbol": str,
       "binance_symbol": str, "quantity": float, "entry_price": float}``
   Written by the position monitor when the exchange reports a position
   the local log knows nothing about (no open record, no dangling intent
   to adopt). Counts toward ``open_positions`` — conservative direction —
   and is closed out by a later ``position_closed`` like any position.
- ``{"type": "position_closed", "ts": ..., "intent_id": str, "symbol": str,
       "pnl_usd": float, "outcome": "stop" | "tp" | "manual" | "unknown"}``
- ``{"type": "trade_skipped", "ts": ..., "symbol": str, "reason": str}``
   (informational; not consumed by gate policies. Carries ``intent_id``
   when it resolves an ``order_submitted`` — executor failures and the
   monitor's dangling-intent resolutions do; gate rejections happen
   before an intent exists and don't.)

Public API:

- :func:`append_event` — atomic append (one event per line).
- :func:`load_events` — parse the whole file; small enough to do every call.
- :func:`derive_state` — fold events into the policy-relevant snapshot.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DanglingIntent:
    """An ``order_submitted`` event with no result event after the timeout.

    The order may or may not have reached the exchange before the crash;
    the gate treats it as a possibly-live position (conservative) until
    the position monitor reconciles it one way or the other.
    """

    intent_id: str
    symbol: str
    submitted_ts: datetime


@dataclass(frozen=True)
class RiskGateSnapshot:
    """Snapshot of risk-gate-relevant state derived from the event log."""

    open_positions: int
    """Count of positions currently open (opened + untracked minus closed)."""

    daily_realised_pnl_usd: float
    """Sum of realised P&L from positions closed since 00:00 UTC today."""

    last_stop_loss_close_ts: Optional[datetime]
    """Timestamp of the most recent stop-out close, or None if never."""

    dangling_intents: tuple[DanglingIntent, ...] = ()
    """Submits older than the timeout with no result event — the local
    log may be blind to a live exchange position. Non-empty makes the
    gate reject new entries until the monitor reconciles."""


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
    """Return all events in chronological (file) order. Missing file → [].

    Malformed lines (torn writes from a crash / power loss) are skipped
    with a warning instead of raising — a single bad line must not brick
    every gate evaluation and monitor run until someone hand-edits the
    file. Callers that need the skip count (alerts) use
    :func:`load_events_with_errors`.
    """
    events, _ = load_events_with_errors(path)
    return events


def load_events_with_errors(path: Path | str) -> tuple[list[dict], int]:
    """Like :func:`load_events`, but also return the malformed-line count.

    The count feeds the alerts layer: skipping a torn line is the right
    availability call, but if the dropped line happened to be a
    ``position_opened`` the replayed state silently drifts from reality —
    the operator has to be told the file needs inspection.
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    events: list[dict] = []
    malformed = 0
    with p.open("r", encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
                logger.warning(
                    "Skipping malformed line %d in %s (torn write?) — "
                    "replayed state may be missing an event; inspect the file",
                    line_num, p,
                )
    return events, malformed


DEFAULT_DANGLING_INTENT_MINUTES = 5
"""Minutes after which an unresolved ``order_submitted`` counts as dangling."""


def derive_state(
    events: Iterable[dict],
    *,
    now: datetime,
    dangling_intent_minutes: float = DEFAULT_DANGLING_INTENT_MINUTES,
) -> RiskGateSnapshot:
    """Fold events into the policy-relevant snapshot at time ``now``.

    ``now`` must be timezone-aware UTC. The daily-drawdown window is
    [today 00:00 UTC, now] — events outside this window contribute to
    ``open_positions`` and ``last_stop_loss_close_ts`` but not to
    ``daily_realised_pnl_usd``.

    An ``order_submitted`` is *resolved* by any later event carrying the
    same ``intent_id``; unresolved submits older than
    ``dangling_intent_minutes`` surface as ``dangling_intents``. Submits
    younger than that are assumed in-flight (the executor writes its
    result event within the same run) and are ignored.
    """
    if now.tzinfo is None:
        raise ValueError("derive_state requires a timezone-aware 'now' (UTC)")

    today_start = now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    open_count = 0
    daily_pnl = 0.0
    last_stop_close_ts: Optional[datetime] = None
    submitted: dict[str, dict] = {}  # intent_id -> order_submitted event
    resolved_intents: set[str] = set()

    for ev in events:
        ev_type = ev.get("type")
        intent_id = ev.get("intent_id")
        if ev_type == "order_submitted":
            if intent_id:
                submitted[intent_id] = ev
            continue
        if intent_id:
            resolved_intents.add(intent_id)
        if ev_type in ("position_opened", "position_untracked"):
            open_count += 1
        elif ev_type == "position_closed":
            open_count -= 1
            ts = _parse_ts(ev["ts"])
            if ts >= today_start:
                daily_pnl += float(ev.get("pnl_usd", 0.0))
            if ev.get("outcome") == "stop":
                if last_stop_close_ts is None or ts > last_stop_close_ts:
                    last_stop_close_ts = ts
        # trade_skipped / position_naked matter only as intent resolutions

    dangling = []
    cutoff = now - timedelta(minutes=dangling_intent_minutes)
    for intent_id, ev in submitted.items():
        if intent_id in resolved_intents:
            continue
        ts = _parse_ts(ev["ts"])
        if ts <= cutoff:
            dangling.append(DanglingIntent(
                intent_id=intent_id,
                symbol=ev.get("symbol", "unknown"),
                submitted_ts=ts,
            ))
    dangling.sort(key=lambda d: d.submitted_ts)

    return RiskGateSnapshot(
        open_positions=max(0, open_count),
        daily_realised_pnl_usd=daily_pnl,
        last_stop_loss_close_ts=last_stop_close_ts,
        dangling_intents=tuple(dangling),
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
