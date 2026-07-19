"""Human approval gate for trade execution.

This module implements the final safety fence before orders hit the wire:
approval timestamps, staleness validation, and the decision → ExecutionIntent
reconstruction flow that the CLI uses before calling the executor.

Design principle: the CLI parses args and decision JSON, this module enforces
the approval semantics (who, when, staleness bounds, gate re-evaluation).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from tradingagents.agents.schemas import FuturesSide, FuturesDecision
from tradingagents.futures.risk_gate import ExecutionIntent, evaluate, from_config
from tradingagents.futures.risk_state import (
    RiskGateSnapshot,
    append_event,
    derive_state,
    load_events,
    utcnow_iso,
)


@dataclass(frozen=True)
class ApprovalMetadata:
    """Captures who approved and when."""

    approved: bool
    """True iff user approved (vs rejected or timeout)."""

    approved_by: str
    """Discord username or other identifier of the approver."""

    approved_at: str
    """ISO-8601 UTC timestamp of approval decision."""


def check_staleness(
    decision_timestamp: str,
    *,
    now: Optional[datetime] = None,
    approval_timeout_minutes: int = 15,
) -> tuple[bool, Optional[str]]:
    """Check whether a decision is too old to approve.

    Parameters
    ----------
    decision_timestamp
        ISO-8601 timestamp from the analyze_json output (when decision was made).
    now
        Current UTC time. Defaults to datetime.now(timezone.utc).
    approval_timeout_minutes
        Maximum age in minutes. Decisions older than this are stale.

    Returns
    -------
    (is_stale, reason)
        If not stale: (False, None).
        If stale: (True, reason string for trade_skipped event).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("check_staleness requires timezone-aware 'now' (UTC)")

    # Parse decision timestamp
    if decision_timestamp.endswith("Z"):
        decision_timestamp = decision_timestamp[:-1] + "+00:00"
    decision_dt = datetime.fromisoformat(decision_timestamp)

    age = now - decision_dt
    timeout_delta = timedelta(minutes=approval_timeout_minutes)

    if age > timeout_delta:
        reason = (
            f"approval timeout / stale decision "
            f"(age {age.total_seconds():.0f}s > timeout {approval_timeout_minutes}min)"
        )
        return True, reason

    return False, None


def parse_decision_json(decision_json: dict) -> tuple[FuturesDecision, datetime]:
    """Extract and reconstruct a FuturesDecision from analyze_json output.

    Parameters
    ----------
    decision_json
        The ``decision`` key from analyze_json output (or full output if only
        decision is present).

    Returns
    -------
    (decision, timestamp)
        The reconstructed FuturesDecision and its timestamp.

    Raises
    ------
    KeyError, ValueError
        If required fields are missing or malformed.
    """
    # Handle both full analyze_json output and just the decision dict.
    # analyze_json stamps the timestamp at the TOP level (build_analysis_result),
    # so capture it before narrowing to the nested decision.
    outer_ts = decision_json.get("timestamp") or decision_json.get("created_at")
    if "decision" in decision_json:
        decision_json = decision_json["decision"]

    # Map string side to FuturesSide enum
    side_str = decision_json.get("side")
    if isinstance(side_str, str):
        # FuturesSide values are "Long", "Short", "Flat" (title case)
        # Normalize from various inputs (LONG, long, Long, etc.)
        side = FuturesSide(side_str.capitalize())
    else:
        side = side_str

    decision = FuturesDecision(
        side=side,
        leverage=float(decision_json["leverage"]),
        position_size_pct=float(decision_json["position_size_pct"]),
        entry_price=(
            float(decision_json["entry_price"])
            if decision_json.get("entry_price") is not None
            else None
        ),
        stop_loss=float(decision_json["stop_loss"]),
        take_profit=(
            float(decision_json["take_profit"])
            if decision_json.get("take_profit") is not None
            else None
        ),
        executive_summary=decision_json.get("executive_summary", ""),
        investment_thesis=decision_json.get("investment_thesis", ""),
        time_horizon=decision_json.get("time_horizon", ""),
    )

    # Parse timestamp from the decision or analysis level. Fail closed: an
    # undated decision cannot be staleness-checked, and defaulting to "now"
    # would make it permanently fresh — a bypass of the approval timeout.
    ts_str = decision_json.get("timestamp") or decision_json.get("created_at") or outer_ts
    if not ts_str:
        raise ValueError(
            "decision JSON has no timestamp/created_at — cannot verify "
            "staleness, refusing to treat an undated decision as fresh"
        )
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    ts = datetime.fromisoformat(ts_str)

    return decision, ts


def reconstruct_intent_from_decision(
    decision: FuturesDecision,
    *,
    symbol: str,
    equity_usd: float,
    config: dict,
    now: Optional[datetime] = None,
    state_path: Optional[Path] = None,
) -> tuple[Optional[ExecutionIntent], Optional[str]]:
    """Re-evaluate a decision through the risk gate and return the intent or reason.

    This is called by trade_execute to verify that the decision still passes the
    gate (in case conditions changed since analyze ran). The gate is the source
    of truth for what is executable.

    Parameters
    ----------
    decision
        The FuturesDecision from analyze_json.
    symbol
        Trading symbol (e.g. "BTC-USD").
    equity_usd
        Current account equity.
    config
        Monopoly config dict (used to build RiskGateConfig).
    now
        Current UTC time. Defaults to datetime.now(timezone.utc).
    state_path
        Path to risk_gate_state.jsonl. Defaults to standard location.

    Returns
    -------
    (intent, reason)
        If approved: (ExecutionIntent, None).
        If rejected: (None, reason string for trade_skipped event).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    gate_config = from_config(config)
    if state_path is not None:
        gate_config = gate_config.__class__(
            max_leverage=gate_config.max_leverage,
            per_trade_risk_pct=gate_config.per_trade_risk_pct,
            daily_drawdown_halt_pct=gate_config.daily_drawdown_halt_pct,
            cooldown_after_loss_minutes=gate_config.cooldown_after_loss_minutes,
            max_concurrent_positions=gate_config.max_concurrent_positions,
            state_path=state_path,
        )

    # Load current risk state
    risk_events = load_events(gate_config.resolved_state_path())
    snapshot = derive_state(risk_events, now=now)

    # Evaluate through the gate
    result = evaluate(
        decision,
        symbol=symbol,
        equity_usd=equity_usd,
        config=gate_config,
        now=now,
        snapshot=snapshot,
    )

    if result.approved and result.intent is not None:
        return result.intent, None
    else:
        return None, result.reason or "gate rejected decision (reason unknown)"


def write_trade_skipped_event(
    reason: str,
    *,
    symbol: str,
    approval_metadata: Optional[ApprovalMetadata] = None,
    state_path: Optional[Path] = None,
) -> None:
    """Write a trade_skipped event to the risk state log.

    Used when a decision is rejected (stale, unapproved, gate re-eval fails,
    or human rejects).

    Parameters
    ----------
    reason
        Why the trade was skipped.
    symbol
        Trading symbol.
    approval_metadata
        Optional metadata about who/when it was approved/rejected.
    state_path
        Path to risk_gate_state.jsonl. Defaults to standard location.
    """
    from tradingagents.futures.risk_state import default_state_path

    if state_path is None:
        state_path = default_state_path()

    event = {
        "type": "trade_skipped",
        "ts": utcnow_iso(),
        "symbol": symbol,
        "reason": reason,
    }

    if approval_metadata is not None:
        event["approval_by"] = approval_metadata.approved_by
        event["approval_at"] = approval_metadata.approved_at
        event["approval_decision"] = "approved" if approval_metadata.approved else "rejected"

    append_event(state_path, event)
