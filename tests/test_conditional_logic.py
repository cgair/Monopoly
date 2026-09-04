"""Tests for graph routing: debate alternation, risk rotation, termination.

2026-09-04 review (N3): swapping the Bull/Bear routing returns and making
the Aggressive speaker route back to itself left the whole suite green —
the rotation logic had zero coverage.  The speaker prefixes are hardcoded
in the researcher/debator nodes while the ``startswith()`` checks live in
``ConditionalLogic``; the contract tests here invoke the real nodes and
feed their output back into the router so a prefix rename on either side
breaks a test instead of silently degenerating the debate into one voice.
"""

from unittest.mock import MagicMock

import pytest

from tradingagents.graph.conditional_logic import ConditionalLogic


def _debate_state(count=0, current_response=""):
    return {"investment_debate_state": {"count": count, "current_response": current_response}}


def _risk_state(count=0, latest_speaker=""):
    return {"risk_debate_state": {"count": count, "latest_speaker": latest_speaker}}


@pytest.mark.unit
class TestDebateRouting:
    def test_opens_with_bull(self):
        logic = ConditionalLogic(max_debate_rounds=1)
        assert logic.should_continue_debate(_debate_state()) == "Bull Researcher"

    def test_bull_hands_over_to_bear(self):
        logic = ConditionalLogic(max_debate_rounds=2)
        state = _debate_state(count=1, current_response="Bull Analyst: upside intact.")
        assert logic.should_continue_debate(state) == "Bear Researcher"

    def test_bear_hands_over_to_bull(self):
        logic = ConditionalLogic(max_debate_rounds=2)
        state = _debate_state(count=2, current_response="Bear Analyst: funding overheated.")
        assert logic.should_continue_debate(state) == "Bull Researcher"

    def test_terminates_at_two_turns_per_round(self):
        logic = ConditionalLogic(max_debate_rounds=1)
        state = _debate_state(count=2, current_response="Bear Analyst: done.")
        assert logic.should_continue_debate(state) == "Research Manager"


@pytest.mark.unit
class TestRiskRouting:
    def test_opens_with_aggressive(self):
        logic = ConditionalLogic(max_risk_discuss_rounds=1)
        assert logic.should_continue_risk_analysis(_risk_state()) == "Aggressive Analyst"

    def test_rotation_is_aggressive_conservative_neutral(self):
        logic = ConditionalLogic(max_risk_discuss_rounds=2)
        assert (
            logic.should_continue_risk_analysis(_risk_state(1, "Aggressive"))
            == "Conservative Analyst"
        )
        assert (
            logic.should_continue_risk_analysis(_risk_state(2, "Conservative"))
            == "Neutral Analyst"
        )
        assert (
            logic.should_continue_risk_analysis(_risk_state(3, "Neutral"))
            == "Aggressive Analyst"
        )

    def test_terminates_at_three_turns_per_round(self):
        logic = ConditionalLogic(max_risk_discuss_rounds=1)
        state = _risk_state(count=3, latest_speaker="Neutral")
        assert logic.should_continue_risk_analysis(state) == "Portfolio Manager"


# ---------------------------------------------------------------------------
# Cross-file prefix contract: node output → router input
# ---------------------------------------------------------------------------


def _mock_llm(text="argument text"):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=text)
    return llm


def _researcher_state():
    return {
        "investment_debate_state": {
            "history": "", "bull_history": "", "bear_history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
        "market_report": "", "sentiment_report": "", "news_report": "",
    }


def _debator_state():
    return {
        "risk_debate_state": {
            "history": "", "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "latest_speaker": "",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "judge_decision": "", "count": 0,
        },
        "market_report": "", "sentiment_report": "", "news_report": "",
        "trader_investment_plan": "",
    }


@pytest.mark.unit
class TestSpeakerPrefixContract:
    def test_bull_output_routes_to_bear(self):
        from tradingagents.agents.researchers.bull_researcher import create_bull_researcher

        result = create_bull_researcher(_mock_llm())(_researcher_state())
        logic = ConditionalLogic(max_debate_rounds=2)
        assert logic.should_continue_debate(result) == "Bear Researcher"

    def test_bear_output_routes_to_bull(self):
        from tradingagents.agents.researchers.bear_researcher import create_bear_researcher

        result = create_bear_researcher(_mock_llm())(_researcher_state())
        logic = ConditionalLogic(max_debate_rounds=2)
        assert logic.should_continue_debate(result) == "Bull Researcher"

    def test_aggressive_output_routes_to_conservative(self):
        from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator

        result = create_aggressive_debator(_mock_llm())(_debator_state())
        logic = ConditionalLogic(max_risk_discuss_rounds=2)
        assert logic.should_continue_risk_analysis(result) == "Conservative Analyst"

    def test_conservative_output_routes_to_neutral(self):
        from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator

        result = create_conservative_debator(_mock_llm())(_debator_state())
        logic = ConditionalLogic(max_risk_discuss_rounds=2)
        assert logic.should_continue_risk_analysis(result) == "Neutral Analyst"

    def test_neutral_output_routes_to_aggressive(self):
        from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator

        result = create_neutral_debator(_mock_llm())(_debator_state())
        logic = ConditionalLogic(max_risk_discuss_rounds=2)
        assert logic.should_continue_risk_analysis(result) == "Aggressive Analyst"
