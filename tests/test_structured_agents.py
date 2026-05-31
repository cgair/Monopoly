"""Tests for structured-output agents (Trader and Research Manager).

The Portfolio Manager has its own coverage in tests/test_memory_log.py
(which exercises the full memory-log → PM injection cycle).  This file
covers the parallel schemas, render functions, and graceful-fallback
behavior we added for the Trader and Research Manager so all three
decision-making agents share the same shape.
"""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import (
    FuturesDecision,
    FuturesProposal,
    FuturesSide,
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    TraderAction,
    TraderProposal,
    render_research_plan,
    render_trader_proposal,
)
from tradingagents.agents.trader.trader import create_trader


# ---------------------------------------------------------------------------
# Render functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderTraderProposal:
    def test_minimal_required_fields(self):
        p = TraderProposal(action=TraderAction.HOLD, reasoning="Balanced setup; no edge.")
        md = render_trader_proposal(p)
        assert "**Action**: Hold" in md
        assert "**Reasoning**: Balanced setup; no edge." in md
        # The trailing FINAL TRANSACTION PROPOSAL line is preserved for the
        # analyst stop-signal text and any external code that greps for it.
        assert "FINAL TRANSACTION PROPOSAL: **HOLD**" in md

    def test_optional_fields_included_when_present(self):
        p = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong technicals + fundamentals.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        md = render_trader_proposal(p)
        assert "**Action**: Buy" in md
        assert "**Entry Price**: 189.5" in md
        assert "**Stop Loss**: 178.0" in md
        assert "**Position Sizing**: 6% of portfolio" in md
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in md

    def test_optional_fields_omitted_when_absent(self):
        p = TraderProposal(action=TraderAction.SELL, reasoning="Guidance cut.")
        md = render_trader_proposal(p)
        assert "Entry Price" not in md
        assert "Stop Loss" not in md
        assert "Position Sizing" not in md
        assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md


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
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ...\n**Strategic Actions**: ...",
    }


def _structured_trader_llm(captured: dict, proposal: TraderProposal | None = None):
    """Build a MagicMock LLM whose with_structured_output binding captures the
    prompt and returns a real TraderProposal so render_trader_proposal works.
    """
    if proposal is None:
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong setup.",
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
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="AI capex cycle intact; institutional flows constructive.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        llm = _structured_trader_llm(captured, proposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert "**Action**: Buy" in plan
        assert "**Entry Price**: 189.5" in plan
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
        assert any("Proposed Investment Plan" in m["content"] for m in prompt)

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        plain_response = (
            "**Action**: Sell\n\nGuidance cut hits margins.\n\n"
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
        "company_of_interest": "NVDA",
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
            strategic_actions="Hold current position; reassess after earnings.",
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
            rationale="Bull case is stronger; AI tailwind intact.",
            strategic_actions="Build position gradually over two weeks.",
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
# Trader agent: crypto-mode routing
# ---------------------------------------------------------------------------


def _make_crypto_trader_state():
    return {
        "company_of_interest": "BTC-USD",
        "asset_type": "crypto",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ETF inflows + funding reset.\n"
                           "**Strategic Actions**: Enter long, scale over two sessions.",
    }


def _make_pm_state_crypto():
    return {
        "company_of_interest": "BTC-USD",
        "asset_type": "crypto",
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


def _structured_trader_llm_routing(proposal, expects_schema):
    """Mock that asserts which schema bind_structured was called with on the matching invoke."""
    captured = {"schemas": []}

    def _with_structured(schema, **kwargs):
        captured["schemas"].append(schema)
        structured = MagicMock()
        # All bindings can respond, but only the one we expect will be invoked
        if schema is expects_schema:
            structured.invoke.return_value = proposal
        else:
            structured.invoke.side_effect = AssertionError(
                f"wrong schema invoked: {schema.__name__}"
            )
        return structured

    llm = MagicMock()
    llm.with_structured_output.side_effect = _with_structured
    return llm, captured


@pytest.mark.unit
class TestTraderCryptoRouting:
    def test_crypto_asset_type_emits_futures_proposal(self):
        proposal = FuturesProposal(
            side=FuturesSide.LONG,
            reasoning="ETF inflows + funding reset; analysts aligned long.",
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            leverage=2.0,
            position_size_pct=0.01,
        )
        llm, captured = _structured_trader_llm_routing(proposal, FuturesProposal)
        trader = create_trader(llm)
        result = trader(_make_crypto_trader_state())
        # Both schemas bound at factory time (one per asset_type path)
        assert FuturesProposal in captured["schemas"]
        assert TraderProposal in captured["schemas"]
        plan = result["trader_investment_plan"]
        assert "**Side**: Long" in plan
        assert "**Leverage**: 2.0x" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan

    def test_stock_asset_type_still_emits_trader_proposal(self):
        # Default state (no asset_type set) routes to stock path
        proposal = TraderProposal(action=TraderAction.BUY, reasoning="Stock fundamentals.")
        llm, _ = _structured_trader_llm_routing(proposal, TraderProposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert "**Action**: Buy" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan


# ---------------------------------------------------------------------------
# Portfolio Manager: crypto-mode routing
# ---------------------------------------------------------------------------


def _structured_pm_llm_routing(decision, expects_schema):
    captured = {"schemas": []}

    def _with_structured(schema, **kwargs):
        captured["schemas"].append(schema)
        structured = MagicMock()
        if schema is expects_schema:
            structured.invoke.return_value = decision
        else:
            structured.invoke.side_effect = AssertionError(
                f"wrong schema invoked: {schema.__name__}"
            )
        return structured

    llm = MagicMock()
    llm.with_structured_output.side_effect = _with_structured
    return llm, captured


@pytest.mark.unit
class TestPortfolioManagerCryptoRouting:
    def test_crypto_asset_type_emits_futures_decision(self):
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
        llm, captured = _structured_pm_llm_routing(decision, FuturesDecision)
        pm = create_portfolio_manager(llm)
        result = pm(_make_pm_state_crypto())
        assert FuturesDecision in captured["schemas"]
        assert PortfolioDecision in captured["schemas"]
        final = result["final_trade_decision"]
        assert "**Side**: Long" in final
        assert "**Leverage**: 2.0x" in final
        assert "**Stop Loss**: 62800.0" in final
        # The structured object is also surfaced for the risk-gate node
        assert result["final_decision_structured"] is decision

    def test_stock_path_does_not_emit_structured_decision(self):
        # Stock path uses the existing helper which only returns the rendered
        # markdown; structured object stays None in state.
        decision = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        )
        llm, _ = _structured_pm_llm_routing(decision, PortfolioDecision)
        pm = create_portfolio_manager(llm)
        state = _make_pm_state_crypto()
        state["asset_type"] = "stock"
        result = pm(state)
        assert result["final_decision_structured"] is None

    def test_stock_asset_type_still_emits_portfolio_decision(self):
        decision = PortfolioDecision(
            rating=PortfolioRating.OVERWEIGHT,
            executive_summary="Build gradually over two weeks.",
            investment_thesis="Bull case carried.",
        )
        llm, _ = _structured_pm_llm_routing(decision, PortfolioDecision)
        pm = create_portfolio_manager(llm)
        state = _make_pm_state_crypto()
        state["asset_type"] = "stock"
        state["company_of_interest"] = "NVDA"
        result = pm(state)
        final = result["final_trade_decision"]
        assert "**Rating**: Overweight" in final
