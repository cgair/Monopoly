"""LangGraph tool wrappers for news data.

Monopoly fork status: these wrappers still expose the upstream
``(ticker, start_date, end_date)`` signature so callers (notably
``news_analyst.py`` and ``sentiment_analyst.py``) keep working
without churn. Internally they adapt to the crypto vendor signatures
(``crypto_news.get_news(sources, hours, symbols)``). The non-news
tools (``get_global_news``, ``get_insider_transactions``) are not
implemented in the crypto data layer; they return a clear
placeholder for now.

TODO (Week 3 — news_analyst rewrite): once the news analyst is
rewritten for crypto, redesign these tools to expose a crypto-native
surface (hours / symbols / sources) and remove the adapter shims.
"""

from datetime import datetime
from typing import Annotated, Optional

from langchain_core.tools import tool

from tradingagents.agents.utils.symbol_utils import to_base_symbol
from tradingagents.dataflows.interface import route_to_vendor


_DEFAULT_NEWS_SOURCES = ("coindesk", "cointelegraph")


def _date_window_to_hours(start_date: str, end_date: str) -> int:
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return 24 * 7
    hours = int((end_dt - start_dt).total_seconds() / 3600)
    return hours if hours > 0 else 24


def _ticker_to_symbols(ticker: str) -> tuple[str, ...] | None:
    try:
        return (to_base_symbol(ticker),)
    except ValueError:
        # Unknown ticker shape — let the news vendor return everything
        # rather than crashing the agent run.
        return None


@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol (e.g. BTC-USD)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Retrieve crypto news headlines covering the window for the given ticker.

    Sources: CoinDesk + CoinTelegraph RSS. Items are filtered by
    base-symbol keyword (e.g. BTC → Bitcoin/BTC).
    """
    hours = _date_window_to_hours(start_date, end_date)
    symbols = _ticker_to_symbols(ticker)
    return route_to_vendor("get_news", _DEFAULT_NEWS_SOURCES, hours, symbols)


@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[Optional[int], "Days to look back"] = None,
    limit: Annotated[Optional[int], "Max articles to return"] = None,
) -> str:
    """Retrieve macro/global news.

    Monopoly fork: a crypto-specific global-news pipeline is not wired
    yet. Returns a placeholder so dependent analyst nodes degrade
    gracefully rather than crash. See TODO in module docstring.
    """
    return (
        "# Global news (crypto/macro) — not yet available in Monopoly\n"
        "# This tool will be wired up when the news analyst is rewritten.\n"
        "<no global news available>"
    )


@tool
def get_insider_transactions(
    ticker: Annotated[str, "Ticker symbol"],
) -> str:
    """Insider transactions.

    Monopoly fork: not meaningful for crypto perpetual futures (there is
    no public insider-transaction registry equivalent to SEC filings).
    Returns a placeholder so dependent analyst nodes degrade gracefully.
    """
    return (
        f"# Insider transactions for {ticker} — not applicable to crypto\n"
        "<insider transaction data not available for crypto assets>"
    )
