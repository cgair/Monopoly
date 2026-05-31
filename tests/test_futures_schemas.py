"""Tests for the futures (crypto perp) decision schemas.

Covers shape, render output, and the analyst stop-signal marker mapping
(Long → BUY, Short → SELL, Flat → HOLD). The Trader/PM node-level wiring
that selects these vs the stock-mode schemas lives in Phase 2 and is
covered by separate tests.
"""

import pytest

from tradingagents.agents.schemas import (
    FuturesDecision,
    FuturesProposal,
    FuturesSide,
    render_futures_decision,
    render_futures_proposal,
)


@pytest.mark.unit
class TestFuturesSide:
    def test_enum_values(self):
        assert FuturesSide.LONG.value == "Long"
        assert FuturesSide.SHORT.value == "Short"
        assert FuturesSide.FLAT.value == "Flat"


@pytest.mark.unit
class TestRenderFuturesProposal:
    def test_flat_minimal(self):
        p = FuturesProposal(
            side=FuturesSide.FLAT,
            reasoning="Conviction absent on both sides; sit out this round.",
        )
        md = render_futures_proposal(p)
        assert "**Side**: Flat" in md
        assert "**Reasoning**: Conviction absent on both sides" in md
        # Flat → HOLD in the analyst stop-signal marker
        assert "FINAL TRANSACTION PROPOSAL: **HOLD**" in md
        # Flat must not surface leverage/sizing markdown rows
        assert "**Leverage**" not in md
        assert "**Position Size**" not in md
        assert "**Stop Loss**" not in md

    def test_long_with_full_params(self):
        p = FuturesProposal(
            side=FuturesSide.LONG,
            reasoning="Spot ETF inflows + funding reset.",
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            leverage=2.0,
            position_size_pct=0.01,
        )
        md = render_futures_proposal(p)
        assert "**Side**: Long" in md
        assert "**Entry Price**: 64500.0" in md
        assert "**Stop Loss**: 62800.0" in md
        assert "**Take Profit**: 68000.0" in md
        assert "**Leverage**: 2.0x" in md
        assert "**Position Size**: 1.00% of equity at risk" in md
        # Long → BUY in the analyst stop-signal marker
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in md

    def test_short_maps_to_sell_marker(self):
        p = FuturesProposal(
            side=FuturesSide.SHORT,
            reasoning="Funding bid hot; deleverage flush likely.",
            stop_loss=66000.0,
            leverage=2.0,
            position_size_pct=0.01,
        )
        md = render_futures_proposal(p)
        assert "**Side**: Short" in md
        assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md


@pytest.mark.unit
class TestRenderFuturesDecision:
    def test_flat_minimal(self):
        d = FuturesDecision(
            side=FuturesSide.FLAT,
            executive_summary="Stand down; analysts split on direction.",
            investment_thesis="Bull case rests on ETF flows that have softened; bear case "
                              "on funding-rate compression that has already played out.",
        )
        md = render_futures_decision(d)
        assert "**Side**: Flat" in md
        assert "**Executive Summary**: Stand down" in md
        assert "**Investment Thesis**:" in md
        # Flat should not produce execution params in the rendered output
        for marker in ("**Leverage**", "**Position Size**", "**Stop Loss**", "**Take Profit**"):
            assert marker not in md

    def test_long_with_full_params(self):
        d = FuturesDecision(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.01,
            entry_price=64500.0,
            stop_loss=62800.0,
            take_profit=68000.0,
            executive_summary="Enter long on retrace; stop tight below LTF demand.",
            investment_thesis="Macro tailwind + on-chain accumulation + funding reset all point up.",
            time_horizon="2-5 days",
        )
        md = render_futures_decision(d)
        assert "**Side**: Long" in md
        assert "**Leverage**: 2.0x" in md
        assert "**Position Size**: 1.00% of equity at risk" in md
        assert "**Entry Price**: 64500.0" in md
        assert "**Stop Loss**: 62800.0" in md
        assert "**Take Profit**: 68000.0" in md
        assert "**Time Horizon**: 2-5 days" in md


@pytest.mark.unit
class TestFuturesSchemaValidation:
    def test_proposal_requires_side_and_reasoning(self):
        with pytest.raises(Exception):
            FuturesProposal(reasoning="missing side")
        with pytest.raises(Exception):
            FuturesProposal(side=FuturesSide.LONG)

    def test_decision_requires_summary_and_thesis(self):
        with pytest.raises(Exception):
            FuturesDecision(side=FuturesSide.LONG)

    def test_proposal_rejects_unknown_side(self):
        with pytest.raises(Exception):
            FuturesProposal(side="Sideways", reasoning="invalid")  # type: ignore[arg-type]
