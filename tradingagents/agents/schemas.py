"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Futures (crypto perp) — Monopoly fork
# ---------------------------------------------------------------------------


class FuturesSide(str, Enum):
    """Direction for a perpetual-futures position.

    ``Flat`` means the desk should not open a position this round.
    """

    LONG = "Long"
    SHORT = "Short"
    FLAT = "Flat"


_FUTURES_SIDE_TO_LEGACY_ACTION = {
    FuturesSide.LONG: "BUY",
    FuturesSide.SHORT: "SELL",
    FuturesSide.FLAT: "HOLD",
}


class FuturesProposal(BaseModel):
    """Structured perp-futures proposal produced by the Trader (crypto-mode).

    The Trader reads the Research Manager's plan and the analyst reports
    and commits to a directional view plus the execution params the risk
    gate will validate. Sizing is expressed as ``position_size_pct`` —
    the share of equity to *risk* on this trade — so the risk gate can
    enforce per-trade risk independently of leverage.
    """

    side: FuturesSide = Field(
        description=(
            "Position direction. Exactly one of Long / Short / Flat. "
            "Pick Flat only when conviction is genuinely absent; bias toward "
            "committing to the side the analysts and Research Manager support."
        ),
    )
    reasoning: str = Field(
        description=(
            "The case for this side, anchored in the analyst reports and the "
            "research plan. Two to four sentences."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description=(
            "Optional entry price target (quote currency). Omit for market entry."
        ),
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description=(
            "Stop-loss price (quote currency). Required when side != Flat — the "
            "risk gate uses this to compute and enforce per-trade risk."
        ),
    )
    take_profit: Optional[float] = Field(
        default=None,
        description=(
            "Optional take-profit price (quote currency). Used by the executor "
            "when present; not required by the risk gate."
        ),
    )
    leverage: Optional[float] = Field(
        default=None,
        description=(
            "Requested leverage multiplier (e.g. 2.0 = 2x). MUST be <= 3.0 — "
            "the risk gate rejects anything higher. Omit for Flat."
        ),
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description=(
            "Fraction of equity to risk on this trade, expressed as a decimal "
            "(0.01 = 1%). MUST be <= 0.01 — the risk gate rejects anything higher. "
            "Common LLM mistake: writing 0.1 thinking it is small — 0.1 = 10% = "
            "REJECTED. Use 0.001-0.01 only. Omit for Flat."
        ),
    )


def render_futures_proposal(proposal: FuturesProposal) -> str:
    """Render a FuturesProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    kept for backward compatibility with the analyst stop-signal text and
    any external code that greps for it. Long → BUY, Short → SELL, Flat
    → HOLD.
    """
    parts = [
        f"**Side**: {proposal.side.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.take_profit is not None:
        parts.extend(["", f"**Take Profit**: {proposal.take_profit}"])
    if proposal.leverage is not None:
        parts.extend(["", f"**Leverage**: {proposal.leverage}x"])
    if proposal.position_size_pct is not None:
        parts.extend(["", f"**Position Size**: {proposal.position_size_pct * 100:.2f}% of equity at risk"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{_FUTURES_SIDE_TO_LEGACY_ACTION[proposal.side]}**",
    ])
    return "\n".join(parts)


TIME_HORIZON_BUCKETS = ("intraday", "1-3 days", "1-2 weeks", "2-4 weeks", "1 month+")
"""Closed set of holding-period buckets for ``FuturesDecision.time_horizon``.

A free-text field with an example in its description carried no
information: 46/51 replayed decisions echoed the example verbatim
(2026-08-08 review). A closed enum forces an actual choice and keeps
the values comparable across runs.
"""


class FuturesDecision(BaseModel):
    """Final perp-futures decision produced by the Portfolio Manager (crypto-mode).

    The PM ratifies (or adjusts) the Trader's proposal into the decision
    the risk gate and executor will act on. ``side``, ``leverage``,
    ``stop_loss``, and ``position_size_pct`` together describe the
    intended position; the risk gate validates them against configured
    ceilings before any order is placed.
    """

    side: FuturesSide = Field(
        description="Final position direction. Exactly one of Long / Short / Flat.",
    )
    leverage: Optional[float] = Field(
        default=None,
        description=(
            "Final leverage multiplier (e.g. 2.0 = 2x). Required when side != Flat. "
            "MUST be <= 3.0 — the risk gate rejects anything higher. Typical values: "
            "1.0 conservative, 2.0 default, 3.0 high conviction at the ceiling."
        ),
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description=(
            "Final fraction of equity to risk on this trade (decimal; 0.01 = 1%). "
            "Required when side != Flat. MUST be <= 0.01 — the risk gate rejects "
            "anything higher. Examples: 0.003 (0.3%, low conviction), 0.005 (0.5%, "
            "moderate), 0.010 (1.0%, high conviction at the ceiling). "
            "Common LLM mistake: writing 0.1 thinking it is small — 0.1 = 10% = REJECTED. "
            "Always check the value is between 0.001 and 0.01."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Optional entry price target (quote currency). Omit for market entry.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description=(
            "Stop-loss price (quote currency). Required when side != Flat."
        ),
    )
    take_profit: Optional[float] = Field(
        default=None,
        description="Optional take-profit price (quote currency).",
    )
    executive_summary: str = Field(
        description=(
            "Concise action plan covering entry strategy, key risk levels, and "
            "time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in evidence from the analysts' debate."
        ),
    )
    time_horizon: Optional[
        Literal["intraday", "1-3 days", "1-2 weeks", "2-4 weeks", "1 month+"]
    ] = Field(
        default=None,
        description=(
            "Intended holding period for this position. Pick the single "
            "bucket that matches how long the investment thesis needs to "
            "play out; do not default to a middle value."
        ),
    )

    @field_validator("time_horizon", mode="before")
    @classmethod
    def _coerce_time_horizon(cls, value):
        # An out-of-bucket string must not sink the whole decision: a
        # structured-output validation failure falls back to free text,
        # which the risk gate rejects for missing structured decision.
        # Degrade the one optional, purely-informational field to None
        # instead. (2026-08-08 review: the old free-text field echoed
        # the description's example in 46/51 decisions.)
        if value is None:
            return None
        normalised = str(value).strip().lower()
        return normalised if normalised in TIME_HORIZON_BUCKETS else None


def render_futures_decision(decision: FuturesDecision) -> str:
    """Render a FuturesDecision to markdown.

    Keeps the ``**Executive Summary**`` / ``**Investment Thesis**``
    section headers so the memory log, CLI display, and saved report
    writers continue to work against the same parsers.
    """
    parts = [
        f"**Side**: {decision.side.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.leverage is not None:
        parts.extend(["", f"**Leverage**: {decision.leverage}x"])
    if decision.position_size_pct is not None:
        parts.extend(["", f"**Position Size**: {decision.position_size_pct * 100:.2f}% of equity at risk"])
    if decision.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {decision.entry_price}"])
    if decision.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {decision.stop_loss}"])
    if decision.take_profit is not None:
        parts.extend(["", f"**Take Profit**: {decision.take_profit}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)
