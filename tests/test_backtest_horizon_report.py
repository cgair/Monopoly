"""Tests for the holding-period aggregation (tradingagents.backtest.horizon_report)."""

import pytest

from tradingagents.backtest.horizon_report import (
    BAD_LEVELS, ERROR, FLAT, INVALID_STOP, NO_LEVELS, OK,
    Sample, _classify, decision_agreement, horizon_summary, wilson,
)

AS_OF = 1_770_033_600_000


def _rec(**kw) -> dict:
    base = {"symbol": "BTC-USD", "as_of": AS_OF, "rep": 0, "side": "Short",
            "entry_price": None, "stop_loss": 80500.0, "take_profit": 70000.0,
            "reference_price": 77716.1, "error": None}
    base.update(kw)
    return base


@pytest.mark.unit
class TestClassify:
    def test_a_well_formed_decision_is_ok(self):
        assert _classify(_rec())[0] == OK

    def test_flat_is_not_an_error(self):
        assert _classify(_rec(side="Flat"))[0] == FLAT

    def test_replay_error_wins_over_everything(self):
        assert _classify(_rec(error="Timeout"))[0] == ERROR

    def test_missing_levels(self):
        assert _classify(_rec(stop_loss=None))[0] == NO_LEVELS

    def test_short_with_stop_below_the_reference_is_invalid(self):
        """The market-order gap: gate never compared these. See risk_gate.py:284."""
        assert _classify(_rec(stop_loss=70000.0, take_profit=60000.0))[0] == INVALID_STOP

    def test_long_with_stop_above_the_reference_is_invalid(self):
        assert _classify(_rec(side="Long", stop_loss=80000.0,
                              take_profit=90000.0))[0] == INVALID_STOP

    def test_target_on_the_wrong_side_of_the_stop(self):
        assert _classify(_rec(side="Long", stop_loss=70000.0,
                              take_profit=69000.0))[0] == BAD_LEVELS


@pytest.mark.unit
class TestWilson:
    def test_no_samples_gives_no_interval(self):
        assert wilson(0, 0) is None

    def test_a_tiny_sample_gives_a_useless_interval(self):
        lo, hi = wilson(2, 2)
        assert lo < 0.5 and hi == pytest.approx(1.0, abs=0.01)

    def test_interval_tightens_with_more_samples(self):
        small = wilson(6, 10)
        large = wilson(60, 100)
        assert (large[1] - large[0]) < (small[1] - small[0])

    def test_interval_brackets_the_point_estimate(self):
        lo, hi = wilson(7, 10)
        assert lo <= 0.7 <= hi


def _sample(as_of, rep, ret_by_h, *, side="Short", stop_pct=2.0, mae=None,
            status=OK) -> Sample:
    s = Sample(as_of=as_of, rep=rep, label="down", status=status, side=side,
               stop_loss=1.0, take_profit=1.0, market_entry=100.0, stop_pct=stop_pct)
    for h, ret in ret_by_h.items():
        s.hold[h] = {"covered": True, "return_pct": ret, "mfe_pct": abs(ret),
                     "mae_pct": mae if mae is not None else 0.5}
        s.race[h] = {"covered": True, "outcome": "open", "r": ret / 2,
                     "pnl_pct": ret, "hold_hours": h}
    return s


@pytest.mark.unit
class TestHorizonSummary:
    def test_accuracy_counts_positive_blind_returns(self):
        samples = [_sample(AS_OF, 0, {24: 1.0}), _sample(AS_OF + 10**9, 0, {24: -1.0})]
        row = horizon_summary(samples, (24,))[0]
        assert row["decided"] == 2 and row["hits"] == 1
        assert row["accuracy"] == 0.5

    def test_uncovered_horizons_are_left_out_not_zeroed(self):
        s = _sample(AS_OF, 0, {24: 1.0})
        s.hold[720] = {"covered": False, "return_pct": None,
                       "mfe_pct": None, "mae_pct": None}
        row = horizon_summary([s], (720,))[0]
        assert row["n"] == 0 and row["accuracy"] is None

    def test_excluded_samples_never_enter_the_statistics(self):
        good = _sample(AS_OF, 0, {24: 1.0})
        bad = _sample(AS_OF + 10**9, 0, {24: -5.0}, status=INVALID_STOP)
        row = horizon_summary([good, bad], (24,))[0]
        assert row["decided"] == 1 and row["accuracy"] == 1.0

    def test_stop_hit_counts_adverse_excursion_past_the_stop(self):
        killed = _sample(AS_OF, 0, {24: 2.0}, stop_pct=1.0, mae=3.0)
        survived = _sample(AS_OF + 10**9, 0, {24: 2.0}, stop_pct=5.0, mae=3.0)
        row = horizon_summary([killed, survived], (24,))[0]
        assert row["stopped_out"] == 1
        assert row["stopped_but_right"] == 1     # direction was right; the stop was not

    def test_repeat_agreement_is_unanimity_per_window(self):
        agree = [_sample(AS_OF, 0, {24: 1.0}), _sample(AS_OF, 1, {24: 1.0})]
        split = [_sample(AS_OF + 10**9, 0, {24: 1.0}),
                 _sample(AS_OF + 10**9, 1, {24: -1.0}, side="Long")]
        row = horizon_summary(agree + split, (24,))[0]
        assert row["agreement_windows"] == 2
        assert row["agreement"] == 0.5

    def test_a_flat_repeat_counts_as_disagreement(self):
        traded = _sample(AS_OF, 0, {24: 1.0})
        stood_aside = Sample(as_of=AS_OF, rep=1, label="down", status=FLAT)
        row = horizon_summary([traded, stood_aside], (24,))[0]
        assert row["agreement"] == 0.0


@pytest.mark.unit
class TestDecisionAgreement:
    def test_side_unanimity_is_per_window(self):
        samples = [_sample(AS_OF, 0, {24: 1.0}, side="Short"),
                   _sample(AS_OF, 1, {24: 1.0}, side="Long")]
        out = decision_agreement(samples)
        assert out["windows_with_repeats"] == 1
        assert out["unanimous_side_rate"] == 0.0

    def test_single_sample_windows_are_not_counted(self):
        out = decision_agreement([_sample(AS_OF, 0, {24: 1.0})])
        assert out["windows_with_repeats"] == 0
        assert out["unanimous_side_rate"] is None

    def test_stop_spread_reports_the_dispersion_between_repeats(self):
        samples = [_sample(AS_OF, 0, {24: 1.0}, stop_pct=1.0),
                   _sample(AS_OF, 1, {24: 1.0}, stop_pct=3.5)]
        out = decision_agreement(samples)
        assert out["max_stop_spread_pct"] == pytest.approx(2.5)
