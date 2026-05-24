"""Normalised dataclasses shared across crypto vendor modules.

All vendor low-level fetchers (`_get_*`) return ``Response[T]`` so the
cache layer and tests can rely on a stable structure regardless of
vendor-specific JSON shapes. The public prompt-formatting wrappers
(`get_*`, registered in ``interface.VENDOR_METHODS``) consume these
dataclasses and emit plaintext for LangGraph Analyst nodes.

Timestamp contract: integer milliseconds since Unix epoch, UTC.
Rendering to ISO 8601 happens only at the prompt-formatting boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class Meta:
    fetched_at: int           # ms epoch UTC; time the fetch (or cache hit) resolved
    vendor: str               # "binance" / "coinglass" / "coindesk" / "reddit" / ...
    ok: bool                  # True iff data is complete and trustworthy
    is_stale: bool = False    # True when served from cache past its TTL
    note: str | None = None   # optional human-readable explanation on degraded paths


@dataclass
class Bar:
    symbol: str
    interval: str             # "15m" / "1h" / "4h" / "1d"
    open_time: int            # ms epoch UTC, inclusive
    close_time: int           # ms epoch UTC, inclusive
    open: float
    high: float
    low: float
    close: float
    volume: float             # base-asset volume
    quote_volume: float       # quote-asset (USDT) volume
    trades: int


@dataclass
class FundingRate:
    symbol: str
    funding_time: int         # ms epoch UTC of the funding settlement
    rate: float               # e.g. 0.0001 == 0.01% per 8h


@dataclass
class OpenInterest:
    symbol: str
    interval: str             # "5m" / "15m" / "1h" / ... (Binance OI is sampled)
    timestamp: int            # ms epoch UTC
    oi: float                 # number of contracts (vendor-dependent unit)


@dataclass
class Liquidation:
    id: str                   # synthesised by vendor: hash(symbol|ts|side|qty|price)
    symbol: str
    timestamp: int            # ms epoch UTC
    side: str                 # "long" (long got liquidated) / "short"
    qty: float                # contract qty liquidated
    price: float


@dataclass
class LongShortRatio:
    id: str                   # synthesised by vendor: hash(symbol|interval|timestamp)
    symbol: str
    interval: str             # "5m" / "15m" / "1h" / ...
    timestamp: int            # ms epoch UTC
    long_account: float       # share of accounts that are net long, 0..1
    short_account: float      # share net short, 0..1
    ratio: float              # long_account / short_account


@dataclass
class NewsItem:
    id: str                   # stable id: hash(url) when vendor lacks native id
    timestamp: int            # ms epoch UTC of publication
    source: str               # "coindesk" / "cointelegraph"
    title: str
    summary: str
    url: str


@dataclass
class SocialPost:
    id: str                   # vendor-native id or hash(url)
    timestamp: int            # ms epoch UTC
    platform: str             # "twitter" / "reddit"
    author: str
    text: str
    engagement: dict = field(default_factory=dict)   # {likes, retweets, score, comments, ...}
    url: str = ""


@dataclass
class Response(Generic[T]):
    data: list[T]
    meta: Meta
