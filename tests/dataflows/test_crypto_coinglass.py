"""Coinglass vendor: success path and missing-key degradation."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.dataflows import crypto_coinglass as cg


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        monkeypatch.setenv("COINGLASS_API_KEY", "test-key")
        yield td


@pytest.mark.unit
def test_get_liquidations_success(tmp_root):
    now = int(time.time() * 1000)
    payload = {"data": [
        {"createTime": now - 3_600_000, "side": "long",
         "baseVolume": "1.5", "price": "50000"},
        {"createTime": now - 1_800_000, "side": "short",
         "baseVolume": "2.0", "price": "50100"},
    ]}
    with patch.object(cg, "_http_get", return_value=payload):
        out = cg.get_liquidations("BTCUSDT", hours=24)
    assert "Coinglass liquidations" in out
    assert "long_liq_qty=1.5000" in out
    assert "short_liq_qty=2.0000" in out
    assert "ok=True" in out


@pytest.mark.unit
def test_get_long_short_ratio_success(tmp_root):
    now = int(time.time() * 1000)
    payload = {"data": [
        {"createTime": now - i * 3_600_000,
         "longAccount": 0.55, "shortAccount": 0.45, "longShortRatio": 1.22}
        for i in range(3)
    ]}
    with patch.object(cg, "_http_get", return_value=payload):
        out = cg.get_long_short_ratio("BTCUSDT", "1h", limit=3)
    assert "long/short account ratio" in out
    assert "ok=True" in out


@pytest.mark.unit
def test_missing_api_key_degrades_gracefully(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
        out = cg.get_liquidations("BTCUSDT", hours=24)
        assert "ok=False" in out
        assert "COINGLASS_API_KEY" in out
        # no exception should escape; the prompt is rendered with a clear note
