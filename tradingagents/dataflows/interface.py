"""Vendor routing for the Monopoly crypto data layer.

Replaces the original stock vendor router. Same routing shape
(``TOOLS_CATEGORIES`` + ``VENDOR_METHODS`` + ``route_to_vendor`` with
fallback chain) so future extensions (e.g. CCXT as a second
market-data vendor) drop in without churning callers.
"""

from .crypto_binance import get_funding_rate, get_ohlcv, get_open_interest
from .crypto_coinglass import get_liquidations, get_long_short_ratio
from .crypto_news import get_news
from .crypto_reddit import get_reddit
from .crypto_twitter import get_tweets

from .config import get_config


class VendorRateLimitError(Exception):
    """Vendor-explicit rate limit. Caught by ``route_to_vendor`` to trigger
    fallback to the next vendor in the chain. Vendors that lack an explicit
    rate-limit signal should not raise this — generic errors bubble up to
    the caller unchanged."""


TOOLS_CATEGORIES = {
    "crypto_market_data": {
        "description": "OHLCV / funding rate / open interest for crypto perpetual futures",
        "tools": ["get_ohlcv", "get_funding_rate", "get_open_interest"],
    },
    "crypto_derivatives": {
        "description": "Derivatives metrics: liquidations and long/short account ratio",
        "tools": ["get_liquidations", "get_long_short_ratio"],
    },
    "crypto_news": {
        "description": "Crypto news headlines (CoinDesk / CoinTelegraph RSS)",
        "tools": ["get_news"],
    },
    "crypto_social_reddit": {
        "description": "Reddit posts across crypto subreddits",
        "tools": ["get_reddit"],
    },
    "crypto_social_twitter": {
        "description": "Twitter / X posts for crypto symbols (placeholder; source pending)",
        "tools": ["get_tweets"],
    },
}

VENDOR_LIST = ["binance", "coinglass", "rss", "reddit", "twitter"]

VENDOR_METHODS = {
    # crypto_market_data
    "get_ohlcv":              {"binance": get_ohlcv},
    "get_funding_rate":       {"binance": get_funding_rate},
    "get_open_interest":      {"binance": get_open_interest},
    # crypto_derivatives
    "get_liquidations":       {"coinglass": get_liquidations},
    "get_long_short_ratio":   {"coinglass": get_long_short_ratio},
    # crypto_news
    "get_news":               {"rss": get_news},
    # crypto_social
    "get_reddit":             {"reddit": get_reddit},
    "get_tweets":             {"twitter": get_tweets},
}


def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def get_vendor(category: str, method: str | None = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level."""
    config = get_config()
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]
    return config.get("data_vendors", {}).get(category, "default")


def route_to_vendor(method: str, *args, **kwargs):
    """Route ``method`` to the configured vendor, with fallback on rate limits."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(",")]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available = list(VENDOR_METHODS[method].keys())
    fallback_chain = primary_vendors.copy()
    for vendor in all_available:
        if vendor not in fallback_chain:
            fallback_chain.append(vendor)

    for vendor in fallback_chain:
        if vendor not in VENDOR_METHODS[method]:
            continue
        impl = VENDOR_METHODS[method][vendor]
        impl_func = impl[0] if isinstance(impl, list) else impl
        try:
            return impl_func(*args, **kwargs)
        except VendorRateLimitError:
            continue  # try next vendor in chain

    raise RuntimeError(f"No available vendor for '{method}'")
