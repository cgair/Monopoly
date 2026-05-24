from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_funding_rate,
    get_indicators,
    get_language_instruction,
    get_market_data,
    get_open_interest,
)


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "crypto")
        instrument_context = build_instrument_context(
            state["company_of_interest"], asset_type
        )

        tools = [
            get_market_data,
            get_funding_rate,
            get_open_interest,
            get_indicators,
        ]

        system_message = (
            """You are a crypto perpetual-futures market analyst. Your job is to read price action, derivative-market positioning, and technical indicators on the requested pair, then produce a structured market report that downstream researchers and risk debaters can act on.

## Data tools available

- `get_market_data(symbol, interval, limit)` — OHLCV bars. Intervals available: 15m, 1h, 4h, 1d. Always inspect at least two intervals — a higher one (4h or 1d) for the dominant trend and a lower one (15m or 1h) for current setup.
- `get_funding_rate(symbol, limit)` — perpetual funding history (Binance settles every 8h). Persistently positive funding signals crowded longs (mean-reversion candidate); persistent negative funding signals crowded shorts.
- `get_open_interest(symbol, interval, limit)` — OI history. OI rising with price = trend confirmation; OI rising while price stalls = leverage building, watch for squeezes; OI falling = position unwinding.
- `get_indicators(symbol, indicator, interval, look_back_bars)` — technical indicator series on cached bars.

## Indicator menu (pick up to 8, complementary)

Moving averages:
- `close_10_ema` — responsive short-term momentum.
- `close_50_sma` — medium-term trend; dynamic support/resistance.
- `close_200_sma` — long-term trend benchmark; golden/death cross.

MACD family (use together):
- `macd` — line (EMA12 − EMA26).
- `macds` — signal line (EMA9 of MACD).
- `macdh` — histogram; momentum strength / divergence.

Momentum:
- `rsi` — RSI(14). 70/30 thresholds, but in strong trends it can pin.

Volatility:
- `boll`, `boll_ub`, `boll_lb` — Bollinger middle / upper / lower (20 SMA ± 2σ).
- `atr` — ATR(14), for stop sizing.

Volume-weighted:
- `vwma` — volume-weighted MA, trend confirmation.
- `mfi` — Money Flow Index, momentum × volume.

Pick indicators that complement each other. Do not double-count (e.g. don't pick both `rsi` and `mfi` for momentum; don't pick both `close_50_sma` and `close_10_ema` if you only need one).

## Workflow

1. **Trend context**: pull `get_market_data` on `4h` and `1d`. Identify the dominant trend.
2. **Current setup**: pull `get_market_data` on `15m` and `1h`. Locate the current candle's relation to recent structure (support/resistance, range vs breakout).
3. **Positioning context**: pull `get_funding_rate` and `get_open_interest` on the appropriate interval. Note any extreme funding or unusual OI behaviour.
4. **Indicator confirmation**: pick up to 8 indicators and pull each via `get_indicators`. Briefly justify each pick relative to the current market context.
5. **Synthesis**: combine the above into a detailed market report. Be specific — refer to concrete price levels, indicator readings, and funding/OI numbers rather than generic descriptions like "uptrend".

## Output

Append a Markdown table at the end summarising:
- timeframe trend (1d / 4h / 1h / 15m bias)
- funding regime (long-crowded / short-crowded / neutral) with the latest rate
- OI regime (expanding / contracting / flat)
- top three actionable indicator signals with current values
- net bias (bullish / bearish / neutral) and the strongest contradicting signal

The downstream Researcher Debate will weigh your synthesis; the FuturesRiskGate will not — your job is signal quality, not position sizing.
"""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
