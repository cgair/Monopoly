"""Tests for the pure directional scorer (tradingagents.backtest.horizon)."""

import pytest

from tradingagents.backtest.horizon import (
    HORIZONS,
    directional_returns,
)


DECISION_TS = 1_000_000_000  # arbitrary ms base
_MIN = 60 * 1000
_HOUR = 60 * _MIN


def _series(prices: list[float], *, step_min: int = 5, start_min: int = 5) -> list[tuple]:
    """Flat bars at ``prices``, one every ``step_min`` minutes after the decision."""
    return [
        (DECISION_TS + (start_min + i * step_min) * _MIN, p, p, p, p)
        for i, p in enumerate(prices)
    ]


def _ramp(start: float, end: float, hours: int) -> list[tuple]:
    """One bar every 5 minutes for ``hours``, moving linearly from start to end."""
    n = hours * 12
    return _series([start + (end - start) * i / (n - 1) for i in range(n)])


@pytest.mark.unit
class TestEntry:
    def test_entry_is_open_of_first_bar_after_decision(self):
        bars = [
            (DECISION_TS - _MIN, 90, 90, 90, 90),   # before the decision: ignored
            (DECISION_TS + 5 * _MIN, 100, 101, 99, 100),
            (DECISION_TS + 10 * _MIN, 100, 101, 99, 100),
        ]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS, horizons=(6,), bars=bars
        )
        assert out.entry_price == 100
        assert out.entry_ts == DECISION_TS + 5 * _MIN

    def test_no_bars_after_decision_marks_everything_uncovered(self):
        bars = [(DECISION_TS - _MIN, 90, 90, 90, 90)]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS, horizons=(6, 24), bars=bars
        )
        assert out.entry_price is None
        assert all(not p.covered for p in out.points.values())
        assert out.notes


@pytest.mark.unit
class TestDirectionalReturn:
    def test_long_profits_when_price_rises(self):
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6,), bars=_ramp(100, 110, 6),
        )
        p = out.points[6]
        assert p.covered
        assert p.return_pct == pytest.approx(10.0, abs=0.1)

    def test_short_profits_when_price_falls(self):
        out = directional_returns(
            symbol="BTC-USD", side="SHORT", decision_ts=DECISION_TS,
            horizons=(6,), bars=_ramp(100, 90, 6),
        )
        assert out.points[6].return_pct == pytest.approx(10.0, abs=0.1)

    def test_short_loses_when_price_rises(self):
        out = directional_returns(
            symbol="BTC-USD", side="SHORT", decision_ts=DECISION_TS,
            horizons=(6,), bars=_ramp(100, 110, 6),
        )
        assert out.points[6].return_pct == pytest.approx(-10.0, abs=0.1)

    def test_no_stop_out_a_deep_drawdown_that_recovers_still_scores_positive(self):
        """The whole point of this scorer: intermediate pain does not exit."""
        down = _ramp(100, 80, 3)
        up = [
            (down[-1][0] + (i + 1) * 5 * _MIN, p, p, p, p)
            for i, p in enumerate([80 + 30 * i / 35 for i in range(36)])
        ]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6,), bars=down + up,
        )
        p = out.points[6]
        assert p.return_pct > 0
        assert p.mae_pct == pytest.approx(20.0, abs=0.5)

    def test_horizons_are_measured_independently(self):
        """Up for 6h then back down: 6h wins, 24h does not."""
        up = _ramp(100, 110, 6)
        back = [
            (up[-1][0] + (i + 1) * 5 * _MIN, p, p, p, p)
            for i, p in enumerate([110 - 20 * i / 215 for i in range(216)])
        ]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6, 24), bars=up + back,
        )
        assert out.points[6].return_pct > 0
        assert out.points[24].return_pct < 0


@pytest.mark.unit
class TestCoverage:
    def test_horizon_beyond_the_bars_is_uncovered_not_truncated(self):
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6, 720), bars=_ramp(100, 110, 6),
        )
        assert out.points[6].covered
        far = out.points[720]
        assert not far.covered
        assert far.return_pct is None

    def test_one_bar_of_slack_is_allowed(self):
        """The bar opening exactly at the horizon has not closed; 5m short still counts."""
        bars = _ramp(100, 110, 6)[:-1]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6,), bars=bars,
        )
        assert out.points[6].covered


@pytest.mark.unit
class TestExcursions:
    def test_mfe_and_mae_are_bounded_by_the_horizon(self):
        """A spike after the 6h mark belongs to the 24h row, not the 6h row."""
        calm = _series([100.0] * 72)                       # 6h flat
        spike = [
            (calm[-1][0] + (i + 1) * 5 * _MIN, 100, 130, 100, 100)
            for i in range(216)
        ]
        out = directional_returns(
            symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
            horizons=(6, 24), bars=calm + spike,
        )
        assert out.points[6].mfe_pct == pytest.approx(0.0, abs=0.01)
        assert out.points[24].mfe_pct == pytest.approx(30.0, abs=0.1)

    def test_excursions_are_never_negative(self):
        out = directional_returns(
            symbol="BTC-USD", side="SHORT", decision_ts=DECISION_TS,
            horizons=(6,), bars=_ramp(100, 110, 6),
        )
        assert out.points[6].mfe_pct >= 0
        assert out.points[6].mae_pct >= 0


@pytest.mark.unit
class TestValidation:
    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="side must be"):
            directional_returns(
                symbol="BTC-USD", side="sideways", decision_ts=DECISION_TS,
                horizons=(6,), bars=_ramp(100, 110, 6),
            )

    def test_non_positive_horizon_raises(self):
        with pytest.raises(ValueError, match="positive hours"):
            directional_returns(
                symbol="BTC-USD", side="LONG", decision_ts=DECISION_TS,
                horizons=(0,), bars=_ramp(100, 110, 6),
            )

    def test_default_horizons_are_the_ones_under_test(self):
        assert HORIZONS == (6, 24, 72, 168, 336, 720)
