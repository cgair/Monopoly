"""Verify market-data tools normalise BASE-USD → BASEUSDT before dispatching."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.agents.utils import core_market_tools as cmt
from tradingagents.agents.utils import technical_indicators_tools as tit
from tradingagents.dataflows import crypto_binance as cb


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


def _fake_klines(n: int = 3) -> list:
    now = int(time.time() * 1000)
    return [[
        now - (n - i) * 3_600_000, "50000", "50500", "49500", "50200",
        "100", now - (n - i - 1) * 3_600_000 - 1, "5000000", 100,
        "50", "2500000", "0",
    ] for i in range(n)]


@pytest.mark.unit
def test_get_market_data_normalises_symbol(tmp_root):
    with patch.object(cb, "_http_get", return_value=_fake_klines(3)) as mock:
        out = cmt.get_market_data.invoke({"symbol": "BTC-USD", "interval": "1h", "limit": 3})
    # The underlying Binance call must see BTCUSDT, never BTC-USD
    call_kwargs = mock.call_args.kwargs if mock.call_args.kwargs else mock.call_args[1] if len(mock.call_args) > 1 else {}
    call_args = mock.call_args.args if mock.call_args.args else mock.call_args[0]
    # _http_get is called with (path, params); params['symbol'] should be BTCUSDT
    params = call_args[1] if len(call_args) >= 2 else call_kwargs.get("params", {})
    assert params.get("symbol") == "BTCUSDT"
    # The output header reflects the Binance form
    assert "BTCUSDT" in out
    assert "BTC-USD" not in out   # the data-layer prompt should not leak agent-side symbol


@pytest.mark.unit
def test_get_funding_rate_normalises_symbol(tmp_root):
    now = int(time.time() * 1000)
    payload = [{"symbol": "ETHUSDT", "fundingTime": now, "fundingRate": "0.0001"}]
    with patch.object(cb, "_http_get", return_value=payload) as mock:
        out = cmt.get_funding_rate.invoke({"symbol": "ETH-USD", "limit": 1})
    call_args = mock.call_args.args
    assert call_args[1]["symbol"] == "ETHUSDT"
    assert "ETHUSDT" in out


@pytest.mark.unit
def test_get_open_interest_normalises_symbol(tmp_root):
    now = int(time.time() * 1000)
    payload = [{"timestamp": now, "sumOpenInterest": "100000"}]
    with patch.object(cb, "_http_get", return_value=payload) as mock:
        out = cmt.get_open_interest.invoke({"symbol": "btc-usd", "interval": "1h", "limit": 1})
    assert mock.call_args.args[1]["symbol"] == "BTCUSDT"
    assert "BTCUSDT" in out


@pytest.mark.unit
def test_get_indicators_normalises_symbol(tmp_root):
    now = int(time.time() * 1000)
    klines = [[now - (250-i)*3_600_000, str(100+i*0.5), str(102+i*0.5),
               str(99+i*0.5), str(101+i*0.5), str(1000+i),
               now - (249-i)*3_600_000 - 1, str((101+i*0.5)*1000),
               100, "50", "60000", "0"] for i in range(250)]
    with patch.object(cb, "_http_get", return_value=klines) as mock:
        out = tit.get_indicators.invoke({
            "symbol": "BTC-USD", "indicator": "rsi", "interval": "1h", "look_back_bars": 5,
        })
    assert mock.call_args.args[1]["symbol"] == "BTCUSDT"
    assert "rsi" in out


@pytest.mark.unit
def test_get_indicators_multi_value_string(tmp_root):
    now = int(time.time() * 1000)
    klines = [[now - (250-i)*3_600_000, str(100+i*0.5), str(102+i*0.5),
               str(99+i*0.5), str(101+i*0.5), str(1000+i),
               now - (249-i)*3_600_000 - 1, str((101+i*0.5)*1000),
               100, "50", "60000", "0"] for i in range(250)]
    with patch.object(cb, "_http_get", return_value=klines):
        out = tit.get_indicators.invoke({
            "symbol": "BTC-USD", "indicator": "macd, close_50_sma", "interval": "1h",
            "look_back_bars": 5,
        })
    assert "macd" in out
    assert "close_50_sma" in out


@pytest.mark.unit
def test_indicator_unknown_name_does_not_crash(tmp_root):
    out = tit.get_indicators.invoke({
        "symbol": "BTC-USD", "indicator": "bogus_xyz", "interval": "1h",
    })
    assert "not supported" in out or "unsupported" in out
