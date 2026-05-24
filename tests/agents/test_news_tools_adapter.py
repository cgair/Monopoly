"""News tool adapter: legacy (ticker, start, end) signature → crypto vendor."""

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
def test_get_news_adapter_routes_to_crypto_news(tmp_root):
    entries = [
        _FakeEntry("Bitcoin breaks resistance", "https://coindesk.com/btc-r",
                   "BTC surged...", time.time() - 3600),
        _FakeEntry("Random altcoin moves", "https://coindesk.com/alt-1",
                   "Some altcoin.", time.time() - 1800),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_news.invoke({
            "ticker": "BTC-USD", "start_date": "2026-05-17", "end_date": "2026-05-24",
        })
    assert "Crypto news" in out
    assert "Bitcoin breaks resistance" in out
    # symbol filter for BTC keeps Bitcoin headline, drops generic altcoin
    assert "altcoin" not in out


@pytest.mark.unit
def test_get_news_adapter_passes_through_unknown_ticker(tmp_root):
    # unknown ticker shape → symbols=None → no filter, all entries returned
    entries = [
        _FakeEntry("Altcoin headline", "https://coindesk.com/alt", "x", time.time() - 600),
    ]
    with patch.object(cn.feedparser, "parse", return_value=_FakeFeed(entries)):
        out = ndt.get_news.invoke({
            "ticker": "WEIRD-FOO", "start_date": "2026-05-23", "end_date": "2026-05-24",
        })
    assert "Altcoin headline" in out


@pytest.mark.unit
def test_get_global_news_stub_returns_placeholder():
    out = ndt.get_global_news.invoke({"curr_date": "2026-05-24"})
    assert "not yet available" in out
    assert "no global news" in out


@pytest.mark.unit
def test_get_insider_transactions_stub_returns_placeholder():
    out = ndt.get_insider_transactions.invoke({"ticker": "BTC-USD"})
    assert "not applicable to crypto" in out
