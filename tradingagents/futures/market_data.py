"""Live mark-price fetch for the futures executor pipeline.

The risk gate and the executor both need a current mark price when the
decision uses market entry (``entry_price is None``): the gate to check
the stop sits on the losing side of the price, the executor to size the
order. This module exposes a tiny fetcher and the LangGraph node that
wires it into the crypto-mode pipeline *before* the gate — with the
fetch after the gate, market-order stops went unvalidated (2026-08-08
review gap: two replayed shorts with profit-side stops were approved).

Both adapters (dryrun / testnet) can run without it when the PM sets an
explicit ``entry_price`` — the node falls back to ``entry_price`` in
that case so the graph doesn't pay an HTTP round-trip we don't need.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from tradingagents.agents.schemas import FuturesSide
from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.futures.executor import resolve_executor_mode

logger = logging.getLogger(__name__)


# Reference price must come from the venue orders fill on. dryrun places
# no orders — its conclusions are executed by a human on mainnet, so
# mainnet is the right reference. testnet fills on testnet, whose thin
# books can sit percent-level away from mainnet — sizing against the
# mainnet price there invalidates every margin/stop-distance check.
_PREMIUM_INDEX_URLS = {
    "dryrun": "https://fapi.binance.com/fapi/v1/premiumIndex",
    "testnet": "https://testnet.binancefuture.com/fapi/v1/premiumIndex",
}
_HTTP_TIMEOUT = 5.0


def fetch_mark_price(state_ticker: str, *, mode: str = "dryrun") -> Optional[float]:
    """Return the current mark price for ``state_ticker`` (e.g. ``"BTC-USD"``).

    Hits the ``premiumIndex`` endpoint of the venue selected by ``mode``
    (unauthenticated; no API key needed for reads). Returns ``None`` on
    any failure so callers can degrade gracefully — the risk gate maps a
    missing mark price on a market order into a fail-closed rejection.
    """
    symbol = to_binance_symbol(state_ticker)
    try:
        response = requests.get(
            _PREMIUM_INDEX_URLS[mode],
            params={"symbol": symbol},
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        return float(payload["markPrice"])
    except Exception as exc:
        logger.warning("fetch_mark_price(%s) failed: %s", state_ticker, exc)
        return None


def create_mark_price_node(config: Optional[dict] = None):
    """LangGraph node that populates ``state["mark_price"]`` before the risk gate.

    Runs between Portfolio Manager and Risk Gate, so it reads the PM's
    ``final_decision_structured`` — no execution intent exists yet.
    The fetch venue follows :func:`resolve_executor_mode` so the price
    the gate and executor size against is the one orders will fill at.

    Behavior:
    - No structured decision, or side == Flat → ``mark_price = None``.
      Nothing to price; the gate no-ops on these anyway.
    - Limit decision (``entry_price`` set) → reuse entry_price as the
      reference so we skip the HTTP fetch.
    - Market decision → fetch from Binance. Result may be ``None`` on
      network failure; the gate then rejects fail-closed.
    """

    mode = resolve_executor_mode(config)

    def mark_price_node(state) -> dict:
        decision = state.get("final_decision_structured")
        if decision is None or decision.side == FuturesSide.FLAT:
            return {"mark_price": None}
        if decision.entry_price is not None:
            return {"mark_price": float(decision.entry_price)}
        price = fetch_mark_price(state["company_of_interest"], mode=mode)
        return {"mark_price": price}

    return mark_price_node
