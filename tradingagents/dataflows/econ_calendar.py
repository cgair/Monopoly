"""Forward-looking economic calendar (ForexFactory weekly JSON).

Free, no API key: ``https://nfs.faireconomy.media/ff_calendar_thisweek.json``
lists this week's macro events with timestamps and impact levels. This is
an *advisory* layer, not a safety layer: the PM prompt gets a warning
line when a high-impact USD event (FOMC / CPI / NFP …) is inside the
look-ahead window, and the risk gate can optionally hard-block new
entries around events (``futures_macro_block_hours``, default off).

Failure semantics are deliberately fail-open — a broken calendar fetch
must never block trading or crash the graph. All public helpers degrade
to "no events" and log a warning.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_USER_AGENT = "monopoly/0.1 (+https://github.com/cgair/Monopoly)"
_HTTP_TIMEOUT = 10.0

# The weekly file changes rarely; cache in-process to keep a graph run
# at a single fetch even when both PM and gate consult the calendar.
_CACHE_TTL_SEC = 6 * 3600
_cache: tuple[float, list["EconEvent"]] | None = None


@dataclass(frozen=True)
class EconEvent:
    title: str
    country: str      # currency code, e.g. "USD"
    impact: str       # "High" / "Medium" / "Low" / "Holiday"
    when: datetime    # timezone-aware UTC


def _parse_events(raw: list) -> list[EconEvent]:
    events = []
    for r in raw:
        try:
            when = datetime.fromisoformat(r["date"])
            # A naive timestamp would otherwise be read as *server-local*
            # time by astimezone(); the feed is UTC-offset-annotated, so
            # treat a missing offset as UTC rather than shifting silently.
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            else:
                when = when.astimezone(timezone.utc)
            events.append(EconEvent(
                title=str(r.get("title") or "").strip(),
                country=str(r.get("country") or "").strip().upper(),
                impact=str(r.get("impact") or "").strip(),
                when=when,
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return events


def fetch_calendar() -> list[EconEvent]:
    """This week's events, cached in-process. Returns [] on any failure."""
    global _cache
    now = time.time()
    if _cache is not None and now - _cache[0] < _CACHE_TTL_SEC:
        return _cache[1]
    try:
        r = requests.get(_URL, timeout=_HTTP_TIMEOUT,
                         headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        events = _parse_events(r.json())
    except Exception as exc:
        logger.warning("econ calendar fetch failed (fail-open, no events): %s", exc)
        return _cache[1] if _cache is not None else []
    _cache = (now, events)
    return events


def upcoming_high_impact(
    hours_ahead: float,
    *,
    currencies: tuple[str, ...] = ("USD",),
    now: datetime | None = None,
    events: list[EconEvent] | None = None,
) -> list[EconEvent]:
    """High-impact events for ``currencies`` within the next ``hours_ahead``
    hours, soonest first. ``events`` is injectable for tests."""
    if now is None:
        now = datetime.now(timezone.utc)
    if events is None:
        events = fetch_calendar()
    horizon = now + timedelta(hours=hours_ahead)
    hits = [e for e in events
            if e.impact.lower() == "high"
            and e.country in currencies
            and now <= e.when <= horizon]
    return sorted(hits, key=lambda e: e.when)


def format_macro_event_warning(
    hours_ahead: float,
    *,
    now: datetime | None = None,
    events: list[EconEvent] | None = None,
) -> str:
    """Prompt-ready warning block, or empty string when the window is clear
    (or the calendar is unavailable — advisory layer, fail-open)."""
    if now is None:
        now = datetime.now(timezone.utc)
    hits = upcoming_high_impact(hours_ahead, now=now, events=events)
    if not hits:
        return ""
    lines = [f"## ⚠ Upcoming high-impact macro events (next {hours_ahead:g}h)"]
    for e in hits:
        delta_h = (e.when - now).total_seconds() / 3600
        lines.append(f"- {e.title} ({e.country}) at "
                     f"{e.when.strftime('%Y-%m-%d %H:%M UTC')} — in {delta_h:.1f}h")
    lines.append(
        "Macro prints move crypto perps violently (stop-hunt wicks, "
        "liquidation cascades). Weigh reducing leverage / sizing, widening "
        "stops, or staying Flat until the print is absorbed."
    )
    return "\n".join(lines)
