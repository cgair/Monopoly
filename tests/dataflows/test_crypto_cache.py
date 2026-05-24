"""Round-trip + dedup tests for the crypto disk cache layer."""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from tradingagents.dataflows import crypto_cache as cache
from tradingagents.dataflows.crypto_types import (
    Bar,
    FundingRate,
    Liquidation,
    LongShortRatio,
    NewsItem,
    OpenInterest,
    SocialPost,
)


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


@pytest.fixture
def now() -> int:
    return int(time.time() * 1000)


@pytest.mark.unit
def test_bars_round_trip_and_idempotent_upsert(tmp_root, now):
    bars = [
        Bar(symbol="BTCUSDT", interval="1h",
            open_time=now + i * 3_600_000,
            close_time=now + (i + 1) * 3_600_000 - 1,
            open=1.0 + i, high=2.0 + i, low=0.5 + i, close=1.5 + i,
            volume=100, quote_volume=150, trades=5)
        for i in range(3)
    ]
    assert cache.write_bars(bars) == 3
    # second write of same payload must not duplicate
    cache.write_bars(bars)
    out = cache.read_bars("BTCUSDT", "1h", start_ms=now, end_ms=now + 10 * 3_600_000)
    assert len(out) == 3
    assert out[0].open == 1.0


@pytest.mark.unit
def test_funding_round_trip(tmp_root, now):
    rates = [FundingRate(symbol="BTCUSDT", funding_time=now + i * 8 * 3_600_000,
                         rate=0.0001 * (i + 1)) for i in range(2)]
    cache.write_funding(rates)
    out = cache.read_funding("BTCUSDT", start_ms=now, end_ms=now + 20 * 3_600_000)
    assert len(out) == 2
    assert out[1].rate == pytest.approx(0.0002)


@pytest.mark.unit
def test_oi_round_trip(tmp_root, now):
    items = [OpenInterest(symbol="BTCUSDT", interval="1h",
                          timestamp=now + i * 3_600_000, oi=1000.0 + i)
             for i in range(2)]
    cache.write_oi(items)
    out = cache.read_oi("BTCUSDT", "1h", start_ms=now, end_ms=now + 10 * 3_600_000)
    assert len(out) == 2


@pytest.mark.unit
def test_news_dedup(tmp_root, now):
    news = [
        NewsItem(id="a", timestamp=now, source="coindesk", title="t1", summary="s1", url="u1"),
        NewsItem(id="b", timestamp=now + 1, source="cointelegraph", title="t2", summary="s2", url="u2"),
    ]
    cache.upsert_news(news)
    cache.upsert_news(news)  # idempotent
    out = cache.read_news(since_ms=now - 1000)
    assert len(out) == 2


@pytest.mark.unit
def test_liquidations_and_long_short(tmp_root, now):
    cache.upsert_liquidations([
        Liquidation(id="L1", symbol="BTCUSDT", timestamp=now,
                    side="long", qty=1.0, price=50000.0),
    ])
    assert len(cache.read_liquidations(since_ms=now - 1000)) == 1

    cache.upsert_long_short([
        LongShortRatio(id="LS1", symbol="BTCUSDT", interval="15m",
                       timestamp=now, long_account=0.6, short_account=0.4, ratio=1.5),
    ])
    out = cache.read_long_short(since_ms=now - 1000)
    assert len(out) == 1 and out[0].ratio == 1.5


@pytest.mark.unit
def test_social_round_trip_with_engagement_json(tmp_root, now):
    cache.upsert_social([
        SocialPost(id="S1", timestamp=now, platform="reddit", author="u",
                   text="hi", engagement={"score": 10, "comments": 3}, url="/r/foo"),
    ])
    out = cache.read_social(since_ms=now - 1000)
    assert len(out) == 1
    assert out[0].engagement == {"score": 10, "comments": 3}


@pytest.mark.unit
def test_meta_and_freshness(tmp_root):
    assert cache.get_meta("never_set") is None
    cache.set_meta("recent")
    assert cache.is_fresh("recent", ttl_sec=60)
    cache.set_meta("stale", fetched_at_ms=1000)
    assert not cache.is_fresh("stale", ttl_sec=60)
