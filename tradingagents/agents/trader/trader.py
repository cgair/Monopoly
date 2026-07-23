"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal.

Emits a :class:`FuturesProposal` (Long / Short / Flat with leverage,
stop-loss, take-profit, and a fractional ``position_size_pct``). The
downstream risk gate validates the execution params against the
configured ceilings.
"""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import (
    FuturesProposal,
    render_futures_proposal,
)
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_futures = bind_structured(llm, FuturesProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)
        investment_plan = state["investment_plan"]

        messages = _build_crypto_messages(
            company_name=company_name,
            instrument_context=instrument_context,
            investment_plan=investment_plan,
        )
        trader_plan = invoke_structured_or_freetext(
            structured_futures,
            llm,
            messages,
            render_futures_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")


def _build_crypto_messages(*, company_name: str, instrument_context: str, investment_plan: str):
    system = (
        "You are a crypto perpetual-futures trader. Your job is to turn the Research Manager's "
        "investment plan into a concrete perp-futures proposal: pick a side (Long / Short / Flat), "
        "set leverage and per-trade risk, and define stop-loss / take-profit levels.\n\n"
        "## Mapping the Research Manager's rating to a side\n"
        "- **Buy** → Long (high conviction)\n"
        "- **Overweight** → Long (lower conviction; reduce leverage and/or sizing)\n"
        "- **Hold** → Flat (do not open a position this round)\n"
        "- **Underweight** → Short (lower conviction; reduce leverage and/or sizing)\n"
        "- **Sell** → Short (high conviction)\n\n"
        "## Execution-param guidance\n"
        "- **Leverage**: keep modest. 2-3x for high-conviction (Buy / Sell); 1-2x for low-conviction "
        "(Overweight / Underweight). The downstream risk gate caps leverage at the configured maximum "
        "(default 3x) and will reject anything above.\n"
        "- **Position size (`position_size_pct`)**: decimal fraction of equity to *risk* on this trade. "
        "Default 0.005-0.01 (0.5% - 1%). The risk gate enforces this ceiling.\n"
        "- **Stop-loss**: required when side != Flat. Place it at a level where the bull / bear thesis "
        "is invalidated, not at a round number. Tight stops on high-leverage entries get wicked out; "
        "size DOWN before widening a stop.\n"
        "- **Take-profit**: optional but recommended. Aim for at least 2:1 reward:risk against the stop. "
        "If structure suggests partial scale-out, name the first level.\n"
        "- **Entry price**: omit for market entry; specify only if you want a limit at a clear level "
        "(e.g. retest of broken resistance, LTF demand zone).\n\n"
        "## Hard rules\n"
        "1. If side != Flat, you MUST set `stop_loss`, `leverage`, and `position_size_pct`. The risk "
        "gate rejects proposals missing these.\n"
        "2. Stop-loss must be on the correct side of entry (below entry for Long, above for Short). "
        "Do not invert.\n"
        "3. Reasoning must cite the analysts' reports and the research plan, not generic market commentary.\n\n"
        + get_language_instruction()
    )
    user = (
        f"The Research Manager has produced an investment plan for the perpetual-futures pair "
        f"`{company_name}`. {instrument_context}\n\n"
        f"## Research Manager's plan\n{investment_plan}\n\n"
        f"Translate this plan into a concrete FuturesProposal. Commit to the side the plan supports — "
        f"flip to Flat only if the plan itself is genuinely ambivalent. Anchor your execution params "
        f"(leverage, sizing, stop, take-profit) in the analysts' technical and sentiment evidence."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
