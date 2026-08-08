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
              skip_done: bool = True, repeats: int = 1) -> list[ReplayResult]:
    """Replay each instant in ``windows`` ``repeats`` times, appending to ``out_path``.

    Repeats exist because the graph is stochastic: the same instant
    replayed twice has produced different stop and target levels. One
    sample per window cannot separate model variance from signal, so the
    resume key is ``(as_of, rep)`` — re-running a sweep tops up missing
    samples rather than treating a window as finished after one.

    Repeats are interleaved (every window's rep 0, then every window's
    rep 1) so a sweep killed halfway still has at least one sample of
    every window rather than three samples of the first third.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple[int, int]] = set()
    if skip_done and out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("symbol") == symbol and rec.get("error") is None:
                done.add((int(rec["as_of"]), int(rec.get("rep", 0))))

    jobs = [(rep, when.astimezone(timezone.utc))
            for rep in range(repeats) for when in windows]

    results: list[ReplayResult] = []
    for i, (rep, when) in enumerate(jobs, 1):
        as_of_ms = int(when.timestamp() * 1000)
        if (as_of_ms, rep) in done:
            logger.info("[%d/%d] %s rep%d already replayed, skipping", i, len(jobs), when, rep)
            continue

        logger.info("[%d/%d] replaying %s at %s rep%d",
                    i, len(jobs), symbol, when.isoformat(), rep)
        res = replay(symbol, when,
                     scratch=scratch_root / f"{when.strftime('%Y%m%dT%H%M')}_r{rep}",
                     config=config, rep=rep)
        results.append(res)
        with out_path.open("a") as fh:
            fh.write(json.dumps(asdict(res), default=str) + "\n")
        logger.info("    -> side=%s entry=%s sl=%s tp=%s err=%s",
                    res.side, res.entry_price, res.stop_loss, res.take_profit, res.error)

    return results
