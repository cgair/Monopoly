"""Tests for structured-output agents (Trader and Research Manager).

The Portfolio Manager has its own coverage in tests/test_memory_log.py
(which exercises the full memory-log → PM injection cycle), and the
futures schema render helpers are covered in tests/test_futures_schemas.py.
This file covers the Research Manager schema/render pair and the Trader's
structured happy path + graceful fallback so all three decision-making
agents share the same shape.
"""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import (
    FuturesDecision,
    FuturesProposal,
    FuturesSide,
    PortfolioRating,
    ResearchPlan,
    render_research_plan,
)
from tradingagents.agents.trader.trader import create_trader


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderResearchPlan:
    def test_required_fields(self):
        p = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            rationale="Bull case carried; tailwinds intact.",
            strategic_actions="Build position over two weeks; cap at 5%.",
        )
        md = render_research_plan(p)
        assert "**Recommendation**: Overweight" in md
        assert "**Rationale**: Bull case carried" in md
        assert "**Strategic Actions**: Build position" in md

    def test_all_5_tier_ratings_render(self):
        for rating in PortfolioRating:
            p = ResearchPlan(
                recommendation=rating,
                rationale="r",
                strategic_actions="s",
            )
            md = render_research_plan(p)
            assert f"**Recommendation**: {rating.value}" in md


# ---------------------------------------------------------------------------
# Trader agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_trader_state():
    return {
        "company_of_interest": "BTC-USD",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ETF inflows + funding reset.\n"
                           "**Strategic Actions**: Enter long, scale over two sessions.",
    }


def _structured_trader_llm(captured: dict, proposal: FuturesProposal | None = None):
    """Build a MagicMock LLM whose with_structured_output binding captures the
    prompt and returns a real FuturesProposal so render_futures_proposal works.
    """
    if proposal is None:
        proposal = FuturesProposal(
            side=FuturesSide.LONG,
            reasoning="Strong setup.",
            stop_loss=62800.0,
            leverage=2.0,
            position_size_pct=0.005,
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or proposal
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestTraderAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        proposal = FuturesProposal(
            side=FuturesSide.LONG,
            reasoning="ETF inflows + funding reset; analysts aligned long.",
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            leverage=2.0,
            position_size_pct=0.01,
        )
        llm = _structured_trader_llm(captured, proposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert "**Side**: Long" in plan
        assert "**Entry Price**: 64500.0" in plan
        assert "**Leverage**: 2.0x" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan
        # The same rendered markdown is also added to messages for downstream agents.
        assert plan in result["messages"][0].content

    def test_prompt_includes_investment_plan(self):
        captured = {}
        llm = _structured_trader_llm(captured)
        trader = create_trader(llm)
        trader(_make_trader_state())
        # The investment plan is in the user message of the captured prompt.
        prompt = captured["prompt"]
        assert any("Research Manager's plan" in m["content"] for m in prompt)
        assert any("ETF inflows + funding reset." in m["content"] for m in prompt)

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = (
            "**Side**: Short\n\nFunding flip hits crowded longs.\n\n"
            "FINAL TRANSACTION PROPOSAL: **SELL**"
        )
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        assert result["trader_investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# Research Manager agent: structured happy path + fallback
# ---------------------------------------------------------------------------


def _make_rm_state():
    return {
        "company_of_interest": "BTC-USD",
        "investment_debate_state": {
            "history": "Bull and bear arguments here.",
            "bull_history": "Bull says...",
            "bear_history": "Bear says...",
            "current_response": "",
            "judge_decision": "",
            "count": 1,
        },
    }


def _structured_rm_llm(captured: dict, plan: ResearchPlan | None = None):
    if plan is None:
        plan = ResearchPlan(
            recommendation=PortfolioRating.HOLD,
            rationale="Balanced view across both sides.",
            strategic_actions="Stay flat; reassess after the funding print.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or plan
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestResearchManagerAgent:
    def test_structured_path_produces_rendered_markdown(self):
        captured = {}
        plan = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            rationale="Bull case is stronger; ETF tailwind intact.",
            strategic_actions="Build position gradually over two sessions.",
        )
        llm = _structured_rm_llm(captured, plan)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        ip = result["investment_plan"]
        assert "**Recommendation**: Overweight" in ip
        assert "**Rationale**: Bull case" in ip
        assert "**Strategic Actions**: Build position" in ip

    def test_prompt_uses_5_tier_rating_scale(self):
        """The RM prompt must list all five tiers so the schema enum matches user expectations."""
        captured = {}
        llm = _structured_rm_llm(captured)
        rm = create_research_manager(llm)
        rm(_make_rm_state())
        prompt = captured["prompt"]
        for tier in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            assert f"**{tier}**" in prompt, f"missing {tier} in prompt"

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = "**Recommendation**: Sell\n\n**Rationale**: ...\n\n**Strategic Actions**: ..."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        assert result["investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# Portfolio Manager: structured decision surfaced for the risk gate
# ---------------------------------------------------------------------------


def _make_pm_state():
    return {
        "company_of_interest": "BTC-USD",
        "past_context": "",
        "risk_debate_state": {
            "history": "Aggressive: lean in. Conservative: stop tight. Neutral: trim leverage.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "judge_decision": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 1,
        },
        "investment_plan": "Research plan: Buy.",
        "trader_investment_plan": "Trader: Long 2x.",
    }


@pytest.mark.unit
class TestPortfolioManagerStructuredDecision:
    def test_emits_futures_decision_and_surfaces_structured_object(self):
        decision = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.01,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="Enter long with 2x; stop tight below LTF demand.",
            investment_thesis="Bull case carried debate; conservative concern addressed by reduced size.",
            time_horizon="2-5 days",
        )
        structured = MagicMock()
        structured.invoke.return_value = decision
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        pm = create_portfolio_manager(llm)
        result = pm(_make_pm_state())
        final = result["final_trade_decision"]
        assert "**Side**: Long" in final
        assert "**Leverage**: 2.0x" in final
        assert "**Stop Loss**: 62800.0" in final
        # The structured object is also surfaced for the risk-gate node
        assert result["final_decision_structured"] is decision
