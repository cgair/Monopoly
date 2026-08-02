"""Economic calendar: parsing, windowing, prompt warning, fail-open."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests

from tradingagents.dataflows import econ_calendar as ec


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _event(title: str, hours_from_now: float, *, country="USD", impact="High") -> ec.EconEvent:
    return ec.EconEvent(title=title, country=country, impact=impact,
                        when=NOW + timedelta(hours=hours_from_now))


@pytest.fixture(autouse=True)
def clear_cache():
    ec._cache = None
    yield
    ec._cache = None


@pytest.mark.unit
def test_parse_events_skips_malformed_rows():
    raw = [
        {"title": "FOMC Statement", "country": "USD", "impact": "High",
         "date": "2026-07-31T14:00:00-04:00"},
        {"title": "broken", "country": "USD", "impact": "High", "date": "not-a-date"},
        {"country": "USD", "impact": "High"},   # no date at all
    ]
    events = ec._parse_events(raw)
    assert len(events) == 1
    assert events[0].title == "FOMC Statement"
    assert events[0].when.tzinfo is not None
    assert events[0].when.hour == 18   # 14:00 -04:00 → 18:00 UTC


@pytest.mark.unit
def test_upcoming_high_impact_windows_and_filters():
    events = [
        _event("FOMC Statement", 6),
        _event("Non-Farm Payrolls", 30),          # outside 12h window
        _event("German CPI", 2, country="EUR"),   # wrong currency
        _event("Crude Inventories", 3, impact="Medium"),
        _event("CPI y/y", 1),
    ]
    hits = ec.upcoming_high_impact(12, now=NOW, events=events)
    assert [e.title for e in hits] == ["CPI y/y", "FOMC Statement"]


@pytest.mark.unit
def test_format_warning_contains_events_and_countdown():
    events = [_event("FOMC Statement", 6.5)]
    out = ec.format_macro_event_warning(12, now=NOW, events=events)
    assert "FOMC Statement" in out
    assert "in 6.5h" in out
    assert "high-impact" in out.lower()


@pytest.mark.unit
def test_format_warning_empty_when_window_clear():
    events = [_event("Non-Farm Payrolls", 48)]
    assert ec.format_macro_event_warning(12, now=NOW, events=events) == ""


@pytest.mark.unit
def test_fetch_calendar_fail_open_returns_empty():
    with patch.object(ec.requests, "get",
                      side_effect=requests.ConnectionError("down")):
        assert ec.fetch_calendar() == []


@pytest.mark.unit
def test_fetch_calendar_serves_cached_on_failure():
    payload = [{"title": "FOMC Statement", "country": "USD", "impact": "High",
                "date": "2026-07-31T14:00:00-04:00"}]

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    with patch.object(ec.requests, "get", return_value=_Resp()):
        first = ec.fetch_calendar()
    assert len(first) == 1

    ec._cache = (0.0, first)   # force TTL expiry, keep cached events
    with patch.object(ec.requests, "get",
                      side_effect=requests.ConnectionError("down")):
        again = ec.fetch_calendar()
    assert again == first
