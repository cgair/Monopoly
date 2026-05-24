"""Twitter vendor placeholder: contract is stable; behaviour is degraded."""

import pytest

from tradingagents.dataflows import crypto_twitter as ct


@pytest.mark.unit
def test_get_tweets_returns_degraded_prompt():
    out = ct.get_tweets(("BTC", "ETH"), hours=24)
    assert "ok=False" in out
    assert "no Twitter data" in out


@pytest.mark.unit
def test_underlying_response_is_empty_and_explicit():
    resp = ct._get_tweets(("BTC",), hours=24)
    assert resp.data == []
    assert resp.meta.ok is False
    assert resp.meta.vendor == "twitter"
    assert "pending" in (resp.meta.note or "")
