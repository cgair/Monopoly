"""News tool wrappers: crypto-native signatures (symbols, hours, sources).

The Week-3 rewrite dropped the legacy (ticker, start, end) adapter. These
tests pin the new tool surface:
  * ``get_news`` takes ``symbols`` + ``hours`` and applies symbol filter.
  * ``get_global_news`` takes ``hours`` only and returns unfiltered news
    from the same RSS feeds (no longer a "not available" placeholder).
  * ``get_insider_transactions`` stays as a stock-mode placeholder.
"""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.agents.utils import news_data_tools as ndt
from tradingagents.dataflows import crypto_news as cn


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


class _FakeEntry:
    def __init__(self, title: str, url: str, summary: str, ts: float):
        self._d = {
            "title": title, "link": url, "summary": summary,
            "published_parsed": time.gmtime(ts),
        }

    def get(self, k, default=None):
        return self._d.get(k, default)


class _FakeFeed:
    def __init__(self, entries):
        self.entries = entries


@pytest.mark.unit
def test_get_news_filters_by_symbol(tmp_root):
    entries = [
        _FakeEntry("Bitcoin breaks resistance", "https://coindesk.com/btc-r",
                   "BTC surged...", time.time() - 3600),
        _FakeEntry("Random altcoin moves", "https://coindesk.com/alt-1",
                   "Some altcoin.", time.time() - 1800),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_news.invoke({
            "symbols": ["BTC"],
            "hours": 24,
        })
    assert "Crypto news" in out
    assert "Bitcoin breaks resistance" in out
    # symbol filter for BTC keeps Bitcoin headline, drops generic altcoin
    assert "altcoin" not in out


@pytest.mark.unit
def test_get_news_normalizes_symbols_case(tmp_root):
    entries = [
        _FakeEntry("Ethereum upgrade lands", "https://coindesk.com/eth-1",
                   "Ether merge moves forward.", time.time() - 1200),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        # lowercase + whitespace should be normalized to ETH
        out = ndt.get_news.invoke({"symbols": [" eth "], "hours": 24})
    assert "Ethereum upgrade lands" in out


@pytest.mark.unit
def test_get_news_respects_custom_sources(tmp_root):
    entries = [
        _FakeEntry("CoinDesk-only headline", "https://coindesk.com/x",
                   "BTC ...", time.time() - 600),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_news.invoke({
            "symbols": ["BTC"],
            "hours": 24,
            "sources": ["coindesk"],
        })
    assert "sources: coindesk" in out
    # cointelegraph should not appear in the header when only coindesk is requested
    assert "cointelegraph" not in out.lower()


@pytest.mark.unit
def test_get_global_news_returns_unfiltered_articles(tmp_root):
    # An altcoin-only article should appear in global news (no symbol filter)
    entries = [
        _FakeEntry("Generic altcoin headline", "https://coindesk.com/alt-99",
                   "Some altcoin moves.", time.time() - 900),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_global_news.invoke({"hours": 48})
    assert "Crypto news" in out
    assert "Generic altcoin headline" in out
    # Header should show ALL symbols (no filter applied)
    assert "ALL" in out


@pytest.mark.unit
def test_get_global_news_default_hours(tmp_root):
    entries = [
        _FakeEntry("Recent crypto headline", "https://coindesk.com/r1",
                   "Industry news.", time.time() - 3600),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_global_news.invoke({})
    # default hours=48 should be reflected in header
    assert "last 48h" in out
