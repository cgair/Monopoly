import unittest

from cli.models import AnalystType
from cli.utils import ANALYST_ORDER, validate_crypto_ticker
from tradingagents.graph.propagation import Propagator


class CryptoOnlyModeTests(unittest.TestCase):
    def test_accepts_crypto_pair_symbols(self):
        self.assertTrue(validate_crypto_ticker("BTC-USD"))
        self.assertTrue(validate_crypto_ticker("eth-usd"))
        self.assertTrue(validate_crypto_ticker("BTCUSDT"))

    def test_rejects_non_crypto_symbols(self):
        self.assertFalse(validate_crypto_ticker("AAPL"))
        self.assertFalse(validate_crypto_ticker("SPY"))
        self.assertFalse(validate_crypto_ticker("0700.HK"))

    def test_analyst_order_has_no_fundamentals(self):
        analyst_values = [value for _, value in ANALYST_ORDER]
        self.assertEqual(
            analyst_values,
            [AnalystType.MARKET, AnalystType.SOCIAL, AnalystType.NEWS],
        )

    def test_propagator_initial_state_is_crypto_native(self):
        state = Propagator().create_initial_state("BTC-USD", "2026-04-18")

        self.assertEqual(state["company_of_interest"], "BTC-USD")
        self.assertNotIn("asset_type", state)
        self.assertNotIn("fundamentals_report", state)


if __name__ == "__main__":
    unittest.main()
