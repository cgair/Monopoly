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
from typing import Optional

from pydantic import BaseModel, Field


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


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
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
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    position_sizing: Optional[str] = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: Optional[float] = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Futures (crypto perp) — Monopoly fork
# ---------------------------------------------------------------------------
#
# These shapes are used when ``asset_type == "crypto"``. Stock-mode keeps the
# TraderProposal / PortfolioDecision pair above. Routing happens at the Trader
# and Portfolio Manager nodes — schema definitions stay decoupled.


class FuturesSide(str, Enum):
    """Direction for a perpetual-futures position.

    ``Flat`` means the desk should not open a position this round. It is the
    futures-mode equivalent of ``Hold`` in the stock-mode ``TraderAction``.
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
            "Requested leverage multiplier (e.g. 2.0 = 2x). The risk gate "
            "caps this at the configured maximum. Omit for Flat."
        ),
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description=(
            "Fraction of equity to risk on this trade, expressed as a decimal "
            "(0.01 = 1%). The risk gate enforces the per-trade-risk ceiling. "
            "Omit for Flat."
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
            "Final leverage multiplier. Required when side != Flat; capped by "
            "the risk gate's configured maximum."
        ),
    )
    position_size_pct: Optional[float] = Field(
        default=None,
        description=(
            "Final fraction of equity to risk on this trade (decimal; 0.01 = 1%). "
            "Required when side != Flat."
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
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional intended holding period, e.g. '2-5 days' or 'intraday'.",
    )


def render_futures_decision(decision: FuturesDecision) -> str:
    """Render a FuturesDecision to markdown.

    Mirrors the section-header shape of :func:`render_pm_decision` so the
    memory log, CLI display, and saved report writers continue to work
    against the same parsers.
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
