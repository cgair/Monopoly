"""Replay the 24 selected windows three times each, for the holding-period study.

Runs strictly sequentially — ``pit.point_in_time`` patches module-level
fetchers, so two concurrent replays would read each other's cutoff.
Three samples per window because the graph is stochastic; one sample
cannot tell model variance from signal.

Only the market analyst runs. News, Reddit and the newsflash relay have
no dated archive, so including them would mean reading today's headlines
while claiming to stand in January.

    .venv/bin/python scripts/backtest_horizon_sweep.py

Resumable: re-running skips (window, repeat) pairs already in the JSONL.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from tradingagents.backtest.sweep import run_sweep
from tradingagents.default_config import DEFAULT_CONFIG

SYMBOL = "BTC-USD"
REPEATS = 3
WINDOWS_JSON = Path("docs/reviews/2026-08-08-horizon-windows.json")
OUT_PATH = Path("docs/reviews/2026-08-08-horizon-replays.jsonl")
SCRATCH_ROOT = Path.home() / ".tradingagents" / "backtest_scratch" / "horizon"


def build_config() -> dict:
    """Gemini via the google provider — the only key with a real value in .env."""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({
        "llm_provider": "google",
        "deep_think_llm": "gemini-3.1-pro-preview",
        "quick_think_llm": "gemini-3.1-flash-lite",
        "backend_url": None,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    })
    return cfg


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set — the sweep would fail on every window")
        return 1
    if not WINDOWS_JSON.exists():
        print(f"missing {WINDOWS_JSON}; run scripts/backtest_horizon_windows.py first")
        return 1

    picks = json.loads(WINDOWS_JSON.read_text())
    windows = [datetime.fromisoformat(p["when"]) for p in picks]
    print(f"{len(windows)} windows x {REPEATS} repeats = {len(windows) * REPEATS} graph runs")

    run_sweep(
        SYMBOL, windows,
        out_path=OUT_PATH,
        scratch_root=SCRATCH_ROOT,
        config=build_config(),
        repeats=REPEATS,
    )
    print(f"done -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
