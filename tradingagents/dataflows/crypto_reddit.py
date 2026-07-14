"""Reddit search fetcher for crypto-related discussion posts.

Adapted from the original ``reddit.py`` stock fetcher: same public
JSON endpoint (``reddit.com/r/{sub}/search.json``, no API key, ~10
req/min per IP), but with crypto-native subreddits and symbol → query
mapping (e.g. BTC → "Bitcoin OR BTC").

Two-layer pattern: ``_get_reddit`` returns ``Response[SocialPost]``
for cache + tests, ``get_reddit`` returns prompt plaintext.

HTTP retry strategy:
- Primary: https://www.reddit.com (modern endpoint)
- Fallback 1: https://old.reddit.com (legacy endpoint)
- Fallback 2: Empty result with data_gap note (graceful degradation)

User-Agent follows Reddit's requirement: ``platform:app-id:version (by /u/username)``
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import crypto_cache as cache
from .crypto_types import Meta, Response, SocialPost

logger = logging.getLogger(__name__)

_REDDIT_TTL = 120  # 2 min per docs/dataflows-decisions.md §4.2
_REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "monopoly-trader")
_UA = f"trading-agent:monopoly-crypto:1.0 (by /u/{_REDDIT_USERNAME})"

DEFAULT_SUBREDDITS = ("CryptoCurrency", "Bitcoin", "ethfinance", "CryptoMarkets")

_SYMBOL_QUERIES: dict[str, str] = {
    "BTC": "Bitcoin OR BTC",
    "ETH": "Ethereum OR ETH",
    "SOL": "Solana OR SOL",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _symbol_query(symbol: str) -> str:
    return _SYMBOL_QUERIES.get(symbol.upper(), symbol)


def _fetch_subreddit_with_retry(
    query: str, sub: str, limit: int, timeout: float, max_retries: int = 2
) -> tuple[list[dict], bool]:
    """Fetch subreddit posts with exponential backoff retry and fallback endpoints.

    Returns:
        (posts, success): posts list and whether fetch succeeded (not degraded)
    """
    qs = urlencode({
        "q": query, "restrict_sr": "on", "sort": "new", "t": "week", "limit": limit,
    })

    # Try endpoints in order: primary (reddit.com), fallback (old.reddit.com)
    endpoints = [
        f"https://www.reddit.com/r/{sub}/search.json?{qs}",
        f"https://old.reddit.com/r/{sub}/search.json?{qs}",
    ]

    for endpoint_idx, endpoint_url in enumerate(endpoints):
        for attempt in range(max_retries + 1):
            try:
                req = Request(
                    endpoint_url,
                    headers={
                        "User-Agent": _UA,
                        "Accept": "application/json",
                    }
                )
                with urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read())

                children = (payload.get("data") or {}).get("children") or []
                posts = [c.get("data", {}) for c in children if isinstance(c, dict)]

                if posts or endpoint_idx == 0:  # success on any endpoint, or primary returned empty
                    return posts, True

            except HTTPError as e:
                # 403 Forbidden, 429 Too Many Requests: exponential backoff + retry
                if e.code in (403, 429):
                    wait_time = min(2 ** attempt, 8)  # cap at 8 seconds
                    if attempt < max_retries:
                        logger.debug(
                            "reddit HTTP %d (r/%s, attempt %d/%d), waiting %.1fs before retry on %s",
                            e.code, sub, attempt + 1, max_retries + 1, wait_time,
                            "fallback" if endpoint_idx > 0 else "primary"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.debug(
                            "reddit HTTP %d (r/%s) exhausted retries on %s endpoint",
                            e.code, sub, "fallback" if endpoint_idx > 0 else "primary"
                        )
                    continue  # try next attempt on same endpoint
                else:
                    # Other HTTP errors (4xx, 5xx not 403/429): log and skip this endpoint
                    logger.debug("reddit HTTP %d (r/%s): %s", e.code, sub, e.reason)
                    break  # try next endpoint

            except (URLError, json.JSONDecodeError, TimeoutError) as exc:
                logger.debug("reddit fetch error (r/%s · %s, attempt %d/%d): %s",
                            sub, query, attempt + 1, max_retries + 1, type(exc).__name__)
                if attempt < max_retries:
                    wait_time = min(2 ** attempt, 8)
                    time.sleep(wait_time)
                else:
                    break  # try next endpoint
                continue

    # All endpoints exhausted
    logger.warning("reddit fetch failed for r/%s · %s: all endpoints exhausted", sub, query)
    return [], False


def _fetch_subreddit(query: str, sub: str, limit: int, timeout: float) -> list[dict]:
    """Legacy interface: returns posts only, discards success flag."""
    posts, _ = _fetch_subreddit_with_retry(query, sub, limit, timeout)
    return posts


def _to_social_post(sub: str, raw: dict) -> SocialPost:
    text = (raw.get("title") or "").strip()
    body = (raw.get("selftext") or "").replace("\n", " ").strip()
    if body:
        text = f"{text}\n\n{body[:400]}"
    permalink = raw.get("permalink") or ""
    url = f"https://reddit.com{permalink}" if permalink else (raw.get("url") or "")
    created = raw.get("created_utc") or 0
    return SocialPost(
        id=hashlib.sha1((raw.get("id") or url).encode()).hexdigest()[:16],
        timestamp=int(created * 1000),
        platform="reddit",
        author=raw.get("author") or "?",
        text=text,
        engagement={
            "score": raw.get("score", 0),
            "comments": raw.get("num_comments", 0),
            "subreddit": sub,
        },
        url=url,
    )


def _get_reddit(symbols: tuple[str, ...],
                subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
                *,
                hours: int = 24,
                limit_per_sub: int = 10,
                inter_request_delay: float = 0.4,
                timeout: float = 10.0) -> Response[SocialPost]:
    scope = f"reddit:{','.join(sorted(symbols))}:{','.join(sorted(subreddits))}"
    now = _now_ms()
    since_ms = now - hours * 3_600_000

    if cache.is_fresh(scope, _REDDIT_TTL):
        items = cache.read_social(platform="reddit", since_ms=since_ms, until_ms=now)
        if items:
            return Response(
                data=items,
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="reddit", ok=True),
            )

    fetched_any = False
    fetch_failures = 0
    errors: list[str] = []

    for symbol in symbols:
        query = _symbol_query(symbol)
        for idx, sub in enumerate(subreddits):
            if idx > 0 or symbol != symbols[0]:
                time.sleep(inter_request_delay)
            raw_posts, success = _fetch_subreddit_with_retry(query, sub, limit_per_sub, timeout)
            if raw_posts:
                fetched_any = True
                cache.upsert_social([_to_social_post(sub, r) for r in raw_posts])
            else:
                if not success:  # explicit failure (403/429 all retries exhausted, etc.)
                    fetch_failures += 1
                errors.append(f"r/{sub} ({query}): no posts")

    if fetched_any:
        cache.set_meta(scope, now)

    items = cache.read_social(platform="reddit", since_ms=since_ms, until_ms=now)

    # Determine response quality:
    # 1. If fresh fetch succeeded with data: ok=True
    # 2. If fresh fetch had failures but cache has old data: ok=True, is_stale=True
    # 3. If all fetches failed (no fresh posts + no cache): ok=False, data_gap=True
    if items and fetched_any:
        return Response(data=items, meta=Meta(fetched_at=now, vendor="reddit", ok=True))

    if items:  # cached but stale
        return Response(data=items, meta=Meta(
            fetched_at=cache.get_meta(scope) or 0, vendor="reddit",
            ok=True, is_stale=True, note="; ".join(errors[:3]),
        ))

    # No data and fresh fetch failed: graceful degradation with data_gap marker
    return Response(data=[], meta=Meta(
        fetched_at=now, vendor="reddit", ok=False,
        note="data_gap: no reddit posts retrieved; all endpoints/retries exhausted"
             + (f"; {errors[0]}" if errors else ""),
    ))


def get_reddit(symbols: tuple[str, ...],
               subreddits: tuple[str, ...] = DEFAULT_SUBREDDITS,
               hours: int = 24) -> str:
    resp = _get_reddit(symbols, subreddits, hours=hours)
    sym_label = ",".join(s.upper() for s in symbols)
    sub_label = ",".join(f"r/{s}" for s in subreddits)
    header = [
        f"# Reddit posts — symbols: {sym_label} · subs: {sub_label} · last {hours}h",
        f"# Posts: {len(resp.data)}",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no Reddit posts found for {sym_label}>")
        return "\n".join(header)
    # group by subreddit for prompt readability
    by_sub: dict[str, list[SocialPost]] = {}
    for p in resp.data:
        sub = p.engagement.get("subreddit", "?")
        by_sub.setdefault(sub, []).append(p)
    for sub, posts in by_sub.items():
        header.append("")
        header.append(f"r/{sub} — {len(posts)} posts:")
        for p in sorted(posts, key=lambda x: x.timestamp, reverse=True)[:8]:
            title_line = p.text.split("\n", 1)[0][:140]
            header.append(
                f"  [{_iso(p.timestamp)} · {p.engagement.get('score',0):>4}↑ "
                f"· {p.engagement.get('comments',0):>3}c · u/{p.author}] {title_line}"
            )
    return "\n".join(header)
