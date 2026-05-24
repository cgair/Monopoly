"""Config isolation: get/set must not leak nested-dict references."""

import copy
import unittest

import pytest

import tradingagents.default_config as default_config
from tradingagents.dataflows.config import get_config, set_config


@pytest.mark.unit
class DataflowsConfigIsolationTests(unittest.TestCase):
    def setUp(self):
        set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    def test_get_config_returns_deep_copy(self):
        cfg = get_config()
        cfg["data_vendors"]["crypto_market_data"] = "ccxt"
        cfg["tool_vendors"]["get_ohlcv"] = "ccxt"

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["crypto_market_data"], "binance")
        self.assertNotIn("get_ohlcv", fresh["tool_vendors"])

    def test_set_config_does_not_alias_caller_nested_dicts(self):
        custom = copy.deepcopy(default_config.DEFAULT_CONFIG)
        custom["data_vendors"]["crypto_market_data"] = "ccxt"
        custom["tool_vendors"]["get_ohlcv"] = "ccxt"

        set_config(custom)

        custom["data_vendors"]["crypto_market_data"] = "binance"
        custom["tool_vendors"]["get_ohlcv"] = "binance"

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["crypto_market_data"], "ccxt")
        self.assertEqual(fresh["tool_vendors"]["get_ohlcv"], "ccxt")

    def test_partial_nested_update_preserves_existing_defaults(self):
        set_config(
            {
                "data_vendors": {
                    "crypto_market_data": "ccxt",
                }
            }
        )

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["crypto_market_data"], "ccxt")
        self.assertEqual(fresh["data_vendors"]["crypto_derivatives"], "coinglass")
        self.assertEqual(fresh["data_vendors"]["crypto_news"], "rss")
        self.assertEqual(fresh["data_vendors"]["crypto_social_reddit"], "reddit")

    def test_nested_dict_updates_merge_one_level_deep(self):
        set_config({"tool_vendors": {"get_ohlcv": "ccxt"}})
        set_config({"tool_vendors": {"get_news": "newsapi"}})

        fresh = get_config()
        self.assertEqual(fresh["tool_vendors"]["get_ohlcv"], "ccxt")
        self.assertEqual(fresh["tool_vendors"]["get_news"], "newsapi")
