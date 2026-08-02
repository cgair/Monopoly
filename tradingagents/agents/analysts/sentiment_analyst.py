"""Sentiment analyst — crypto-native retail sentiment & narrative read.

Pre-fetches three blocks (News, Twitter, Reddit) and asks the LLM for
a compressed read on: dominant narratives, retail temperature, and
emerging catalysts. The News block is framing baseline only — the
News Analyst already covers depth — so this agent's distinct job is
the retail / community pulse that news flow does not capture.

The Twitter block is an editorial relay: `crypto_twitter.get_tweets`
surfaces KOL / institution X activity as reported by newsflash desks
(BlockBeats) rather than raw tweets, so engagement metrics are absent.
The prompt telegraphs this so the LLM reads it as a directional
narrative signal and never weights it by engagement.
"""

from datetime import datetime, timedelta

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.symbol_utils import to_base_symbol
from tradingagents.dataflows.crypto_news import get_news as crypto_get_news
from tradingagents.dataflows.crypto_reddit import get_reddit
from tradingagents.dataflows.crypto_twitter import get_tweets


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a sentiment report in a
    single LLM call.
    """

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = build_instrument_context(ticker)

        # Base-asset code (e.g. BTC-USD → BTC) for symbol-keyword filters.
        base = to_base_symbol(ticker)

        # Pre-fetch all three sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        news_block = crypto_get_news(symbols=(base,), hours=24 * 7)
        twitter_block = get_tweets((base,), hours=24 * 7)
        reddit_block = get_reddit((base,), hours=24 * 7)

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            twitter_block=twitter_block,
            reddit_block=reddit_block,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    "\n{system_message}\n"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # No bind_tools — the data is already in the prompt; a single LLM
        # call produces the report directly.
        chain = prompt | llm
        result = chain.invoke(state["messages"])

        return {
            "messages": [result],
            "sentiment_report": result.content,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    twitter_block: str,
    reddit_block: str,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    return f"""You are a crypto sentiment analyst covering the perpetual-futures pair `{ticker}` over the period {start_date} → {end_date}. The News Analyst is already producing a deep read on the news flow itself; your distinct job is the retail / community pulse and the narratives that are *forming or breaking* — signal that headlines alone do not capture.

## Data blocks (pre-fetched below)

### News headlines — CoinDesk / CoinTelegraph, past 7 days
**Use as institutional-framing baseline only.** Do not re-summarise headline content; the News Analyst handles depth. Compare news tone against community tone to surface divergence.

<start_of_news>
{news_block}
<end_of_news>

### X / Twitter signal — editorial relay, past 7 days
**Not raw tweets.** This block relays KOL / institution X activity as reported by crypto newsflash desks (minutes-level latency, but editorially filtered). There are **no engagement metrics** — treat each item as a directional narrative data point of roughly equal weight, and never infer virality. If the block notes "general newsflash items as weak proxy", explicit X activity was quiet in the window — say so rather than over-reading.

<start_of_twitter>
{twitter_block}
<end_of_twitter>

### Reddit posts — r/CryptoCurrency, r/Bitcoin, r/ethfinance, r/CryptoMarkets, past 7 days
Primary retail-sentiment surface for this report.

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## Subreddit character (load-bearing — weight evidence accordingly)

- **r/CryptoCurrency** — broad firehose, retail-heavy, shitcoin-spam-prone. Volume is high but signal-to-noise is lower; treat it as the market-wide retail mood barometer.
- **r/Bitcoin** — BTC-maximalist tilt. Reliably bullish on BTC; the interesting read here is *intensity* (euphoria, capitulation) and what they're bearish on (ETH, alts, regulators).
- **r/ethfinance** — ETH-thesis-driven, technically literate. Watch for shifts in the L2 / staking / deflation narrative.
- **r/CryptoMarkets** — trader-leaning, more TA / funding-rate / positioning talk. Closest the Reddit set gets to a professional voice.

Weight by engagement (upvote score + comment count) within a subreddit, **but do not compare raw upvote counts across subreddits** — r/CryptoCurrency posts will dominate by sheer base rate.

## How to read this data

1. **Identify dominant narratives.** What 2–4 themes keep recurring across posts? Name each narrative concretely (e.g. "ETF outflows resuming", "L2 fee compression", "exchange counterparty FUD"). A narrative that appears in both News and Reddit is a *consensus* narrative; one that's only in Reddit is *emerging* (or fringe).

2. **Read retail temperature.** Where on the euphoria ↔ fear axis is the community? Cite specific posts and their engagement. Watch for the contrarian extremes: capitulation language with high engagement is often a local-bottom tell; "this time is different" / generational-wealth posts at the top of cycles signal froth.

3. **Surface emerging catalysts.** What's the community *anticipating* in the next days / weeks that news hasn't fully priced yet? (Token unlocks, macro prints, upgrade dates, conference cycles, rumour-driven moves.)

4. **Cross-source divergence is the highest-value signal.** When News framing and Reddit mood disagree — bearish news but euphoric retail, or quiet news with rising community anxiety — explicitly name the divergence and offer a read on which is likely leading.

5. **Be honest about data limits.** The X block is an editorial relay without engagement data (see above); if it fell back to general newsflash, or the Reddit block is thin or a subreddit is silent, say so. A confident sentiment read on three engaged posts is not a confident read.

6. **Past sentiment is not predictive.** Your output is signal for the Researchers and Risk team to weigh against price, funding, and on-chain — not a price call.

## Output

Produce a markdown report in this order:

1. **Retail temperature** — one of: Capitulation / Fearful / Cautious / Neutral / Constructive / Greedy / Euphoric — with a one-sentence confidence note tied to data volume and subreddit coverage.
2. **Dominant narratives (2–4)** — for each: name, which subreddits / news carry it, supporting evidence (cite engagement numbers and notable posts), and whether it's *consensus* or *emerging*.
3. **News-vs-Reddit divergence** — explicit call-out if framing disagrees; "no meaningful divergence" is a valid finding if true.
4. **Emerging catalysts surfaced by the community** — what retail is positioning around that's not yet in the news flow.
5. **Data-limits note** — X signal is relay-based (no engagement data; note if it fell back to general newsflash); any thin subreddits; any subreddit returning `<unavailable>`.
6. **Summary table** at the end with columns: Signal | Direction | Source | Evidence | Confidence.

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
