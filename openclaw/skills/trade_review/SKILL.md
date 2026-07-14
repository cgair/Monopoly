---
name: trade_review
description: Summarize recent trade decisions, open positions, and rejection statistics
version: 1.0
author: Claude Fable 5
---

# trade_review

## Overview

Reads trade memory logs and risk gate state files to produce a machine-readable JSON summary of:
- Recent trade decisions (past N hours)
- Current open positions and daily P&L
- Gate rejection reasons and frequency
- Last stop-loss close timestamp

Use this to monitor trading activity, diagnose gate rejections, and audit decision history across runs.

## Prerequisites

1. **Monopoly repository** cloned at `~/Monopoly`
2. **Virtual environment** activated
3. **Trade history files** must exist:
   - `~/.tradingagents/memory/memory.jsonl` (memory log from previous runs)
   - `~/.tradingagents/risk_gate_state.jsonl` (risk gate events)
4. **Permissions**: Read access to files above

## Usage

### Basic command (all symbols, past 24 hours)

```bash
cd ~/Monopoly
python -m cli.main trade_review
```

### Filter by symbol and time window

```bash
python -m cli.main trade_review --symbol BTCUSDT --hours 12
```

### View last week's decisions (168 hours)

```bash
python -m cli.main trade_review --hours 168
```

## Output Format

**JSON schema** (stdout):

```json
{
  "timestamp": "2026-07-14T12:00:00+00:00",
  "symbol": "BTCUSDT",
  "time_window_hours": 24,
  "recent_decisions_count": 3,
  "open_positions": 1,
  "daily_pnl_usd": 156.75,
  "last_stop_close_ts": "2026-07-14T08:30:00+00:00",
  "gate_rejections": [
    {
      "reason": "max_positions_reached",
      "count": 2,
      "first_occurrence_ts": "2026-07-14T09:00:00+00:00",
      "last_occurrence_ts": "2026-07-14T10:30:00+00:00"
    },
    {
      "reason": "daily_drawdown_halt",
      "count": 1,
      "first_occurrence_ts": "2026-07-14T11:00:00+00:00",
      "last_occurrence_ts": "2026-07-14T11:00:00+00:00"
    }
  ],
  "recent_gate_rejections": [
    {
      "type": "trade_skipped",
      "ts": "2026-07-14T09:00:00+00:00",
      "symbol": "BTCUSDT",
      "reason": "max_positions_reached"
    }
  ]
}
```

### Field meanings

| Field | Type | Meaning |
|-------|------|---------|
| `timestamp` | string | ISO-8601 UTC when review was generated |
| `symbol` | string \| null | Filter applied (null = all symbols) |
| `time_window_hours` | int | Lookback period used |
| `recent_decisions_count` | int | Number of decision events in window |
| `open_positions` | int | Positions currently open (not yet closed) |
| `daily_pnl_usd` | float | Realized P&L since 00:00 UTC today |
| `last_stop_close_ts` | string \| null | When the most recent stop-loss closed, or null |
| `gate_rejections` | array | Aggregated rejection reasons (all-time, within window) |
| `recent_gate_rejections` | array | Raw rejection events within time window |

## Integration with OpenClaw / Telegram

### 1. Copy the skill

```bash
cp -r ~/Monopoly/openclaw/skills/trade_review \
      ~/.local/share/OpenClaw/skills/
```

### 2. Configure OpenClaw

In `~/.config/OpenClaw/config.yaml`:

```yaml
skills:
  - name: trade_review
    enabled: true
    command: |
      cd ~/Monopoly && \
      python -m cli.main trade_review --symbol $SYMBOL --hours $HOURS
    parameters:
      - name: SYMBOL
        type: string
        default: null
        description: "Crypto symbol (e.g. BTCUSDT), or null for all"
      - name: HOURS
        type: integer
        default: 24
        description: "Lookback window in hours"
    output_format: json
```

### 3. Telegram usage

```
/trade_review
```

or with parameters:

```
/trade_review BTCUSDT 12
```

Expected response:
- **Success**: Summary card showing positions, recent decisions, rejection stats
- **Error**: JSON error object with reason

## Typical use cases

### Daily standup: "How did we do overnight?"

```bash
python -m cli.main trade_review --hours 8
```

Check:
- `daily_pnl_usd` — profit/loss since market open
- `open_positions` — any positions left overnight?
- `recent_decisions_count` — how active were we?

### Audit: "Why did the system reject so many trades?"

```bash
python -m cli.main trade_review --hours 168
# Look at gate_rejections[].count and .reason
```

Common reasons:
- `max_positions_reached` — hit concurrent position limit
- `daily_drawdown_halt` — exceeded daily loss threshold
- `leverage_over_cap` — PM tried to lever beyond 3x
- `risk_over_cap` — position size > 1% of equity
- `cooldown_active` — still cooling down from a stop-loss

### Monitor specific symbol

```bash
python -m cli.main trade_review --symbol ETHUSDT --hours 24
```

Check if ETHUSDT has open positions or recent rejections.

## Memory file schema

### `~/.tradingagents/memory/memory.jsonl`

Each line is a trade decision event:

```json
{
  "ts": "2026-07-14T10:00:00+00:00",
  "symbol": "BTCUSDT",
  "decision_type": "FuturesDecision",
  "side": "Long",
  "leverage": 2.0,
  "position_size_pct": 0.005,
  "stop_loss": 62800.0,
  "take_profit": 68000.0,
  "thesis": "Bullish breakout above resistance..."
}
```

### `~/.tradingagents/risk_gate_state.jsonl`

Each line is a gate event:

```json
{
  "type": "position_opened",
  "ts": "2026-07-14T10:00:00+00:00",
  "symbol": "BTCUSDT"
}
```

```json
{
  "type": "position_closed",
  "ts": "2026-07-14T10:30:00+00:00",
  "symbol": "BTCUSDT",
  "pnl_usd": 150.50,
  "outcome": "tp"
}
```

```json
{
  "type": "trade_skipped",
  "ts": "2026-07-14T10:45:00+00:00",
  "symbol": "BTCUSDT",
  "reason": "max_positions_reached"
}
```

## Troubleshooting

### "recent_decisions_count: 0"

Memory log doesn't exist or is empty. Check:
- Did Monopoly run successfully yet? (First run creates the log)
- Is the file at `~/.tradingagents/memory/memory.jsonl`?
- Do you have read permissions? (`ls -la ~/.tradingagents/memory/`)

### "daily_pnl_usd" looks wrong

P&L includes only positions closed **since 00:00 UTC today**. If your timezone is off:
- Check system time: `date -u` (should show UTC)
- Manually close the position to flush realized P&L

### Symbol filter returns 0 results

Check spelling (case-sensitive): use `BTCUSDT` not `btcusdt`.

## See Also

- `trade_analyze`: Run a fresh analysis and get a decision
- `trade_execute`: Push a decision through approval and onto Binance
- `~/Monopoly/tradingagents/futures/risk_state.py`: Event schema docs
- `~/Monopoly/docs/monopoly-spec.md` §3 Week 5: Data model
