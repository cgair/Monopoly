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


@pytest.fixture(autouse=True)
def _no_oauth_creds(monkeypatch):
    """Default to no OAuth credentials so public-endpoint tests stay deterministic."""
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    cr._invalidate_oauth_token()
    yield
    cr._invalidate_oauth_token()


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
    """All endpoints exhausted (403 everywhere, RSS included) → empty with success=False."""
    from urllib.error import HTTPError

    def always_403(req, timeout=None):
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with patch.object(cr, "urlopen", side_effect=always_403), \
         patch.object(cr.time, "sleep") as mock_sleep:
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0, max_retries=2)

    assert success is False
    assert result == []
    # 2 backoff sleeps between the 3 attempts on each of the 4 endpoints
    # (www json, old json, search.rss, new.rss)
    assert mock_sleep.call_count == 8


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

    def always_403(req, timeout=None):
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with patch.object(cr, "urlopen", side_effect=always_403), \
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

    # CryptoCurrency: every endpoint (json + rss) fails; Bitcoin succeeds on primary
    def fake_urlopen(req, timeout=None):
        if "CryptoCurrency" in req.full_url:
            raise HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return _FakeResp(_payload(posts))

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        resp = cr._get_reddit(("BTC",), subreddits=("CryptoCurrency", "Bitcoin"), hours=24)

    assert resp.meta.ok is True  # Had at least one success
    assert len(resp.data) == 1
    assert resp.data[0].text == "BTC post"


def _token_payload(token: str = "tok", expires_in: int = 3600) -> bytes:
    return json.dumps({"access_token": token, "expires_in": expires_in}).encode()


@pytest.mark.unit
def test_oauth_endpoint_used_when_creds_set(tmp_root, monkeypatch):
    """With REDDIT_CLIENT_ID/SECRET set, data is fetched from oauth.reddit.com with a bearer token."""
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")
    now_epoch = time.time()
    posts = [{"id": "p1", "title": "BTC via oauth", "selftext": "", "score": 9, "num_comments": 1,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x"}]
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if "api/v1/access_token" in req.full_url:
            return _FakeResp(_token_payload())
        return _FakeResp(_payload(posts))

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep"):
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert len(result) == 1
    # calls[0] = token request, calls[1] = data request against the OAuth host
    assert "api/v1/access_token" in calls[0].full_url
    assert "oauth.reddit.com" in calls[1].full_url
    assert calls[1].get_header("Authorization") == "bearer tok"


@pytest.mark.unit
def test_oauth_token_cached_across_calls(monkeypatch):
    """A cached, unexpired token is reused without a second HTTP round-trip."""
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")

    with patch.object(cr, "urlopen", return_value=_FakeResp(_token_payload())) as mock_open:
        first = cr._get_oauth_token()
        second = cr._get_oauth_token()

    assert first == second == "tok"
    assert mock_open.call_count == 1


@pytest.mark.unit
def test_oauth_token_failure_falls_back_to_public(tmp_root, monkeypatch):
    """Token endpoint failing must not break the fetch — public endpoint is used instead."""
    from urllib.error import HTTPError

    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")
    now_epoch = time.time()
    posts = [{"id": "p1", "title": "BTC via public", "selftext": "", "score": 1, "num_comments": 0,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x"}]
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if "api/v1/access_token" in req.full_url:
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        return _FakeResp(_payload(posts))

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep"):
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert result[0]["title"] == "BTC via public"
    assert "www.reddit.com" in calls[1].full_url


@pytest.mark.unit
def test_oauth_401_invalidates_token_and_refetches(tmp_root, monkeypatch):
    """A 401 from the OAuth data endpoint invalidates the cache and refetches the token."""
    from urllib.error import HTTPError

    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")
    now_epoch = time.time()
    posts = [{"id": "p1", "title": "BTC ok", "selftext": "", "score": 1, "num_comments": 0,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x"}]
    tokens = iter(["tok1", "tok2"])
    data_calls = []

    def fake_urlopen(req, timeout=None):
        if "api/v1/access_token" in req.full_url:
            return _FakeResp(_token_payload(next(tokens)))
        data_calls.append(req)
        if req.get_header("Authorization") == "bearer tok1":
            raise HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        return _FakeResp(_payload(posts))

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep"):
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert len(result) == 1
    assert [c.get_header("Authorization") for c in data_calls] == ["bearer tok1", "bearer tok2"]


@pytest.mark.unit
def test_no_creds_skips_oauth_endpoint(tmp_root):
    """Without credentials the first request goes straight to the public endpoint."""
    now_epoch = time.time()
    posts = [{"id": "p1", "title": "t", "selftext": "", "score": 1, "num_comments": 0,
              "author": "user", "created_utc": now_epoch, "permalink": "/r/Bitcoin/p1", "url": "x"}]
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _FakeResp(_payload(posts))

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep"):
        cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert "www.reddit.com" in calls[0].full_url


def _atom_payload(entries: list[dict]) -> bytes:
    items = "".join(
        f"""<entry>
  <id>{e['id']}</id>
  <title>{e['title']}</title>
  <author><name>/u/{e['author']}</name></author>
  <link href="{e['url']}"/>
  <published>{e['published']}</published>
</entry>""" for e in entries
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>'.encode()


@pytest.mark.unit
def test_parse_rss_atom_feed():
    posts = cr._parse_rss(_atom_payload([{
        "id": "t3_abc", "title": "Bitcoin hits new high", "author": "hodler",
        "url": "https://www.reddit.com/r/Bitcoin/abc", "published": "2026-07-19T03:00:00+00:00",
    }]))
    assert len(posts) == 1
    p = posts[0]
    assert p["title"] == "Bitcoin hits new high"
    assert p["author"] == "hodler"  # /u/ prefix stripped
    assert p["created_utc"] > 0
    assert p["score"] == 0 and p["num_comments"] == 0


@pytest.mark.unit
def test_rss_fallback_when_json_endpoints_403(tmp_root):
    """All .json endpoints 403 → search.rss serves the posts."""
    from urllib.error import HTTPError

    atom = _atom_payload([{
        "id": "t3_r1", "title": "BTC rally", "author": "user",
        "url": "https://www.reddit.com/r/Bitcoin/r1", "published": "2026-07-19T03:00:00+00:00",
    }])

    def fake_urlopen(req, timeout=None):
        if ".json" in req.full_url:
            raise HTTPError(req.full_url, 403, "Forbidden", {}, None)
        return _FakeResp(atom)

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        result, success = cr._fetch_subreddit_with_retry("BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert result[0]["title"] == "BTC rally"


@pytest.mark.unit
def test_rss_new_feed_filtered_by_query(tmp_root):
    """search.rss also blocked → new.rss fetched and filtered client-side."""
    from urllib.error import HTTPError

    atom = _atom_payload([
        {"id": "1", "title": "Bitcoin dips below support", "author": "a",
         "url": "u1", "published": "2026-07-19T03:00:00+00:00"},
        {"id": "2", "title": "Daily discussion thread", "author": "b",
         "url": "u2", "published": "2026-07-19T03:00:00+00:00"},
    ])

    def fake_urlopen(req, timeout=None):
        if "new.rss" in req.full_url:
            return _FakeResp(atom)
        raise HTTPError(req.full_url, 403, "Forbidden", {}, None)

    with patch.object(cr, "urlopen", side_effect=fake_urlopen), \
         patch.object(cr.time, "sleep", lambda *_a, **_k: None):
        result, success = cr._fetch_subreddit_with_retry("Bitcoin OR BTC", "Bitcoin", 10, 5.0)

    assert success is True
    assert [p["id"] for p in result] == ["1"]  # non-matching title filtered out


@pytest.mark.unit
def test_matches_query_or_terms():
    assert cr._matches_query("BTC to the moon", "Bitcoin OR BTC")
    assert cr._matches_query("bitcoin etf inflows", "Bitcoin OR BTC")
    assert not cr._matches_query("Daily discussion", "Bitcoin OR BTC")


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
