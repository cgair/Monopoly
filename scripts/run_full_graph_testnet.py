"""Run the full Monopoly trading graph end-to-end on Binance Futures Testnet.

Non-interactive equivalent of ``.venv/bin/tradingagents``: skips the
questionary pickers so it can be invoked unattended (and so an agent
can drive it for the user). All knobs come from constants here or
``.env`` env vars.

Pre-conditions:
- ``.env`` has ``GOOGLE_API_KEY`` and the testnet binance keys
- ``EXECUTOR_MODE=testnet`` is set (either in .env or shell)
- Phase 4 smoke (``scripts/smoke_testnet_executor.py``) already passed

Cost / risk:
- Calls Gemini API: a few cents to ~$0.50 depending on prompt sizes
- Places real orders on Binance Futures Testnet (no real money)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pprint import pprint

from dotenv import load_dotenv

load_dotenv()  # must come before any TradingAgents imports

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------

TICKER = "BTC-USD"
TRADE_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SELECTED_ANALYSTS = ["market", "social"]   # minimal first run; add news later

LLM_PROVIDER = "google"
DEEP_MODEL = "gemini-3.1-pro-preview"      # Portfolio Manager + Research Manager
QUICK_MODEL = "gemini-3-flash-preview"     # analysts + researchers + debaters

MAX_DEBATE_ROUNDS = 1                       # default; bump for richer debate
MAX_RISK_ROUNDS = 1


def main() -> None:
    print(f"=== Monopoly closed-loop testnet run ===")
    print(f"  ticker:        {TICKER}")
    print(f"  trade date:    {TRADE_DATE}")
    print(f"  analysts:      {SELECTED_ANALYSTS}")
    print(f"  provider:      {LLM_PROVIDER}")
    print(f"  deep model:    {DEEP_MODEL}")
    print(f"  quick model:   {QUICK_MODEL}")
    print(f"  EXECUTOR_MODE: {os.environ.get('EXECUTOR_MODE', '(unset → dryrun)')}")
    print()
    if "BINANCE_TESTNET_API_KEY" not in os.environ:
        sys.exit("ERROR: BINANCE_TESTNET_API_KEY not set in env / .env")
    if "GOOGLE_API_KEY" not in os.environ:
        sys.exit("ERROR: GOOGLE_API_KEY not set in env / .env")

    config = {
        **DEFAULT_CONFIG,
        "llm_provider": LLM_PROVIDER,
        "deep_think_llm": DEEP_MODEL,
        "quick_think_llm": QUICK_MODEL,
        "max_debate_rounds": MAX_DEBATE_ROUNDS,
        "max_risk_discuss_rounds": MAX_RISK_ROUNDS,
        # google_thinking_level on the deep model — keep moderate for cost
        "google_thinking_level": "medium",
    }

    print("Building graph…")
    ta = TradingAgentsGraph(
        selected_analysts=SELECTED_ANALYSTS,
        debug=False,
        config=config,
    )
    print(f"  graph compiled; nodes: {len(ta.graph.nodes)}")
    print()

    print(f"Propagating {TICKER} ({TRADE_DATE})…")
    print("  (this will take a few minutes — analysts + researchers + PM + futures tail)")
    print()
    _, processed = ta.propagate(TICKER, TRADE_DATE)
    state = ta.curr_state

    print()
    print("=== Final state summary ===")
    print(f"  final_decision_structured:  {state.get('final_decision_structured')}")
    print()
    print(f"  execution_intent:")
    pprint(state.get("execution_intent"))
    print()
    print(f"  risk_gate_rejection_reason: {state.get('risk_gate_rejection_reason')}")
    print()
    print(f"  execution_result:")
    pprint(state.get("execution_result"))
    print()
    print(f"  processed_signal: {processed}")


if __name__ == "__main__":
    main()
