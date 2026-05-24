"""Symbol normalisation between TradingAgents (`BASE-USD`) and Binance (`BASEUSDT`)."""

import pytest

from tradingagents.agents.utils.symbol_utils import to_base_symbol, to_binance_symbol


@pytest.mark.unit
class TestToBinanceSymbol:
    def test_dash_usd_form(self):
        assert to_binance_symbol("BTC-USD") == "BTCUSDT"
        assert to_binance_symbol("ETH-USD") == "ETHUSDT"
        assert to_binance_symbol("SOL-USD") == "SOLUSDT"

    def test_case_insensitive(self):
        assert to_binance_symbol("btc-usd") == "BTCUSDT"
        assert to_binance_symbol("  eth-usd  ") == "ETHUSDT"

    def test_already_binance_format_passes_through(self):
        assert to_binance_symbol("BTCUSDT") == "BTCUSDT"
        assert to_binance_symbol("ethusdt") == "ETHUSDT"

    def test_unknown_format_raises(self):
        for bad in ("", "AAPL", "BTC-USDC", "USDT", "BTC", "FOOBAR"):
            with pytest.raises(ValueError):
                to_binance_symbol(bad)

    def test_empty_base_raises(self):
        with pytest.raises(ValueError):
            to_binance_symbol("-USD")


@pytest.mark.unit
class TestToBaseSymbol:
    def test_strips_quote(self):
        assert to_base_symbol("BTC-USD") == "BTC"
        assert to_base_symbol("ETH-USD") == "ETH"

    def test_from_binance_format(self):
        assert to_base_symbol("BTCUSDT") == "BTC"
        assert to_base_symbol("ethusdt") == "ETH"
