"""Reddit vendor: urlopen mocked, symbol → query mapping verified."""

from __future__ import annotations

import json
import tempfile
import time
from unittest.mock import patch

import pytest

from tradingagents.dataflows import crypto_reddit as cr


@pytest.fixture
def tmp_root(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("MONOPOLY_DATA_ROOT", td)
        yield td


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self) -> bytes:
        return self._payload


def _payload(posts: list[dict]) -> bytes:
    return json.dumps({"data": {"children": [{"data": p} for p in posts]}}).encode()


@pytest.mark.unit
def test_get_reddit_success(tmp_root):
    now_epoch = time.time()
    posts = [{
        "id": "p1", "title": "BTC moon soon", "selftext": "Lambo incoming",
        "score": 1234, "num_comments": 56, "author": "cryptobro",
        "created_utc": now_epoch - 3600, "permalink": "/r/CryptoCurrency/p1", "url": "x",
    }]
    with patch.object(cr, "urlopen", return_value=_FakeResp(_payload(posts))), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):    # skip rate-limit sleeps
        out = cr.get_reddit(("BTC",), subreddits=("CryptoCurrency",), hours=24)
    assert "ok=True" in out
    assert "BTC moon soon" in out
    assert "u/cryptobro" in out


@pytest.mark.unit
def test_get_reddit_symbol_to_query_mapping():
    assert cr._symbol_query("BTC") == "Bitcoin OR BTC"
    assert cr._symbol_query("ETH") == "Ethereum OR ETH"
    # unknown symbol falls back to the symbol itself
    assert cr._symbol_query("FOOBAR") == "FOOBAR"


@pytest.mark.unit
def test_get_reddit_all_subs_empty_returns_ok_false(tmp_root):
    with patch.object(cr, "urlopen", return_value=_FakeResp(_payload([]))), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        out = cr.get_reddit(("BTC",), subreddits=("CryptoCurrency",), hours=24)
    assert "ok=False" in out
