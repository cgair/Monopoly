---
name: trade_analyze
description: Run comprehensive crypto futures trading analysis and get machine-readable JSON decision
version: 1.0
author: Claude Fable 5
---

# trade_analyze

## Overview

Executes a complete technical and sentiment analysis on a crypto futures symbol, producing a structured JSON decision with trading direction, leverage, position sizing, and stop-loss/take-profit levels.

**Key features:**
- Non-interactive, deterministic analysis pipeline
- Outputs pure JSON (no rich formatting) for programmatic consumption
- Execution mode: **dryrun only** (no real trades or testnet orders)
- Logs to stderr; JSON to stdout (machine-readable separation)

## Prerequisites

1. **Monopoly repository** cloned at `~/Monopoly`
2. **Virtual environment** activated: `. ~/.venv/bin/activate` (or your env path)
3. **Environment variables** set:
   - `TRADINGAGENTS_LLM_PROVIDER`: "google" (default) or "anthropic" or "openai"
   - `TRADINGAGENTS_GOOGLE_API_KEY`: Your Gemini API key (if using Google)
   - Other provider keys as needed
4. **Symbol resolution**: Use crypto format e.g. `BTC-USD` or `BTCUSDT`

## Usage

### Basic command

```bash
cd ~/Monopoly
python -m cli.main analyze_json
```

### With symbol override (via environment variable)

```bash
TRADINGAGENTS_JSON_TICKER="ETH-USD" python -m cli.main analyze_json
```

### With checkpoint resume

```bash
python -m cli.main analyze_json --checkpoint
```

## Output Format

**JSON schema** (stdout):

```json
{
  "timestamp": "2026-07-14T12:00:00+00:00",
  "status": "success",
  "error": null,
  "analysis": {
    "ticker": "BTC-USD",
    "asset_type": "crypto",
    "analysis_date": "2026-07-14",
    "selected_analysts": ["market"]
  },
  "decision": {
    "type": "FuturesDecision",
    "side": "Long",
    "leverage": 2.0,
    "position_size_pct": 0.005,
    "entry_price": 64500.0,
    "stop_loss": 62800.0,
    "take_profit": 68000.0,
    "executive_summary": "Market shows bullish technical setup...",
    "investment_thesis": "Detailed reasoning from analysts...",
    "time_horizon": "2-5 days"
  },
  "risk_state": {
    "open_positions": 0,
    "daily_realised_pnl_usd": 0.0
  },
  "reports": {
    "market_report": "...",
    "investment_plan": "...",
    "final_trade_decision": "..."
  }
}
```

### Decision fields explained

| Field | Type | Meaning |
|-------|------|---------|
| `side` | Enum | "Long", "Short", or "Flat" (no trade) |
| `leverage` | float | Position multiplier (1.0 = 1x, max 3.0). Omit if Flat. |
| `position_size_pct` | float | Fraction of equity at risk (decimal; 0.01 = 1%, max 0.01). Omit if Flat. |
| `entry_price` | float | Optional: Limit order price. Omit for market order. |
| `stop_loss` | float | Stop price. Required if not Flat. |
| `take_profit` | float | Optional: Profit target. |
| `executive_summary` | string | 2–4 sentences: entry strategy, key risk levels, time horizon. |
| `investment_thesis` | string | Detailed reasoning from analysts' debate (multi-paragraph). |
| `time_horizon` | string | Optional: e.g., "2-5 days", "intraday", "1 week". |

## Integration with OpenClaw / Discord

### 1. Copy the skill to OpenClaw

Assuming OpenClaw is installed at `~/.local/share/OpenClaw/skills/`:

```bash
cp -r ~/Monopoly/openclaw/skills/trade_analyze \
      ~/.local/share/OpenClaw/skills/
```

### 2. Configure OpenClaw bot (Discord)

In your OpenClaw configuration (typically `~/.config/OpenClaw/config.yaml`):

```yaml
skills:
  - name: trade_analyze
    enabled: true
    command: |
      cd ~/Monopoly && \
      python -m cli.main analyze_json 2>/tmp/trade_analyze.log
    output_format: json
```

### 3. Discord usage

Send a message to your OpenClaw bot (via Discord):

```
/trade_analyze
```

or

```
@trade_analyze BTC-USD
```

Expected response:
- **Success**: A formatted card with decision summary (direction, leverage, size, SL/TP, thesis)
- **Error**: Parseable error JSON with reason

## Troubleshooting

### "No structured FuturesDecision in state"

The LLM didn't produce a properly structured decision. Check:
- Is the LLM API key valid? (`echo $TRADINGAGENTS_GOOGLE_API_KEY`)
- Is the network proxy configured if needed? (`TRADINGAGENTS_HTTP_PROXY`, `TRADINGAGENTS_HTTPS_PROXY`)
- Try again (transient LLM failures are normal)

### "max_debate_rounds=1 too shallow"

If analysts seem rushed or incoherent, try increasing rounds via env var:

```bash
TRADINGAGENTS_MAX_DEBATE_ROUNDS=2 python -m cli.main analyze_json
```

### Logs buried in output

Check stderr (logs are redirected there):

```bash
python -m cli.main analyze_json 2>errors.log 1>decision.json
```

## Safety & Guardrails

- **Dryrun mode is hardcoded** — no real orders or testnet trades via this command
- **LLM output is analyzed, not executed** — an external system (e.g., OpenClaw's `trade_execute` skill) handles order placement with human approval
- **Risk gate is bypassed in JSON mode** (analysis only) — gate validation happens in the `trade_execute` step

## Notification Routing Convention

**Important**: When calling this skill from OpenClaw (Discord interaction), do **NOT** pass the `--notify` flag to the underlying CLI command.

**Reason** (see `docs/design/notification-push.md` §2): The system uses two notification modes:
1. **Scheduled (launchd) → static webhook push**: Launchd jobs invoke CLI with `--notify` to send alerts to Discord webhook.
2. **Interactive (Discord → OpenClaw) → session-local reply**: OpenClaw receives the JSON output and replies directly in the chat session — passing `--notify` would cause **double-push** (both webhook and session reply), confusing the user.

To prevent double-push, OpenClaw skills always call the CLI without `--notify`, letting the skill's own message handling deliver results into the chat.


## See Also

- `trade_review`: Read historical decisions and position summaries
- `trade_execute`: Take a JSON decision through the risk gate and onto Binance (with human approval)
- `~/Monopoly/docs/monopoly-spec.md` §3 Week 5: OpenClaw integration architecture
