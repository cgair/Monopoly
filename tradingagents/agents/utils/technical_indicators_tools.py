"""LangGraph tool wrapper for technical indicators on cached bars.

Reuses ``dataflows.crypto_indicators`` (stockstats over Binance bars).
Converts the agent-native ``BASE-USD`` ticker to ``BASEUSDT`` so the
underlying indicator engine talks to the Binance-native cache.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.dataflows.crypto_indicators import (
    SUPPORTED_INDICATORS,
    get_indicators as _get_indicators_plaintext,
)


@tool
def get_indicators(
    symbol: Annotated[str, "Ticker in BASE-USD form, e.g. BTC-USD"],
    indicator: Annotated[
        str,
        "Indicator name from the supported list (rsi, macd, macds, macdh, "
        "close_10_ema, close_50_sma, close_200_sma, boll, boll_ub, boll_lb, "
        "atr, vwma, mfi). Multiple names may be comma-separated.",
    ],
    interval: Annotated[str, "K-line interval used for the indicator"] = "1h",
    look_back_bars: Annotated[int, "How many recent bars to report"] = 60,
) -> str:
    """Compute one or more technical indicators on the configured pair.

    The underlying bars are fetched on demand via the binance vendor;
    a previous ``get_market_data`` call on the same (symbol, interval)
    just warms the cache.

    Call this tool **once per indicator** to keep prompts grep-friendly,
    but a comma-separated list is tolerated as an LLM-mistake guardrail.
    """
    binance_symbol = to_binance_symbol(symbol)
    names = [n.strip().lower() for n in indicator.split(",") if n.strip()]

    blocks: list[str] = []
    for name in names:
        if name not in SUPPORTED_INDICATORS:
            blocks.append(
                f"# Indicator '{name}' is not supported. "
                f"Choose from: {', '.join(sorted(SUPPORTED_INDICATORS))}"
            )
            continue
        blocks.append(_get_indicators_plaintext(
            binance_symbol, name, interval, look_back_bars=look_back_bars,
        ))
    return "\n\n".join(blocks) if blocks else "# No indicator requested."
