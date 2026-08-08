"""Which holding period do these decisions actually work on?

Joins the replayed decisions with two independent views of what happened
next, at every holding period under test:

* **Blind hold** (:mod:`~tradingagents.backtest.horizon`) — enter at the
  decision instant, hold to the h-hour mark, no stop, no target. Measures
  the direction call and nothing else, so the columns are comparable.
* **The race** (:mod:`~tradingagents.backtest.score`) — the decision's own
  stop and target, marked to market at the h-hour mark if neither is hit.
  Measures what the tactics would have delivered.

Keeping both is the point. A direction that pays under the blind hold and
loses under the race is not a bad read; it is a stop too tight for the
period the read needs.

Everything here is derived from the JSONL sample rows, so any number in
the report can be traced back to a window, a repeat index, and a horizon.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .horizon import HORIZONS, directional_returns, fetch_window_bars
from .report import _stop_is_inverted
from .score import score_decision

_HOUR_MS = 3_600_000

# Status of one replay sample. Only "ok" rows carry outcome numbers.
OK = "ok"
FLAT = "flat"
INVALID_STOP = "invalid_stop"       # stop on the profit side — see risk_gate.py:284
NO_LEVELS = "no_levels"
BAD_LEVELS = "bad_levels"           # target on the wrong side of the stop
ERROR = "error"


@dataclass
class Sample:
    """One replay of one window: the decision, and what followed."""

    as_of: int
    rep: int
    label: str                      # "up" / "down" — the window's own 168h move
    status: str
    side: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reference_price: float | None = None
    time_horizon: str | None = None
    detail: str | None = None       # why a non-ok sample was excluded

    market_entry: float | None = None
    # Risk as the decision declared it: distance from the entry it named
    # (its limit, or the market price when it asked for a market order).
    stop_pct: float | None = None
    target_pct: float | None = None
    entry_gap_pct: float | None = None   # how far a limit sat from the market

    @property
    def is_market_order(self) -> bool:
        """Market orders enter where the blind hold enters, so their stop
        distance and the blind hold's excursions are measured from the same
        price and can be compared. A limit order's cannot."""
        return self.entry_price is None

    hold: dict[int, dict] = field(default_factory=dict)   # blind hold, per horizon
    race: dict[int, dict] = field(default_factory=dict)   # stop/target race, per horizon

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.as_of / 1000, timezone.utc)


# ---------------------------------------------------------------------------
# building samples
# ---------------------------------------------------------------------------

def _classify(rec: dict) -> tuple[str, str | None]:
    side = (rec.get("side") or "").strip()
    if rec.get("error"):
        return ERROR, str(rec["error"])[:200]
    if side.lower() in ("flat", ""):
        return FLAT, "no position taken"
    if rec.get("stop_loss") is None or rec.get("take_profit") is None:
        return NO_LEVELS, "decision lacks a stop or a target"
    if _stop_is_inverted(side, rec.get("entry_price"), rec.get("reference_price"),
                         float(rec["stop_loss"])):
        # The gate never checked this for a market order. Scoring it would
        # report a number for a position that could not have been protected.
        return INVALID_STOP, "stop sits on the side the trade profits toward"
    sl, tp = float(rec["stop_loss"]), float(rec["take_profit"])
    if (side.lower() == "long" and tp <= sl) or (side.lower() == "short" and tp >= sl):
        return BAD_LEVELS, "target on the wrong side of the stop"
    return OK, None


def build_samples(replays: list[dict], windows: list[dict],
                  horizons: tuple[int, ...] = HORIZONS,
                  bars_by_window: dict[int, list] | None = None) -> list[Sample]:
    """Score every replay against both views, at every horizon."""
    label_of = {int(w["as_of"]): w["label"] for w in windows}
    bars_by_window = bars_by_window if bars_by_window is not None else {}
    longest = max(horizons)

    samples: list[Sample] = []
    for rec in sorted(replays, key=lambda r: (int(r["as_of"]), int(r.get("rep", 0)))):
        as_of = int(rec["as_of"])
        status, detail = _classify(rec)
        s = Sample(
            as_of=as_of, rep=int(rec.get("rep", 0)),
            label=label_of.get(as_of, "?"), status=status, detail=detail,
            side=(rec.get("side") or None),
            entry_price=rec.get("entry_price"),
            stop_loss=rec.get("stop_loss"),
            take_profit=rec.get("take_profit"),
            reference_price=rec.get("reference_price"),
            time_horizon=(rec.get("time_horizon") or None),
        )
        if status != OK:
            samples.append(s)
            continue

        if as_of not in bars_by_window:
            bars_by_window[as_of] = fetch_window_bars(rec["symbol"], as_of, longest)
        bars = bars_by_window[as_of]

        blind = directional_returns(
            symbol=rec["symbol"], side=s.side, decision_ts=as_of,
            horizons=horizons, bars=bars,
        )
        s.market_entry = blind.entry_price
        intended = s.entry_price if s.entry_price is not None else s.market_entry
        if intended:
            s.stop_pct = abs(float(s.stop_loss) - intended) / intended * 100
            s.target_pct = abs(float(s.take_profit) - intended) / intended * 100
        if s.entry_price is not None and s.market_entry:
            s.entry_gap_pct = (s.entry_price - s.market_entry) / s.market_entry * 100
        for h, p in blind.points.items():
            s.hold[h] = {
                "covered": p.covered, "return_pct": p.return_pct,
                "mfe_pct": p.mfe_pct, "mae_pct": p.mae_pct,
            }

        for h in horizons:
            cutoff = as_of + h * _HOUR_MS
            window_bars = [b for b in bars if b[0] <= cutoff]
            if not s.hold.get(h, {}).get("covered"):
                s.race[h] = {"covered": False}
                continue
            scored = score_decision(
                symbol=rec["symbol"], side=s.side, decision_ts=as_of,
                entry_price=rec.get("entry_price"),
                stop_loss=float(s.stop_loss), take_profit=float(s.take_profit),
                horizon_hours=h, bars=window_bars,
            )
            s.race[h] = {
                "covered": True,
                # Market-entry counterfactual: same entry as the blind hold,
                # so the two views differ only in the stop and target.
                "outcome": scored.market_outcome,
                "r": scored.market_r_multiple,
                "pnl_pct": scored.market_pnl_pct,
                "hold_hours": scored.market_hold_hours,
                # As written: the limit price the decision actually named.
                "as_written_filled": scored.filled,
                "as_written_outcome": scored.outcome,
                "as_written_r": scored.r_multiple,
                "miss_by_pct": scored.miss_by_pct,
            }
        samples.append(s)
    return samples


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval — honest at the sample sizes this study has."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    mid = len(ys) // 2
    return ys[mid] if len(ys) % 2 else (ys[mid - 1] + ys[mid]) / 2


def horizon_summary(samples: list[Sample], horizons: tuple[int, ...] = HORIZONS) -> list[dict]:
    """One row per holding period: accuracy, blind return, race R, agreement."""
    ok = [s for s in samples if s.status == OK]
    span_hours = 0.0
    stamps = [s.as_of for s in samples]
    if len(stamps) > 1:
        span_hours = (max(stamps) - min(stamps)) / _HOUR_MS

    rows = []
    for h in horizons:
        covered = [s for s in ok if s.hold.get(h, {}).get("covered")]
        rets = [s.hold[h]["return_pct"] for s in covered]
        hits = sum(1 for r in rets if r > 0)
        decided = sum(1 for r in rets if r != 0)
        ci = wilson(hits, decided)

        rs = [s.race[h]["r"] for s in covered
              if s.race.get(h, {}).get("covered") and s.race[h].get("r") is not None]
        resolved = [s for s in covered
                    if s.race.get(h, {}).get("outcome") in ("take_profit", "stop_loss")]
        race_wins = sum(1 for s in resolved if s.race[h]["outcome"] == "take_profit")

        # Would the decision's own stop have been hit before the horizon?
        # Only market orders qualify: their stop distance and the blind
        # hold's excursion are measured from the same entry price.
        comparable = [s for s in covered if s.is_market_order and s.stop_pct is not None]
        stopped = [s for s in comparable if s.hold[h]["mae_pct"] is not None
                   and s.hold[h]["mae_pct"] >= s.stop_pct]
        stopped_but_right = [s for s in stopped if s.hold[h]["return_pct"] > 0]

        # Per side, and the two constant strategies on the same population.
        # A model that only ever shorts scores whatever "always short"
        # scores; without this column its accuracy cannot be read.
        longs = [s for s in covered if (s.side or "").lower() == "long"]
        shorts = [s for s in covered if (s.side or "").lower() == "short"]
        long_hits = sum(1 for s in longs if s.hold[h]["return_pct"] > 0)
        short_hits = sum(1 for s in shorts if s.hold[h]["return_pct"] > 0)
        # Underlying move: the blind return, sign-corrected back to the tape.
        moves = [s.hold[h]["return_pct"] * (1 if (s.side or "").lower() == "long" else -1)
                 for s in covered]
        tape_up = sum(1 for m in moves if m > 0)

        # Agreement between repeats of the same window, at this horizon.
        # "Flat" is a verdict of its own: a window where one repeat traded
        # and another stood aside did not agree, whatever the price did.
        verdicts: dict[int, list[str]] = {}
        for s in samples:
            if s.status == FLAT:
                v = "flat"
            elif s.status != OK:
                v = "excluded"
            elif not s.hold.get(h, {}).get("covered"):
                continue
            else:
                v = "right" if s.hold[h]["return_pct"] > 0 else "wrong"
            verdicts.setdefault(s.as_of, []).append(v)
        multi = [k for k, vs in verdicts.items() if len(vs) >= 2]
        unanimous = sum(1 for k in multi if len(set(verdicts[k])) == 1)

        rows.append({
            "horizon": h,
            "n": len(covered),
            "n_windows": len({s.as_of for s in covered}),
            "effective_windows": min(len({s.as_of for s in covered}),
                                     round(span_hours / h, 1)) if span_hours else None,
            "hits": hits,
            "decided": decided,
            "accuracy": (hits / decided) if decided else None,
            "ci": ci,
            "mean_return": _mean(rets),
            "median_return": _median(rets),
            "mean_mfe": _mean([s.hold[h]["mfe_pct"] for s in covered]),
            "mean_mae": _mean([s.hold[h]["mae_pct"] for s in covered]),
            "race_n": len(rs),
            "race_mean_r": _mean(rs),
            "race_resolved": len(resolved),
            "race_win_rate": (race_wins / len(resolved)) if resolved else None,
            "comparable": len(comparable),
            "stopped_out": len(stopped),
            "stopped_but_right": len(stopped_but_right),
            "long_n": len(longs),
            "long_accuracy": (long_hits / len(longs)) if longs else None,
            "short_n": len(shorts),
            "short_accuracy": (short_hits / len(shorts)) if shorts else None,
            "always_long": (tape_up / len(moves)) if moves else None,
            "always_short": (1 - tape_up / len(moves)) if moves else None,
            "agreement_windows": len(multi),
            "agreement": (unanimous / len(multi)) if multi else None,
        })
    return rows


def decision_agreement(samples: list[Sample]) -> dict:
    """How reproducible is the decision itself, window by window?"""
    by_window: dict[int, list[Sample]] = {}
    for s in samples:
        by_window.setdefault(s.as_of, []).append(s)

    unanimous_side = 0
    considered = 0
    stop_spread: list[float] = []
    for reps in by_window.values():
        if len(reps) < 2:
            continue
        considered += 1
        sides = {(s.side or "flat").lower() for s in reps}
        if len(sides) == 1:
            unanimous_side += 1
        stops = [s.stop_pct for s in reps if s.stop_pct is not None]
        if len(stops) >= 2:
            stop_spread.append(max(stops) - min(stops))

    ok = [s for s in samples if s.status == OK]
    return {
        "windows_with_repeats": considered,
        "unanimous_side": unanimous_side,
        "unanimous_side_rate": (unanimous_side / considered) if considered else None,
        "mean_stop_spread_pct": _mean(stop_spread),
        "max_stop_spread_pct": max(stop_spread) if stop_spread else None,
        "long": sum(1 for s in ok if (s.side or "").lower() == "long"),
        "short": sum(1 for s in ok if (s.side or "").lower() == "short"),
        "flat": sum(1 for s in samples if s.status == FLAT),
        "excluded": sum(1 for s in samples if s.status in
                        (INVALID_STOP, NO_LEVELS, BAD_LEVELS, ERROR)),
        "mean_stop_pct": _mean([s.stop_pct for s in ok if s.stop_pct is not None]),
        "min_stop_pct": min([s.stop_pct for s in ok if s.stop_pct is not None], default=None),
        "max_stop_pct": max([s.stop_pct for s in ok if s.stop_pct is not None], default=None),
        "mean_target_pct": _mean([s.target_pct for s in ok if s.target_pct is not None]),
    }


def load_json(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text())


def load_jsonl(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
