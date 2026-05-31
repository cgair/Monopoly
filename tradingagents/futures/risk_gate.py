"""Futures risk gate — the last check before an order is sent to the executor.

Validates a :class:`FuturesDecision` against configured ceilings and
cross-run state (daily drawdown, cooldown after stop-out, max concurrent
positions). Approved decisions become an :class:`ExecutionIntent` that
the executor consumes; rejected ones surface a structured reason that
the graph logs (and that downstream review tooling can grep).

The gate is implemented as a pure :func:`evaluate` function plus a thin
LangGraph node factory :func:`create_risk_gate_node`. Tests target
``evaluate`` directly; the node only adapts state ↔ function arguments.

Validation phase (G3=3 in the spec): cross-run state events come from
the executor (Phase 4). In dryrun mode the executor still writes
``trade_skipped`` events when the gate rejects, so the JSONL log
captures the gate's decision history even when no orders hit the wire.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from tradingagents.agents.schemas import FuturesDecision, FuturesSide
from tradingagents.futures.risk_state import (
    RiskGateSnapshot,
    append_event,
    default_state_path,
    derive_state,
    load_events,
    utcnow_iso,
)


# ---------------------------------------------------------------------------
# Config + result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskGateConfig:
    """Configured ceilings the gate enforces.

    Defaults are loaded from ``DEFAULT_CONFIG`` in
    ``tradingagents/default_config.py`` (keys prefixed ``futures_``) and
    can be overridden via env vars. See :func:`from_config`.
    """

    max_leverage: float = 3.0
    """Hard ceiling on leverage. Decisions exceeding this are rejected."""

    per_trade_risk_pct: float = 0.01
    """Maximum fraction of equity a single trade may risk (decimal)."""

    daily_drawdown_halt_pct: float = 0.03
    """Absolute drawdown threshold; once cumulative realised P&L since
    00:00 UTC falls below ``-daily_drawdown_halt_pct * equity``, the
    gate blocks new entries for the rest of the UTC day."""

    cooldown_after_loss_minutes: int = 60
    """Minutes of cooldown after a stop-out close before new entries are
    accepted."""

    max_concurrent_positions: int = 2
    """Maximum number of simultaneously open positions across all
    symbols. Matches the 2-symbol scope (BTC + ETH) in the spec."""

    state_path: Optional[Path] = None
    """Path to the JSONL event log. ``None`` → :func:`default_state_path`."""

    def resolved_state_path(self) -> Path:
        return self.state_path if self.state_path is not None else default_state_path()


def from_config(config: dict) -> RiskGateConfig:
    """Build a :class:`RiskGateConfig` from a Monopoly config dict.

    Falls back to dataclass defaults for any missing key, so callers can
    pass a partial config (e.g. tests) without setting every futures_*
    field.
    """
    state_path = config.get("futures_risk_state_path")
    return RiskGateConfig(
        max_leverage=float(config.get("futures_max_leverage", 3.0)),
        per_trade_risk_pct=float(config.get("futures_per_trade_risk_pct", 0.01)),
        daily_drawdown_halt_pct=float(config.get("futures_daily_drawdown_halt_pct", 0.03)),
        cooldown_after_loss_minutes=int(config.get("futures_cooldown_after_loss_minutes", 60)),
        max_concurrent_positions=int(config.get("futures_max_concurrent_positions", 2)),
        state_path=Path(state_path) if state_path else None,
    )


@dataclass(frozen=True)
class ExecutionIntent:
    """A validated order intent the executor consumes.

    Sizing is expressed in equity-fraction terms (``risk_pct``); the
    executor converts to notional + quantity using live mark price.
    Decoupling sizing from price here means the gate is testable
    without a price feed.
    """

    intent_id: str
    symbol: str
    side: FuturesSide
    leverage: float
    risk_pct: float
    entry_price: Optional[float]
    stop_loss: float
    take_profit: Optional[float]
    created_at: str


@dataclass(frozen=True)
class GateResult:
    """Outcome of :func:`evaluate`. Exactly one of ``intent`` / ``reason``
    is populated."""

    approved: bool
    intent: Optional[ExecutionIntent] = None
    reason: Optional[str] = None
    snapshot: Optional[RiskGateSnapshot] = None


# ---------------------------------------------------------------------------
# Rejection reasons (stable strings — log scrapers and tests grep these)
# ---------------------------------------------------------------------------

REASON_FLAT = "side=Flat — no position requested"
REASON_MISSING_LEVERAGE = "leverage missing on non-Flat decision"
REASON_MISSING_RISK_PCT = "position_size_pct missing on non-Flat decision"
REASON_MISSING_STOP = "stop_loss missing on non-Flat decision"
REASON_LEVERAGE_OVER_CAP = "leverage exceeds configured max_leverage"
REASON_RISK_OVER_CAP = "position_size_pct exceeds configured per_trade_risk_pct"
REASON_STOP_WRONG_SIDE = "stop_loss is on the wrong side of entry"
REASON_DAILY_DRAWDOWN_HALT = "daily drawdown halt active until next UTC day"
REASON_COOLDOWN_ACTIVE = "cooldown window active after recent stop-out"
REASON_MAX_POSITIONS = "max_concurrent_positions already open"


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------


def evaluate(
    decision: FuturesDecision,
    *,
    symbol: str,
    equity_usd: float,
    config: RiskGateConfig,
    now: Optional[datetime] = None,
    snapshot: Optional[RiskGateSnapshot] = None,
) -> GateResult:
    """Validate ``decision`` against ``config`` and optional cross-run state.

    Parameters
    ----------
    decision
        The PM's :class:`FuturesDecision`.
    symbol
        Trading symbol (e.g. ``"BTC-USD"``). Stored on the intent;
        the gate does not look it up.
    equity_usd
        Current account equity in USD. Used to compute the daily
        drawdown halt threshold.
    config
        Resolved :class:`RiskGateConfig`.
    now
        Current UTC time. Defaults to ``datetime.now(timezone.utc)``;
        tests should pass an explicit value for determinism.
    snapshot
        Cross-run state. If ``None``, the gate loads + derives from
        ``config.resolved_state_path()``. Tests can pass a synthesised
        snapshot to skip file I/O.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("evaluate requires a timezone-aware 'now' (UTC)")

    if snapshot is None:
        snapshot = derive_state(load_events(config.resolved_state_path()), now=now)

    # 1) Flat is a no-op; not a rejection in the user-facing sense, but the
    # gate emits no intent. Caller distinguishes via approved=False + reason.
    if decision.side == FuturesSide.FLAT:
        return GateResult(approved=False, reason=REASON_FLAT, snapshot=snapshot)

    # 2) Required-field checks. The PM prompt asks for these, but a
    # free-text fallback path could omit them — the gate is the safety net.
    if decision.leverage is None:
        return GateResult(approved=False, reason=REASON_MISSING_LEVERAGE, snapshot=snapshot)
    if decision.position_size_pct is None:
        return GateResult(approved=False, reason=REASON_MISSING_RISK_PCT, snapshot=snapshot)
    if decision.stop_loss is None:
        return GateResult(approved=False, reason=REASON_MISSING_STOP, snapshot=snapshot)

    # 3) Ceiling checks.
    if decision.leverage > config.max_leverage:
        return GateResult(
            approved=False,
            reason=f"{REASON_LEVERAGE_OVER_CAP} ({decision.leverage}x > {config.max_leverage}x)",
            snapshot=snapshot,
        )
    if decision.position_size_pct > config.per_trade_risk_pct:
        return GateResult(
            approved=False,
            reason=(f"{REASON_RISK_OVER_CAP} "
                    f"({decision.position_size_pct:.4f} > {config.per_trade_risk_pct:.4f})"),
            snapshot=snapshot,
        )

    # 4) Stop on the correct side of entry. Entry is optional (market);
    # only enforce when both stop and entry are set.
    if decision.entry_price is not None:
        if decision.side == FuturesSide.LONG and decision.stop_loss >= decision.entry_price:
            return GateResult(approved=False, reason=REASON_STOP_WRONG_SIDE, snapshot=snapshot)
        if decision.side == FuturesSide.SHORT and decision.stop_loss <= decision.entry_price:
            return GateResult(approved=False, reason=REASON_STOP_WRONG_SIDE, snapshot=snapshot)

    # 5) Cross-run policies: daily drawdown halt.
    drawdown_threshold_usd = -config.daily_drawdown_halt_pct * equity_usd
    if snapshot.daily_realised_pnl_usd <= drawdown_threshold_usd:
        return GateResult(
            approved=False,
            reason=(f"{REASON_DAILY_DRAWDOWN_HALT} "
                    f"(realised pnl ${snapshot.daily_realised_pnl_usd:.2f} <= "
                    f"threshold ${drawdown_threshold_usd:.2f})"),
            snapshot=snapshot,
        )

    # 6) Cross-run policies: cooldown after stop-out.
    if snapshot.last_stop_loss_close_ts is not None:
        cooldown_end = snapshot.last_stop_loss_close_ts + timedelta(
            minutes=config.cooldown_after_loss_minutes
        )
        if now < cooldown_end:
            return GateResult(
                approved=False,
                reason=(f"{REASON_COOLDOWN_ACTIVE} "
                        f"(active until {cooldown_end.isoformat()})"),
                snapshot=snapshot,
            )

    # 7) Cross-run policies: max concurrent positions.
    if snapshot.open_positions >= config.max_concurrent_positions:
        return GateResult(
            approved=False,
            reason=(f"{REASON_MAX_POSITIONS} "
                    f"({snapshot.open_positions} / {config.max_concurrent_positions})"),
            snapshot=snapshot,
        )

    intent = ExecutionIntent(
        intent_id=str(uuid.uuid4()),
        symbol=symbol,
        side=decision.side,
        leverage=decision.leverage,
        risk_pct=decision.position_size_pct,
        entry_price=decision.entry_price,
        stop_loss=decision.stop_loss,
        take_profit=decision.take_profit,
        created_at=now.replace(microsecond=0).isoformat(),
    )
    return GateResult(approved=True, intent=intent, snapshot=snapshot)


# ---------------------------------------------------------------------------
# LangGraph node factory (consumed by graph wiring in Phase 5)
# ---------------------------------------------------------------------------


def create_risk_gate_node(config: dict):
    """Build a LangGraph node that runs :func:`evaluate` against ``state``.

    Reads from state:
    - ``company_of_interest`` → symbol
    - ``final_trade_decision`` (markdown) — informational only; the
      structured :class:`FuturesDecision` is read from
      ``final_decision_structured`` when present, otherwise the node
      logs a ``trade_skipped`` event with reason "no structured
      decision" and emits no intent.
    - ``equity_usd`` (float) — current account equity; defaults to
      ``config["futures_starting_equity_usd"]`` (default 1000) if absent.

    Writes to state:
    - ``execution_intent``: :class:`ExecutionIntent` dict (or None if
      rejected).
    - ``risk_gate_rejection_reason``: str | None.
    """
    gate_config = from_config(config)
    starting_equity = float(config.get("futures_starting_equity_usd", 1000.0))

    def risk_gate_node(state) -> dict:
        if state.get("asset_type", "stock") != "crypto":
            # Stock-mode runs do not pass through the futures gate.
            return {"execution_intent": None, "risk_gate_rejection_reason": None}

        structured: Optional[FuturesDecision] = state.get("final_decision_structured")
        if structured is None:
            reason = "no structured FuturesDecision in state"
            _log_skip(gate_config, state["company_of_interest"], reason)
            return {"execution_intent": None, "risk_gate_rejection_reason": reason}

        equity_usd = float(state.get("equity_usd", starting_equity))
        result = evaluate(
            structured,
            symbol=state["company_of_interest"],
            equity_usd=equity_usd,
            config=gate_config,
        )

        if not result.approved:
            _log_skip(gate_config, state["company_of_interest"], result.reason or "unknown")
            return {"execution_intent": None, "risk_gate_rejection_reason": result.reason}

        return {
            "execution_intent": asdict(result.intent),
            "risk_gate_rejection_reason": None,
        }

    return risk_gate_node


def _log_skip(config: RiskGateConfig, symbol: str, reason: str) -> None:
    append_event(
        config.resolved_state_path(),
        {"type": "trade_skipped", "ts": utcnow_iso(), "symbol": symbol, "reason": reason},
    )
