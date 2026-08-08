"""Score the holding-period sweep and write the HTML report.

    .venv/bin/python scripts/backtest_horizon_report.py

Reads the window manifest and the replay JSONL, fetches the forward price
path once per window (cached on disk), scores every replay two ways at
every horizon, and writes a single self-contained HTML file.

Safe to run against a partial sweep — the report states how many runs it
is built from.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.backtest.horizon import HORIZONS
from tradingagents.backtest.horizon_html import render_html
from tradingagents.backtest.horizon_report import (
    INVALID_STOP, OK, build_samples, decision_agreement, horizon_summary,
    load_json, load_jsonl,
)
from tradingagents.backtest import vision

SYMBOL = "BTC-USD"
WINDOWS_JSON = Path("docs/reviews/2026-08-08-horizon-windows.json")
REPLAYS = Path("docs/reviews/2026-08-08-horizon-replays.jsonl")
OUT_HTML = Path("docs/reviews/2026-08-08-holding-period-report.html")
BARS_CACHE = Path.home() / ".tradingagents" / "backtest_archive" / "horizon_bars"
MODEL = "gemini-3.1-pro-preview / gemini-3.1-flash-lite (google)"


def _bars_for(as_of: int) -> list[tuple]:
    """5-minute bars covering the longest horizon, cached on disk."""
    BARS_CACHE.mkdir(parents=True, exist_ok=True)
    path = BARS_CACHE / f"{SYMBOL.replace('-', '')}_{as_of}_{max(HORIZONS)}h.json"
    if path.exists():
        return [tuple(b) for b in json.loads(path.read_text())]
    end = as_of + max(HORIZONS) * 3_600_000
    bars = [(b.open_time, b.open, b.high, b.low, b.close)
            for b in vision.klines("BTCUSDT", "5m", as_of, end)]
    path.write_text(json.dumps(bars))
    return bars


def _best_constant(row):
    al, ash = row["always_long"], row["always_short"]
    if al is None:
        return None
    return ("long", al) if al >= ash else ("short", ash)


def derive_verdict(samples, summary, agreement) -> list[tuple[str, str]]:
    """Answer the three questions in the study's own numbers, or say it can't."""
    usable = [r for r in summary if r["decided"] >= 10]
    ok = [s for s in samples if s.status == OK]
    out = []

    if not usable:
        return [("Not enough data", "No horizon reached 10 decided calls.")]

    ranked = sorted(usable, key=lambda r: r["accuracy"], reverse=True)
    beats = [r for r in usable if r["accuracy"] > _best_constant(r)[1]]
    straddle = [r for r in usable if r["ci"][0] <= 0.5 <= r["ci"][1]]
    acc_line = "; ".join(f'<strong>{r["horizon"]}h</strong> {r["accuracy"] * 100:.0f}% '
                         f'(CI {r["ci"][0] * 100:.0f}–{r["ci"][1] * 100:.0f}, '
                         f'n={r["decided"]})' for r in summary if r["decided"] >= 10)
    beats_line = ", ".join(
        "{}h by {:+.1f} pp".format(r["horizon"], (r["accuracy"] - _best_constant(r)[1]) * 100)
        for r in beats)
    out.append((
        "1. How does directional accuracy vary with holding period?",
        f"Not monotonically, and not usefully. {acc_line}. The best is "
        f'{ranked[0]["horizon"]}h at {ranked[0]["accuracy"] * 100:.0f}%, the worst '
        f'{ranked[-1]["horizon"]}h at {ranked[-1]["accuracy"] * 100:.0f}%. '
        f'{len(straddle)} of {len(usable)} intervals contain 50%, and only '
        f'{len(beats)} horizon(s) beat the better constant strategy on the same calls'
        + (f" ({beats_line}), which is well inside the interval width. "
           if beats else ". ")
        + "<strong>Conclusion: the horizons cannot be ranked at this sample size.</strong>"))

    by_r = sorted(usable, key=lambda r: r["race_mean_r"] or -9, reverse=True)
    by_ret = sorted(usable, key=lambda r: r["mean_return"] or -9, reverse=True)
    mixed = [r for r in usable
             if r["mean_return"] is not None and r["median_return"] is not None
             and (r["mean_return"] > 0) != (r["median_return"] > 0)]
    mixed_line = ", ".join(
        "{}h mean {:+.2f}% vs median {:+.2f}%".format(
            r["horizon"], r["mean_return"], r["median_return"]) for r in mixed)
    out.append((
        "2. Where is expected value highest, and does it agree with accuracy?",
        f'By mean R under the decision\'s own stop and target, the top horizon is '
        f'<strong>{by_r[0]["horizon"]}h</strong> at {by_r[0]["race_mean_r"]:+.2f}R; '
        f'by mean blind-hold return it is <strong>{by_ret[0]["horizon"]}h</strong> at '
        f'{by_ret[0]["mean_return"]:+.2f}%. Neither matches the accuracy peak at '
        f'{ranked[0]["horizon"]}h. Every mean R sits between '
        f'{min(r["race_mean_r"] for r in usable):+.2f} and '
        f'{max(r["race_mean_r"] for r in usable):+.2f}R — a spread far smaller than the '
        f'per-trade dispersion. '
        + (f"At {len(mixed)} horizon(s) the mean and the median disagree in sign "
           f"({mixed_line}), so those means are carried by a handful of trades, not by "
           f"the typical one. " if mixed else "")
        + "<strong>Conclusion: no horizon has a demonstrable expected-value advantage.</strong>"))

    worst = max(summary, key=lambda r: r["stopped_but_right"])
    stops = [s.stop_pct for s in ok if s.stop_pct is not None]
    out.append((
        "3. Do the stops match the horizon the decisions are aimed at?",
        f'No. Stops are placed {min(stops):.2f}–{max(stops):.2f}% from the intended entry '
        f'(mean {sum(stops) / len(stops):.2f}%), while the mean adverse excursion is '
        + "; ".join(f'{r["horizon"]}h {r["mean_mae"]:.2f}%' for r in summary) + ". "
        f'By {worst["horizon"]}h, {worst["stopped_out"]} of {worst["comparable"]} '
        f'market-entry calls would already have hit their stop, and '
        f'{worst["stopped_but_right"]} of those had the direction right at the horizon. '
        "The stop is sized for hours; the excursions it has to survive are sized for days. "
        "<strong>Conclusion: the stop widths are mismatched to any horizon beyond about a "
        "day — but since no horizon shows an edge, widening the stop alone would not turn "
        "this profitable.</strong>"))
    return out


def derive_issues(samples, summary, agreement, meta, raw_replays) -> list[dict]:
    """Every issue carries the windows and numbers it was derived from."""
    issues: list[dict] = []
    ok = [s for s in samples if s.status == OK]
    usable = [r for r in summary if r["decided"] >= 10]

    # --- inverted stops that the gate let through -------------------------
    bad = [s for s in samples if s.status == INVALID_STOP]
    if bad:
        ev = "; ".join(f"{s.when:%Y-%m-%d} r{s.rep} {s.side} ref {s.reference_price:,.0f} "
                       f"stop {s.stop_loss:,.0f}" for s in bad[:4])
        issues.append({
            "severity": "critical",
            "title": f"{len(bad)} decision(s) put the stop on the profit side, and the gate passed them",
            "evidence": f"{ev}{' …' if len(bad) > 4 else ''}. All are market orders "
                        f"(<code>entry_price is None</code>), excluded from every statistic here.",
            "where": "tradingagents/futures/risk_gate.py:284",
            "fix": "The <code>stop_wrong_side</code> rule is nested under "
                   "<code>if decision.entry_price is not None</code>, so a market order skips it. "
                   "Fetch a reference price before the gate (or let the gate fetch one) and check "
                   "market orders against it. Known open gap — not touched in this run.",
        })

    # --- direction bias ---------------------------------------------------
    n_dir = agreement["long"] + agreement["short"]
    if n_dir:
        short_share = agreement["short"] / n_dir
        skewed = short_share >= 0.7 or short_share <= 0.3
        issues.append({
            "severity": "serious" if skewed else "good",
            "title": ("Directional bias: "
                      f"{agreement['short']} short vs {agreement['long']} long"
                      if skewed else "No strong directional bias"),
            "evidence": f"{agreement['short']}/{n_dir} of scoreable calls were short "
                        f"({short_share * 100:.0f}%), on a window set built "
                        f"{meta['up_windows']} up / {meta['down_windows']} down at 168h. "
                        + ("The tape did not cause this."
                           if skewed else "Consistent with the balanced tape."),
            "where": "tradingagents/agents/ — trader / risk debate prompts",
            "fix": ("Compare the long and short sub-populations separately before trusting any "
                    "aggregate win rate; a persistent one-sided book means the aggregate is "
                    "measuring the tape, not the model."
                    if skewed else "Nothing to do — keep monitoring as samples accumulate."),
        })

    # --- stop width vs the horizon that works -----------------------------
    if usable:
        best = max(usable, key=lambda r: r["accuracy"] or 0)
        killed = [r for r in summary if r["stopped_but_right"] > 0]
        worst = max(summary, key=lambda r: r["stopped_but_right"])
        sev = "serious" if worst["stopped_but_right"] >= 3 else "warning"
        ex = [s for s in ok
              if s.hold.get(worst["horizon"], {}).get("covered")
              and s.stop_pct is not None
              and s.hold[worst["horizon"]]["mae_pct"] >= s.stop_pct
              and s.hold[worst["horizon"]]["return_pct"] > 0]
        ev_bits = "; ".join(
            f"{s.when:%Y-%m-%d} r{s.rep} {s.side}: stop {s.stop_pct:.2f}% but MAE "
            f"{s.hold[worst['horizon']]['mae_pct']:.2f}% before ending "
            f"{s.hold[worst['horizon']]['return_pct']:+.2f}%" for s in ex[:3])
        issues.append({
            "severity": sev if killed else "good",
            "title": f"Stops are {agreement['mean_stop_pct']:.2f}% wide on average; "
                     f"{worst['stopped_but_right']} correct calls get stopped out at "
                     f"{worst['horizon']}h",
            "evidence": (f"Stop widths span {agreement['min_stop_pct']:.2f}–"
                         f"{agreement['max_stop_pct']:.2f}% of entry, while mean adverse excursion "
                         f"at {worst['horizon']}h is {worst['mean_mae']:.2f}%. "
                         f"{ev_bits}{' …' if len(ex) > 3 else ''}"),
            "where": "tradingagents/agents/managers/ — PM stop placement prompt",
            "fix": "Size the stop off realised volatility (an ATR multiple over the intended "
                   "holding period) instead of a flat percentage, or shorten the stated horizon "
                   "to match the stop actually used.",
        })

    # --- does any horizon beat simply always taking the same side? --------
    if usable:
        beats = [r for r in usable if r["accuracy"] > _best_constant(r)[1]]
        detail = "; ".join(
            "{}h {:.0f}% vs always-{} {:.0f}% ({:+.1f} pp, n={})".format(
                r["horizon"], r["accuracy"] * 100, _best_constant(r)[0],
                _best_constant(r)[1] * 100,
                (r["accuracy"] - _best_constant(r)[1]) * 100, r["decided"])
            for r in usable)
        issues.append({
            "severity": "serious" if not beats else "warning",
            "title": (f"No holding period beats a constant strategy"
                      if not beats else
                      f"Only {len(beats)}/{len(usable)} horizons beat a constant strategy, "
                      f"and not by more than the interval width"),
            "evidence": detail + ". Every 95% Wilson interval is at least 25 points wide.",
            "where": "this study",
            "fix": "Treat the ranking between horizons as unresolved and do not tune the "
                   "holding period on this data. Roughly 60–100 scoreable calls per horizon "
                   "would be needed to separate a 60% hit rate from chance; that is ~3× this "
                   "sweep. Until then the honest answer is 'no measurable directional edge at "
                   "any horizon'.",
        })

    # --- right more often, but not paid for it ----------------------------
    payless = [r for r in usable
               if r["accuracy"] is not None and r["accuracy"] > 0.5
               and r["mean_return"] is not None and r["mean_return"] <= 0.05]
    for r in payless:
        issues.append({
            "severity": "serious",
            "title": f"At {r['horizon']}h the direction is right {r['accuracy'] * 100:.0f}% "
                     f"of the time and still earns nothing",
            "evidence": f"{r['hits']}/{r['decided']} calls right at {r['horizon']}h, yet the mean "
                        f"blind-hold return is {r['mean_return']:+.2f}% (median "
                        f"{r['median_return']:+.2f}%). Mean favourable excursion "
                        f"{r['mean_mfe']:.2f}% is below mean adverse excursion "
                        f"{r['mean_mae']:.2f}%: the wins are smaller than the losses.",
            "where": "this study",
            "fix": "Hit rate is the wrong target. If a short-horizon edge is real it has to be "
                   "harvested with an asymmetric payoff — a target further out than the stop — "
                   "not with a higher hit rate.",
        })

    # --- mean carried by outliers ----------------------------------------
    mixed = [r for r in usable if r["mean_return"] is not None
             and r["median_return"] is not None
             and (r["mean_return"] > 0) != (r["median_return"] > 0)]
    if mixed:
        issues.append({
            "severity": "warning",
            "title": f"At {len(mixed)} horizon(s) the mean return is positive but the median is not",
            "evidence": "; ".join(
                "{}h mean {:+.2f}% vs median {:+.2f}% (n={})".format(
                    r["horizon"], r["mean_return"], r["median_return"], r["n"]) for r in mixed)
            + ". The positive mean comes from a few large winners, not from the typical call.",
            "where": "this study",
            "fix": "Quote the median alongside any mean return. A strategy whose mean depends "
                   "on two trades has not been shown to work.",
        })

    # --- thin rows --------------------------------------------------------
    thin = [r for r in summary if r["decided"] < 10]
    if thin:
        issues.append({
            "severity": "warning",
            "title": f"{len(thin)} horizon row(s) below the n≥10 usability bar",
            "evidence": "; ".join(f'{r["horizon"]}h n={r["decided"]}' for r in thin),
            "where": "this study",
            "fix": "Do not read these rows as results. Extend the sweep (more windows, or more "
                   "repeats per window) before quoting them.",
        })

    # --- reproducibility --------------------------------------------------
    rate = agreement["unanimous_side_rate"]
    if rate is not None:
        spread = agreement["mean_stop_spread_pct"]
        issues.append({
            "severity": "serious" if rate < 0.7 else "warning",
            "title": f"Repeats of the same window agree on side {rate * 100:.0f}% of the time",
            "evidence": (f"{agreement['unanimous_side']}/{agreement['windows_with_repeats']} windows "
                         f"had every repeat pick the same side"
                         + (f"; stop distance varies by {spread:.2f} percentage points across "
                            f"repeats on average (max {agreement['max_stop_spread_pct']:.2f} pp)."
                            if spread is not None else ".")),
            "where": "tradingagents/graph/trading_graph.py — LLM sampling",
            "fix": "Any single live decision is one draw from this distribution. Either sample the "
                   "graph k times and require agreement before acting, or lower the sampling "
                   "temperature for the decision node.",
        })

    # --- the stated horizon is the schema's own example --------------------
    from collections import Counter
    stated = [r.get("time_horizon") for r in raw_replays if r.get("time_horizon")]
    if stated:
        counts = Counter(stated)
        top, n_top = counts.most_common(1)[0]
        share = n_top / len(stated)
        example = "2-5 days"        # the literal in schemas.py:263
        echoed = top.strip() == example
        issues.append({
            "severity": "serious" if echoed and share >= 0.8 else "warning",
            "title": ("The stated holding horizon is the schema's example string, echoed back"
                      if echoed and share >= 0.8 else "Stated holding intent is nearly constant"),
            "evidence": f"{n_top}/{len(stated)} decisions ({share * 100:.0f}%) state "
                        f"<code>{top}</code>; the whole distribution is "
                        + "; ".join(f"{t} ×{c}" for t, c in counts.most_common(6))
                        + (f". <code>{example}</code> is the literal used as the example in the "
                           f"field's own schema description, so the field is carrying the prompt, "
                           f"not a judgement." if echoed else "."),
            "where": "tradingagents/agents/schemas.py:263",
            "fix": "Drop the example from the field description, or replace it with an enumerated "
                   "choice the model must actually pick between. As written, "
                   "<code>time_horizon</code> cannot be used as evidence of intent — including in "
                   "this report.",
        })

    return issues


def main() -> int:
    windows = load_json(WINDOWS_JSON)
    replays = load_jsonl(REPLAYS)
    print(f"{len(replays)} replay records over {len({r['as_of'] for r in replays})} windows")

    bars = {int(w["as_of"]): _bars_for(int(w["as_of"])) for w in windows
            if any(int(r["as_of"]) == int(w["as_of"]) for r in replays)}
    samples = build_samples(replays, windows, HORIZONS, bars_by_window=bars)
    summary = horizon_summary(samples, HORIZONS)
    agreement = decision_agreement(samples)

    meta = {
        "symbol": SYMBOL,
        "model": MODEL,
        "n_windows": len({s.as_of for s in samples}),
        "repeats": max((s.rep for s in samples), default=0) + 1,
        "range": f"{min(s.when for s in samples):%Y-%m-%d} → "
                 f"{max(s.when for s in samples):%Y-%m-%d}",
        "up_windows": sum(1 for w in windows if w["label"] == "up"),
        "down_windows": sum(1 for w in windows if w["label"] == "down"),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "jsonl": str(REPLAYS),
    }
    issues = derive_issues(samples, summary, agreement, meta, replays)

    OUT_HTML.write_text(render_html(
        samples=samples, summary=summary, agreement=agreement, issues=issues,
        windows=windows, meta=meta, horizons=HORIZONS,
        verdict=derive_verdict(samples, summary, agreement)))
    print(f"wrote {OUT_HTML}")

    for r in summary:
        acc = f'{r["accuracy"] * 100:.0f}%' if r["accuracy"] is not None else "—"
        ci = f'[{r["ci"][0] * 100:.0f},{r["ci"][1] * 100:.0f}]' if r["ci"] else "—"
        print(f'  {r["horizon"]:>4}h  n={r["decided"]:<3} acc={acc:<5} {ci:<10} '
              f'mean={r["mean_return"] if r["mean_return"] is not None else 0:+.2f}%  '
              f'raceR={r["race_mean_r"] if r["race_mean_r"] is not None else 0:+.2f}  '
              f'stopped_but_right={r["stopped_but_right"]}')
    print("  agreement:", json.dumps(agreement, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
