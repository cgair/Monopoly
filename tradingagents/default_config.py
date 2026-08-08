import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    # Futures (crypto perp) — risk gate ceilings
    "TRADINGAGENTS_FUTURES_MAX_LEVERAGE":              "futures_max_leverage",
    "TRADINGAGENTS_FUTURES_PER_TRADE_RISK_PCT":        "futures_per_trade_risk_pct",
    "TRADINGAGENTS_FUTURES_DAILY_DRAWDOWN_HALT_PCT":   "futures_daily_drawdown_halt_pct",
    "TRADINGAGENTS_FUTURES_COOLDOWN_MINUTES":          "futures_cooldown_after_loss_minutes",
    "TRADINGAGENTS_FUTURES_MAX_CONCURRENT_POSITIONS":  "futures_max_concurrent_positions",
    "TRADINGAGENTS_FUTURES_STARTING_EQUITY_USD":       "futures_starting_equity_usd",
    "TRADINGAGENTS_FUTURES_MACRO_WARN_HOURS":          "futures_macro_warn_hours",
    "TRADINGAGENTS_FUTURES_MACRO_BLOCK_HOURS":         "futures_macro_block_hours",
    "TRADINGAGENTS_FUTURES_DANGLING_INTENT_MINUTES":   "futures_dangling_intent_minutes",
    "TRADINGAGENTS_FUTURES_EXECUTOR_MODE":             "futures_executor_mode",
}


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value."""
    if isinstance(reference, bool):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        config[key] = _coerce(raw, config.get(key))
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "quick_think_llm": "gpt-5.4-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "analyst_concurrency_limit": 1,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    # Monopoly fork: crypto perpetual futures data vendors
    "data_vendors": {
        "crypto_market_data": "binance",         # OHLCV / funding rate / open interest
        # binance first: free unauth long/short endpoints; coinglass remains the
        # only liquidations vendor (needs COINGLASS_API_KEY, wait-and-see — PLAN.md T9).
        # Fallback only fires on VendorRateLimitError, so with a Coinglass key
        # prefer it explicitly: tool_vendors={"get_long_short_ratio": "coinglass"}.
        "crypto_derivatives": "binance,coinglass",
        "crypto_news": "rss",                    # CoinDesk + CoinTelegraph RSS
        "crypto_social_reddit": "reddit",        # crypto subreddits
        "crypto_social_twitter": "twitter",      # placeholder (data source pending)
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # Alpha is measured against BTC (spec §2.1); override via
    # TRADINGAGENTS_BENCHMARK_TICKER for experiments (e.g. ETH-USD).
    "benchmark_ticker": "BTC-USD",
    # ----------------------------------------------------------------------
    # Futures (crypto perp) — risk gate ceilings (Monopoly fork)
    # ----------------------------------------------------------------------
    # All can be overridden via TRADINGAGENTS_FUTURES_* env vars; see the
    # _ENV_OVERRIDES table above.
    # Execution venue: "dryrun" (no orders; conclusions consumed manually)
    # or "testnet". The EXECUTOR_MODE env var still wins over this key —
    # see futures.executor.resolve_executor_mode.
    "futures_executor_mode": "dryrun",
    "futures_max_leverage": 3.0,
    "futures_per_trade_risk_pct": 0.01,           # 1% of equity per trade
    "futures_daily_drawdown_halt_pct": 0.03,      # -3% halts new entries until next UTC day
    "futures_cooldown_after_loss_minutes": 60,
    "futures_max_concurrent_positions": 2,        # matches the BTC + ETH scope
    "futures_starting_equity_usd": 1000.0,        # used when state doesn't carry live equity yet
    # Macro-event calendar (ForexFactory, free). Warn hours feed the PM
    # prompt advisory (L1); block hours arm a hard gate rejection (L3,
    # default off — enable only if testnet reviews show macro-window losses).
    "futures_macro_warn_hours": 12.0,
    "futures_macro_block_hours": 0.0,
    # An order_submitted with no result event after this many minutes is a
    # dangling intent (process died mid-execution) — the gate blocks new
    # entries until the position monitor reconciles it.
    "futures_dangling_intent_minutes": 5.0,
    # Optional override for the risk-gate state file (JSONL event log).
    # ``None`` → ~/.tradingagents/risk_gate_state.jsonl
    "futures_risk_state_path": os.getenv("TRADINGAGENTS_RISK_GATE_STATE_PATH"),
})
