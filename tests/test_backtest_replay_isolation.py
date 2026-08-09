"""A replay must never be able to reach a live trading venue.

The 2026-08-08 sweep intended to run in ``dryrun`` — ``_scratch_config``
sets ``futures_executor_mode`` to it — and still sent 12 market orders to
Binance testnet, 2 of which opened positions. The venue is resolved from
``EXECUTOR_MODE`` in the environment, which outranks the config
(``executor.py`` ``resolve_executor_mode``), so it sat outside the scratch
isolation boundary that redirects every other side effect.

These tests pin the venue itself, not the config that asks for it.
"""

import os

import pytest

from tradingagents.backtest import replay as replay_mod
from tradingagents.dataflows.crypto_types import Bar
from tradingagents.futures.executor import resolve_executor_mode

from datetime import datetime, timezone

AS_OF = datetime(2026, 3, 3, 12, tzinfo=timezone.utc)
AS_OF_MS = int(AS_OF.timestamp() * 1000)


@pytest.fixture
def archive(monkeypatch):
    """One closed bar at the cutoff, so replay gets past its reference price."""
    bar = Bar(
        symbol="BTCUSDT", interval="1h",
        open_time=AS_OF_MS - 3_600_000, close_time=AS_OF_MS,
        open=67000.0, high=67100.0, low=66900.0, close=67000.0,
        volume=1.0, quote_volume=67000.0, trades=1,
    )
    monkeypatch.setattr(replay_mod.vision, "klines", lambda *a, **k: [bar])


@pytest.fixture
def fake_graph(monkeypatch):
    """Stand in for the real graph and record the venue it would execute on.

    The executor adapter is built when the nodes are created, so the
    constructor is the moment that matters.
    """
    import tradingagents.graph.trading_graph as tg

    seen: dict = {"constructed": False}

    class FakeGraph:
        def __init__(self, selected_analysts=None, config=None, **kwargs):
            seen["constructed"] = True
            seen["config"] = config
            seen["resolved_mode"] = resolve_executor_mode(config)
            seen["env"] = os.environ.get("EXECUTOR_MODE")
            if seen.get("raise_in_graph"):
                raise RuntimeError("graph blew up")

        def propagate(self, symbol, date):
            return {"final_trade_decision": "**Side**: Flat"}, None

    monkeypatch.setattr(tg, "TradingAgentsGraph", FakeGraph)
    return seen


@pytest.mark.unit
class TestVenueIsPinned:
    def test_replay_runs_on_dryrun_even_when_the_environment_says_testnet(
            self, tmp_path, monkeypatch, archive, fake_graph):
        """The exact 2026-08-08 configuration: .env pins testnet, config asks dryrun."""
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")

        replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")

        assert fake_graph["constructed"]
        assert fake_graph["resolved_mode"] == "dryrun"
        assert fake_graph["env"] == "dryrun"

    def test_config_alone_is_not_enough_to_pin_the_venue(self):
        """Documents why the fix cannot live in the config: env outranks it."""
        cfg = {"futures_executor_mode": "dryrun"}
        os.environ["EXECUTOR_MODE"] = "testnet"
        try:
            assert resolve_executor_mode(cfg) == "testnet"
        finally:
            os.environ.pop("EXECUTOR_MODE", None)

    def test_replay_refuses_to_start_when_the_venue_cannot_be_pinned(
            self, tmp_path, monkeypatch, archive, fake_graph):
        """A venue misconfiguration is global, not per-window: fail loudly.

        ``replay`` swallows per-window exceptions into ``result.error`` so a
        bad window cannot sink a sweep. This one must escape that, or a
        sweep would quietly produce 72 rows while pointed at a live venue.
        """
        monkeypatch.setattr(replay_mod, "resolve_executor_mode", lambda cfg: "testnet")

        with pytest.raises(RuntimeError, match="dryrun"):
            replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")

        assert not fake_graph["constructed"], "graph must not run on an unpinned venue"


@pytest.mark.unit
class TestEnvironmentIsRestored:
    def test_previous_value_is_put_back(self, tmp_path, monkeypatch, archive, fake_graph):
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")
        replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")
        assert os.environ["EXECUTOR_MODE"] == "testnet"

    def test_an_unset_variable_stays_unset(self, tmp_path, monkeypatch, archive, fake_graph):
        """Restoring must remove the key, not write the string 'None' into it."""
        monkeypatch.delenv("EXECUTOR_MODE", raising=False)
        replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")
        assert "EXECUTOR_MODE" not in os.environ

    def test_restored_even_when_the_graph_raises(
            self, tmp_path, monkeypatch, archive, fake_graph):
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")
        fake_graph["raise_in_graph"] = True

        result = replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")

        assert result.error and "graph blew up" in result.error
        assert os.environ["EXECUTOR_MODE"] == "testnet"

    def test_restored_when_the_archive_has_no_bars(self, tmp_path, monkeypatch, fake_graph):
        """The early return path must not leak an overridden environment either."""
        monkeypatch.setenv("EXECUTOR_MODE", "testnet")
        monkeypatch.setattr(replay_mod.vision, "klines", lambda *a, **k: [])

        result = replay_mod.replay("BTC-USD", AS_OF, scratch=tmp_path / "s")

        assert result.error == "no archived bars at cutoff"
        assert os.environ["EXECUTOR_MODE"] == "testnet"
