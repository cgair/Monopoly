"""Replay a set of past instants and record what the graph decided.

Windows run strictly sequentially: :func:`~tradingagents.backtest.pit.point_in_time`
patches module-level fetchers, so two replays sharing a process would
read each other's cutoff. Results are appended to a JSONL file as each
window finishes, so a long sweep that dies partway keeps its work.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .replay import ReplayResult, replay

logger = logging.getLogger(__name__)


def run_sweep(symbol: str, windows: list[datetime], *, out_path: Path,
              scratch_root: Path, config: dict | None = None,
              skip_done: bool = True) -> list[ReplayResult]:
    """Replay each instant in ``windows``, appending results to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[int] = set()
    if skip_done and out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("symbol") == symbol and rec.get("error") is None:
                done.add(int(rec["as_of"]))

    results: list[ReplayResult] = []
    for i, when in enumerate(windows, 1):
        when = when.astimezone(timezone.utc)
        as_of_ms = int(when.timestamp() * 1000)
        if as_of_ms in done:
            logger.info("[%d/%d] %s already replayed, skipping", i, len(windows), when)
            continue

        logger.info("[%d/%d] replaying %s at %s", i, len(windows), symbol, when.isoformat())
        res = replay(symbol, when,
                     scratch=scratch_root / when.strftime("%Y%m%dT%H%M"),
                     config=config)
        results.append(res)
        with out_path.open("a") as fh:
            fh.write(json.dumps(asdict(res), default=str) + "\n")
        logger.info("    -> side=%s entry=%s sl=%s tp=%s err=%s",
                    res.side, res.entry_price, res.stop_loss, res.take_profit, res.error)

    return results
