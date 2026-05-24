"""Binance USDT-M Futures (``fapi.binance.com``) data vendor.

Two layers per endpoint:

* ``_get_*`` low-level fetchers return ``Response[T]`` after cache lookup,
  network fetch, and stale fallback. These are the testable units and the
  source of truth for the cache.
* ``get_*`` public functions return prompt-ready plaintext (header + table)
  for registration in ``interface.VENDOR_METHODS`` and consumption by
  LangGraph Analyst nodes.

Failure semantics follow ``docs/dataflows-decisions.md`` §2 row 8:
network failure → degrade to stale cache with ``is_stale=True``; cache
empty too → return empty ``data`` with ``ok=False`` and a ``note``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from . import crypto_cache as cache
from .crypto_types import Bar, FundingRate, Meta, OpenInterest, Response

logger = logging.getLogger(__name__)

_BASE = "https://fapi.binance.com"
_USER_AGENT = "monopoly/0.1 (+https://github.com/cgair/Monopoly)"
_HTTP_TIMEOUT = 10.0
_HTTP_RETRIES = 1   # one immediate retry; further policy lives at the agent level

# TTL in seconds keyed by interval; matches docs/dataflows-decisions.md §4.2.
_OHLCV_TTL = {"15m": 30, "1h": 300, "4h": 1800, "1d": 1800}
_FUNDING_TTL = 60
_OI_TTL = {"5m": 30, "15m": 60, "1h": 300, "4h": 1800, "1d": 1800}

# Interval to milliseconds for cache window calculations.
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _http_get(path: str, params: dict) -> list | dict:
    """GET ``path`` with one immediate retry. Raises ``requests`` exceptions on
    final failure; callers translate to degraded ``Response``."""
    last_exc: Exception | None = None
    for attempt in range(_HTTP_RETRIES + 1):
        try:
            r = requests.get(
                _BASE + path,
                params=params,
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _HTTP_RETRIES:
                time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------

def _parse_klines(symbol: str, interval: str, raw: list) -> list[Bar]:
    """Binance kline rows: [openTime, open, high, low, close, volume, closeTime,
    quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]."""
    out = []
    for k in raw:
        out.append(Bar(
            symbol=symbol,
            interval=interval,
            open_time=int(k[0]),
            close_time=int(k[6]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            quote_volume=float(k[7]),
            trades=int(k[8]),
        ))
    return out


def _get_ohlcv(symbol: str, interval: str, *, limit: int = 200) -> Response[Bar]:
    """Latest ``limit`` bars; cache-first with stale fallback.

    Note: range mode (start/end) is intentionally not exposed here yet;
    a separate backfill helper will be added when historical backtesting
    lands (Week 2 end / Week 3).
    """
    scope = f"binance:ohlcv:{symbol}:{interval}"
    ttl = _OHLCV_TTL.get(interval, 60)
    now = _now_ms()
    window_ms = limit * _INTERVAL_MS.get(interval, 3_600_000)
    start_ms = now - window_ms - _INTERVAL_MS.get(interval, 3_600_000)  # 1 bar safety

    # Fast path: cache fresh.
    if cache.is_fresh(scope, ttl):
        bars = cache.read_bars(symbol, interval, start_ms=start_ms, end_ms=now)
        if bars:
            return Response(
                data=bars[-limit:],
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="binance", ok=True),
            )

    # Cache miss or stale: fetch.
    try:
        raw = _http_get("/fapi/v1/klines", {
            "symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1500),
        })
        bars = _parse_klines(symbol.upper(), interval, raw)
        if bars:
            cache.write_bars(bars)
            cache.set_meta(scope, now)
            return Response(data=bars[-limit:],
                            meta=Meta(fetched_at=now, vendor="binance", ok=True))
        # Empty response treated as soft failure → try stale.
        note = "vendor returned empty kline payload"
    except requests.RequestException as exc:
        note = f"binance kline fetch failed: {exc}"
        logger.warning("binance ohlcv fetch failed for %s %s: %s", symbol, interval, exc)

    # Degrade to stale cache.
    stale = cache.read_bars(symbol, interval, start_ms=start_ms, end_ms=now)
    if stale:
        return Response(
            data=stale[-limit:],
            meta=Meta(fetched_at=cache.get_meta(scope) or 0, vendor="binance",
                      ok=True, is_stale=True, note=note),
        )
    return Response(data=[], meta=Meta(fetched_at=now, vendor="binance", ok=False, note=note))


def get_ohlcv(symbol: str, interval: str, limit: int = 200) -> str:
    """Prompt-formatted OHLCV table for ``symbol`` on ``interval``."""
    resp = _get_ohlcv(symbol, interval, limit=limit)
    sym = symbol.upper()
    header = [
        f"# Binance Futures OHLCV — {sym} {interval}",
        f"# Bars returned: {len(resp.data)} (requested {limit})",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · vendor: {resp.meta.vendor} · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no OHLCV data available for {sym} {interval}>")
        return "\n".join(header)
    header.append("open_time,close_time,open,high,low,close,volume,quote_volume,trades")
    for b in resp.data:
        header.append(
            f"{_iso(b.open_time)},{_iso(b.close_time)},"
            f"{b.open},{b.high},{b.low},{b.close},{b.volume},{b.quote_volume},{b.trades}"
        )
    return "\n".join(header)


# ---------------------------------------------------------------------------
# Funding rate
# ---------------------------------------------------------------------------

def _parse_funding(raw: list) -> list[FundingRate]:
    out = []
    for r in raw:
        out.append(FundingRate(
            symbol=r["symbol"],
            funding_time=int(r["fundingTime"]),
            rate=float(r["fundingRate"]),
        ))
    return out


def _get_funding_rate(symbol: str, *, limit: int = 100) -> Response[FundingRate]:
    scope = f"binance:funding:{symbol}"
    now = _now_ms()
    # Funding settles every 8h on Binance; pull a wide window from cache.
    window_ms = max(limit, 50) * 8 * 3_600_000

    if cache.is_fresh(scope, _FUNDING_TTL):
        rates = cache.read_funding(symbol, start_ms=now - window_ms, end_ms=now)
        if rates:
            return Response(
                data=rates[-limit:],
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="binance", ok=True),
            )

    try:
        raw = _http_get("/fapi/v1/fundingRate", {
            "symbol": symbol.upper(), "limit": min(limit, 1000),
        })
        rates = _parse_funding(raw)
        if rates:
            cache.write_funding(rates)
            cache.set_meta(scope, now)
            return Response(data=rates[-limit:],
                            meta=Meta(fetched_at=now, vendor="binance", ok=True))
        note = "vendor returned empty funding payload"
    except requests.RequestException as exc:
        note = f"binance funding fetch failed: {exc}"
        logger.warning("binance funding fetch failed for %s: %s", symbol, exc)

    stale = cache.read_funding(symbol, start_ms=now - window_ms, end_ms=now)
    if stale:
        return Response(
            data=stale[-limit:],
            meta=Meta(fetched_at=cache.get_meta(scope) or 0, vendor="binance",
                      ok=True, is_stale=True, note=note),
        )
    return Response(data=[], meta=Meta(fetched_at=now, vendor="binance", ok=False, note=note))


def get_funding_rate(symbol: str, limit: int = 100) -> str:
    resp = _get_funding_rate(symbol, limit=limit)
    sym = symbol.upper()
    header = [
        f"# Binance Futures funding rate — {sym}",
        f"# Settlements returned: {len(resp.data)} (requested {limit})",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no funding rate data available for {sym}>")
        return "\n".join(header)
    header.append("funding_time,rate,rate_pct")
    for r in resp.data:
        header.append(f"{_iso(r.funding_time)},{r.rate},{r.rate*100:.4f}%")
    return "\n".join(header)


# ---------------------------------------------------------------------------
# Open interest
# ---------------------------------------------------------------------------

def _parse_oi(symbol: str, interval: str, raw: list) -> list[OpenInterest]:
    out = []
    for r in raw:
        out.append(OpenInterest(
            symbol=symbol,
            interval=interval,
            timestamp=int(r["timestamp"]),
            oi=float(r["sumOpenInterest"]),
        ))
    return out


def _get_open_interest(symbol: str, interval: str, *, limit: int = 100) -> Response[OpenInterest]:
    scope = f"binance:oi:{symbol}:{interval}"
    ttl = _OI_TTL.get(interval, 60)
    now = _now_ms()
    window_ms = limit * _INTERVAL_MS.get(interval, 3_600_000)
    start_ms = now - window_ms - _INTERVAL_MS.get(interval, 3_600_000)

    if cache.is_fresh(scope, ttl):
        items = cache.read_oi(symbol, interval, start_ms=start_ms, end_ms=now)
        if items:
            return Response(
                data=items[-limit:],
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="binance", ok=True),
            )

    try:
        raw = _http_get("/futures/data/openInterestHist", {
            "symbol": symbol.upper(), "period": interval, "limit": min(limit, 500),
        })
        items = _parse_oi(symbol.upper(), interval, raw)
        if items:
            cache.write_oi(items)
            cache.set_meta(scope, now)
            return Response(data=items[-limit:],
                            meta=Meta(fetched_at=now, vendor="binance", ok=True))
        note = "vendor returned empty open interest payload"
    except requests.RequestException as exc:
        note = f"binance open interest fetch failed: {exc}"
        logger.warning("binance oi fetch failed for %s %s: %s", symbol, interval, exc)

    stale = cache.read_oi(symbol, interval, start_ms=start_ms, end_ms=now)
    if stale:
        return Response(
            data=stale[-limit:],
            meta=Meta(fetched_at=cache.get_meta(scope) or 0, vendor="binance",
                      ok=True, is_stale=True, note=note),
        )
    return Response(data=[], meta=Meta(fetched_at=now, vendor="binance", ok=False, note=note))


def get_open_interest(symbol: str, interval: str, limit: int = 100) -> str:
    resp = _get_open_interest(symbol, interval, limit=limit)
    sym = symbol.upper()
    header = [
        f"# Binance Futures open interest — {sym} {interval}",
        f"# Samples returned: {len(resp.data)} (requested {limit})",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no open interest data available for {sym} {interval}>")
        return "\n".join(header)
    header.append("timestamp,open_interest")
    for o in resp.data:
        header.append(f"{_iso(o.timestamp)},{o.oi}")
    return "\n".join(header)
