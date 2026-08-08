"""Targeted replays to verify the time_horizon schema fix changed behavior.

2026-08-08 review, gap #2: the old free-text field echoed the schema
description's example ('2-5 days') in 46/51 decisions. After the bucket
enum fix, replays of the same historical windows should spread across
buckets (or at least stop echoing a string that is no longer offered).

Four windows spread across the study period, two repeats each — enough
to see whether the distribution moves, cheap enough to run inline
(~8 graph runs). Results land in a separate JSONL; the original sweep
output is untouched.

    .venv/bin/python scripts/backtest_time_horizon_validation.py
"""

from __future__ import annotations

import collections
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from tradingagents.backtest.sweep import run_sweep
from tradingagents.default_config import DEFAULT_CONFIG

SYMBOL = "BTC-USD"
REPEATS = 2
WINDOW_INDICES = [0, 8, 16, 23]   # spread across the 24-window study period
WINDOWS_JSON = Path("docs/reviews/2026-08-08-horizon-windows.json")
OUT_PATH = Path("docs/reviews/2026-08-08-time-horizon-validation.jsonl")
SCRATCH_ROOT = Path.home() / ".tradingagents" / "backtest_scratch" / "th_validation"


def build_config() -> dict:
    """Same config as the sweep: Gemini via the google provider."""
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
        print("GOOGLE_API_KEY is not set — replays would fail on every window")
        return 1

    picks = json.loads(WINDOWS_JSON.read_text())
    windows = [datetime.fromisoformat(picks[i]["when"]) for i in WINDOW_INDICES]
    print(f"{len(windows)} windows x {REPEATS} repeats = {len(windows) * REPEATS} graph runs")

    run_sweep(
        SYMBOL, windows,
        out_path=OUT_PATH,
        scratch_root=SCRATCH_ROOT,
        config=build_config(),
        repeats=REPEATS,
    )

    dist = collections.Counter(
        json.loads(line).get("time_horizon")
        for line in OUT_PATH.read_text().splitlines()
    )
    print(f"time_horizon distribution: {dict(dist)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
