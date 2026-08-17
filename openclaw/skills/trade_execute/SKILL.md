# OpenClaw Skill: trade_execute

**Purpose**: Human-approved trade execution on Monopoly.

Final safety fence for the TradingAgents system: after `trade_analyze` produces a decision, OpenClaw collects explicit Discord user approval before executing. No defaults, no automatic approvals—LLM recommends, human decides.

## Workflow

```
OpenClaw receives                  Monopoly trade_analyze output
                    ↓                              
    Push decision card to Discord (direction, leverage, size, stops)
                    ↓
    User clicks ✅ Approve / ❌ Reject / ⏱ timeout
                    ↓
    Call trade_execute CLI with --approved / --rejected (or nothing)
                    ↓
    trade_execute validates & executes (or logs rejection)
```

## Integration Points

### 1. Receive analyze_json Output

```bash
# T4 produces:
python -m cli.main analyze_json > decision.json

# T5 consumes it:
python -m cli.main trade_execute --decision-file decision.json \
    --approved --approved-by alice@discord
```

### 2. OpenClaw Slash Command Handler

Define a `/trade_execute` command that:

```python
@bot.tree.command(name="trade_execute", description="Execute approved trade decision")
@app.command_checks.check_private()
async def execute_trade_decision(
    interaction: discord.Interaction,
    decision_json: str,  # Paste from trade_analyze output
):
    """
    1. Parse decision_json into a dict
    2. Extract: symbol, side, leverage, position_size_pct, entry, stop, take_profit
    3. Display decision card with ✅ Approve / ❌ Reject buttons
    4. On button click, call trade_execute with appropriate flags
    """
```

### 3. Decision Card Template

```
🔔 Trade Decision Approval Required

Symbol: BTC-USD
Side: LONG
Leverage: 2.0x
Risk Size: 1% of equity
Entry: $64,500 (market)
Stop Loss: $62,800
Take Profit: $68,000

Analyst Summary: [truncated thesis]

━━━━━━━━━━━━━━━━━━━━
[✅ Approve] [❌ Reject]

Approval timeout: 15 min (or env TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN)
```

### 4. Button Click Handler

**On ✅ Approve:**
```bash
python -m cli.main trade_execute \
    --decision-file ./decision.json \
    --approved \
    --approved-by alice@discord
```

**On ❌ Reject:**
```bash
python -m cli.main trade_execute \
    --decision-file ./decision.json \
    --rejected \
    --approved-by alice@discord
```

**On timeout (no action):**
- trade_execute not called
- Next `/trade_analyze` will emit a fresh decision (stale one auto-rejected if manually approved later)

## CLI Semantics

### Approval is Explicit & Mandatory

- `--approved` flag MUST be present (boolean, no value needed)
- `--approved-by <username>` MUST be present (identifies approver for audit)
- **No defaults**. Missing either flag → rejection, not execution.
- Rationale: LLM trades must never execute without human sign-off.

### Approval Metadata Written to Log

Every execution path (approve, reject, timeout) writes a `trade_skipped` or `position_opened` event with:
```json
{
  "type": "trade_skipped",
  "ts": "2026-07-14T12:15:30+00:00",
  "symbol": "BTC-USD",
  "reason": "human rejected",
  "approval_by": "alice@discord",
  "approval_at": "2026-07-14T12:15:30+00:00",
  "approval_decision": "rejected"
}
```

### Staleness Check (Auto-Reject Timeout)

- Default timeout: **15 minutes** (env `TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN`)
- Decisions older than timeout are auto-rejected even if manually approved later
- Reason logged: `"approval timeout / stale decision (age Xs > timeout 15min)"`

### Gate Re-Evaluation

Before execution, trade_execute re-runs the risk gate against current state:
- New positions opened since analyze → may hit `max_concurrent_positions` cap
- Daily drawdown threshold crossed → halt new entries for rest of day
- Cooldown active after recent stop-out → no new entries yet

If gate rejects: write `trade_skipped` with gate's reason, exit 1.

### No Mainnet Path

- Executor is forced to `dryrun` mode by default
- Env `TRADINGAGENTS_FUTURES_EXECUTOR_MODE` can override to `testnet` (already tested on testnet; mainnet never exists)
- Output JSON to stdout, logs to stderr

## JSON Output

On all paths, trade_execute outputs JSON to stdout:

### Success (Execution)
```json
{
  "status": "executed",
  "symbol": "BTC-USD",
  "side": "BUY",
  "quantity": 0.0059,
  "notional_usd": 379.35,
  "margin_required_usd": 189.68,
  "mode": "dryrun",
  "order_id": "dryrun-uuid-...",
  "avg_fill_price": 64500.0,
  "placed_at": "2026-07-14T12:15:30+00:00",
  "approved_by": "alice@discord",
  "timestamp": "2026-07-14T12:15:30+00:00"
}
```

### Rejection (Human)
```json
{
  "status": "rejected",
  "symbol": "BTC-USD",
  "reason": "human rejected",
  "approved_by": "alice@discord",
  "timestamp": "2026-07-14T12:15:30+00:00"
}
```

### Timeout / Stale
```json
{
  "status": "stale",
  "symbol": "BTC-USD",
  "reason": "approval timeout / stale decision (age 901s > timeout 15min)",
  "timestamp": "2026-07-14T12:15:30+00:00"
}
```

### Gate Re-Eval Rejection
```json
{
  "status": "gate_rejected",
  "symbol": "BTC-USD",
  "reason": "max_concurrent_positions already open (2 / 2)",
  "timestamp": "2026-07-14T12:15:30+00:00"
}
```

### Unapproved (Missing Flags)
```json
{
  "status": "unapproved",
  "symbol": "BTC-USD",
  "reason": "missing --approved flag",
  "timestamp": "2026-07-14T12:15:30+00:00"
}
```

## Exit Codes

| Exit | Meaning | Next Step |
|------|---------|-----------|
| 0 | Executed successfully | Monitor position_opened event in JSONL |
| 1 | Rejected / gate failed | Log rejection, wait for new signal |
| 2 | Error (malformed JSON, etc) | Debug input, retry |

## Installation (OpenClaw Side)

1. Copy `openclaw/skills/trade_execute/` to your OpenClaw skills directory.
2. Register the skill in your OpenClaw config to expose it to the slash-command router.
3. Ensure bot has permission to post/edit messages + add reactions in target channel.
4. Verify `TRADINGAGENTS_RISK_GATE_STATE_PATH` env var points to Monopoly's JSONL log (shared filesystem or NFS mount).

## Configuration

| Env Var | Default | Meaning |
|---------|---------|---------|
| `TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN` | 15 | Max age of decision before auto-reject |
| `TRADINGAGENTS_FUTURES_EXECUTOR_MODE` | dryrun | dryrun or testnet (never mainnet) |
| `TRADINGAGENTS_RISK_GATE_STATE_PATH` | ~/.tradingagents/risk_gate_state.jsonl | Shared JSONL log |

## Audit Trail

Every trade_execute invocation writes a `trade_skipped` (rejection) or `position_opened` (success) event with:
- Timestamp (ISO-8601 UTC)
- Symbol
- Approval metadata (who, when, yes/no)
- Reason (if rejected)

Use `/trade_review` to query recent approvals & rejections.

## Design Principles

1. **No LLM execution authority**: trade_execute enforces the separation—LLM only analyzes & recommends.
2. **Explicit approval semantics**: flags must be present; no inference or defaults.
3. **Staleness is a safety brake**: old decisions are auto-rejected, preventing "zombie" approvals.
4. **Gate re-evaluation closes the gap**: conditions may change between analyze and execute; gate catches it.
5. **Audit-first logging**: every decision point is logged for compliance & debugging.

## Troubleshooting

### Decision Rejected as Stale
- Increase `TRADINGAGENTS_FUTURES_APPROVAL_TIMEOUT_MIN` if 15 min is too short.
- Or re-run `/trade_analyze` for a fresh decision.

### Gate Rejects After Approval
- Check `/trade_review` for current positions, daily P&L, cooldown status.
- If positions hit, close one and retry.

### Missing --approved-by
- OpenClaw must pass the Discord username (or user ID) as this parameter.
- Used for audit logging.

### Execution Returns Exit 2 (Error)
- Check stdin/decision_file JSON structure.
- Ensure all required fields: side, leverage, position_size_pct, stop_loss.
- Review stderr for detailed error message.

## Notification Routing Convention

**Important**: When calling this skill from OpenClaw (Discord interaction), do **NOT** pass the `--notify` flag to the underlying CLI command.

**Reason** (see `docs/design/notification-push.md` §2): The system uses two notification modes:
1. **Scheduled (launchd) → static webhook push**: Launchd jobs invoke CLI with `--notify` to send alerts to Discord webhook.
2. **Interactive (Discord → OpenClaw) → session-local reply**: OpenClaw receives the JSON output and replies directly in the chat session — passing `--notify` would cause **double-push** (both webhook and session reply), confusing the user.

To prevent double-push, OpenClaw skills always call the CLI without `--notify`, letting the skill's own message handling deliver results into the chat.

