"""Tests for the sweep driver's repeat and resume behaviour."""

import json
from datetime import datetime, timezone

import pytest

from tradingagents.backtest import sweep as sweep_mod
from tradingagents.backtest.replay import ReplayResult


WINDOWS = [
    datetime(2026, 1, 11, 12, tzinfo=timezone.utc),
    datetime(2026, 1, 17, 12, tzinfo=timezone.utc),
]


@pytest.fixture
def fake_replay(monkeypatch):
    """Record every (as_of, rep) the sweep asks for, without running a graph."""
    calls = []

    def _replay(symbol, as_of, *, scratch, config=None, rep=0, **kw):
        calls.append((int(as_of.timestamp() * 1000), rep, scratch))
        return ReplayResult(symbol=symbol, as_of=int(as_of.timestamp() * 1000),
                            rep=rep, side="Flat")

    monkeypatch.setattr(sweep_mod, "replay", _replay)
    return calls


@pytest.mark.unit
class TestRepeats:
    def test_every_window_is_replayed_once_per_repeat(self, tmp_path, fake_replay):
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=tmp_path / "out.jsonl",
                            scratch_root=tmp_path / "scratch", repeats=3)
        assert len(fake_replay) == 6
        assert {(rep) for _, rep, _ in fake_replay} == {0, 1, 2}

    def test_repeats_are_interleaved_not_batched_per_window(self, tmp_path, fake_replay):
        """A sweep killed halfway should have one sample of everything."""
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=tmp_path / "out.jsonl",
                            scratch_root=tmp_path / "scratch", repeats=2)
        reps = [rep for _, rep, _ in fake_replay]
        assert reps == [0, 0, 1, 1]

    def test_each_repeat_gets_its_own_scratch_dir(self, tmp_path, fake_replay):
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=tmp_path / "out.jsonl",
                            scratch_root=tmp_path / "scratch", repeats=2)
        scratches = {str(s) for _, _, s in fake_replay}
        assert len(scratches) == 2

    def test_rep_is_recorded_in_the_jsonl(self, tmp_path, fake_replay):
        out = tmp_path / "out.jsonl"
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=2)
        recs = [json.loads(l) for l in out.read_text().splitlines()]
        assert [r["rep"] for r in recs] == [0, 1]


@pytest.mark.unit
class TestResume:
    def test_a_finished_repeat_is_skipped_on_rerun(self, tmp_path, fake_replay):
        out = tmp_path / "out.jsonl"
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        fake_replay.clear()
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        assert fake_replay == []

    def test_resume_tops_up_missing_repeats_rather_than_skipping_the_window(
            self, tmp_path, fake_replay):
        """The old key was the window alone; one sample would have ended it."""
        out = tmp_path / "out.jsonl"
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        fake_replay.clear()
        sweep_mod.run_sweep("BTC-USD", WINDOWS, out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=3)
        assert sorted(rep for _, rep, _ in fake_replay) == [1, 1, 2, 2]

    def test_a_failed_run_is_retried(self, tmp_path, monkeypatch):
        out = tmp_path / "out.jsonl"
        attempts = []

        def _replay(symbol, as_of, *, scratch, config=None, rep=0, **kw):
            attempts.append(rep)
            return ReplayResult(symbol=symbol, as_of=int(as_of.timestamp() * 1000),
                                rep=rep, error="boom")

        monkeypatch.setattr(sweep_mod, "replay", _replay)
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        assert len(attempts) == 2

    def test_skip_done_false_replays_everything(self, tmp_path, fake_replay):
        out = tmp_path / "out.jsonl"
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1)
        fake_replay.clear()
        sweep_mod.run_sweep("BTC-USD", WINDOWS[:1], out_path=out,
                            scratch_root=tmp_path / "scratch", repeats=1,
                            skip_done=False)
        assert len(fake_replay) == 1
