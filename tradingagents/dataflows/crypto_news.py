"""Crypto news vendor: CoinDesk + CoinTelegraph RSS.

Public RSS feeds, no API key needed. Items are deduplicated by
``id = sha1(url)`` so the same article from multiple polls stays a
single row in the events sqlite. Symbol filtering matches keywords in
title or summary (e.g. BTC/Bitcoin for symbol ``BTC``).
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import re
import time

import feedparser

from . import crypto_cache as cache
from .crypto_types import Meta, NewsItem, Response

logger = logging.getLogger(__name__)

_NEWS_TTL = 300  # 5 min per docs/dataflows-decisions.md §4.2

_FEEDS: dict[str, str] = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
}

# Symbol → list of keyword regex patterns (case-insensitive) for filtering.
_SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "BTC": [r"\bBTC\b", r"\bbitcoin\b"],
    "ETH": [r"\bETH\b", r"\bether\b", r"\bethereum\b"],
    "SOL": [r"\bSOL\b", r"\bsolana\b"],
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _parse_feed(source: str, url: str) -> list[NewsItem]:
    parsed = feedparser.parse(url)
    items = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        published = e.get("published_parsed") or e.get("updated_parsed")
        ts_ms = int(calendar.timegm(published) * 1000) if published else _now_ms()
        title = (e.get("title") or "").strip()
        summary = re.sub(r"<[^>]+>", "", (e.get("summary") or "")).strip()
        if len(summary) > 500:
            summary = summary[:500] + "…"
        items.append(NewsItem(
            id=hashlib.sha1(link.encode()).hexdigest()[:16],
            timestamp=ts_ms,
            source=source,
            title=title,
            summary=summary,
            url=link,
        ))
    return items


def _matches_symbols(item: NewsItem, symbols: list[str]) -> bool:
    if not symbols:
        return True
    text = f"{item.title} {item.summary}"
    for sym in symbols:
        patterns = _SYMBOL_KEYWORDS.get(sym.upper(), [re.escape(sym)])
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return True
    return False


def _get_news(sources: tuple[str, ...] = ("coindesk", "cointelegraph"),
              *, hours: int = 24,
              symbols: tuple[str, ...] | None = None) -> Response[NewsItem]:
    scope = f"news:{','.join(sorted(sources))}"
    now = _now_ms()
    since_ms = now - hours * 3_600_000
    sym_list = list(symbols) if symbols else []

    if cache.is_fresh(scope, _NEWS_TTL):
        items = cache.read_news(sources=sources, since_ms=since_ms, until_ms=now)
        items = [i for i in items if _matches_symbols(i, sym_list)]
        if items:
            return Response(
                data=items,
                meta=Meta(fetched_at=cache.get_meta(scope) or now,
                          vendor=",".join(sources), ok=True),
            )

    fetched_any = False
    fetch_errors = []
    for source in sources:
        url = _FEEDS.get(source)
        if not url:
            fetch_errors.append(f"{source}: no feed url configured")
            continue
        try:
            items = _parse_feed(source, url)
            cache.upsert_news(items)
            fetched_any = True
        except Exception as exc:
            fetch_errors.append(f"{source}: {exc}")
            logger.warning("news fetch failed for %s: %s", source, exc)

    if fetched_any:
        cache.set_meta(scope, now)

    items = cache.read_news(sources=sources, since_ms=since_ms, until_ms=now)
    items = [i for i in items if _matches_symbols(i, sym_list)]

    if items and fetched_any:
        return Response(data=items, meta=Meta(fetched_at=now,
                                              vendor=",".join(sources), ok=True))
    if items:
        # served from stale cache; all sources failed
        return Response(data=items, meta=Meta(
            fetched_at=cache.get_meta(scope) or 0,
            vendor=",".join(sources), ok=True, is_stale=True,
            note="; ".join(fetch_errors),
        ))
    return Response(data=[], meta=Meta(
        fetched_at=now, vendor=",".join(sources), ok=False,
        note="; ".join(fetch_errors) or "no items matched filters",
    ))


def get_news(sources: tuple[str, ...] = ("coindesk", "cointelegraph"),
             hours: int = 24,
             symbols: tuple[str, ...] | None = None) -> str:
    resp = _get_news(sources, hours=hours, symbols=symbols)
    sym_label = ",".join(symbols).upper() if symbols else "ALL"
    header = [
        f"# Crypto news — sources: {','.join(sources)} · symbols: {sym_label} · last {hours}h",
        f"# Articles: {len(resp.data)}",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no news available for {sym_label}>")
        return "\n".join(header)
    # sorted desc by timestamp already from cache; cap to 30 for prompt size
    for i in resp.data[:30]:
        header.append("")
        header.append(f"[{_iso(i.timestamp)} · {i.source}] {i.title}")
        if i.summary:
            header.append(f"  {i.summary}")
        header.append(f"  ↗ {i.url}")
    if len(resp.data) > 30:
        header.append(f"\n# ... {len(resp.data) - 30} older articles truncated")
    return "\n".join(header)
