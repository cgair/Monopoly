"""Binance vendor: success path, cache hit, and stale fallback."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest
import requests

from tradingagents.dataflows import crypto_binance as cb
from tradingagents.dataflows import crypto_cache as cache


@pytest.fixture
def tmp_root(monkeypatch):
    cb._AUX_CACHE.clear()
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


def _fake_klines(now: int, n: int = 3) -> list:
    return [[now - (n - i) * 3_600_000, "50000.0", "50500.0", "49500.0", "50200.0",
             "1234.5", now - (n - i - 1) * 3_600_000 - 1, "61750000", 9876,
             "600", "30000000", "0"] for i in range(n)]


@pytest.mark.unit
def test_get_ohlcv_success(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", return_value=_fake_klines(now, 3)):
        out = cb.get_ohlcv("BTCUSDT", "1h", limit=3)
    assert "Binance Futures OHLCV" in out
    assert "ok=True" in out
    assert "stale=False" in out
    assert out.count("\n") >= 5    # header + 3 rows


@pytest.mark.unit
def test_get_ohlcv_uses_cache_on_repeat(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", return_value=_fake_klines(now, 3)) as mock:
        cb.get_ohlcv("BTCUSDT", "1h", limit=3)
        first_calls = mock.call_count
        cb.get_ohlcv("BTCUSDT", "1h", limit=3)
        assert mock.call_count == first_calls, "second call should hit cache, not http"


@pytest.mark.unit
def test_get_ohlcv_degrades_to_stale_on_network_failure(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", return_value=_fake_klines(now, 3)):
        cb.get_ohlcv("BTCUSDT", "1h", limit=3)   # warm cache

    # Force a re-fetch by clearing the freshness marker, then fail HTTP.
    with patch.object(cache, "is_fresh", return_value=False), \
         patch.object(cb, "_http_get", side_effect=requests.ConnectionError("simulated")):
        resp = cb._get_ohlcv("BTCUSDT", "1h", limit=3)
        assert resp.meta.is_stale is True
        assert resp.meta.ok is True
        assert resp.meta.note and "simulated" in resp.meta.note
        assert len(resp.data) == 3


@pytest.mark.unit
def test_get_ohlcv_returns_ok_false_when_no_cache_and_fetch_fails(tmp_root):
    with patch.object(cb, "_http_get", side_effect=requests.ConnectionError("down")):
        resp = cb._get_ohlcv("ETHUSDT", "1h", limit=3)
    assert resp.meta.ok is False
    assert resp.data == []


@pytest.mark.unit
def test_get_funding_rate_success(tmp_root):
    now = int(time.time() * 1000)
    payload = [{"symbol": "BTCUSDT", "fundingTime": now - i * 8 * 3_600_000,
                "fundingRate": str(0.0001 * (i + 1))} for i in range(3)]
    with patch.object(cb, "_http_get", return_value=payload):
        out = cb.get_funding_rate("BTCUSDT", limit=3)
    assert "funding rate" in out
    assert "ok=True" in out


@pytest.mark.unit
def test_get_open_interest_success(tmp_root):
    now = int(time.time() * 1000)
    payload = [{"timestamp": now - i * 3_600_000,
                "sumOpenInterest": str(100000.0 + i)} for i in range(3)]
    with patch.object(cb, "_http_get", return_value=payload):
        out = cb.get_open_interest("BTCUSDT", "1h", limit=3)
    assert "open interest" in out
    assert "ok=True" in out


# ---------------------------------------------------------------------------
# Long/short ratio (free /futures/data endpoints)
# ---------------------------------------------------------------------------

def _fake_ls_rows(now: int, n: int = 3) -> list:
    return [{"symbol": "BTCUSDT",
             "longAccount": str(0.60 + i * 0.01),
             "shortAccount": str(0.40 - i * 0.01),
             "longShortRatio": str((0.60 + i * 0.01) / (0.40 - i * 0.01)),
             "timestamp": now - (n - i) * 3_600_000} for i in range(n)]


def _fake_taker_rows(now: int, n: int = 2) -> list:
    return [{"buySellRatio": "1.2345", "buyVol": "500.0", "sellVol": "405.0",
             "timestamp": now - (n - i) * 3_600_000} for i in range(n)]


def _ls_router(now: int, *, fail_aux: bool = False):
    """Dispatch mock: route by endpoint path like the real API would."""
    def _get(path, params):
        if path == "/futures/data/globalLongShortAccountRatio":
            return _fake_ls_rows(now, 3)
        if fail_aux:
            raise requests.ConnectionError("aux down")
        if path == "/futures/data/topLongShortPositionRatio":
            return _fake_ls_rows(now, 2)
        if path == "/futures/data/takerlongshortRatio":
            return _fake_taker_rows(now, 2)
        raise AssertionError(f"unexpected path {path}")
    return _get


@pytest.mark.unit
def test_get_long_short_ratio_success(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", side_effect=_ls_router(now)):
        out = cb.get_long_short_ratio("BTCUSDT", "1h", limit=3)
    assert "long/short positioning" in out
    assert "ok=True" in out
    assert "Global long/short account ratio" in out
    assert "Top-trader long/short position ratio" in out
    assert "Taker buy/sell volume ratio" in out
    assert "1.2345" in out


@pytest.mark.unit
def test_get_long_short_ratio_global_cached_on_repeat(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", side_effect=_ls_router(now)) as mock:
        cb._get_long_short_ratio("BTCUSDT", "1h", limit=3)
        first = mock.call_count
        cb._get_long_short_ratio("BTCUSDT", "1h", limit=3)
        assert mock.call_count == first, "second global fetch should hit cache"


@pytest.mark.unit
def test_get_long_short_ratio_aux_failure_degrades_gracefully(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", side_effect=_ls_router(now, fail_aux=True)):
        out = cb.get_long_short_ratio("BTCUSDT", "1h", limit=3)
    # Global table survives; aux sections carry an explanatory note.
    assert "Global long/short account ratio" in out
    assert "ok=True" in out
    assert out.count("fetch failed") == 2


@pytest.mark.unit
def test_get_long_short_ratio_stale_fallback(tmp_root):
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", side_effect=_ls_router(now)):
        cb._get_long_short_ratio("BTCUSDT", "1h", limit=3)   # warm cache

    with patch.object(cache, "is_fresh", return_value=False), \
         patch.object(cb, "_http_get", side_effect=requests.ConnectionError("simulated")):
        resp = cb._get_long_short_ratio("BTCUSDT", "1h", limit=3)
    assert resp.meta.is_stale is True
    assert resp.meta.ok is True
    assert len(resp.data) == 3


@pytest.mark.unit
def test_get_long_short_ratio_ok_false_when_no_cache_and_fetch_fails(tmp_root):
    with patch.object(cb, "_http_get", side_effect=requests.ConnectionError("down")):
        resp = cb._get_long_short_ratio("ETHUSDT", "1h", limit=3)
    assert resp.meta.ok is False
    assert resp.data == []


@pytest.mark.unit
def test_route_to_vendor_prefers_binance_for_long_short(tmp_root):
    from tradingagents.dataflows import interface
    now = int(time.time() * 1000)
    with patch.object(cb, "_http_get", side_effect=_ls_router(now)):
        out = interface.route_to_vendor("get_long_short_ratio", "BTCUSDT", "1h", 3)
    assert "Binance Futures long/short positioning" in out
