"""Tests for the shared rating heuristic and the SignalProcessor adapter.

The Portfolio Manager produces a typed FuturesDecision via structured
output and renders it to markdown whose first line is a ``**Side**: X``
header.  The deterministic heuristic in ``tradingagents.agents.utils.rating``
is therefore sufficient to extract the rating downstream — no second LLM
call is needed — and SignalProcessor is now a thin adapter that delegates
to it.  The legacy ``Rating: X`` label pass survives for free-text
fallback decisions.
"""

import pytest

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.graph.signal_processing import SignalProcessor


# ---------------------------------------------------------------------------
# Heuristic parser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        assert parse_rating("Rating: Buy\nReasoning here.") == "Buy"

    def test_explicit_label_overweight(self):
        assert parse_rating("Rating: Overweight\nDetails.") == "Overweight"

    def test_explicit_label_with_markdown_bold_value(self):
        # Regression: Rating: **Sell** — markdown around the value.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "Sell"

    def test_explicit_label_with_markdown_bold_label(self):
        assert parse_rating("**Rating**: Underweight\nTrim exposure.") == "Underweight"

    def test_rendered_pm_markdown_shape(self):
        # The exact shape produced by render_pm_decision must always parse.
        text = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Enter at $189-192, 6% portfolio cap.\n\n"
            "**Investment Thesis**: AI capex cycle intact; institutional flows constructive."
        )
        assert parse_rating(text) == "Buy"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "Sell"

    def test_no_rating_returns_default(self):
        assert parse_rating("No clear directional signal at this time.") == "Hold"

    def test_no_rating_custom_default(self):
        assert parse_rating("Plain prose.", default="Underweight") == "Underweight"

    def test_all_five_tiers_recognised(self):
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r

    def test_crypto_side_long_maps_to_buy(self):
        # Crypto FuturesDecision renders "**Side**: Long" with no Rating line.
        assert parse_rating("**Side**: Long\n\n**Executive Summary**: ...") == "Buy"

    def test_crypto_side_short_maps_to_sell(self):
        assert parse_rating("**Side**: Short\n\n**Executive Summary**: ...") == "Sell"

    def test_crypto_side_flat_maps_to_hold(self):
        # Sentinel default: proves Hold comes from the Side mapping, not
        # from falling through to the parser's default.
        text = "**Side**: Flat\n\n**Executive Summary**: ..."
        assert parse_rating(text, default="Underweight") == "Hold"

    def test_rendered_futures_decision_shape(self):
        # The exact shape produced by render_futures_decision must parse to a
        # side-mapped rating, not the prose-fallback default.
        from tradingagents.agents.schemas import (
            FuturesDecision,
            FuturesSide,
            render_futures_decision,
        )

        decision = FuturesDecision(
            side=FuturesSide.SHORT,
            leverage=2.0,
            position_size_pct=0.005,
            stop_loss=66500.0,
            executive_summary="Fade the failed breakout; downside to 60k.",
            investment_thesis="Funding overheated; OI divergence supports the short.",
        )
        assert parse_rating(render_futures_decision(decision)) == "Sell"

    def test_word_ending_in_rating_is_not_a_label(self):
        # Regression: "decelerating - sell" used to match the Rating-label
        # regex ("rating" is a suffix of decelerating/accelerating/
        # deteriorating…) and override the authoritative Side header,
        # flipping a Flat decision's rating to Sell.
        text = (
            "**Side**: Flat\n\n"
            "**Executive Summary**: 资金费率过热正在降温。\n\n"
            "**Investment Thesis**: OI is decelerating - sell pressure fading."
        )
        assert parse_rating(text) == "Hold"

    def test_real_rating_label_wins_over_rating_suffix_word(self):
        text = (
            "Momentum is accelerating - sell pressure is fading.\n"
            "Rating: Buy"
        )
        assert parse_rating(text) == "Buy"

    def test_side_header_wins_over_rating_label_in_prose(self):
        # The Side header is rendered deterministically by our own code;
        # a "rating: buy" citation inside the free-prose thesis must not
        # override it.
        text = (
            "**Side**: Flat\n\n"
            "**Investment Thesis**: desks cite a consensus analyst rating: buy."
        )
        assert parse_rating(text) == "Hold"

    def test_downside_in_prose_does_not_false_match_side(self):
        # "downside"/"upside" contain "side" — the anchored regex must not
        # treat them as a Side header. With no real Side/Rating line, the
        # parser falls through to the prose scan / default.
        text = "Executive Summary: Significant downside risk remains; stay cautious."
        assert parse_rating(text) == "Hold"


# ---------------------------------------------------------------------------
# SignalProcessor: thin adapter over the heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSignalProcessor:
    def test_returns_rating_from_pm_markdown(self):
        sp = SignalProcessor()
        md = "**Rating**: Overweight\n\n**Executive Summary**: Build gradually."
        assert sp.process_signal(md) == "Overweight"

    def test_makes_no_llm_calls(self):
        """SignalProcessor must not invoke the LLM it was constructed with —
        the rating is parseable from the rendered PM markdown directly."""
        from unittest.mock import MagicMock

        llm = MagicMock()
        sp = SignalProcessor(llm)
        sp.process_signal("Rating: Buy\nDetails.")
        llm.invoke.assert_not_called()
        llm.with_structured_output.assert_not_called()

    def test_default_when_no_rating_present(self):
        sp = SignalProcessor()
        assert sp.process_signal("Plain prose without a recommendation.") == "Hold"
