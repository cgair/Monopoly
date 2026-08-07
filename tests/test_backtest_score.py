"""Tests for backtest decision scoring (tradingagents.backtest.score)."""

import pytest

from tradingagents.backtest.score import (
    score_decision,
    ScoredOutcome,
)


# ---------------------------------------------------------------------------
# Utilities for synthetic bar construction
# ---------------------------------------------------------------------------


def _bar(ts_offset_min: int, open_price: float, high: float, low: float, close: float) -> tuple:
    """Create a synthetic bar tuple. ts_offset_min is minutes after decision_ts."""
    decision_ts = 1_000_000_000  # arbitrary base timestamp in ms
    bar_ts = decision_ts + (ts_offset_min * 60 * 1000)
    return (bar_ts, open_price, high, low, close)


# ---------------------------------------------------------------------------
# Test: input validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInputValidation:
    """Tests for input validation and error handling."""

    def test_invalid_side_raises(self):
        """Invalid side should raise ValueError."""
        bars = [_bar(5, 100, 101, 99, 100)]
        with pytest.raises(ValueError, match="side must be"):
            score_decision(
                symbol="BTC-USD",
                side="INVALID",
                decision_ts=1_000_000_000,
                entry_price=100,
                stop_loss=99,
                take_profit=102,
                bars=bars,
            )

    def test_valid_side_variants(self):
        """Should accept LONG, SHORT, BUY, SELL (case-insensitive)."""
        bars = [_bar(5, 100, 101, 99, 100)]
        decision_ts = 1_000_000_000

        for side_input in ["LONG", "long", "Buy", "buy"]:
            result = score_decision(
                symbol="BTC-USD",
                side=side_input,
                decision_ts=decision_ts,
                entry_price=100,
                stop_loss=99,
                take_profit=102,
                bars=bars,
            )
            assert result.side == "LONG"

        for side_input in ["SHORT", "short", "Sell", "SELL"]:
            result = score_decision(
                symbol="BTC-USD",
                side=side_input,
                decision_ts=decision_ts,
                entry_price=100,
                stop_loss=102,
                take_profit=98,
                bars=bars,
            )
            assert result.side == "SHORT"

    def test_invalid_symbol_format(self):
        """Invalid symbol format should raise ValueError."""
        bars = [_bar(5, 100, 101, 99, 100)]
        with pytest.raises(ValueError, match="Invalid symbol"):
            score_decision(
                symbol="INVALID",
                side="LONG",
                decision_ts=1_000_000_000,
                entry_price=100,
                stop_loss=99,
                take_profit=102,
                bars=bars,
            )

    def test_symbol_formats_accepted(self):
        """Accept both BTC-USD and BTCUSDT formats."""
        bars = [_bar(5, 100, 101, 99, 100)]
        decision_ts = 1_000_000_000

        for symbol in ["BTC-USD", "btc-usd", "BTCUSDT", "btcusdt"]:
            result = score_decision(
                symbol=symbol,
                side="LONG",
                decision_ts=decision_ts,
                entry_price=100,
                stop_loss=99,
                take_profit=102,
                bars=bars,
            )
            assert result.symbol == symbol

    def test_invalid_tp_sl_long(self):
        """For LONG, tp must be > sl."""
        bars = [_bar(5, 100, 101, 99, 100)]
        with pytest.raises(ValueError, match="take_profit must be"):
            score_decision(
                symbol="BTC-USD",
                side="LONG",
                decision_ts=1_000_000_000,
                entry_price=100,
                stop_loss=102,
                take_profit=98,
                bars=bars,
            )

    def test_invalid_tp_sl_short(self):
        """For SHORT, tp must be < sl."""
        bars = [_bar(5, 100, 101, 99, 100)]
        with pytest.raises(ValueError, match="take_profit must be"):
            score_decision(
                symbol="BTC-USD",
                side="SHORT",
                decision_ts=1_000_000_000,
                entry_price=100,
                stop_loss=98,
                take_profit=102,
                bars=bars,
            )

    def test_empty_bars_returns_insufficient_bars(self):
        """Empty bars list should return insufficient_bars outcome."""
        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=1_000_000_000,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=[],
        )
        assert not result.filled
        assert "insufficient bars" in result.notes


# ---------------------------------------------------------------------------
# Test: Long trade - win at take profit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLongWin:
    """Test a LONG trade that hits take-profit."""

    def test_long_win_at_tp(self):
        """LONG trade should fill and hit TP."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 101, 100, 100),   # bar after decision, price stays near entry
            _bar(10, 100, 103, 100, 101),  # price approaches TP, high=103 > tp=102
            _bar(15, 101, 105, 100, 104),  # TP hit earlier, just tracking further
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.fill_price == 100
        assert result.outcome == "take_profit"
        assert result.exit_price == 102
        assert result.r_multiple == 2.0  # (102 - 100) / (100 - 99) = 2.0
        assert result.pnl_pct == pytest.approx(2.0)
        assert "insufficient bars" not in result.notes


# ---------------------------------------------------------------------------
# Test: Long trade - stop loss
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLongStop:
    """Test a LONG trade that hits stop-loss."""

    def test_long_stop(self):
        """LONG trade should fill and hit SL."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100, 99.5, 99.5),   # bar after decision, price drops
            _bar(10, 99.5, 99.6, 98, 98.5),  # price continues down
            _bar(15, 98.5, 99, 97, 97.5),    # SL at 99 hit (low=97 <= 99)
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.fill_price == 100
        assert result.outcome == "stop_loss"
        assert result.exit_price == 99
        assert result.r_multiple == -1.0  # Always -1.0 for SL hit
        assert result.pnl_pct == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Test: Short trade - win at take profit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShortWin:
    """Test a SHORT trade that hits take-profit."""

    def test_short_win_at_tp(self):
        """SHORT trade should fill and hit TP."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100.5, 100, 100),   # bar after decision
            _bar(10, 100, 100.2, 97, 97.5),  # price drops toward TP, low=97 < tp=98
            _bar(15, 97, 97.5, 95, 96),      # TP hit earlier, just tracking further
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="SHORT",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=102,
            take_profit=98,
            bars=bars,
        )

        assert result.filled
        assert result.fill_price == 100
        assert result.outcome == "take_profit"
        assert result.exit_price == 98
        assert result.r_multiple == 1.0  # (100 - 98) / (102 - 100) = 1.0
        assert result.pnl_pct == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Test: Short trade - stop loss
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShortStop:
    """Test a SHORT trade that hits stop-loss."""

    def test_short_stop(self):
        """SHORT trade should fill and hit SL."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 101, 100, 100.5),    # bar after decision
            _bar(10, 100.5, 102, 100, 101),   # price rises
            _bar(15, 101, 104, 100, 103),     # SL at 102 hit (high=104 >= 102)
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="SHORT",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=102,
            take_profit=98,
            bars=bars,
        )

        assert result.filled
        assert result.fill_price == 100
        assert result.outcome == "stop_loss"
        assert result.exit_price == 102
        assert result.r_multiple == -1.0
        assert result.pnl_pct == pytest.approx(-2.0)  # (100-102)/100*100 = -2%


# ---------------------------------------------------------------------------
# Test: Limit never filled -> filled=False, miss_by_pct
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLimitNeverFilled:
    """Test when a limit order never fills."""

    def test_long_limit_never_filled(self):
        """LONG limit entry should never fill if price never touches."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 101, 102, 100.5, 101),   # low=100.5, entry=100, misses
            _bar(10, 102, 103, 101, 102),
            _bar(15, 102, 103, 101, 102),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert not result.filled
        assert result.fill_price is None
        assert result.closest_approach == pytest.approx(100.5)
        assert result.miss_by_pct == pytest.approx(0.5)
        assert result.outcome is None
        assert "limit never filled" in result.notes

    def test_long_limit_close_miss(self):
        """LONG limit that comes close but never fills."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 101, 102, 100.5, 101),  # low=100.5, entry=100, misses by 0.5%
            _bar(10, 102, 103, 101, 102),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert not result.filled
        assert result.closest_approach == pytest.approx(100.5)
        assert result.miss_by_pct == pytest.approx(0.5)

    def test_short_limit_never_filled(self):
        """SHORT limit entry should never fill if price never touches."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 99, 99.5, 98, 99),   # high=99.5, entry=100, misses
            _bar(10, 98, 99, 97, 98),
            _bar(15, 97, 98, 96, 97),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="SHORT",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=102,
            take_profit=98,
            bars=bars,
        )

        assert not result.filled
        assert result.closest_approach == pytest.approx(99.5)
        assert result.miss_by_pct == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Test: Ambiguous same-bar TP+SL
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAmbiguousSamebar:
    """Test when TP and SL both trigger in the same bar."""

    def test_long_ambiguous_tp_and_sl_same_bar(self):
        """LONG where both TP and SL levels exist in same bar."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 101, 100, 100),
            _bar(10, 100, 102, 98, 101),  # high=102 (TP), low=98 (SL), intrabar order unknown
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.outcome == "ambiguous"
        assert "ambiguous same-bar tp/sl" in result.notes

    def test_short_ambiguous_tp_and_sl_same_bar(self):
        """SHORT where both TP and SL levels exist in same bar."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100.5, 100, 100),
            _bar(10, 100, 103, 97, 99),  # high=103 (SL at 102), low=97 (TP at 98)
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="SHORT",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=102,
            take_profit=98,
            bars=bars,
        )

        assert result.filled
        assert result.outcome == "ambiguous"
        assert "ambiguous same-bar tp/sl" in result.notes


# ---------------------------------------------------------------------------
# Test: Horizon reached with position still open
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHorizonOpen:
    """Test when the horizon ends with the trade still open."""

    def test_long_horizon_reached_open(self):
        """LONG trade should close at horizon with outcome='open'."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100.5, 99.5, 100),
            _bar(10, 100, 100.8, 99.8, 100.2),
            _bar(15, 100.2, 100.5, 100, 100.3),  # horizon ends, last close=100.3
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            horizon_hours=1,  # small horizon to ensure we don't go beyond bars
            bars=bars,
        )

        assert result.filled
        assert result.outcome == "open"
        assert result.exit_price == 100.3  # last close
        assert "horizon reached with position open" in result.notes


# ---------------------------------------------------------------------------
# Test: Market entry counterfactual
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMarketCounterfactual:
    """Test the market-entry counterfactual diagnostics."""

    def test_market_entry_price_is_first_bar_open(self):
        """Market entry price should be the first bar open after decision_ts."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 101, 99, 100),    # first bar open is 100
            _bar(10, 100, 100.5, 99.5, 100),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.market_entry_price == 100

    def test_market_entry_no_data(self):
        """Market entry should handle no-data gracefully."""
        decision_ts = 1_000_000_000
        bars = []

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.market_outcome in ("no_data", "insufficient_bars")


# ---------------------------------------------------------------------------
# Test: MFE / MAE correctness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMfeMae:
    """Test max favorable and adverse excursion calculations."""

    def test_long_mfe_mae(self):
        """LONG trade should track MFE (upside) and MAE (downside)."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100.5, 99, 99.5),      # entry at 100, low=99 (MAE start)
            _bar(10, 99.5, 105, 98, 104),       # MFE=105, MAE=98
            _bar(15, 104, 106, 103, 105),       # MFE continues to 106, but we hit TP at 102
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.mfe_pct == pytest.approx(5.0)  # (105 - 100) / 100 * 100 = 5.0, bar 10 high
        assert result.mae_pct == pytest.approx(2.0)  # (100 - 98) / 100 * 100 = 2.0

    def test_short_mfe_mae(self):
        """SHORT trade should track MFE (downside) and MAE (upside)."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100.5, 100, 100.2),    # entry at 100, high=100.5 (MAE start)
            _bar(10, 100.2, 103, 95, 95.5),     # MFE=95, MAE=103
            _bar(15, 95.5, 96, 94, 94.5),       # MFE continues to 94, but we hit TP at 98
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="SHORT",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=102,
            take_profit=98,
            bars=bars,
        )

        assert result.filled
        assert result.mfe_pct == pytest.approx(5.0)  # (100 - 95) / 100 * 100 = 5.0, bar 10 low
        assert result.mae_pct == pytest.approx(3.0)  # (103 - 100) / 100 * 100 = 3.0

    def test_mfe_mae_no_excursion(self):
        """MFE/MAE should be 0 if price never goes beyond entry."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100, 100, 100, 100),    # price stays at entry
            _bar(10, 100, 100, 100, 100),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.mfe_pct == pytest.approx(0.0)
        assert result.mae_pct == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test: Market entry vs. limit entry (direct comparison)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMarketVsLimitComparison:
    """Test scenarios where market and limit entries diverge significantly."""

    def test_both_fill_but_with_different_prices(self):
        """Both limit and market fill, but at different prices."""
        decision_ts = 1_000_000_000
        bars = [
            _bar(5, 100.5, 101, 99.5, 100),    # market entry at 100.5; limit at 100 fills
            _bar(10, 100, 105, 99, 104),       # TP hit
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100.0,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        # Both fill
        assert result.filled
        assert result.fill_price == 100.0
        assert result.market_entry_price == pytest.approx(100.5)
        # But market entry has worse r_multiple due to higher entry
        assert result.market_r_multiple < result.r_multiple


# ---------------------------------------------------------------------------
# Test: Hold time calculation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHoldTime:
    """Test hold_hours calculation."""

    def test_hold_hours_calculated(self):
        """hold_hours should be the duration from fill to exit in hours."""
        decision_ts = 1_000_000_000
        fill_ts = decision_ts + (5 * 60 * 1000)    # 5 minutes
        exit_ts = decision_ts + (305 * 60 * 1000)  # 305 minutes = 5 hours 5 minutes
        bars = [
            _bar(5, 100, 101, 100, 100),
            _bar(305, 100, 105, 99, 104),  # TP hit
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        # 305 - 5 = 300 minutes / 60 = 5 hours
        # But more precisely: (305*60*1000 - 5*60*1000) / (60*60*1000) = 300*60*1000 / (60*60*1000)
        #                    = 300 * 60 / (60 * 60) = 300 / 60 = 5.0
        # Actually: 305 minutes = 5 hours 5 minutes, fill at 5 minutes, so 300 minutes = 5 hours exactly
        assert result.hold_hours == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Test unusual but valid scenarios."""

    def test_bars_with_timestamp_before_decision_ignored(self):
        """Bars with timestamp <= decision_ts should be ignored for fill."""
        decision_ts = 1_000_000_000
        bars = [
            (decision_ts - 60000, 99, 100, 98, 99),  # before decision, ignored
            _bar(5, 100, 101, 100, 100),               # first bar after
            _bar(10, 100, 105, 99, 104),               # TP hit
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            bars=bars,
        )

        assert result.filled
        assert result.fill_ts == decision_ts + (5 * 60 * 1000)

    def test_market_entry_uses_first_bar_after_decision(self):
        """Market entry should use the first bar's open after decision_ts."""
        decision_ts = 1_000_000_000
        bars = [
            (decision_ts - 60000, 50, 51, 49, 50),      # before decision, ignored
            (decision_ts, 75, 76, 74, 75),              # at decision, not counted
            _bar(5, 100, 101, 100, 100),                  # first after, should use open=100
            _bar(10, 100, 105, 99, 104),
        ]

        result = score_decision(
            symbol="BTC-USD",
            side="LONG",
            decision_ts=decision_ts,
            entry_price=99,  # will fill at first bar
            stop_loss=98,
            take_profit=102,
            bars=bars,
        )

        assert result.market_entry_price == 100
