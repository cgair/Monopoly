"""Symbol-format conversion for the Monopoly fork.

Agent state holds tickers in the TradingAgents-native ``BASE-USD``
form (e.g. ``BTC-USD``, ``ETH-USD``), inherited from the upstream
crypto-asset support. The data layer talks to Binance Futures, which
expects ``BASEUSDT`` (e.g. ``BTCUSDT``, ``ETHUSDT``).

This module performs the conversion at the agent-tool boundary so
that ``dataflows/`` modules remain vendor-native and the
``-USD`` convention stays scoped to the LangGraph layer.
"""

from __future__ import annotations

# Aliases for assets whose Binance Futures perpetual is quoted in a
# non-default stablecoin (rare; extend as needed).
_DIRECT_OVERRIDES: dict[str, str] = {
    # "FOOUSDT": "FOOUSD",  # placeholder for future exceptions
}


def to_binance_symbol(state_ticker: str) -> str:
    """Convert a TradingAgents state ticker to the Binance Futures symbol.

    Pass-through cases:
      * Already-Binance-style (``BTCUSDT``) — returned unchanged after
        sanity-stripping whitespace and upper-casing.

    Conversion cases:
      * ``BTC-USD`` → ``BTCUSDT``
      * ``btc-usd`` → ``BTCUSDT`` (case-insensitive)

    Raises:
        ValueError: when the input cannot be reduced to a recognised
        ``BASE-USD`` or ``BASEUSDT`` shape.
    """
    if not state_ticker:
        raise ValueError("symbol is empty")
    s = state_ticker.strip().upper().replace(" ", "")

    if s in _DIRECT_OVERRIDES:
        return _DIRECT_OVERRIDES[s]

    # Already Binance-style: ends with USDT and contains no dash.
    if "-" not in s and s.endswith("USDT") and len(s) > 4:
        return s

    # TradingAgents-style: BASE-USD → BASEUSDT
    if s.endswith("-USD"):
        base = s[:-4]
        if not base:
            raise ValueError(f"symbol '{state_ticker}' is missing a base asset")
        return f"{base}USDT"

    # Stablecoin-quoted variants we don't currently handle (e.g. -USDC).
    raise ValueError(
        f"unrecognised symbol '{state_ticker}'; expected BASE-USD or BASEUSDT"
    )


def to_base_symbol(state_ticker: str) -> str:
    """Return just the base asset code (e.g. ``BTC-USD`` → ``BTC``).

    Used by vendors that filter by base symbol — Reddit query mapping,
    news keyword filtering, Twitter (when wired) — where the quote
    asset is irrelevant.
    """
    binance = to_binance_symbol(state_ticker)
    if binance.endswith("USDT"):
        return binance[:-4]
    return binance
