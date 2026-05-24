"""News vendor: RSS parsing, dedup, symbol filtering."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.dataflows import crypto_news as cn


class _FakeEntry:
    def __init__(self, title: str, url: str, summary: str, ts_epoch: float):
        self._d = {
            "title": title, "link": url, "summary": summary,
            "published_parsed": time.gmtime(ts_epoch),
        }

    def get(self, k, default=None):
        return self._d.get(k, default)


class _FakeFeed:
    def __init__(self, entries):
        self.entries = entries


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


@pytest.mark.unit
def test_get_news_filters_by_symbol(tmp_root):
    now_epoch = time.time()
    entries = [
        _FakeEntry("Bitcoin hits new high", "https://coindesk.com/btc-1",
                   "BTC surged...", now_epoch - 3600),
        _FakeEntry("Ethereum upgrade lands", "https://coindesk.com/eth-1",
                   "ETH Pectra is live.", now_epoch - 7200),
        _FakeEntry("Random altcoin moves", "https://coindesk.com/alt-1",
                   "Some altcoin news.", now_epoch - 1800),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = cn.get_news(sources=("coindesk",), hours=24, symbols=("BTC", "ETH"))
    assert "Bitcoin hits new high" in out
    assert "Ethereum upgrade lands" in out
    assert "altcoin" not in out
    assert "Articles: 2" in out


@pytest.mark.unit
def test_get_news_dedup_across_runs(tmp_root):
    now_epoch = time.time()
    entries = [
        _FakeEntry("Same article", "https://coindesk.com/same",
                   "x", now_epoch - 600),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        cn.get_news(sources=("coindesk",), hours=24, symbols=None)
        cn.get_news(sources=("coindesk",), hours=24, symbols=None)
    # Second call should not double-insert; cache.read_news returns 1 row.
    from tradingagents.dataflows import crypto_cache as cache
    rows = cache.read_news(since_ms=int((now_epoch - 3600) * 1000))
    assert len(rows) == 1


@pytest.mark.unit
def test_get_news_all_sources_fail_returns_ok_false(tmp_root):
    with patch.object(cn.feedparser, "parse",
                      side_effect=RuntimeError("network down")):
        out = cn.get_news(sources=("coindesk",), hours=24, symbols=None)
    assert "ok=False" in out
    assert "network down" in out
