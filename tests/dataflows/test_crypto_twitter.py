"""Twitter vendor: newsflash-relay implementation behind the stable contract."""

import time
from unittest.mock import patch

import pytest

from tradingagents.dataflows import crypto_twitter as ct
from tradingagents.dataflows.crypto_types import Meta, NewsItem, Response


def _news_item(idx: int, title: str, summary: str = "") -> NewsItem:
    now = int(time.time() * 1000)
    return NewsItem(
        id=f"item{idx}",
        timestamp=now - idx * 3_600_000,
        source="blockbeats",
        title=title,
        summary=summary,
        url=f"https://theblockbeats.news/flash/{idx}",
    )


def _news_response(items, *, ok=True, note=None) -> Response[NewsItem]:
    return Response(data=items, meta=Meta(
        fetched_at=int(time.time() * 1000), vendor="blockbeats",
        ok=ok, note=note,
    ))


@pytest.mark.unit
def test_get_tweets_prefers_symbol_specific_x_relay_items():
    items = [
        _news_item(1, "Arthur Hayes tweeted that BTC will rip"),
        _news_item(2, "BTC ETF sees $200M inflow"),
        _news_item(3, "Saylor posted on X: buying more Bitcoin"),
        _news_item(4, "Vitalik tweeted about ETH scaling"),
    ]
    with patch.object(ct.crypto_news, "_get_news", return_value=_news_response(items)):
        resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.meta.ok is True
    assert resp.meta.vendor == "twitter"
    assert [p.id for p in resp.data] == ["item1", "item3"]
    assert all(p.platform == "twitter" for p in resp.data)
    assert all(p.engagement == {} for p in resp.data)


@pytest.mark.unit
def test_get_tweets_widens_to_marketwide_x_relays():
    items = [
        _news_item(1, "Vitalik tweeted about ETH scaling"),
        _news_item(2, "Some exchange listed a new token"),
    ]
    with patch.object(ct.crypto_news, "_get_news", return_value=_news_response(items)):
        resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.meta.ok is True
    assert [p.id for p in resp.data] == ["item1"]
    assert "market-wide X relays" in (resp.meta.note or "")


@pytest.mark.unit
def test_get_tweets_falls_back_to_symbol_newsflash_with_note():
    items = [
        _news_item(1, "BTC ETF sees $200M inflow"),
        _news_item(2, "Some exchange listed a new token"),
    ]
    with patch.object(ct.crypto_news, "_get_news", return_value=_news_response(items)):
        resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.meta.ok is True
    assert [p.id for p in resp.data] == ["item1"]
    assert "weak proxy" in (resp.meta.note or "")


@pytest.mark.unit
def test_get_tweets_falls_back_to_general_newsflash_with_note():
    items = [_news_item(1, "Some exchange listed a new token")]
    with patch.object(ct.crypto_news, "_get_news", return_value=_news_response(items)):
        resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.meta.ok is True
    assert len(resp.data) == 1
    assert "general newsflash items as weak proxy" in (resp.meta.note or "")


@pytest.mark.unit
def test_get_tweets_degrades_when_relay_unavailable():
    with patch.object(ct.crypto_news, "_get_news",
                      return_value=_news_response([], ok=False, note="feed down")):
        resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.data == []
    assert resp.meta.ok is False
    assert resp.meta.vendor == "twitter"
    assert "data gap" in (resp.meta.note or "")


@pytest.mark.unit
def test_get_tweets_prompt_format():
    items = [_news_item(1, "Vitalik tweeted about ETH scaling", "Details inside.")]
    with patch.object(ct.crypto_news, "_get_news", return_value=_news_response(items)):
        out = ct.get_tweets(("ETH",), hours=24)
    assert "editorial relay" in out
    assert "ok=True" in out
    assert "Vitalik tweeted about ETH scaling" in out
    assert "blockbeats-newsflash" in out


@pytest.mark.unit
def test_get_tweets_prompt_degraded_format():
    with patch.object(ct.crypto_news, "_get_news",
                      return_value=_news_response([], ok=False, note="feed down")):
        out = ct.get_tweets(("BTC",), hours=24)
    assert "ok=False" in out
    assert "no X/Twitter relay data" in out
