"""Render the holding-period study as one self-contained HTML file.

No build step, no CDN, no JavaScript: a single file that opens from disk
and keeps working when the repo moves. Colours follow the project data
viz palette (validated categorical slots, status colours reserved for
severity), and every status cue ships with a glyph so nothing is carried
by colour alone.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone

from .horizon_report import OK, FLAT, Sample

_RACE_SHORT = {
    "take_profit": "target",
    "stop_loss": "stop",
    "open": "open",
    "ambiguous": "tie",
    "no_data": "no data",
}

_STATUS_LABEL = {
    FLAT: "Flat — no position",
    "invalid_stop": "INVALID — stop on the profit side",
    "no_levels": "no stop/target parsed",
    "bad_levels": "target on the wrong side of the stop",
    "error": "replay error",
}


def _e(x) -> str:
    return html.escape(str(x))


def _pct(x: float | None, digits: int = 2, sign: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def _num(x: float | None, digits: int = 2, sign: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.{digits}f}" if sign else f"{x:.{digits}f}"


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

def _rule(x1: float, x2: float, y: float, title: str) -> str:
    """A dashed reference rule with a surface halo, so it reads over a bar too."""
    return (f'<line class="halo" x1="{x1:.1f}" x2="{x2:.1f}" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<line class="base" x1="{x1:.1f}" x2="{x2:.1f}" y1="{y:.1f}" y2="{y:.1f}">'
            f'<title>{title}</title></line>')


def _clear_of(label_y: float, obstacle_y: float | None, lift: float = 13.0) -> float:
    """Nudge a label out of a rule's way rather than letting them collide."""
    if obstacle_y is not None and abs(label_y - obstacle_y) < 11:
        return label_y - lift
    return label_y


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Bar anchored to the baseline with a rounded data-end."""
    r = min(r, w / 2, max(h, 0.1))
    if h <= 0.5:
        return f"M{x:.1f},{y + h:.1f} h{w:.1f}"
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
            f"V{y + h:.1f} Z")


def _accuracy_chart(rows: list[dict]) -> str:
    """Directional accuracy per holding period, with 95% Wilson intervals."""
    W, H = 640, 260
    pad_l, pad_r, pad_t, pad_b = 46, 16, 24, 46
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    slot = plot_w / len(rows)
    bar_w = min(46, slot * 0.5)

    def y_of(v: float) -> float:
        return pad_t + plot_h * (1 - v)

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" width="100%" '
             f'aria-label="Directional accuracy by holding period">']
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        y = y_of(tick)
        dashed = ' stroke-dasharray="4 4"' if tick == 0.5 else ""
        cls = "ref" if tick == 0.5 else "grid"
        parts.append(f'<line class="{cls}" x1="{pad_l}" x2="{W - pad_r}" y1="{y:.1f}" '
                     f'y2="{y:.1f}"{dashed}/>')
        parts.append(f'<text class="tick" x="{pad_l - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{int(tick * 100)}%</text>')
    # In the top margin, clear of any bar or whisker.
    parts.append(f'<text class="tick" x="{W - pad_r}" y="{pad_t - 10:.1f}" '
                 f'text-anchor="end">the 50% rule is a coin flip</text>')

    for i, r in enumerate(rows):
        cx = pad_l + slot * (i + 0.5)
        parts.append(f'<text class="axis" x="{cx:.1f}" y="{H - pad_b + 18:.1f}" '
                     f'text-anchor="middle">{r["horizon"]}h</text>')
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{H - pad_b + 34:.1f}" '
                     f'text-anchor="middle">n={r["decided"]}</text>')
        acc = r["accuracy"]
        if acc is None:
            parts.append(f'<text class="tick" x="{cx:.1f}" y="{y_of(0) - 8:.1f}" '
                         f'text-anchor="middle">no data</text>')
            continue
        y = y_of(acc)
        parts.append(
            f'<path class="bar" d="{_bar_path(cx - bar_w / 2, y, bar_w, y_of(0) - y)}">'
            f'<title>{r["horizon"]}h — {acc * 100:.0f}% of {r["decided"]} calls right</title></path>')
        base = _best_constant(r)
        base_y = y_of(base[1]) if base else None
        if base is not None:
            parts.append(_rule(cx - bar_w / 2 - 5, cx + bar_w / 2 + 5, base_y,
                               f"always {base[0]} would have scored "
                               f"{base[1] * 100:.0f}% on the same calls"))
        if r["ci"]:
            lo, hi = r["ci"]
            parts.append(f'<line class="ci" x1="{cx:.1f}" x2="{cx:.1f}" '
                         f'y1="{y_of(lo):.1f}" y2="{y_of(hi):.1f}"/>')
            for v in (lo, hi):
                parts.append(f'<line class="ci" x1="{cx - 7:.1f}" x2="{cx + 7:.1f}" '
                             f'y1="{y_of(v):.1f}" y2="{y_of(v):.1f}"/>')
            parts.append(f'<text class="tick" x="{cx:.1f}" '
                         f'y="{_clear_of(y_of(hi) - 8, base_y):.1f}" '
                         f'text-anchor="middle">{acc * 100:.0f}%</text>')
    parts.append("</svg>")
    return "".join(parts)


def _best_constant(row: dict) -> tuple[str, float] | None:
    """The better of "always long" / "always short" on this row's own calls."""
    al, as_ = row.get("always_long"), row.get("always_short")
    if al is None or as_ is None:
        return None
    return ("long", al) if al >= as_ else ("short", as_)


def _return_chart(rows: list[dict]) -> str:
    """Mean blind-hold return per holding period — diverging around zero."""
    W, H = 640, 240
    pad_l, pad_r, pad_t, pad_b = 46, 16, 20, 46
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    slot = plot_w / len(rows)
    bar_w = min(46, slot * 0.5)
    vals = [r["mean_return"] for r in rows if r["mean_return"] is not None]
    scale = max([abs(v) for v in vals] + [0.5]) * 1.35
    zero_y = pad_t + plot_h / 2

    def y_of(v: float) -> float:
        return zero_y - (v / scale) * (plot_h / 2)

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" width="100%" '
             f'aria-label="Mean blind-hold return by holding period">']
    parts.append(f'<line class="ref" x1="{pad_l}" x2="{W - pad_r}" y1="{zero_y:.1f}" '
                 f'y2="{zero_y:.1f}"/>')
    for i, r in enumerate(rows):
        cx = pad_l + slot * (i + 0.5)
        parts.append(f'<text class="axis" x="{cx:.1f}" y="{H - pad_b + 18:.1f}" '
                     f'text-anchor="middle">{r["horizon"]}h</text>')
        v = r["mean_return"]
        if v is None:
            continue
        top = min(y_of(v), zero_y)
        height = abs(y_of(v) - zero_y)
        cls = "pos" if v >= 0 else "neg"
        parts.append(
            f'<path class="bar {cls}" d="{_bar_path(cx - bar_w / 2, top, bar_w, height)}">'
            f'<title>{r["horizon"]}h — mean {v:+.2f}% over n={r["n"]}</title></path>')
        med = r["median_return"]
        my = y_of(med) if med is not None else None
        if my is not None:
            parts.append(_rule(cx - bar_w / 2 - 5, cx + bar_w / 2 + 5, my,
                               f"median {med:+.2f}%"))
        ly = top - 8 if v >= 0 else top + height + 16
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{_clear_of(ly, my):.1f}" '
                     f'text-anchor="middle">{v:+.2f}%</text>')
    parts.append(f'<text class="tick" x="{pad_l - 8}" y="{zero_y + 4:.1f}" '
                 f'text-anchor="end">0%</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def _matrix_table(samples: list[Sample], horizons: tuple[int, ...]) -> str:
    head = "".join(f"<th>{h}h</th>" for h in horizons)
    rows = []
    last_window = None
    for s in samples:
        stamp = s.when.strftime("%Y-%m-%d")
        first = stamp != last_window
        last_window = stamp
        cls = ' class="wstart"' if first else ""
        label = f'<span class="tag {s.label}">{_e(s.label)}</span>' if first else ""
        cells = []
        if s.status != OK:
            cells.append(f'<td colspan="{len(horizons)}" class="skip">'
                         f'{_e(_STATUS_LABEL.get(s.status, s.status))}'
                         f'{" — " + _e(s.detail) if s.detail else ""}</td>')
        else:
            for h in horizons:
                pt = s.hold.get(h, {})
                if not pt.get("covered"):
                    cells.append('<td class="cell muted">no data</td>')
                    continue
                ret = pt["return_pct"]
                right = ret > 0
                race = s.race.get(h, {})
                r_txt = _num(race.get("r"), 2, sign=True) + "R" if race.get("r") is not None else "—"
                out = _RACE_SHORT.get(race.get("outcome", ""), race.get("outcome", ""))
                mark = "✓" if right else "✗"
                cells.append(
                    f'<td class="cell {"good" if right else "bad"}">'
                    f'<span class="mark">{mark}</span> {_pct(ret)}'
                    f'<span class="sub">{r_txt} · {_e(out)}</span></td>')
        side = _e(s.side or "—")
        entry = "market" if s.entry_price is None else f"{s.entry_price:,.0f}"
        rows.append(
            f'<tr{cls}><td class="w">{_e(stamp) if first else ""}</td>'
            f'<td class="w">{label}</td><td>r{s.rep}</td><td>{side}</td>'
            f'<td class="num">{entry}</td>'
            f'<td class="num">{_num(s.stop_pct, 2) + "%" if s.stop_pct else "—"}</td>'
            f'<td class="num">{_num(s.target_pct, 2) + "%" if s.target_pct else "—"}</td>'
            f'{"".join(cells)}</tr>')
    return (f'<table class="matrix"><thead><tr><th>window (12:00 UTC)</th><th>tape</th>'
            f'<th>rep</th><th>side</th><th>entry</th><th>stop</th><th>target</th>{head}</tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table>')


def _direction_table(rows: list[dict]) -> str:
    body = []
    for r in rows:
        ci = (f'{r["ci"][0] * 100:.0f}–{r["ci"][1] * 100:.0f}%' if r["ci"] else "—")
        acc = f'{r["accuracy"] * 100:.0f}%' if r["accuracy"] is not None else "—"
        agree = f'{r["agreement"] * 100:.0f}%' if r["agreement"] is not None else "—"
        base = _best_constant(r)
        edge = (r["accuracy"] - base[1]) * 100 if base and r["accuracy"] is not None else None
        base_txt = f"always {base[0]} {base[1] * 100:.0f}%" if base else "—"
        edge_txt = f"{edge:+.1f} pp" if edge is not None else "—"
        long_txt = (f'{r["long_accuracy"] * 100:.0f}%'
                    if r["long_accuracy"] is not None else "—")
        short_txt = (f'{r["short_accuracy"] * 100:.0f}%'
                     if r["short_accuracy"] is not None else "—")
        thin = r["decided"] < 10
        flags = []
        if thin:
            flags.append("⚠ too thin")
        if edge is not None and edge <= 0:
            flags.append("✗ no edge")
        body.append(
            f'<tr{" class=thin" if thin else ""}><td class="w">{r["horizon"]}h</td>'
            f'<td class="num">{r["decided"]}</td>'
            f'<td class="num">{r["n_windows"]}</td>'
            f'<td class="num">{r["effective_windows"] if r["effective_windows"] is not None else "—"}</td>'
            f'<td class="num">{acc}</td><td class="num ci">{ci}</td>'
            f'<td class="num">{base_txt}</td>'
            f'<td class="num">{edge_txt}</td>'
            f'<td class="num">{r["long_n"]} @ {long_txt}</td>'
            f'<td class="num">{r["short_n"]} @ {short_txt}</td>'
            f'<td class="num">{agree}</td>'
            f'<td>{" · ".join(flags)}</td></tr>')
    return (
        '<table class="summary"><thead><tr>'
        '<th>horizon</th><th>n calls</th><th>windows</th><th>eff. indep.</th>'
        '<th>direction right</th><th>95% CI</th><th>best constant</th><th>edge</th>'
        '<th>long calls</th><th>short calls</th><th>repeat agreement</th><th></th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>')


def _money_table(rows: list[dict]) -> str:
    body = []
    for r in rows:
        thin = r["decided"] < 10
        body.append(
            f'<tr{" class=thin" if thin else ""}><td class="w">{r["horizon"]}h</td>'
            f'<td class="num">{_pct(r["mean_return"])}</td>'
            f'<td class="num">{_pct(r["median_return"])}</td>'
            f'<td class="num">{_pct(r["mean_mfe"], sign=False)}</td>'
            f'<td class="num">{_pct(r["mean_mae"], sign=False)}</td>'
            f'<td class="num">{_num(r["race_mean_r"], 2, sign=True)}</td>'
            f'<td class="num">{r["race_resolved"]}/{r["n"]}</td>'
            f'<td class="num">{r["stopped_out"]}/{r["comparable"]}</td>'
            f'<td class="num">{r["stopped_but_right"]}</td></tr>')
    return (
        '<table class="summary"><thead><tr>'
        '<th>horizon</th><th>mean blind return</th><th>median</th><th>mean MFE</th>'
        '<th>mean MAE</th><th>race R (mean)</th><th>race resolved</th>'
        '<th>stop reached</th><th>stopped but right</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>')


def _issues_table(issues: list[dict]) -> str:
    order = {"critical": 0, "serious": 1, "warning": 2, "good": 3}
    body = []
    for i in sorted(issues, key=lambda x: order.get(x["severity"], 9)):
        glyph = {"critical": "■", "serious": "▲", "warning": "●", "good": "✓"}[i["severity"]]
        body.append(
            f'<tr><td><span class="sev {i["severity"]}">{glyph} {i["severity"]}</span></td>'
            f'<td><strong>{_e(i["title"])}</strong></td>'
            f'<td>{i["evidence"]}</td>'
            f'<td class="mono">{_e(i["where"])}</td>'
            f'<td>{i["fix"]}</td></tr>')
    return ('<table class="issues">'
            '<colgroup><col class="c1"><col class="c2"><col class="c3">'
            '<col class="c4"><col class="c5"></colgroup>'
            '<thead><tr><th>severity</th><th>issue</th>'
            '<th>evidence (window · number)</th><th>where</th><th>suggested fix</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table>')


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

_CSS = """
:root{color-scheme:light dark}
.viz-root{
  --surface-1:#fcfcfb; --plane:#f9f9f7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --neg:#e34948;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --good-ink:#006300; --bad-ink:#b02a2a;
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme=light])) .viz-root{
    --surface-1:#1a1a19; --plane:#0d0d0d;
    --text-primary:#fff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --neg:#e66767;
    --good-ink:#0ca30c; --bad-ink:#e66767;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:32px 28px 72px;background:var(--plane);color:var(--text-primary);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:17px;margin:38px 0 4px}
h3{font-size:14px;margin:22px 0 4px;color:var(--text-secondary)}
p,li{color:var(--text-secondary);max-width:78ch}
.lede{font-size:15px;color:var(--text-secondary);margin:0 0 4px}
.meta{color:var(--muted);font-size:12px;margin:2px 0 0}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  padding:18px 20px;margin:14px 0}
.tiles{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0 4px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px;min-width:150px;flex:1}
.tile .v{font-size:26px;font-weight:600;letter-spacing:-.02em}
.tile .k{font-size:12px;color:var(--muted);margin-top:2px}
.tile .n{font-size:12px;color:var(--text-secondary);margin-top:6px}
.charts{display:flex;flex-wrap:wrap;gap:14px}
.charts .card{flex:1;min-width:420px}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .ref{stroke:var(--axis);stroke-width:1}
svg .ci{stroke:var(--text-secondary);stroke-width:2}
svg .base{stroke:var(--text-primary);stroke-width:2;stroke-dasharray:5 3}
svg .halo{stroke:var(--surface-1);stroke-width:6;stroke-linecap:round}
svg .bar{fill:var(--series-1)}
svg .bar.neg{fill:var(--neg)}
svg text{font:11px system-ui,-apple-system,sans-serif}
svg .tick{fill:var(--muted)}
svg .axis{fill:var(--text-secondary)}
table{border-collapse:collapse;width:100%;font-size:12.5px;background:var(--surface-1);
  border:1px solid var(--border);border-radius:10px;overflow:hidden}
th{text-align:left;font-weight:600;color:var(--text-secondary);padding:8px 9px;
  border-bottom:1px solid var(--border);white-space:nowrap;font-size:11.5px}
td{padding:7px 9px;border-bottom:1px solid var(--grid);vertical-align:top;
  color:var(--text-primary)}
tr:last-child td{border-bottom:none}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  color:var(--text-secondary);white-space:nowrap}
.w{white-space:nowrap}
.scroll{overflow-x:auto;border-radius:10px}
.matrix{font-size:12px}
.matrix td,.matrix th{padding:6px 7px}
.matrix tr.wstart td{border-top:1px solid var(--axis)}
.matrix .cell{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.matrix .cell .mark{font-weight:700}
.matrix .cell.good .mark{color:var(--good-ink)}
.matrix .cell.bad .mark{color:var(--bad-ink)}
.matrix .cell .sub{display:block;color:var(--muted);font-size:11px}
.matrix .skip{color:var(--text-secondary);font-style:italic}
.matrix .muted{color:var(--muted)}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
  border:1px solid var(--border);color:var(--text-secondary)}
.tag.up::before{content:"▲ "}
.tag.down::before{content:"▼ "}
.summary tr.thin td{background:color-mix(in srgb,var(--warning) 9%,transparent)}
.summary .ci{color:var(--muted)}
.issues{table-layout:fixed}
.issues col.c1{width:8%}.issues col.c2{width:20%}.issues col.c3{width:29%}
.issues col.c4{width:17%}.issues col.c5{width:26%}
.issues .mono{white-space:normal;overflow-wrap:anywhere}
.sev{white-space:nowrap;font-weight:600;font-size:11.5px}
.sev.critical{color:var(--critical)}
.sev.serious{color:var(--serious)}
.sev.warning{color:var(--warning)}
.sev.good{color:var(--good-ink)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
  background:color-mix(in srgb,var(--text-primary) 7%,transparent);padding:1px 4px;
  border-radius:4px}
.note{border-left:3px solid var(--axis);padding-left:12px;margin:14px 0}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);
  margin:2px 0 10px}
.legend .sw{display:inline-block;width:14px;height:9px;border-radius:2px;
  background:var(--series-1);margin-right:6px;vertical-align:middle}
.legend .sw.neg{background:var(--neg)}
.legend .dash{display:inline-block;width:16px;border-top:2px dashed var(--text-primary);
  margin-right:6px;vertical-align:middle}
.legend .whisk{display:inline-block;width:2px;height:12px;background:var(--text-secondary);
  margin-right:6px;vertical-align:middle}
"""


def render_html(*, samples: list[Sample], summary: list[dict], agreement: dict,
                issues: list[dict], windows: list[dict], meta: dict,
                horizons: tuple[int, ...], verdict: list[tuple[str, str]]) -> str:
    ok = [s for s in samples if s.status == OK]
    usable = [r for r in summary if r["decided"] >= 10]
    best = max((r for r in usable if r["accuracy"] is not None),
               key=lambda r: r["accuracy"], default=None)
    best_r = max((r for r in usable if r["race_mean_r"] is not None),
                 key=lambda r: r["race_mean_r"], default=None)
    # An edge only counts if it beats the better of the two constant
    # strategies on the very same calls.
    beats = [r for r in usable
             if r["accuracy"] is not None and _best_constant(r)
             and r["accuracy"] > _best_constant(r)[1]]

    tiles = [
        ("graph runs", f'{len(samples)}',
         f'{meta["n_windows"]} windows × {meta["repeats"]} repeats'),
        ("scoreable calls", f'{len(ok)}',
         f'{agreement["flat"]} flat · {agreement["excluded"]} excluded'),
        ("direction split", f'{agreement["long"]}L / {agreement["short"]}S',
         f'tape was {meta["up_windows"]} up / {meta["down_windows"]} down at 168h'),
        ("best accuracy", f'{best["horizon"]}h' if best else "—",
         (f'{best["accuracy"] * 100:.0f}% (95% CI {best["ci"][0] * 100:.0f}–'
          f'{best["ci"][1] * 100:.0f}%)') if best else "no horizon reached n≥10"),
        ("horizons beating a constant strategy", f'{len(beats)}/{len(usable)}',
         "  ·  ".join(f'{r["horizon"]}h' for r in beats) or "none"),
        ("repeat side agreement", f'{agreement["unanimous_side_rate"] * 100:.0f}%'
         if agreement["unanimous_side_rate"] is not None else "—",
         f'{agreement["unanimous_side"]}/{agreement["windows_with_repeats"]} windows'
         ' where every repeat picked the same side'),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="v">{_e(v)}</div><div class="k">{_e(k)}</div>'
        f'<div class="n">{_e(n)}</div></div>' for k, v, n in tiles)

    win_rows = "".join(
        f'<tr><td class="w">{_e(w["when"][:16].replace("T", " "))}</td>'
        f'<td><span class="tag {w["label"]}">{_e(w["label"])}</span></td>'
        + "".join(f'<td class="num">{w["fwd"][str(h)] if str(h) in w["fwd"] else w["fwd"][h]:+.2f}%</td>'
                  for h in horizons)
        + "</tr>" for w in windows)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monopoly — optimal holding period ({_e(meta['generated'][:10])})</title>
<style>{_CSS}</style></head>
<body class="viz-root"><div class="wrap">

<h1>Which holding period does Monopoly actually trade?</h1>
<p class="lede">{_e(meta['n_windows'])} point-in-time windows across {_e(meta['range'])},
each replayed {_e(meta['repeats'])}× — {len(samples)} graph runs — scored at
{", ".join(f"{h}h" for h in horizons)} two independent ways.</p>
<p class="meta">{_e(meta['symbol'])} · market analyst only · {_e(meta['model'])} ·
generated {_e(meta['generated'])} · raw decisions in <code>{_e(meta['jsonl'])}</code></p>

<div class="tiles">{tiles_html}</div>

<h2>The three questions this run was built to answer</h2>
{"".join(f'<div class="card"><strong>{_e(q)}</strong><p style="margin:6px 0 0">{a}</p></div>'
         for q, a in verdict)}

<div class="card note">
<strong>Two measurements, deliberately.</strong>
<em>Blind hold</em> enters at the first bar after the decision and holds to the
h-hour mark with no stop and no target — it measures the direction call alone,
which is the only thing comparable across columns. <em>Race</em> applies the
decision's own stop and target and marks to market at the horizon if neither is
hit. Where the two disagree, the tactics, not the analysis, are what moved the
number.
</div>

<h2>Directional accuracy by holding period</h2>
<p>Bars are the share of calls whose blind-hold return was positive; whiskers are
95% Wilson intervals. The dashed rule on each bar is the better of <em>always long</em>
and <em>always short</em> measured on exactly the same calls — the bar has to clear
that rule, not the coin-flip line, to mean anything.</p>
<div class="charts">
  <div class="card">
    <div class="legend"><span><span class="sw"></span>directional accuracy</span>
    <span><span class="whisk"></span>95% Wilson interval</span>
    <span><span class="dash"></span>best constant strategy</span></div>
    {_accuracy_chart(summary)}</div>
  <div class="card">
    <div class="legend"><span><span class="sw"></span>mean blind return (positive)</span>
    <span><span class="sw neg"></span>mean (negative)</span>
    <span><span class="dash"></span>median</span></div>
    {_return_chart(summary)}</div>
</div>

<h2>Direction, by holding period</h2>
<p><em>eff. indep.</em> is the number of non-overlapping windows the sample really
contains (calendar span ÷ horizon): at 720h the windows overlap so heavily that they
are closer to 6 independent observations than to {_e(meta['n_windows'])}.
<em>best constant</em> is what a model that always took the same side would have
scored on these same calls; <em>edge</em> is the difference. The per-side columns are descriptive only —
inside the long-only subset the model's accuracy <em>is</em> the tape's up-rate, so
there is nothing there to compare it against; only the pooled edge column is a
comparison. Rows with fewer than 10 decided calls are shaded and must not be used
for a judgement.</p>
<div class="scroll">{_direction_table(summary)}</div>

<h2>Money, by holding period</h2>
<p><em>Blind return</em> ignores the stop and target; <em>race R</em> applies them.
<em>stop reached</em> counts the market-entry calls whose adverse excursion reached
the decision's own stop distance before the horizon — limit orders are left out
because their stop is measured from a price the blind hold never used.
<em>stopped but right</em> is the subset whose direction was nevertheless correct at
the horizon: money the analysis earned and the stop gave back.</p>
<div class="scroll">{_money_table(summary)}</div>

<h2>Every decision, every horizon</h2>
<p>One row per replay. <code>stop</code>/<code>target</code> are distances from the entry
the decision named. Each cell: blind-hold return with ✓/✗ for direction, and underneath
the same trade under its own stop and target — R multiple, then whether the target, the
stop, or neither had been reached by that hour.</p>
<div class="scroll">{_matrix_table(samples, horizons)}</div>

<h2>Issues found</h2>
{_issues_table(issues)}

<h2>Windows under test</h2>
<p>Chosen before any replay ran, one median up-mover and one median down-mover per
time block, so a permanent directional bias shows up as bias rather than skill.
Forward moves of the tape itself (not of any trade):</p>
<div class="scroll"><table><thead><tr><th>window (UTC)</th><th>tape at 168h</th>
{"".join(f"<th>{h}h</th>" for h in horizons)}</tr></thead><tbody>{win_rows}</tbody></table></div>

<h2>Method and its limits</h2>
<ul>
<li><strong>Point in time.</strong> The data layer is frozen at each cutoff: a bar is
visible only once <code>close_time &lt;= as_of</code>, and live HTTP is replaced by a
raiser, so a leak fails the run instead of contaminating it silently.</li>
<li><strong>Market analyst only.</strong> News, Reddit and the newsflash relay have no
dated archive. This measures <code>selected_analysts=["market"]</code>, not the
three-analyst production configuration.</li>
<li><strong>Overlapping forward windows.</strong> The 24 windows span
{_e(meta['range'])}; at 336h and 720h their forward paths overlap, so those rows carry
far less independent information than their n suggests. See <em>eff. indep.</em>.</li>
<li><strong>Race R at a horizon</strong> is realised when the stop or target is hit and
marked to market otherwise, so it is not directly comparable with a pure
win/loss R.</li>
<li><strong>Excluded decisions</strong> are listed in the matrix with their reason and are
absent from every statistic; they are not counted as losses.</li>
</ul>

</div></body></html>
"""
