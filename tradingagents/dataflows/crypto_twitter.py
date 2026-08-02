"""Crypto Twitter vendor — editorial relay via crypto newsflash feeds.

Raw X/Twitter API access stays unresolved (docs/dataflows-decisions.md
§5 — paid tiers are not justified pre-revenue, PLAN.md T10). Instead
this module relays KOL / institution X activity as reported within
minutes by crypto newsflash desks (BlockBeats + Odaily, see
``crypto_news._FEEDS``). Newsflash items that explicitly reference X
activity ("... tweeted that ...", "posted on X") are preferred; when a
window has none, general newsflash items are passed through with an
explanatory note rather than returning nothing.

Contract is stable: returns ``Response[SocialPost]`` with
``platform == "twitter"``. Engagement metrics do not survive the relay
and stay empty — downstream prompts must not weight by engagement.
"""

from __future__ import annotations

import re

from . import crypto_news
from .crypto_types import Meta, Response, SocialPost

# Newsflash sources used for the relay. Odaily is Chinese — the CJK
# patterns below and the CJK symbol aliases in crypto_news carry it.
_RELAY_SOURCES = ("blockbeats", "odaily")

_KOL_PATTERNS = [
    r"\btweet(?:s|ed)?\b",
    r"\btwitter\b",
    r"\bon X\b",
    r"\bX post\b",
    r"\bX account\b",
    r"\bstated on\b",
    r"发推",
    r"推文",
    r"X\s*平台",
]


def _is_x_relay(item) -> bool:
    text = f"{item.title} {item.summary}"
    return any(re.search(p, text, re.IGNORECASE) for p in _KOL_PATTERNS)


def _get_tweets(symbols: tuple[str, ...],
                *, hours: int = 24,
                min_engagement: int | None = None) -> Response[SocialPost]:
    # Fetch unfiltered: newsflash desks expose only ~10 latest items per
    # poll, so a hard symbol filter would report a false data gap most of
    # the time. Prefer symbol-specific X relays, then widen tier by tier —
    # market-wide KOL chatter is still sentiment signal for the analyst.
    resp = crypto_news._get_news(_RELAY_SOURCES, hours=hours, symbols=None)
    if not resp.meta.ok:
        return Response(data=[], meta=Meta(
            fetched_at=resp.meta.fetched_at,
            vendor="twitter",
            ok=False,
            note=f"newsflash relay unavailable (data gap): {resp.meta.note}",
        ))

    sym_list = [s for s in symbols if s]
    relayed = [i for i in resp.data if _is_x_relay(i)]
    sym_relayed = [i for i in relayed
                   if crypto_news._matches_symbols(i, sym_list)]
    sym_general = [i for i in resp.data
                   if crypto_news._matches_symbols(i, sym_list)]

    if sym_relayed:
        items, note = sym_relayed, resp.meta.note
    elif relayed:
        items = relayed
        note = ("no symbol-specific X-relay items in window; showing "
                "market-wide X relays")
    elif sym_general:
        items = sym_general
        note = ("no explicit X-relay items in window; showing "
                "symbol-related newsflash items as weak proxy")
    else:
        items = resp.data
        note = ("no explicit X-relay items in window; passing through "
                "general newsflash items as weak proxy")

    posts = [SocialPost(
        id=i.id,
        timestamp=i.timestamp,
        platform="twitter",
        author=f"{i.source}-newsflash",
        text=i.title if not i.summary else f"{i.title} — {i.summary}",
        engagement={},
        url=i.url,
    ) for i in items]
    return Response(data=posts, meta=Meta(
        fetched_at=resp.meta.fetched_at,
        vendor="twitter",
        ok=True,
        is_stale=resp.meta.is_stale,
        note=note,
    ))


def get_tweets(symbols: tuple[str, ...], hours: int = 24,
               min_engagement: int | None = None) -> str:
    resp = _get_tweets(tuple(symbols), hours=hours,
                       min_engagement=min_engagement)
    sym_label = ",".join(s.upper() for s in symbols)
    header = [
        f"# Crypto X/Twitter signal — symbols: {sym_label} · last {hours}h",
        "# Source: editorial relay of X activity via newsflash desks "
        "(BlockBeats / Odaily) — not raw tweets; engagement metrics unavailable",
        f"# Posts: {len(resp.data)}",
        f"# ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append("<no X/Twitter relay data available>")
        return "\n".join(header)
    for p in resp.data[:30]:
        header.append("")
        header.append(f"[{crypto_news._iso(p.timestamp)} · {p.author}] {p.text}")
        header.append(f"  ↗ {p.url}")
    if len(resp.data) > 30:
        header.append(f"\n# ... {len(resp.data) - 30} older items truncated")
    return "\n".join(header)
