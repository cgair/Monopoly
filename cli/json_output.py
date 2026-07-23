"""JSON output helpers for machine-readable trade analysis results."""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

from tradingagents.agents.schemas import FuturesDecision, FuturesSide
from tradingagents.futures.risk_state import RiskGateSnapshot


def serialize_decision(decision: Any) -> dict:
    """Convert a FuturesDecision (or dataclass fallback) to JSON-serializable dict."""
    if decision is None:
        return {}

    if isinstance(decision, FuturesDecision):
        return {
            "type": "FuturesDecision",
            "side": decision.side.value if isinstance(decision.side, FuturesSide) else decision.side,
            "leverage": decision.leverage,
            "position_size_pct": decision.position_size_pct,
            "entry_price": decision.entry_price,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "executive_summary": decision.executive_summary,
            "investment_thesis": decision.investment_thesis,
            "time_horizon": decision.time_horizon,
        }
    else:
        # Try to convert using asdict if it's a dataclass
        try:
            d = asdict(decision)
            return d
        except TypeError:
            return {"type": type(decision).__name__, "raw": str(decision)}


def serialize_risk_snapshot(snapshot: Optional[RiskGateSnapshot]) -> dict:
    """Convert RiskGateSnapshot to JSON-serializable dict."""
    if snapshot is None:
        return {}
    return {
        "open_positions": snapshot.open_positions,
        "daily_realised_pnl_usd": snapshot.daily_realised_pnl_usd,
        "last_stop_loss_close_ts": (
            snapshot.last_stop_loss_close_ts.isoformat()
            if snapshot.last_stop_loss_close_ts
            else None
        ),
    }


def output_json(
    data: dict,
    pretty: bool = False,
) -> None:
    """Write JSON to stdout; logs to stderr remain unaffected."""
    if pretty:
        json_str = json.dumps(data, indent=2, default=str)
    else:
        json_str = json.dumps(data, separators=(",", ":"), default=str)
    print(json_str)


def build_analysis_result(
    final_state: dict,
    selections: dict,
    decision: Any,
    error: Optional[str] = None,
) -> dict:
    """Build a complete analysis result dict suitable for JSON output.

    Args:
        final_state: Final state from graph execution
        selections: User selections (ticker, analysis_date, etc.)
        decision: Processed decision object
        error: Optional error message if analysis failed

    Returns:
        Dict ready for JSON serialization.
    """
    result = {
        "timestamp": datetime.now(datetime.now().astimezone().tzinfo).isoformat(),
        "status": "error" if error else "success",
        "error": error,
        "analysis": {
            "ticker": selections.get("ticker"),
            # Fixed literal kept for OpenClaw consumers' schema compatibility.
            "asset_type": "crypto",
            "analysis_date": selections.get("analysis_date"),
            "selected_analysts": selections.get("analysts", []),
        },
        "decision": serialize_decision(decision),
        "reports": {
            "market_report": final_state.get("market_report"),
            "sentiment_report": final_state.get("sentiment_report"),
            "news_report": final_state.get("news_report"),
            "investment_plan": final_state.get("investment_plan"),
            "trader_investment_plan": final_state.get("trader_investment_plan"),
            "final_trade_decision": final_state.get("final_trade_decision"),
        },
    }
    return result
