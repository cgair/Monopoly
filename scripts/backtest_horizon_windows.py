"""Choose the replay windows for the holding-period study, and say why.

A directional model tested on a set of windows that mostly went up will
look like a good long-picker. So the windows are chosen to be balanced:
each time block contributes one window that rose over the following week
and one that fell.

The pick inside each block is the *median* mover of its group, not the
biggest — selecting the sharpest rallies and deepest crashes would hand
the model an easier tape than it will ever trade.

Writes a JSON manifest so the sweep and the report agree on what was
tested. Reproducible: same archive in, same 24 instants out.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingagents.backtest import vision

SYMBOL = "BTCUSDT"
HOUR_MS = 3_600_000

# Selection is anchored on the 7-day forward move: long enough that a
# window's label is not decided by one candle, short enough that the
# 24 windows are not all describing the same quarter-long trend.
BALANCE_HORIZON_H = 168
MAX_HORIZON_H = 720          # every window must have this much forward data

FIRST_WINDOW = datetime(2026, 1, 6, 12, tzinfo=timezone.utc)
LAST_WINDOW = datetime(2026, 7, 8, 12, tzinfo=timezone.utc)
N_BLOCKS = 12                # x2 (one up, one down) = 24 windows
MIN_SEPARATION_DAYS = 3


def _closes(start_ms: int, end_ms: int) -> dict[int, float]:
    bars = vision.klines(SYMBOL, "1h", start_ms, end_ms)
    return {b.open_time: b.close for b in bars}


def _fwd_return(closes: dict[int, float], ts: int, hours: int) -> float | None:
    """Percent move from the bar closing at ``ts`` to the bar closing ``hours`` later."""
    here, there = closes.get(ts - HOUR_MS), closes.get(ts + (hours - 1) * HOUR_MS)
    if here is None or there is None:
        return None
    return (there - here) / here * 100.0


def candidates() -> list[dict]:
    """Every daily 12:00 UTC instant in range, with its forward moves."""
    start = int(FIRST_WINDOW.timestamp() * 1000) - 2 * HOUR_MS
    end = int(LAST_WINDOW.timestamp() * 1000) + (MAX_HORIZON_H + 2) * HOUR_MS
    closes = _closes(start, end)

    out = []
    day = FIRST_WINDOW
    while day <= LAST_WINDOW:
        ts = int(day.timestamp() * 1000)
        fwd = {h: _fwd_return(closes, ts, h) for h in (6, 24, 72, 168, 336, 720)}
        if all(v is not None for v in fwd.values()):
            out.append({"when": day, "as_of": ts, "fwd": fwd})
        day += timedelta(days=1)
    return out


def _median_pick(group: list[dict], taken: list[dict]) -> dict | None:
    """The most ordinary mover in ``group`` that is not crowded against ``taken``."""
    if not group:
        return None
    ranked = sorted(group, key=lambda c: abs(c["fwd"][BALANCE_HORIZON_H]))
    median = abs(ranked[len(ranked) // 2]["fwd"][BALANCE_HORIZON_H])
    for cand in sorted(group, key=lambda c: abs(abs(c["fwd"][BALANCE_HORIZON_H]) - median)):
        gap = timedelta(days=MIN_SEPARATION_DAYS)
        if all(abs(cand["when"] - t["when"]) >= gap for t in taken):
            return cand
    return None


def select() -> list[dict]:
    """12 up-windows and 12 down-windows, one pair per time block."""
    pool = candidates()
    if not pool:
        raise RuntimeError("no candidate windows have full forward coverage")

    span = (LAST_WINDOW - FIRST_WINDOW) / N_BLOCKS
    chosen: list[dict] = []
    for i in range(N_BLOCKS):
        lo = FIRST_WINDOW + span * i
        hi = FIRST_WINDOW + span * (i + 1)
        block = [c for c in pool if lo <= c["when"] < hi or (i == N_BLOCKS - 1 and c["when"] == hi)]
        ups = [c for c in block if c["fwd"][BALANCE_HORIZON_H] > 0]
        downs = [c for c in block if c["fwd"][BALANCE_HORIZON_H] < 0]
        for group, label in ((ups, "up"), (downs, "down")):
            pick = _median_pick(group, chosen)
            if pick is None:
                print(f"  block {i + 1}: no {label} window available "
                      f"({len(group)} candidates, separation {MIN_SEPARATION_DAYS}d)")
                continue
            pick["block"] = i + 1
            pick["label"] = label
            chosen.append(pick)

    # A block with no window of one direction (a stretch that only fell,
    # say) would leave the set unbalanced. Top up from anywhere in the
    # range, taking whichever candidate sits furthest from what is
    # already chosen so the fill-in does not bunch against a neighbour.
    for label, sign in (("up", 1), ("down", -1)):
        target = N_BLOCKS
        while sum(1 for c in chosen if c["label"] == label) < target:
            pool_side = [c for c in pool
                         if c not in chosen and sign * c["fwd"][BALANCE_HORIZON_H] > 0]
            if not pool_side:
                print(f"  top-up: no more {label} candidates")
                break
            best = max(pool_side, key=lambda c: min(abs(c["when"] - t["when"]) for t in chosen))
            best["block"] = 0          # 0 = filled in outside the block grid
            best["label"] = label
            chosen.append(best)
            print(f"  top-up: added {label} window {best['when']:%Y-%m-%d}")

    return sorted(chosen, key=lambda c: c["as_of"])


def main() -> None:
    picks = select()
    print(f"\n{len(picks)} windows  "
          f"({sum(1 for p in picks if p['label'] == 'up')} up / "
          f"{sum(1 for p in picks if p['label'] == 'down')} down at {BALANCE_HORIZON_H}h)\n")
    header = "window (UTC)        blk lbl " + "".join(f"{h:>9}h" for h in (6, 24, 72, 168, 336, 720))
    print(header)
    print("-" * len(header))
    for p in picks:
        row = "".join(f"{p['fwd'][h]:>+9.2f}%" for h in (6, 24, 72, 168, 336, 720))
        print(f"{p['when']:%Y-%m-%d %H:%M}  {p['block']:>3}  {p['label']:<4}{row}")

    for h in (6, 24, 72, 168, 336, 720):
        ups = sum(1 for p in picks if p["fwd"][h] > 0)
        print(f"  balance at {h:>3}h: {ups} up / {len(picks) - ups} down")

    out = Path("docs/reviews/2026-08-08-horizon-windows.json")
    out.write_text(json.dumps(
        [{"as_of": p["as_of"], "when": p["when"].isoformat(), "block": p["block"],
          "label": p["label"], "fwd": p["fwd"]} for p in picks], indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
