"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Routes by ``asset_type``:

- ``stock`` (default): emits a :class:`PortfolioDecision` (5-tier rating
  with executive summary + thesis).
- ``crypto`` (Monopoly fork, perp futures): emits a :class:`FuturesDecision`
  with the final side / leverage / sizing / stop / take-profit that the
  downstream risk gate validates before any order is placed. The
  crypto path also surfaces the structured object as
  ``final_decision_structured`` so the risk-gate node consumes it
  directly without re-parsing the rendered markdown.

Both paths use LangChain's ``with_structured_output`` so the LLM produces
the typed object directly. The render helpers convert back to markdown so
``final_trade_decision`` keeps the same shape for memory log, CLI display,
and saved reports.
"""

from __future__ import annotations

import logging

from tradingagents.agents.schemas import (
    FuturesDecision,
    PortfolioDecision,
    render_futures_decision,
    render_pm_decision,
)
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)

logger = logging.getLogger(__name__)


def create_portfolio_manager(llm):
    structured_stock = bind_structured(llm, PortfolioDecision, "Portfolio Manager")
    structured_futures = bind_structured(llm, FuturesDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        asset_type = state.get("asset_type", "stock")
        instrument_context = build_instrument_context(state["company_of_interest"], asset_type)

        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state["history"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        structured_decision = None
        if asset_type == "crypto":
            prompt = _build_crypto_prompt(
                instrument_context=instrument_context,
                research_plan=research_plan,
                trader_plan=trader_plan,
                history=history,
                lessons_line=lessons_line,
            )
            # Inline the structured-then-fallback flow so we can capture
            # the typed FuturesDecision object for the downstream risk
            # gate. ``invoke_structured_or_freetext`` only returns the
            # rendered markdown, which the gate would have to re-parse.
            if structured_futures is None:
                # Provider doesn't support structured output — go straight to
                # free text (no alarming "failed" warning; bind_structured
                # already logged the capability gap at construction time).
                response = llm.invoke(prompt)
                final_trade_decision = response.content
                structured_decision = None
            else:
                try:
                    structured_decision = structured_futures.invoke(prompt)
                    final_trade_decision = render_futures_decision(structured_decision)
                except Exception as exc:
                    logger.warning(
                        "Portfolio Manager: structured FuturesDecision failed (%s); "
                        "falling back to free text — risk gate will reject for missing "
                        "structured decision",
                        exc,
                    )
                    response = llm.invoke(prompt)
                    final_trade_decision = response.content
                    structured_decision = None
        else:
            prompt = _build_stock_prompt(
                instrument_context=instrument_context,
                research_plan=research_plan,
                trader_plan=trader_plan,
                history=history,
                lessons_line=lessons_line,
            )
            final_trade_decision = invoke_structured_or_freetext(
                structured_stock,
                llm,
                prompt,
                render_pm_decision,
                "Portfolio Manager",
            )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_decision_structured": structured_decision,
        }

    return portfolio_manager_node


def _build_stock_prompt(
    *,
    instrument_context: str,
    research_plan: str,
    trader_plan: str,
    history: str,
    lessons_line: str,
) -> str:
    return f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{get_language_instruction()}"""


def _build_crypto_prompt(
    *,
    instrument_context: str,
    research_plan: str,
    trader_plan: str,
    history: str,
    lessons_line: str,
) -> str:
    return f"""As the Portfolio Manager for a crypto perpetual-futures desk, synthesise the risk analysts' debate and emit the **final** futures decision the risk gate will validate.

{instrument_context}

---

## Decision shape (FuturesDecision)
- **side**: exactly one of `Long` / `Short` / `Flat`.
- **leverage**: required when side != Flat. The risk gate caps at the configured maximum (default 3x); do not exceed it.
- **position_size_pct**: required when side != Flat. Decimal fraction of equity to risk on this trade. The risk gate enforces a HARD CEILING at 0.01 (= 1%). Anything above is rejected and the trade is skipped.
    - 0.003 (= 0.3%)  — low conviction
    - 0.005 (= 0.5%)  — moderate; default starting point when in doubt
    - 0.010 (= 1.0%)  — high conviction; AT the ceiling
    - **Common LLM mistake**: writing `0.1` thinking it is small. `0.1 = 10%` = 10× the ceiling = REJECTED. Always check: the value you write should be 0.001 to 0.01.
- **stop_loss**: required when side != Flat. Place where the thesis is invalidated, not at a round number. Must be below entry for Long, above for Short.
- **take_profit**: optional. Aim for ≥ 2:1 reward:risk against the stop.
- **entry_price**: omit for market entry; specify only if you want a limit at a clear level.

## How to ratify or adjust the Trader's proposal
- If the risk analysts' debate **supports** the Trader's side, ratify it. You may tighten leverage / sizing if the conservative analyst raises a credible drawdown risk.
- If the debate **contradicts** the Trader's side strongly, flip the side or move to Flat — but only with explicit reasoning from the debate, not gut feel.
- If the debate is **balanced**, prefer the Trader's side at *reduced* leverage and sizing rather than flipping to Flat.

## Hard rules
1. side != Flat requires `leverage`, `position_size_pct`, and `stop_loss`. The risk gate will reject decisions missing these.
2. Stop-loss must be on the correct side of entry. Do not invert.
3. `position_size_pct` MUST be <= 0.01. Re-read the examples above before writing. If unsure, write 0.005.
4. `leverage` MUST be <= 3.0. Typical values: 1.0 (conservative), 2.0 (default), 3.0 (high conviction, at ceiling).
5. Investment thesis must cite the analysts and the debate, not generic market commentary.

---

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's futures proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{get_language_instruction()}"""
