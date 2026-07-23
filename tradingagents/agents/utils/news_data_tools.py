"""LangGraph tool wrappers for crypto news data.

Monopoly fork: the original (ticker, start_date, end_date) adapter has
been retired in favour of crypto-native signatures that mirror the
underlying ``dataflows.crypto_news`` surface (sources + hours + optional
symbol filter).
"""

from typing import Annotated, Optional

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


_DEFAULT_SOURCES = ("coindesk", "cointelegraph")


def _normalize_sources(sources: Optional[list[str]]) -> tuple[str, ...]:
    if not sources:
        return _DEFAULT_SOURCES
    return tuple(s.strip().lower() for s in sources if s and s.strip())


@tool
def get_news(
    symbols: Annotated[list[str], "Base asset codes to filter on, e.g. ['BTC']. Use base code only, not BASE-USD."],
    hours: Annotated[int, "Look-back window in hours (e.g. 24 for last day, 168 for last week)"] = 24,
    sources: Annotated[
        Optional[list[str]],
        "RSS sources, subset of ['coindesk','cointelegraph']. Omit to use both.",
    ] = None,
) -> str:
    """Retrieve crypto news headlines filtered to the given base symbols.

    Symbol filter matches keywords in title or summary (e.g. ``BTC`` →
    matches "Bitcoin" or "BTC"). Use this FIRST for asset-specific
    catalysts (ETF flows, regulation, listings, security incidents).
    Pass a single-element list (``['BTC']``) — multi-symbol queries
    return the union, not per-symbol breakdown.
    """
    src = _normalize_sources(sources)
    syms = tuple(s.strip().upper() for s in symbols if s and s.strip()) or None
    return route_to_vendor("get_news", src, hours, syms)


@tool
def get_global_news(
    hours: Annotated[int, "Look-back window in hours (e.g. 48 for last two days)"] = 48,
    sources: Annotated[
        Optional[list[str]],
        "RSS sources, subset of ['coindesk','cointelegraph']. Omit to use both.",
    ] = None,
) -> str:
    """Retrieve broad crypto industry news WITHOUT symbol filter.

    Same RSS feeds as ``get_news`` but returns every recent article
    regardless of asset. Use this only when ``get_news`` returned thin
    coverage for the target asset and you need broader market /
    regulatory / industry context (exchange news, macro catalysts that
    affect crypto generally, sector rotation signals).
    """
    src = _normalize_sources(sources)
    return route_to_vendor("get_news", src, hours, None)
