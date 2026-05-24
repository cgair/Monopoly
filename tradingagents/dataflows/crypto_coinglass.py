"""Coinglass derivatives data vendor (open-api-v4.coinglass.com).

Requires ``COINGLASS_API_KEY`` env var. When the key is missing the
public helpers return a degraded ``Response`` (``ok=False``, explanatory
note) rather than raising — so the system can run end-to-end while a
Coinglass plan is being provisioned.

Two layers per endpoint mirror ``crypto_binance.py``: ``_get_*`` returns
``Response[T]`` for cache/test use, ``get_*`` returns prompt plaintext.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import requests

from . import crypto_cache as cache
from .crypto_types import Liquidation, LongShortRatio, Meta, Response

logger = logging.getLogger(__name__)

_BASE = "https://open-api-v4.coinglass.com"
_USER_AGENT = "monopoly/0.1 (+https://github.com/cgair/Monopoly)"
_HTTP_TIMEOUT = 10.0
_HTTP_RETRIES = 1

# Liquidations are events; long_short is sampled. TTL only governs the
# "do we re-poll?" decision; cached events are kept indefinitely.
_LIQ_TTL = 60
_LS_TTL = {"5m": 60, "15m": 120, "1h": 300, "4h": 1800, "1d": 1800}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _api_key() -> str | None:
    return os.environ.get("COINGLASS_API_KEY") or None


def _http_get(path: str, params: dict) -> dict:
    key = _api_key()
    if not key:
        raise RuntimeError("COINGLASS_API_KEY not set")
    headers = {"CG-API-KEY": key, "User-Agent": _USER_AGENT, "Accept": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(_HTTP_RETRIES + 1):
        try:
            r = requests.get(_BASE + path, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _HTTP_RETRIES:
                time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


def _liq_id(symbol: str, ts: int, side: str, qty: float, price: float) -> str:
    raw = f"{symbol}|{ts}|{side}|{qty}|{price}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _ls_id(symbol: str, interval: str, ts: int) -> str:
    raw = f"{symbol}|{interval}|{ts}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Liquidations
# ---------------------------------------------------------------------------

def _parse_liquidations(symbol: str, payload: dict) -> list[Liquidation]:
    rows = (payload.get("data") or [])
    out = []
    for r in rows:
        ts = int(r.get("createTime") or r.get("ts") or r.get("timestamp") or 0)
        side = (r.get("side") or "").lower()
        qty = float(r.get("baseVolume") or r.get("qty") or 0.0)
        price = float(r.get("price") or 0.0)
        out.append(Liquidation(
            id=_liq_id(symbol, ts, side, qty, price),
            symbol=symbol, timestamp=ts, side=side, qty=qty, price=price,
        ))
    return out


def _get_liquidations(symbol: str, *, hours: int = 24) -> Response[Liquidation]:
    scope = f"coinglass:liq:{symbol}"
    now = _now_ms()
    since_ms = now - hours * 3_600_000

    if cache.is_fresh(scope, _LIQ_TTL):
        items = cache.read_liquidations(symbol=symbol, since_ms=since_ms, until_ms=now)
        if items:
            return Response(
                data=items,
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="coinglass", ok=True),
            )

    try:
        payload = _http_get("/api/futures/liquidation/history", {
            "symbol": symbol.upper(), "interval": "1h", "limit": min(hours, 500),
        })
        items = _parse_liquidations(symbol.upper(), payload)
        if items:
            cache.upsert_liquidations(items)
            cache.set_meta(scope, now)
            window = cache.read_liquidations(symbol=symbol, since_ms=since_ms, until_ms=now)
            return Response(data=window,
                            meta=Meta(fetched_at=now, vendor="coinglass", ok=True))
        note = "coinglass returned empty liquidation payload"
    except Exception as exc:
        note = f"coinglass liquidations fetch failed: {exc}"
        logger.warning("coinglass liq fetch failed for %s: %s", symbol, exc)

    stale = cache.read_liquidations(symbol=symbol, since_ms=since_ms, until_ms=now)
    if stale:
        return Response(
            data=stale,
            meta=Meta(fetched_at=cache.get_meta(scope) or 0, vendor="coinglass",
                      ok=True, is_stale=True, note=note),
        )
    return Response(data=[], meta=Meta(fetched_at=now, vendor="coinglass", ok=False, note=note))


def get_liquidations(symbol: str, hours: int = 24) -> str:
    resp = _get_liquidations(symbol, hours=hours)
    sym = symbol.upper()
    long_qty = sum(x.qty for x in resp.data if x.side == "long")
    short_qty = sum(x.qty for x in resp.data if x.side == "short")
    header = [
        f"# Coinglass liquidations — {sym} (last {hours}h)",
        f"# Events: {len(resp.data)} · long_liq_qty={long_qty:.4f} · short_liq_qty={short_qty:.4f}",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no liquidation data available for {sym}>")
        return "\n".join(header)
    header.append("timestamp,side,qty,price")
    for x in resp.data[-50:]:   # cap to last 50 events for prompt size
        header.append(f"{_iso(x.timestamp)},{x.side},{x.qty},{x.price}")
    if len(resp.data) > 50:
        header.append(f"# ... {len(resp.data) - 50} earlier events truncated")
    return "\n".join(header)


# ---------------------------------------------------------------------------
# Long/short account ratio
# ---------------------------------------------------------------------------

def _parse_long_short(symbol: str, interval: str, payload: dict) -> list[LongShortRatio]:
    rows = (payload.get("data") or [])
    out = []
    for r in rows:
        ts = int(r.get("createTime") or r.get("ts") or r.get("timestamp") or 0)
        long_acc = float(r.get("longAccount") or r.get("longRatio") or 0.0)
        short_acc = float(r.get("shortAccount") or r.get("shortRatio") or 0.0)
        ratio = float(r.get("longShortRatio") or
                      (long_acc / short_acc if short_acc else 0.0))
        out.append(LongShortRatio(
            id=_ls_id(symbol, interval, ts),
            symbol=symbol, interval=interval, timestamp=ts,
            long_account=long_acc, short_account=short_acc, ratio=ratio,
        ))
    return out


def _get_long_short_ratio(symbol: str, interval: str, *, limit: int = 50) -> Response[LongShortRatio]:
    scope = f"coinglass:ls:{symbol}:{interval}"
    ttl = _LS_TTL.get(interval, 300)
    now = _now_ms()
    interval_ms = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000,
                   "4h": 14_400_000, "1d": 86_400_000}.get(interval, 3_600_000)
    since_ms = now - (limit + 2) * interval_ms

    if cache.is_fresh(scope, ttl):
        items = cache.read_long_short(symbol=symbol, interval=interval,
                                      since_ms=since_ms, until_ms=now)
        if items:
            return Response(
                data=items[-limit:],
                meta=Meta(fetched_at=cache.get_meta(scope) or now, vendor="coinglass", ok=True),
            )

    try:
        payload = _http_get("/api/futures/global-long-short-account-ratio/history", {
            "symbol": symbol.upper(), "interval": interval, "limit": min(limit, 500),
        })
        items = _parse_long_short(symbol.upper(), interval, payload)
        if items:
            cache.upsert_long_short(items)
            cache.set_meta(scope, now)
            window = cache.read_long_short(symbol=symbol, interval=interval,
                                           since_ms=since_ms, until_ms=now)
            return Response(data=window[-limit:],
                            meta=Meta(fetched_at=now, vendor="coinglass", ok=True))
        note = "coinglass returned empty long/short payload"
    except Exception as exc:
        note = f"coinglass long/short fetch failed: {exc}"
        logger.warning("coinglass ls fetch failed for %s %s: %s", symbol, interval, exc)

    stale = cache.read_long_short(symbol=symbol, interval=interval,
                                  since_ms=since_ms, until_ms=now)
    if stale:
        return Response(
            data=stale[-limit:],
            meta=Meta(fetched_at=cache.get_meta(scope) or 0, vendor="coinglass",
                      ok=True, is_stale=True, note=note),
        )
    return Response(data=[], meta=Meta(fetched_at=now, vendor="coinglass", ok=False, note=note))


def get_long_short_ratio(symbol: str, interval: str, limit: int = 50) -> str:
    resp = _get_long_short_ratio(symbol, interval, limit=limit)
    sym = symbol.upper()
    header = [
        f"# Coinglass long/short account ratio — {sym} {interval}",
        f"# Samples: {len(resp.data)} (requested {limit})",
        f"# Fetched at: {_iso(resp.meta.fetched_at) if resp.meta.fetched_at else 'never'}"
        f" · ok={resp.meta.ok} · stale={resp.meta.is_stale}",
    ]
    if resp.meta.note:
        header.append(f"# Note: {resp.meta.note}")
    if not resp.data:
        header.append(f"<no long/short data available for {sym} {interval}>")
        return "\n".join(header)
    header.append("timestamp,long_account,short_account,ratio")
    for x in resp.data:
        header.append(f"{_iso(x.timestamp)},{x.long_account},{x.short_account},{x.ratio}")
    return "\n".join(header)
