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


# ---------------------------------------------------------------------------
# time_horizon: schema-example echo (2026-08-08 review, gap #2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTimeHorizonBuckets:
    """46/51 replayed decisions echoed the description's example string
    ('2-5 days') verbatim, so the field carried zero information. The
    field is now a closed set of buckets: the JSON schema advertises an
    enum (no free-text example to echo), and values outside the buckets
    degrade to None instead of failing the whole decision — a structured-
    output validation failure would otherwise fall back to free text and
    get the trade rejected by the gate.
    """

    def _decision(self, **overrides):
        base = dict(
            side=FuturesSide.LONG,
            leverage=2.0,
            position_size_pct=0.005,
            stop_loss=62800.0,
            executive_summary="s",
            investment_thesis="t",
        )
        base.update(overrides)
        return FuturesDecision(**base)

    def test_description_carries_no_example_literal(self):
        desc = FuturesDecision.model_fields["time_horizon"].description or ""
        assert "2-5 days" not in desc
        assert "e.g." not in desc

    def test_json_schema_advertises_closed_enum(self):
        import json as _json

        prop = FuturesDecision.model_json_schema()["properties"]["time_horizon"]
        blob = _json.dumps(prop)
        assert "intraday" in blob          # enum choices are in the schema
        assert "2-5 days" not in blob      # the old example is not

    def test_bucket_values_accepted(self):
        from tradingagents.agents.schemas import TIME_HORIZON_BUCKETS

        assert len(TIME_HORIZON_BUCKETS) >= 3
        for bucket in TIME_HORIZON_BUCKETS:
            assert self._decision(time_horizon=bucket).time_horizon == bucket

    def test_old_example_echo_degrades_to_none_not_error(self):
        d = self._decision(time_horizon="2-5 days")
        assert d.time_horizon is None

    def test_whitespace_and_case_normalised(self):
        d = self._decision(time_horizon="  Intraday ")
        assert d.time_horizon == "intraday"

    def test_none_still_allowed(self):
        assert self._decision(time_horizon=None).time_horizon is None
