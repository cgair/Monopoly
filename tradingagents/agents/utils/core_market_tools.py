"""LangGraph tool wrappers for crypto market data.

Each ``@tool`` takes the agent-native ``BASE-USD`` ticker (e.g.
``BTC-USD``), converts to Binance Futures format
(``BTCUSDT``) via :mod:`symbol_utils`, then dispatches through
``route_to_vendor`` so the configured vendor (currently always
binance) handles the actual fetch.

Returned strings are prompt-ready plaintext as defined by the
underlying vendor modules.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_market_data(
    symbol: Annotated[str, "Ticker in BASE-USD form, e.g. BTC-USD"],
    interval: Annotated[str, "K-line interval: 15m / 1h / 4h / 1d"] = "1h",
    limit: Annotated[int, "Number of most recent bars to retrieve (max 1500)"] = 200,
) -> str:
    """Retrieve recent OHLCV bars for a crypto perpetual futures pair.

    Returns a CSV-style table of the latest ``limit`` bars on the
    requested interval. Multi-interval analysis is encouraged — call
    this tool once per interval you want to inspect (15m for entries,
    4h/1d for the dominant trend).
    """
    binance_symbol = to_binance_symbol(symbol)
    return route_to_vendor("get_ohlcv", binance_symbol, interval, limit)


@tool
def get_funding_rate(
    symbol: Annotated[str, "Ticker in BASE-USD form, e.g. BTC-USD"],
    limit: Annotated[int, "Number of most recent funding settlements"] = 50,
) -> str:
    """Retrieve recent perpetual-futures funding rate history.

    Binance settles funding every 8h. Persistently elevated positive
    funding indicates crowded longs (mean-reversion candidate);
    persistent negative funding indicates crowded shorts.
    """
    binance_symbol = to_binance_symbol(symbol)
    return route_to_vendor("get_funding_rate", binance_symbol, limit)


@tool
def get_open_interest(
    symbol: Annotated[str, "Ticker in BASE-USD form, e.g. BTC-USD"],
    interval: Annotated[str, "Sampling interval: 5m / 15m / 1h / 4h / 1d"] = "1h",
    limit: Annotated[int, "Number of most recent OI samples"] = 100,
) -> str:
    """Retrieve open-interest history for a perpetual futures pair.

    OI rising with price → new money entering on the same side
    (trend confirmation). OI rising while price flatlines →
    leverage building up, watch for squeeze. OI falling → position
    unwinding.
    """
    binance_symbol = to_binance_symbol(symbol)
    return route_to_vendor("get_open_interest", binance_symbol, interval, limit)
