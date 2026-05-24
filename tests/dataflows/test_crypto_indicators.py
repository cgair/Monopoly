"""Indicators on cached bars: stockstats wired through crypto_binance fetch."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.dataflows import crypto_binance as cb
from tradingagents.dataflows import crypto_indicators as ci


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


def _seed_klines(n: int = 250) -> list:
    """Monotonically rising close so indicators always have a defined value."""
    now = int(time.time() * 1000)
    return [[
        now - (n - i) * 3_600_000,
        str(100 + i * 0.5), str(102 + i * 0.5),
        str(99 + i * 0.5), str(101 + i * 0.5),
        str(1000 + i), now - (n - i - 1) * 3_600_000 - 1,
        str((101 + i * 0.5) * 1000), 100, "50", "60000", "0",
    ] for i in range(n)]


@pytest.mark.unit
def test_rsi_computes_and_returns_pairs(tmp_root):
    with patch.object(cb, "_http_get", return_value=_seed_klines(250)):
        resp = ci._get_indicators("BTCUSDT", "rsi", "1h", look_back_bars=10)
    assert resp.meta.ok is True
    assert len(resp.data) == 10
    # RSI on monotonically rising prices should saturate near 100
    last_ts, last_val = resp.data[-1]
    assert last_val == pytest.approx(100.0)


@pytest.mark.unit
def test_macd_and_sma_render_plaintext(tmp_root):
    with patch.object(cb, "_http_get", return_value=_seed_klines(250)):
        out_macd = ci.get_indicators("BTCUSDT", "macd", "1h", look_back_bars=5)
        out_sma = ci.get_indicators("BTCUSDT", "close_50_sma", "1h", look_back_bars=5)
    assert "macd" in out_macd
    assert "Bars covered: 5" in out_macd
    assert "close_50_sma" in out_sma
    assert "ok=True" in out_sma


@pytest.mark.unit
def test_unsupported_indicator_returns_clear_error(tmp_root):
    out = ci.get_indicators("BTCUSDT", "bogus_indicator", "1h")
    assert "ok=False" in out
    assert "unsupported indicator" in out


@pytest.mark.unit
def test_no_bars_returns_ok_false(tmp_root):
    import requests
    with patch.object(cb, "_http_get", side_effect=requests.ConnectionError("down")):
        resp = ci._get_indicators("ETHUSDT", "rsi", "1h", look_back_bars=10)
    assert resp.meta.ok is False
    assert resp.data == []


@pytest.mark.unit
def test_stale_bars_propagate_to_indicator_meta(tmp_root):
    # warm cache
    with patch.object(cb, "_http_get", return_value=_seed_klines(250)):
        cb._get_ohlcv("BTCUSDT", "1h", limit=200)

    # force stale fallback on the next fetch
    import requests
    from tradingagents.dataflows import crypto_cache as cache
    with patch.object(cache, "is_fresh", return_value=False), \
         patch.object(cb, "_http_get", side_effect=requests.ConnectionError("down")):
        resp = ci._get_indicators("BTCUSDT", "rsi", "1h", look_back_bars=5)
    assert resp.meta.is_stale is True
    assert resp.meta.ok is True
    assert len(resp.data) == 5
