"""Run the agent graph as of a past instant and capture what it decided.

Wraps :func:`tradingagents.backtest.pit.point_in_time` around a normal
graph run, with every side effect redirected into a scratch directory so
a replay cannot touch the live memory log, risk-gate event stream, or
order log.

The execution venue is pinned separately, because it is the one side
effect the scratch config cannot reach: ``resolve_executor_mode`` reads
``EXECUTOR_MODE`` from the environment ahead of the config, so a
``.env`` naming testnet outranks anything a replay asks for. The
2026-08-08 sweep set the config, assumed it held, and sent 12 market
orders to Binance testnet.

Scope is deliberately narrow. Only the market analyst is selected: its
inputs (OHLCV, funding, open interest, the long/short ratios) all have
dated archives, while news, Reddit and the newsflash relay do not. A
replay that included them would be reading today's headlines while
claiming to stand in the past, so the leak guard in ``pit`` fails the
run instead.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.agents.utils.symbol_utils import to_binance_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.futures import market_data as fmd
from tradingagents.futures.executor import resolve_executor_mode

from . import vision
from .pit import point_in_time

logger = logging.getLogger(__name__)

_FIELD = {
    "side": re.compile(r"\*\*Side\*\*:\s*(\w+)", re.I),
    "leverage": re.compile(r"\*\*Leverage\*\*:\s*([\d.]+)", re.I),
    "position_size_pct": re.compile(r"\*\*Position Size\*\*:\s*([\d.]+)", re.I),
    "entry_price": re.compile(r"\*\*Entry Price\*\*:\s*([\d.]+)", re.I),
    "stop_loss": re.compile(r"\*\*Stop Loss\*\*:\s*([\d.]+)", re.I),
    "take_profit": re.compile(r"\*\*Take Profit\*\*:\s*([\d.]+)", re.I),
    "time_horizon": re.compile(r"\*\*Time Horizon\*\*:\s*(.+)", re.I),
}


@dataclass
class ReplayResult:
    symbol: str
    as_of: int                       # ms epoch UTC
    rep: int = 0                     # repeat index — same instant, resampled
    side: str | None = None
    leverage: float | None = None
    position_size_pct: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    time_horizon: str | None = None
    reference_price: float | None = None   # last closed price at the cutoff
    decision_text: str = ""
    market_report: str = ""
    gate_passed: bool | None = None
    gate_reason: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def _parse_decision(text: str) -> dict:
    out: dict = {}
    for key, rx in _FIELD.items():
        m = rx.search(text or "")
        if not m:
            continue
        raw = m.group(1).strip()
        if key in ("side", "time_horizon"):
            out[key] = raw
        else:
            try:
                out[key] = float(raw)
            except ValueError:
                pass
    return out


_MODE_ENV = "EXECUTOR_MODE"
_DRYRUN = "dryrun"


@contextlib.contextmanager
def _pinned_to_dryrun(cfg: dict):
    """Pin the execution venue to ``dryrun`` for the duration of a replay.

    Setting ``futures_executor_mode`` in the scratch config is not enough.
    :func:`~tradingagents.futures.executor.resolve_executor_mode` reads
    ``EXECUTOR_MODE`` from the environment first, and deliberately so —
    one source of truth is what keeps the mark-price node and the
    executor from resolving the venue independently. The override
    therefore has to happen at the environment, and it has to be
    *verified* rather than assumed.

    An unpinnable venue is a global misconfiguration, not a bad window,
    so this raises rather than being folded into ``ReplayResult.error``:
    a sweep must stop, not quietly produce rows while pointed at a live
    exchange.
    """
    previous = os.environ.get(_MODE_ENV)
    os.environ[_MODE_ENV] = _DRYRUN
    try:
        resolved = resolve_executor_mode(cfg)
        if resolved != _DRYRUN:
            raise RuntimeError(
                f"replay refuses to start: execution venue resolved to "
                f"{resolved!r}, not {_DRYRUN!r} — a replay must never be able "
                f"to reach a live venue"
            )
        yield
    finally:
        if previous is None:
            os.environ.pop(_MODE_ENV, None)
        else:
            os.environ[_MODE_ENV] = previous


def _scratch_config(scratch: Path, base: dict | None = None) -> dict:
    """A config whose every write path lands inside ``scratch``."""
    cfg = dict(base or DEFAULT_CONFIG)
    scratch.mkdir(parents=True, exist_ok=True)
    cfg.update({
        "results_dir": str(scratch / "logs"),
        "data_cache_dir": str(scratch / "cache"),
        "memory_log_path": str(scratch / "memory" / "trading_memory.md"),
        "futures_risk_state_path": str(scratch / "risk_gate_state.jsonl"),
        "futures_executor_mode": "dryrun",
        "futures_orders_log_path": str(scratch / "orders.jsonl"),
        "checkpoint_enabled": False,
        "online_tools": True,
    })
    return cfg


def replay(symbol: str, as_of: datetime, *, scratch: Path,
           config: dict | None = None,
           selected_analysts: list[str] | None = None,
           rep: int = 0) -> ReplayResult:
    """Run the graph as of ``as_of`` and return the decision it produced.

    ``rep`` only labels the result: repeated runs of the same instant are
    independent samples of a stochastic model, not a cached lookup.
    """
    as_of = as_of.astimezone(timezone.utc)
    as_of_ms = int(as_of.timestamp() * 1000)
    binance_symbol = to_binance_symbol(symbol)
    result = ReplayResult(symbol=symbol, as_of=as_of_ms, rep=rep)

    cfg = _scratch_config(scratch, config)
    cfg["selected_analysts"] = selected_analysts or ["market"]

    # Reference price = last bar closed at the cutoff. Also stands in for
    # the live mark-price fetch the executor tail would otherwise make.
    bars = vision.klines(binance_symbol, "1h", as_of_ms - 6 * 3_600_000, as_of_ms)
    closed = [b for b in bars if b.close_time <= as_of_ms]
    if not closed:
        result.error = "no archived bars at cutoff"
        return result
    result.reference_price = closed[-1].close

    with _pinned_to_dryrun(cfg):
        original_mark = fmd.fetch_mark_price
        fmd.fetch_mark_price = lambda _t, **_kw: result.reference_price
        try:
            with point_in_time(as_of_ms, binance_symbol):
                from tradingagents.graph.trading_graph import TradingAgentsGraph

                graph = TradingAgentsGraph(
                    selected_analysts=cfg["selected_analysts"], config=cfg,
                )
                final_state, _decision = graph.propagate(
                    symbol, as_of.strftime("%Y-%m-%d"))

            text = final_state.get("final_trade_decision", "") or ""
            result.decision_text = text
            result.market_report = final_state.get("market_report", "") or ""
            for k, v in _parse_decision(text).items():
                setattr(result, k, v)

            # The gate reports by absence: an intent means approved, a
            # rejection reason means vetoed.
            if "execution_intent" in final_state or "risk_gate_rejection_reason" in final_state:
                reason = final_state.get("risk_gate_rejection_reason")
                result.gate_reason = reason
                result.gate_passed = (
                    final_state.get("execution_intent") is not None and not reason
                )
        except Exception as exc:                  # a failed window must not sink the sweep
            logger.exception("replay failed for %s at %s", symbol, as_of)
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            fmd.fetch_mark_price = original_mark

    return result
