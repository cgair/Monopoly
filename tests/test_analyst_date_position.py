"""The current-date hint must lead each analyst's system prompt.

With the date sentence at the tail of a long system prompt (tool menu,
indicator catalogue, workflow), models anchor on their training cutoff
when reasoning about "current" market context and news recency. Mirrors
upstream TradingAgents fix 2b2d685: lead every analyst prompt with the
date instead.

Each test invokes the real analyst node with a fake LLM that captures
the rendered prompt, then asserts the date sentence appears before the
analyst brief ("You are a crypto ... analyst").
"""

import unittest
from unittest import mock

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts import sentiment_analyst as sentiment_module
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst

TRADE_DATE = "2026-08-09"
DATE_SENTENCE = f"the current date is {TRADE_DATE}"

STATE = {
    "trade_date": TRADE_DATE,
    "company_of_interest": "BTC-USD",
    "messages": [],
}


class _CaptureLLM:
    """Fake LLM that records the rendered prompt and returns a plain reply."""

    def __init__(self):
        self.system_prompt = None

    def _capture(self, prompt_value):
        self.system_prompt = prompt_value.to_messages()[0].content
        return AIMessage(content="ok")

    def bind_tools(self, tools):
        return RunnableLambda(self._capture)

    # Sentiment analyst pipes the LLM directly (no bind_tools), so the
    # fake must also be runnable via langchain's coercion of callables.
    def __call__(self, prompt_value):
        return self._capture(prompt_value)


def _assert_date_leads(test, system_prompt, brief_marker):
    test.assertIn(DATE_SENTENCE, system_prompt)
    test.assertIn(brief_marker, system_prompt)
    test.assertLess(
        system_prompt.index(DATE_SENTENCE),
        system_prompt.index(brief_marker),
        "date sentence must come before the analyst brief, not trail it",
    )


class AnalystDatePositionTests(unittest.TestCase):
    def test_market_analyst_leads_with_date(self):
        llm = _CaptureLLM()
        create_market_analyst(llm)(STATE)
        _assert_date_leads(
            self, llm.system_prompt, "You are a crypto perpetual-futures market analyst"
        )

    def test_news_analyst_leads_with_date(self):
        llm = _CaptureLLM()
        create_news_analyst(llm)(STATE)
        _assert_date_leads(self, llm.system_prompt, "You are a crypto news analyst")

    def test_sentiment_analyst_leads_with_date(self):
        llm = _CaptureLLM()
        with (
            mock.patch.object(
                sentiment_module, "crypto_get_news", lambda *a, **kw: "<no news>"
            ),
            mock.patch.object(
                sentiment_module, "get_tweets", lambda *a, **kw: "<no tweets>"
            ),
            mock.patch.object(
                sentiment_module, "get_reddit", lambda *a, **kw: "<no posts>"
            ),
        ):
            sentiment_module.create_sentiment_analyst(RunnableLambda(llm))(STATE)
        _assert_date_leads(self, llm.system_prompt, "You are a crypto sentiment analyst")


if __name__ == "__main__":
    unittest.main()
