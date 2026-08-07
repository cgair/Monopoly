"""Join replayed decisions with their realised outcomes.

Reads the JSONL written by :mod:`tradingagents.backtest.sweep`, scores
each decision against the price action that followed, and summarises the
set. The summary deliberately reports directional bias and the
limit-vs-market split alongside the win rate: with a sample this small a
win rate alone cannot distinguish an edge from having been pointed the
right way by a trending market.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .score import ScoredOutcome, score_decision


def load_replays(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _stop_is_inverted(side: str, entry_price, reference_price, stop_loss: float) -> bool:
    """True when the stop sits on the side the trade profits toward.

    The risk gate only runs this check when the decision names an entry
    price, so market-entry decisions reach the executor unchecked; the
    reference price stands in as the entry the gate never compared to.
    """
    ref = entry_price if entry_price is not None else reference_price
    if ref is None:
        return False
    if side.lower() == "long":
        return stop_loss >= float(ref)
    if side.lower() == "short":
        return stop_loss <= float(ref)
    return False


def score_replays(replays: list[dict], *, horizon_hours: int = 240) -> list[dict]:
    """Attach a :class:`ScoredOutcome` to every actionable replay."""
    rows = []
    for r in replays:
        side = (r.get("side") or "").strip()
        row = {"replay": r, "scored": None, "skip_reason": None}
        if r.get("error"):
            row["skip_reason"] = f"replay error: {r['error']}"
        elif side.lower() in ("flat", ""):
            row["skip_reason"] = "no position taken (Flat)"
        elif r.get("stop_loss") is None or r.get("take_profit") is None:
            row["skip_reason"] = "decision lacks stop or target"
        elif _stop_is_inverted(side, r.get("entry_price"), r.get("reference_price"),
                              float(r["stop_loss"])):
            # Scoring an inverted stop would report a number for a position
            # that could never have been protected. Surface it instead.
            row["skip_reason"] = "INVALID: stop_loss on the wrong side of entry"
        else:
            row["scored"] = score_decision(
                symbol=r["symbol"], side=side, decision_ts=int(r["as_of"]),
                entry_price=r.get("entry_price"),
                stop_loss=float(r["stop_loss"]), take_profit=float(r["take_profit"]),
                horizon_hours=horizon_hours,
            )
        rows.append(row)
    return rows


def summarise(rows: list[dict]) -> dict:
    scored = [r["scored"] for r in rows if r["scored"] is not None]
    flat = [r for r in rows if r["skip_reason"] == "no position taken (Flat)"]

    longs = [s for s in scored if s.side == "LONG"]
    shorts = [s for s in scored if s.side == "SHORT"]

    resolved = [s for s in scored if s.market_outcome in ("take_profit", "stop_loss")]
    wins = [s for s in resolved if s.market_outcome == "take_profit"]
    rs = [s.market_r_multiple for s in resolved if s.market_r_multiple is not None]

    filled = [s for s in scored if s.filled]
    unfilled = [s for s in scored if not s.filled]

    return {
        "windows": len(rows),
        "flat": len(flat),
        "actionable": len(scored),
        "long": len(longs),
        "short": len(shorts),
        "resolved_on_market_entry": len(resolved),
        "wins": len(wins),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "total_r": sum(rs) if rs else 0.0,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "limit_orders": len([s for s in scored if s.entry_price is not None]),
        "limit_unfilled": len([s for s in unfilled if s.entry_price is not None]),
        "filled": len(filled),
    }


def format_report(rows: list[dict]) -> str:
    def when(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")

    lines = ["window            side   entry      stop      target    outcome        R      held",
             "-" * 86]
    for row in rows:
        r, s = row["replay"], row["scored"]
        stamp = when(int(r["as_of"]))
        if s is None:
            lines.append(f"{stamp}  {(r.get('side') or '-'):6s} {row['skip_reason']}")
            continue
        entry = f"{s.entry_price:.0f}" if s.entry_price else "market"
        rmult = f"{s.market_r_multiple:+.2f}" if s.market_r_multiple is not None else "   -"
        held = f"{s.market_hold_hours:.0f}h" if s.market_hold_hours else "-"
        lines.append(
            f"{stamp}  {s.side:6s} {entry:>8s}  {s.stop_loss:8.0f}  {s.take_profit:8.0f}  "
            f"{s.market_outcome:12s} {rmult}  {held:>5s}"
        )

    summary = summarise(rows)
    lines += ["", "summary (outcomes measured on a market entry at the decision instant)"]
    for k, v in summary.items():
        if isinstance(v, float):
            lines.append(f"  {k:26s} {v:.3f}")
        else:
            lines.append(f"  {k:26s} {v}")
    return "\n".join(lines)
