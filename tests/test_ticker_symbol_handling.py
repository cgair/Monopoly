import unittest

import pytest

from cli.utils import normalize_ticker_symbol
from tradingagents.agents.utils.agent_utils import build_instrument_context


@pytest.mark.unit
class TickerSymbolHandlingTests(unittest.TestCase):
    def test_normalize_ticker_symbol_preserves_quote_suffix(self):
        self.assertEqual(normalize_ticker_symbol(" btc-usd "), "BTC-USD")

    def test_build_instrument_context_mentions_exact_symbol(self):
        context = build_instrument_context("BTC-USD")
        self.assertIn("BTC-USD", context)
        self.assertIn("quote suffix", context)
        self.assertIn("crypto asset", context)


if __name__ == "__main__":
    unittest.main()
