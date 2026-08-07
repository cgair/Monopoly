"""Binance Vision archive reader — the historical spine of the backtest.

The live ``dataflows.crypto_binance`` module always fetches "latest N
samples relative to now"; it has no notion of an as-of cutoff, and the
``/futures/data/*`` REST endpoints only retain roughly 30 days. Neither
is usable for point-in-time replay.

Binance publishes dated archives at ``data.binance.vision`` that cover
the full history:

* ``daily/klines/<SYM>/<interval>/`` — OHLCV, incl. taker buy volume
* ``daily/metrics/<SYM>/`` — 5-minute snapshots of open interest and the
  three long/short ratios
* ``monthly/fundingRate/<SYM>/`` — funding settlements

Archives lag real time by roughly a day, so anything the archive does
not cover yet falls back to the REST endpoints with an explicit
``startTime``/``endTime`` window. Every fetch here is range-bounded:
this module never asks for "latest".

Raw zips are cached under ``~/.tradingagents/backtest_archive`` so a
repeated replay costs no network.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from tradingagents.dataflows.crypto_types import Bar, FundingRate

logger = logging.getLogger(__name__)

_VISION = "https://data.binance.vision/data/futures/um"
_REST = "https://fapi.binance.com"
_UA = "monopoly-backtest/0.1 (+https://github.com/cgair/Monopoly)"
_TIMEOUT = 60.0

CACHE_DIR = Path(
    os.environ.get("TRADINGAGENTS_BACKTEST_ARCHIVE")
    or Path.home() / ".tradingagents" / "backtest_archive"
)

INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
}


@dataclass
class MetricRow:
    """One 5-minute snapshot from the ``metrics`` archive.

    ``top_ratio`` is position/notional weighted (matches Binance's
    ``topLongShortPositionRatio``); ``top_account_ratio`` is account
    weighted. ``account_ratio`` is the global all-accounts ratio.
    """
    timestamp: int
    open_interest: float
    open_interest_value: float
    account_ratio: float
    top_ratio: float
    top_account_ratio: float
    taker_vol_ratio: float


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _cached_get(url: str) -> bytes | None:
    """GET ``url`` with an on-disk cache. ``None`` when the archive is absent.

    A 404 means the archive is not published for that date (either too
    recent or before the symbol listed) and is the caller's cue to fall
    back to REST — it is not an error.
    """
    key = url.replace(_VISION + "/", "").replace("/", "_")
    path = CACHE_DIR / key
    if path.exists():
        return path.read_bytes()

    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(path)
    return resp.content


def _read_zip_csv(blob: bytes) -> list[list[str]]:
    """Return CSV rows from a single-member zip, header dropped if present."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and not rows[0][0].lstrip("-").isdigit():
        rows = rows[1:]
    return [r for r in rows if r]


def _rest_get(path: str, params: dict) -> list:
    resp = requests.get(_REST + path, params=params,
                        headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _days(start_ms: int, end_ms: int) -> list[str]:
    d = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _months(start_ms: int, end_ms: int) -> list[str]:
    d = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().replace(day=1)
    last = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d.strftime("%Y-%m"))
        d = (d + timedelta(days=32)).replace(day=1)
    return out


# ---------------------------------------------------------------------------
# klines
# ---------------------------------------------------------------------------

def _bar_from_row(symbol: str, interval: str, r: list[str]) -> Bar:
    return Bar(
        symbol=symbol, interval=interval,
        open_time=int(r[0]), close_time=int(r[6]),
        open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
        volume=float(r[5]), quote_volume=float(r[7]), trades=int(r[8]),
    )


def taker_buy_volume(symbol: str, interval: str, start_ms: int, end_ms: int) -> dict[int, tuple[float, float]]:
    """``{open_time: (taker_buy_vol, total_vol)}`` — the input to taker ratio.

    Kept separate from :func:`klines` because :class:`Bar` has no field
    for taker volume and the live layer never needed one.
    """
    out: dict[int, tuple[float, float]] = {}
    for day in _days(start_ms, end_ms):
        blob = _cached_get(f"{_VISION}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip")
        if blob is None:
            continue
        for r in _read_zip_csv(blob):
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                out[ts] = (float(r[9]), float(r[5]))
    return out


def klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Bar]:
    """Bars with ``start_ms <= open_time <= end_ms``, oldest first."""
    bars: dict[int, Bar] = {}
    missing_days = []
    for day in _days(start_ms, end_ms):
        blob = _cached_get(f"{_VISION}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip")
        if blob is None:
            missing_days.append(day)
            continue
        for r in _read_zip_csv(blob):
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                bars[ts] = _bar_from_row(symbol, interval, r)

    if missing_days:
        # Archive not published yet for these days — fill from REST, which
        # honours an explicit window and so stays point-in-time safe.
        logger.debug("vision klines gap for %s %s: %s", symbol, interval, missing_days)
        cursor = start_ms
        step = INTERVAL_MS.get(interval, 3_600_000)
        while cursor <= end_ms:
            raw = _rest_get("/fapi/v1/klines", {
                "symbol": symbol, "interval": interval,
                "startTime": cursor, "endTime": end_ms, "limit": 1500,
            })
            if not raw:
                break
            for k in raw:
                ts = int(k[0])
                if start_ms <= ts <= end_ms and ts not in bars:
                    bars[ts] = Bar(
                        symbol=symbol, interval=interval,
                        open_time=ts, close_time=int(k[6]),
                        open=float(k[1]), high=float(k[2]), low=float(k[3]),
                        close=float(k[4]), volume=float(k[5]),
                        quote_volume=float(k[7]), trades=int(k[8]),
                    )
            if len(raw) < 1500:
                break
            cursor = int(raw[-1][0]) + step

    return [bars[k] for k in sorted(bars)]


# ---------------------------------------------------------------------------
# metrics (open interest + long/short ratios)
# ---------------------------------------------------------------------------

def _metric_from_row(r: list[str]) -> MetricRow | None:
    def num(x: str) -> float:
        return float(x) if x not in ("", "NULL", "null") else 0.0

    ts = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return MetricRow(
        timestamp=int(ts.timestamp() * 1000),
        open_interest=num(r[2]), open_interest_value=num(r[3]),
        top_account_ratio=num(r[4]), top_ratio=num(r[5]),
        account_ratio=num(r[6]), taker_vol_ratio=num(r[7]),
    )


def metrics(symbol: str, start_ms: int, end_ms: int) -> list[MetricRow]:
    """5-minute OI / ratio snapshots in ``[start_ms, end_ms]``, oldest first.

    Falls back to the REST ``/futures/data/*`` endpoints for days the
    archive has not published. Those endpoints only retain ~30 days, so
    the fallback silently yields nothing for older gaps — acceptable
    because the archive covers everything older than about a day.
    """
    rows: dict[int, MetricRow] = {}
    missing = []
    for day in _days(start_ms, end_ms):
        blob = _cached_get(f"{_VISION}/daily/metrics/{symbol}/{symbol}-metrics-{day}.zip")
        if blob is None:
            missing.append(day)
            continue
        for r in _read_zip_csv(blob):
            m = _metric_from_row(r)
            if m and start_ms <= m.timestamp <= end_ms:
                rows[m.timestamp] = m

    if missing:
        logger.debug("vision metrics gap for %s: %s", symbol, missing)
        merged: dict[int, dict] = {}
        specs = [
            ("/futures/data/openInterestHist", "sumOpenInterest", "open_interest"),
            ("/futures/data/openInterestHist", "sumOpenInterestValue", "open_interest_value"),
            ("/futures/data/globalLongShortAccountRatio", "longShortRatio", "account_ratio"),
            ("/futures/data/topLongShortPositionRatio", "longShortRatio", "top_ratio"),
            ("/futures/data/topLongShortAccountRatio", "longShortRatio", "top_account_ratio"),
            ("/futures/data/takerlongshortRatio", "buySellRatio", "taker_vol_ratio"),
        ]
        for path, src, dst in specs:
            try:
                raw = _rest_get(path, {"symbol": symbol, "period": "5m",
                                       "startTime": start_ms, "endTime": end_ms, "limit": 500})
            except requests.RequestException as exc:
                logger.debug("metrics REST fallback failed %s: %s", path, exc)
                continue
            for item in raw:
                ts = int(item["timestamp"])
                if start_ms <= ts <= end_ms:
                    merged.setdefault(ts, {})[dst] = float(item[src])
        for ts, vals in merged.items():
            if ts not in rows:
                rows[ts] = MetricRow(
                    timestamp=ts,
                    open_interest=vals.get("open_interest", 0.0),
                    open_interest_value=vals.get("open_interest_value", 0.0),
                    account_ratio=vals.get("account_ratio", 0.0),
                    top_ratio=vals.get("top_ratio", 0.0),
                    top_account_ratio=vals.get("top_account_ratio", 0.0),
                    taker_vol_ratio=vals.get("taker_vol_ratio", 0.0),
                )

    return [rows[k] for k in sorted(rows)]


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------

def funding(symbol: str, start_ms: int, end_ms: int) -> list[FundingRate]:
    """Funding settlements in ``[start_ms, end_ms]``, oldest first."""
    out: dict[int, FundingRate] = {}
    missing = []
    for month in _months(start_ms, end_ms):
        blob = _cached_get(f"{_VISION}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{month}.zip")
        if blob is None:
            missing.append(month)
            continue
        for r in _read_zip_csv(blob):
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                out[ts] = FundingRate(symbol=symbol, funding_time=ts, rate=float(r[2]))

    if missing:
        logger.debug("vision funding gap for %s: %s", symbol, missing)
        try:
            raw = _rest_get("/fapi/v1/fundingRate", {
                "symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000,
            })
        except requests.RequestException as exc:
            logger.debug("funding REST fallback failed: %s", exc)
            raw = []
        for item in raw:
            ts = int(item["fundingTime"])
            if start_ms <= ts <= end_ms and ts not in out:
                out[ts] = FundingRate(symbol=symbol, funding_time=ts,
                                      rate=float(item["fundingRate"]))

    return [out[k] for k in sorted(out)]
