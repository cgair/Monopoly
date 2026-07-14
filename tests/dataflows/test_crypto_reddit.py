"""Reddit vendor: urlopen mocked, symbol → query mapping verified.

Covers:
- Happy path: successful fetch from primary endpoint
- HTTP 403: retry with exponential backoff, fallback to old.reddit
- HTTP 429: rate limit handling
- Network errors: fallback chain
- All failures: graceful degradation with data_gap marker
"""

from __future__ import annotations

import json
import tempfile
import time
from unittest.mock import MagicMock, patch, call

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


@pytest.mark.unit
def test_get_reddit_ua_follows_reddit_format():
    """User-Agent should follow Reddit's platform:app:version (by /u/username) format."""
    ua = cr._UA
    assert ":" in ua
    assert "(by /u/" in ua
    assert "monopoly" in ua.lower()


@pytest.mark.unit
def test_fetch_subreddit_with_retry_primary_success(tmp_root):
    """Primary endpoint returns posts immediately on first try."""
    now_epoch = time.time()
    posts = [{
        "id": "p1", "title": "BTC moon soon", "selftext": "",
        "score": 100, "num_comments": 5, "author": "cryptobro",
        "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x",
    }]

    with patch.object(cr, "urlopen", return_value=_FakeResp(_payload(posts))), \
         patch.object(cr.time, "sleep") as mock_sleep:
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert len(result) == 1
    assert result[0]["title"] == "BTC moon soon"
    mock_sleep.assert_not_called()  # no retries needed


@pytest.mark.unit
def test_fetch_subreddit_with_retry_http_403_then_fallback_succeeds(tmp_root):
    """HTTP 403 on primary → exponential backoff → fallback endpoint succeeds."""
    from urllib.error import HTTPError

    now_epoch = time.time()
    posts = [{
        "id": "p2", "title": "ETH strong", "selftext": "",
        "score": 50, "num_comments": 3, "author": "ethfan",
        "created_utc": now_epoch, "permalink": "/r/ethfinance/p2", "url": "x",
    }]

    # First call to primary: 403, then second (retry): 403, then fallback succeeds
    side_effects = [
        HTTPError("url", 403, "Forbidden", {}, None),  # primary attempt 1
        HTTPError("url", 403, "Forbidden", {}, None),  # primary attempt 2 (retry)
        _FakeResp(_payload(posts)),  # fallback succeeds
    ]

    with patch.object(cr, "urlopen", side_effect=side_effects), \
         patch.object(cr.time, "sleep") as mock_sleep:
        result, success = cr._fetch_subreddit_with_retry("ETH", "ethfinance", 10, 5.0, max_retries=2)

    assert success is True
    assert len(result) == 1
    assert result[0]["title"] == "ETH strong"
    # Should sleep twice (retry backoff on primary)
    assert mock_sleep.call_count == 2


@pytest.mark.unit
def test_fetch_subreddit_with_retry_all_endpoints_fail_returns_empty(tmp_root):
    """All endpoints exhausted (403 on primary, 403 on fallback) → returns empty with success=False."""
    from urllib.error import HTTPError

    # With max_retries=2: 3 attempts (0,1,2) per endpoint × 2 endpoints = 6 calls total
    side_effects = [
        HTTPError("url", 403, "Forbidden", {}, None),  # primary attempt 0
        HTTPError("url", 403, "Forbidden", {}, None),  # primary attempt 1
        HTTPError("url", 403, "Forbidden", {}, None),  # primary attempt 2
        HTTPError("url", 403, "Forbidden", {}, None),  # fallback attempt 0
        HTTPError("url", 403, "Forbidden", {}, None),  # fallback attempt 1
        HTTPError("url", 403, "Forbidden", {}, None),  # fallback attempt 2
    ]

    with patch.object(cr, "urlopen", side_effect=side_effects), \
         patch.object(cr.time, "sleep") as mock_sleep:
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0, max_retries=2)

    assert success is False
    assert result == []
    # Should have slept 4 times (between retry attempts on primary and fallback)
    # Attempts: 0->1 (sleep), 1->2 (sleep), 2->next endpoint, 3->4 (sleep), 4->5 (sleep)
    assert mock_sleep.call_count == 4


@pytest.mark.unit
def test_fetch_subreddit_with_retry_429_rate_limit_backoff(tmp_root):
    """HTTP 429 (rate limit) → exponential backoff."""
    from urllib.error import HTTPError

    now_epoch = time.time()
    posts = [{"id": "p1", "title": "post", "selftext": "", "score": 1, "num_comments": 0,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/x/p1", "url": "x"}]

    side_effects = [
        HTTPError("url", 429, "Too Many Requests", {}, None),  # 429
        _FakeResp(_payload(posts)),  # retry succeeds
    ]

    with patch.object(cr, "urlopen", side_effect=side_effects), \
         patch.object(cr.time, "sleep") as mock_sleep:
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0, max_retries=2)

    assert success is True
    assert len(result) == 1
    # Should have slept once (1s backoff: 2^0 = 1)
    mock_sleep.assert_called_once()
    call_args = mock_sleep.call_args[0][0]
    assert call_args == 1  # First retry: 2^0 = 1


@pytest.mark.unit
def test_get_reddit_all_endpoints_fail_returns_data_gap_marker(tmp_root):
    """When all endpoints fail, response has ok=False and data_gap marker."""
    from urllib.error import HTTPError

    side_effects = [
        HTTPError("url", 403, "Forbidden", {}, None),
        HTTPError("url", 403, "Forbidden", {}, None),
    ] * 4  # 4 subs * 2 retries each

    with patch.object(cr, "urlopen", side_effect=side_effects), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        resp = cr._get_reddit(("BTC",), subreddits=("CryptoCurrency",), hours=24)

    assert resp.meta.ok is False
    assert "data_gap" in resp.meta.note
    assert resp.data == []


@pytest.mark.unit
def test_get_reddit_mixed_success_and_failure(tmp_root):
    """One sub fails, another succeeds → ok=True with data from successful sub."""
    from urllib.error import HTTPError

    now_epoch = time.time()
    posts = [{"id": "p1", "title": "BTC post", "selftext": "", "score": 100, "num_comments": 5,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x"}]

    # CryptoCurrency: 6 attempts (3 on primary, 3 on fallback), all fail
    # Bitcoin: 1 attempt on primary succeeds
    side_effects = [
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency primary 0
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency primary 1
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency primary 2
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency fallback 0
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency fallback 1
        HTTPError("url", 403, "Forbidden", {}, None),  # CryptoCurrency fallback 2
        _FakeResp(_payload(posts)),  # Bitcoin primary succeeds
    ]

    with patch.object(cr, "urlopen", side_effect=side_effects), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        resp = cr._get_reddit(("BTC",), subreddits=("CryptoCurrency", "Bitcoin"), hours=24)

    assert resp.meta.ok is True  # Had at least one success
    assert len(resp.data) == 1
    assert resp.data[0].text == "BTC post"


@pytest.mark.unit
def test_fetch_subreddit_legacy_interface_discards_success_flag(tmp_root):
    """Legacy _fetch_subreddit() hides success flag, returns posts list."""
    now_epoch = time.time()
    posts = [{"id": "p1", "title": "title", "selftext": "", "score": 1, "num_comments": 0,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/x/p1", "url": "x"}]

    with patch.object(cr, "urlopen", return_value=_FakeResp(_payload(posts))), \
         patch.object(cr.time, "sleep"):
        result = cr._fetch_subreddit("query", "sub", 10, 5.0)

    # Should return list without success flag
    assert isinstance(result, list)
    assert len(result) == 1
