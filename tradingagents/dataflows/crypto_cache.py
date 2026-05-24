"""Disk-backed cache for crypto vendor data.

Two storage backends with a deliberate split:

* **Time-series** (``Bar`` / ``FundingRate`` / ``OpenInterest``) → parquet
  sharded by ``{kind}/{symbol}/[{interval}/]{yyyy-mm}.parquet``. Pandas
  slices read quickly, files stay small, and monthly rotation makes the
  layout obvious to inspect or rsync to the external SSD.

* **Events** (``Liquidation`` / ``LongShortRatio`` / ``NewsItem`` /
  ``SocialPost``) → ``events.sqlite``. Variable shapes plus dedup-by-id
  make a row-with-index store a better fit than columnar parquet.

A tiny ``cache_meta.sqlite`` records ``(scope_key, fetched_at_ms)`` so
vendor functions can answer "is the latest fetch still within TTL?".
The cache layer never enforces TTL itself — it only records when data
landed; vendor functions own the policy (see TTL table in
``docs/dataflows-decisions.md`` §4.2).

Storage root: ``$MONOPOLY_DATA_ROOT`` env var, default
``~/.monopoly/data``. Re-pointing the env var is all it takes to mount
an external SSD.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .crypto_types import (
    Bar,
    FundingRate,
    Liquidation,
    LongShortRatio,
    NewsItem,
    OpenInterest,
    SocialPost,
)

_DEFAULT_ROOT = "~/.monopoly/data"


def root() -> Path:
    """Resolve storage root from env (re-read each call so tests can monkey-patch)."""
    return Path(os.environ.get("MONOPOLY_DATA_ROOT", os.path.expanduser(_DEFAULT_ROOT)))


def _ensure(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _yyyy_mm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _months_between(start_ms: int, end_ms: int) -> list[str]:
    """Inclusive list of yyyy-mm partition labels covering [start, end]."""
    cur = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(day=1)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    out: list[str] = []
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(year=cur.year + 1, month=1)
               if cur.month == 12 else cur.replace(month=cur.month + 1))
    return out


# ---------------------------------------------------------------------------
# Parquet helpers
# ---------------------------------------------------------------------------

def _upsert_parquet(path: Path, new_df: pd.DataFrame, dedup_col: str, sort_col: str) -> int:
    """Merge ``new_df`` into ``path``, deduplicating on ``dedup_col``.

    Rewrites the whole partition file each time. Fine at our scale
    (a month of 15m bars is ~3k rows; news/events are far smaller).
    Returns the count of rows in ``new_df`` (caller's "rows offered").
    """
    _ensure(path.parent)
    if path.exists():
        existing = pd.read_parquet(path)
        existing = existing[~existing[dedup_col].isin(new_df[dedup_col])]
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = merged.sort_values(sort_col).reset_index(drop=True)
    merged.to_parquet(path, index=False)
    return len(new_df)


def _bar_path(symbol: str, interval: str, yyyy_mm: str) -> Path:
    return root() / "ohlcv" / symbol / interval / f"{yyyy_mm}.parquet"


def _funding_path(symbol: str, yyyy_mm: str) -> Path:
    return root() / "funding" / symbol / f"{yyyy_mm}.parquet"


def _oi_path(symbol: str, interval: str, yyyy_mm: str) -> Path:
    return root() / "oi" / symbol / interval / f"{yyyy_mm}.parquet"


# ---------- Bars ----------

def write_bars(bars: Iterable[Bar]) -> int:
    bars = list(bars)
    if not bars:
        return 0
    df = pd.DataFrame([b.__dict__ for b in bars])
    df["_ym"] = df["open_time"].map(_yyyy_mm)
    written = 0
    for (symbol, interval, ym), grp in df.groupby(["symbol", "interval", "_ym"], sort=False):
        grp = grp.drop(columns="_ym")
        written += _upsert_parquet(_bar_path(symbol, interval, ym), grp,
                                   dedup_col="open_time", sort_col="open_time")
    return written


def read_bars(symbol: str, interval: str, *, start_ms: int, end_ms: int) -> list[Bar]:
    frames = []
    for ym in _months_between(start_ms, end_ms):
        p = _bar_path(symbol, interval, ym)
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["open_time"] >= start_ms) & (df["open_time"] <= end_ms)]
    df = df.sort_values("open_time").reset_index(drop=True)
    return [Bar(**row) for row in df.to_dict("records")]


# ---------- Funding rates ----------

def write_funding(rates: Iterable[FundingRate]) -> int:
    rates = list(rates)
    if not rates:
        return 0
    df = pd.DataFrame([r.__dict__ for r in rates])
    df["_ym"] = df["funding_time"].map(_yyyy_mm)
    written = 0
    for (symbol, ym), grp in df.groupby(["symbol", "_ym"], sort=False):
        grp = grp.drop(columns="_ym")
        written += _upsert_parquet(_funding_path(symbol, ym), grp,
                                   dedup_col="funding_time", sort_col="funding_time")
    return written


def read_funding(symbol: str, *, start_ms: int, end_ms: int) -> list[FundingRate]:
    frames = []
    for ym in _months_between(start_ms, end_ms):
        p = _funding_path(symbol, ym)
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["funding_time"] >= start_ms) & (df["funding_time"] <= end_ms)]
    df = df.sort_values("funding_time").reset_index(drop=True)
    return [FundingRate(**row) for row in df.to_dict("records")]


# ---------- Open Interest ----------

def write_oi(items: Iterable[OpenInterest]) -> int:
    items = list(items)
    if not items:
        return 0
    df = pd.DataFrame([x.__dict__ for x in items])
    df["_ym"] = df["timestamp"].map(_yyyy_mm)
    written = 0
    for (symbol, interval, ym), grp in df.groupby(["symbol", "interval", "_ym"], sort=False):
        grp = grp.drop(columns="_ym")
        written += _upsert_parquet(_oi_path(symbol, interval, ym), grp,
                                   dedup_col="timestamp", sort_col="timestamp")
    return written


def read_oi(symbol: str, interval: str, *, start_ms: int, end_ms: int) -> list[OpenInterest]:
    frames = []
    for ym in _months_between(start_ms, end_ms):
        p = _oi_path(symbol, interval, ym)
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)]
    df = df.sort_values("timestamp").reset_index(drop=True)
    return [OpenInterest(**row) for row in df.to_dict("records")]


# ---------------------------------------------------------------------------
# Events SQLite
# ---------------------------------------------------------------------------

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    side TEXT,
    qty REAL,
    price REAL
);
CREATE INDEX IF NOT EXISTS idx_liq_sym_ts ON liquidations(symbol, timestamp);

CREATE TABLE IF NOT EXISTS long_short (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    long_account REAL,
    short_account REAL,
    ratio REAL
);
CREATE INDEX IF NOT EXISTS idx_ls_sym_int_ts ON long_short(symbol, interval, timestamp);

CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    source TEXT,
    title TEXT,
    summary TEXT,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(timestamp);

CREATE TABLE IF NOT EXISTS social (
    id TEXT PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    platform TEXT,
    author TEXT,
    text TEXT,
    engagement TEXT,
    url TEXT
);
CREATE INDEX IF NOT EXISTS idx_social_plat_ts ON social(platform, timestamp);
"""


def _events_db() -> sqlite3.Connection:
    p = root() / "events.sqlite"
    _ensure(p.parent)
    conn = sqlite3.connect(p)
    conn.executescript(_EVENTS_SCHEMA)
    return conn


def upsert_liquidations(items: Iterable[Liquidation]) -> int:
    rows = [(i.id, i.symbol, i.timestamp, i.side, i.qty, i.price) for i in items]
    if not rows:
        return 0
    with _events_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO liquidations VALUES (?,?,?,?,?,?)", rows,
        )
    return len(rows)


def read_liquidations(*, symbol: str | None = None,
                      since_ms: int, until_ms: int | None = None) -> list[Liquidation]:
    until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
    sql = "SELECT id, symbol, timestamp, side, qty, price FROM liquidations WHERE timestamp BETWEEN ? AND ?"
    params: list = [since_ms, until_ms]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += " ORDER BY timestamp"
    with _events_db() as conn:
        cur = conn.execute(sql, params)
        return [Liquidation(id=r[0], symbol=r[1], timestamp=r[2],
                            side=r[3], qty=r[4], price=r[5]) for r in cur]


def upsert_long_short(items: Iterable[LongShortRatio]) -> int:
    rows = [(i.id, i.symbol, i.interval, i.timestamp,
             i.long_account, i.short_account, i.ratio) for i in items]
    if not rows:
        return 0
    with _events_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO long_short VALUES (?,?,?,?,?,?,?)", rows,
        )
    return len(rows)


def read_long_short(*, symbol: str | None = None, interval: str | None = None,
                    since_ms: int, until_ms: int | None = None) -> list[LongShortRatio]:
    until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
    sql = ("SELECT id, symbol, interval, timestamp, long_account, short_account, ratio "
           "FROM long_short WHERE timestamp BETWEEN ? AND ?")
    params: list = [since_ms, until_ms]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if interval:
        sql += " AND interval = ?"
        params.append(interval)
    sql += " ORDER BY timestamp"
    with _events_db() as conn:
        cur = conn.execute(sql, params)
        return [LongShortRatio(id=r[0], symbol=r[1], interval=r[2], timestamp=r[3],
                               long_account=r[4], short_account=r[5], ratio=r[6])
                for r in cur]


def upsert_news(items: Iterable[NewsItem]) -> int:
    rows = [(i.id, i.timestamp, i.source, i.title, i.summary, i.url) for i in items]
    if not rows:
        return 0
    with _events_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?)", rows,
        )
    return len(rows)


def read_news(*, sources: Iterable[str] | None = None,
              since_ms: int, until_ms: int | None = None) -> list[NewsItem]:
    until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
    sql = "SELECT id, timestamp, source, title, summary, url FROM news WHERE timestamp BETWEEN ? AND ?"
    params: list = [since_ms, until_ms]
    if sources:
        srcs = list(sources)
        sql += " AND source IN (" + ",".join("?" * len(srcs)) + ")"
        params.extend(srcs)
    sql += " ORDER BY timestamp DESC"
    with _events_db() as conn:
        cur = conn.execute(sql, params)
        return [NewsItem(id=r[0], timestamp=r[1], source=r[2],
                         title=r[3], summary=r[4], url=r[5]) for r in cur]


def upsert_social(items: Iterable[SocialPost]) -> int:
    rows = [(i.id, i.timestamp, i.platform, i.author, i.text,
             json.dumps(i.engagement, ensure_ascii=False), i.url) for i in items]
    if not rows:
        return 0
    with _events_db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO social VALUES (?,?,?,?,?,?,?)", rows,
        )
    return len(rows)


def read_social(*, platform: str | None = None,
                since_ms: int, until_ms: int | None = None) -> list[SocialPost]:
    until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
    sql = ("SELECT id, timestamp, platform, author, text, engagement, url FROM social "
           "WHERE timestamp BETWEEN ? AND ?")
    params: list = [since_ms, until_ms]
    if platform:
        sql += " AND platform = ?"
        params.append(platform)
    sql += " ORDER BY timestamp DESC"
    with _events_db() as conn:
        cur = conn.execute(sql, params)
        return [SocialPost(id=r[0], timestamp=r[1], platform=r[2],
                           author=r[3], text=r[4],
                           engagement=json.loads(r[5]) if r[5] else {},
                           url=r[6]) for r in cur]


# ---------------------------------------------------------------------------
# Cache metadata: per-scope "last successful fetch" timestamps for TTL checks
# ---------------------------------------------------------------------------

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_meta (
    scope_key TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL
);
"""


def _meta_db() -> sqlite3.Connection:
    p = root() / "cache_meta.sqlite"
    _ensure(p.parent)
    conn = sqlite3.connect(p)
    conn.executescript(_META_SCHEMA)
    return conn


def get_meta(scope_key: str) -> int | None:
    """Return ``fetched_at_ms`` for ``scope_key`` or None if never recorded."""
    with _meta_db() as conn:
        row = conn.execute(
            "SELECT fetched_at FROM cache_meta WHERE scope_key = ?", (scope_key,),
        ).fetchone()
    return row[0] if row else None


def set_meta(scope_key: str, fetched_at_ms: int | None = None) -> None:
    ts = fetched_at_ms if fetched_at_ms is not None else int(time.time() * 1000)
    with _meta_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta VALUES (?, ?)", (scope_key, ts),
        )


def is_fresh(scope_key: str, ttl_sec: int) -> bool:
    """True iff a fetched_at exists for ``scope_key`` and is within TTL."""
    last = get_meta(scope_key)
    if last is None:
        return False
    return (int(time.time() * 1000) - last) < ttl_sec * 1000
