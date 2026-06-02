"""Smoke test: place one tiny live order on Binance Futures Testnet.

Validates the full executor path end-to-end *without* the LLM graph:
fetch live mark price → build ExecutionIntent → TestnetExecutor places
open + STOP_MARKET + TAKE_PROFIT_MARKET, all with closePosition=True on
the conditionals.

Run::

    .venv/bin/python scripts/smoke_testnet_executor.py

Pre-conditions:
- ``.env`` has ``BINANCE_TESTNET_API_KEY`` and ``BINANCE_TESTNET_API_SECRET``.
- Step 2 (``futures_account()`` auth probe) already passed.

Sizing is intentionally tiny:
- risk_pct = 0.001 (0.1% of equity → 5 USDT risk on a 5000 USDT account)
- leverage = 2x
- stop  = mark - 500 USDT
- TP    = mark + 1000 USDT (2:1 reward:risk)

After running, verify on https://testnet.binancefuture.com:
- Positions tab → one open BTC long
- Open Orders tab → STOP_MARKET + TAKE_PROFIT_MARKET conditionals
"""

from __future__ import annotations

import os
from pprint import pprint

from dotenv import load_dotenv

from tradingagents.agents.schemas import FuturesSide
from tradingagents.futures.executor import TestnetExecutor
from tradingagents.futures.market_data import fetch_mark_price
from tradingagents.futures.risk_gate import ExecutionIntent
from tradingagents.futures.risk_state import utcnow_iso


SYMBOL = "BTC-USD"
RISK_PCT = 0.001
LEVERAGE = 2.0
STOP_OFFSET_USD = 500.0
TP_OFFSET_USD = 1000.0


def main() -> None:
    load_dotenv()

    api_key = os.environ["BINANCE_TESTNET_API_KEY"]
    api_secret = os.environ["BINANCE_TESTNET_API_SECRET"]

    print(f"Fetching live mark price for {SYMBOL}…")
    mark = fetch_mark_price(SYMBOL)
    if mark is None:
        raise SystemExit("mark price fetch failed; proxy / network issue")
    print(f"  mark = {mark:.2f} USDT")

    stop = round(mark - STOP_OFFSET_USD, 1)
    take_profit = round(mark + TP_OFFSET_USD, 1)
    print(f"  stop = {stop:.2f}  tp = {take_profit:.2f}  "
          f"R:R = {(TP_OFFSET_USD / STOP_OFFSET_USD):.1f}")

    intent = ExecutionIntent(
        intent_id="smoke-1",
        symbol=SYMBOL,
        side=FuturesSide.LONG,
        leverage=LEVERAGE,
        risk_pct=RISK_PCT,
        entry_price=None,           # market order
        stop_loss=stop,
        take_profit=take_profit,
        created_at=utcnow_iso(),
    )

    executor = TestnetExecutor(api_key, api_secret)
    print(f"\nPlacing order on testnet… (risk_pct={RISK_PCT}, leverage={LEVERAGE}x)")
    result = executor.place_order(intent, equity_usd=5000.0, mark_price=mark)

    print("\nResult:")
    print(f"  success           = {result.success}")
    print(f"  mode              = {result.mode}")
    print(f"  symbol            = {result.symbol}")
    print(f"  side              = {result.side}")
    print(f"  quantity          = {result.quantity}")
    print(f"  notional USD      = {result.notional_usd:.2f}")
    print(f"  margin required   = {result.margin_required_usd:.2f}")
    print(f"  open order id     = {result.order_id}")
    print(f"  stop order id     = {result.stop_order_id}")
    print(f"  take-profit id    = {result.take_profit_order_id}")
    if result.avg_fill_price is not None:
        print(f"  avg fill price    = {result.avg_fill_price}")
    if result.error:
        print(f"  error             = {result.error}")

    if result.success:
        print("\n✅ Three orders placed. Verify on https://testnet.binancefuture.com")
        print("   Positions tab + Open Orders tab.")
    else:
        print("\n❌ Order placement failed. See error above.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
