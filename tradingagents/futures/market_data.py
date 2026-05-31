"""Live mark-price fetch for the futures executor pipeline.

The risk gate is a pure function over the FuturesDecision; the executor
needs a current mark price when the decision uses market entry
(``entry_price is None``). This module exposes a tiny fetcher and the
LangGraph node that wires it into the crypto-mode pipeline.

Both adapters (dryrun / testnet) can run without it when the PM sets an
explicit ``entry_price`` — the node falls back to ``entry_price`` in
that case so the graph doesn't pay an HTTP round-trip we don't need.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from tradingagents.agents.utils.symbol_utils import to_binance_symbol

logger = logging.getLogger(__name__)


_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_HTTP_TIMEOUT = 5.0


def fetch_mark_price(state_ticker: str) -> Optional[float]:
    """Return the current mark price for ``state_ticker`` (e.g. ``"BTC-USD"``).

    Hits Binance Futures' ``premiumIndex`` endpoint (unauthenticated;
    no API key needed for reads). Returns ``None`` on any failure so
    callers can degrade gracefully — the executor node maps a missing
    mark price into a ``trade_skipped`` event with a clear reason.
    """
    symbol = to_binance_symbol(state_ticker)
    try:
        response = requests.get(
            _PREMIUM_INDEX_URL,
            params={"symbol": symbol},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return float(payload["markPrice"])
    except Exception as exc:
        logger.warning("fetch_mark_price(%s) failed: %s", state_ticker, exc)
        return None


def create_mark_price_node():
    """LangGraph node that populates ``state["mark_price"]`` before the executor.

    Behavior:
    - No intent (``execution_intent is None``) → write ``mark_price = None``.
      The executor short-circuits on no-intent anyway.
    - Limit order (``intent["entry_price"]`` set) → reuse entry_price as the
      reference so we skip the HTTP fetch.
    - Market order → fetch from Binance. Result may be ``None`` on
      network failure; the executor node logs ``trade_skipped`` in that
      case.
    """

    def mark_price_node(state) -> dict:
        intent = state.get("execution_intent")
        if intent is None:
            return {"mark_price": None}
        if intent.get("entry_price") is not None:
            return {"mark_price": float(intent["entry_price"])}
        price = fetch_mark_price(intent["symbol"])
        return {"mark_price": price}

    return mark_price_node
