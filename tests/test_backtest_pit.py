"""Point-in-time replay must never show data from after its cutoff.

These are the load-bearing tests of the backtest: a replay that quietly
leaks future data produces confident, meaningless results, which is
worse than no backtest at all. Archive access is stubbed so the suite
stays offline.
"""

from __future__ import annotations

import pytest

from tradingagents.backtest import pit, vision
from tradingagents.dataflows import crypto_binance as cb
from tradingagents.dataflows.crypto_types import Bar, FundingRate

HOUR = 3_600_000
AS_OF = 1_769_688_000_000          # 2026-01-29 12:00:00 UTC


def _bar(open_time: int) -> Bar:
    return Bar(symbol="BTCUSDT", interval="1h", open_time=open_time,
               close_time=open_time + HOUR - 1, open=100.0, high=110.0,
               low=90.0, close=105.0, volume=1.0, quote_volume=100.0, trades=10)


@pytest.fixture
def stub_archive(monkeypatch):
    """Serve synthetic bars/metrics/funding spanning both sides of the cutoff."""
    bars = [_bar(AS_OF + i * HOUR) for i in range(-10, 6)]
    metrics = [
        vision.MetricRow(timestamp=AS_OF + i * HOUR, open_interest=1000.0 + i,
                         open_interest_value=1e6, account_ratio=2.0, top_ratio=1.8,
                         top_account_ratio=1.9, taker_vol_ratio=1.1)
        for i in range(-10, 6)
    ]
    funding = [FundingRate(symbol="BTCUSDT", funding_time=AS_OF + i * 8 * HOUR, rate=1e-4)
               for i in range(-5, 3)]
    taker = {AS_OF + i * HOUR: (60.0, 100.0) for i in range(-10, 6)}

    monkeypatch.setattr(vision, "klines", lambda *a, **k: list(bars))
    monkeypatch.setattr(vision, "metrics", lambda *a, **k: list(metrics))
    monkeypatch.setattr(vision, "funding", lambda *a, **k: list(funding))
    monkeypatch.setattr(vision, "taker_buy_volume", lambda *a, **k: dict(taker))
    return bars


def test_only_bars_closed_at_the_cutoff_are_visible(stub_archive):
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        resp = cb._get_ohlcv("BTCUSDT", "1h", limit=50)
    assert resp.data, "expected some history before the cutoff"
    assert all(b.close_time <= AS_OF for b in resp.data)
    # The bar still forming at the cutoff has not happened yet.
    assert max(b.open_time for b in resp.data) < AS_OF


def test_funding_and_metrics_respect_the_cutoff(stub_archive):
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        funding = cb._get_funding_rate("BTCUSDT", limit=50)
        oi = cb._get_open_interest("BTCUSDT", "1h", limit=50)
        ls = cb._get_long_short_ratio("BTCUSDT", "1h", limit=50)
    assert all(f.funding_time <= AS_OF for f in funding.data)
    assert all(x.timestamp <= AS_OF for x in oi.data)
    assert all(x.timestamp <= AS_OF for x in ls.data)
    assert oi.data and ls.data and funding.data


def test_long_short_shares_are_recovered_from_the_ratio(stub_archive):
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        ls = cb._get_long_short_ratio("BTCUSDT", "1h", limit=5)
    sample = ls.data[-1]
    assert sample.ratio == pytest.approx(2.0)
    assert sample.long_account == pytest.approx(2 / 3, abs=1e-3)
    assert sample.short_account == pytest.approx(1 / 3, abs=1e-3)
    assert sample.long_account + sample.short_account == pytest.approx(1.0, abs=1e-3)


def test_live_http_is_blocked_during_replay(stub_archive):
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        with pytest.raises(pit.LookAheadError):
            cb._http_get("/fapi/v1/klines", {"symbol": "BTCUSDT"})


def test_originals_are_restored_even_when_the_body_raises(stub_archive):
    before = (cb._get_ohlcv, cb._http_get, cb._aux_ratio_lines)
    with pytest.raises(ValueError):
        with pit.point_in_time(AS_OF, "BTCUSDT"):
            raise ValueError("boom")
    assert (cb._get_ohlcv, cb._http_get, cb._aux_ratio_lines) == before


def test_fetchers_refuse_to_run_outside_a_replay(stub_archive):
    with pytest.raises(pit.LookAheadError):
        pit._pit_ohlcv("BTCUSDT", "1h", limit=10)


def test_replay_contexts_cannot_nest(stub_archive):
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        with pytest.raises(RuntimeError):
            with pit.point_in_time(AS_OF + HOUR, "BTCUSDT"):
                pass


def test_prompt_text_carries_no_post_cutoff_timestamp(stub_archive):
    """The formatted report is what the analyst actually reads."""
    import re
    from datetime import datetime, timezone

    cutoff = datetime.fromtimestamp(AS_OF / 1000, timezone.utc)
    with pit.point_in_time(AS_OF, "BTCUSDT"):
        text = cb.get_ohlcv("BTCUSDT", "1h", 20) + "\n" + cb.get_long_short_ratio("BTCUSDT", "1h", 20)

    stamps = re.findall(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?", text)
    assert stamps, "expected timestamps in the rendered report"
    for raw in stamps:
        parsed = datetime.fromisoformat(raw.replace(" ", "T")).replace(tzinfo=timezone.utc)
        assert parsed <= cutoff, f"{raw} is after the cutoff"
