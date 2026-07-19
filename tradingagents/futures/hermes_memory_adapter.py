"""Export Monopoly memory logs and risk events to Hermes skill memory format.

This module converts Monopoly's trading memory log (markdown) and risk gate
events (JSONL) into a Hermes-compatible markdown format for User Modeling.
The output helps Hermes learn user's manual override patterns (e.g., which
signals the user tends to reject) for better feedback on future analyses.

Hermes Memory Format Reference:
- MEMORY.md: Agent's learnings about user environment, typically 2,200 chars max
- USER.md: User profile (name, preferences), typically 1,375 chars max
Both are injected into Hermes' system prompt as frozen snapshots.

Output Format (human-readable markdown):
A single markdown file with sections for:
1. User's override patterns (trade_skipped events with reason)
2. Decision summary (recent analyses from memory log)
3. Gate rejection patterns (frequency analysis)

Each entry includes a frontmatter tag for idempotency (by intent_id/ts).

See also:
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md
- https://www.glukhov.org/ai-systems/hermes/authoring-hermes-skill/
"""

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _parse_memory_log(memory_log_path: Path | str) -> list[dict]:
    """Parse Monopoly memory log in markdown format.

    Format (from TradingMemoryLog):
        [YYYY-MM-DD | TICKER | RATING | pending/outcome]
        DECISION:
        <decision text>
        REFLECTION:
        <reflection text>
        <!-- ENTRY_END -->

    Returns list of dicts with keys: date, ticker, rating, decision, reflection.
    """
    p = Path(memory_log_path)
    if not p.exists():
        return []

    text = p.read_text(encoding="utf-8")
    entries = []

    # Split by separator
    blocks = text.split("\n\n<!-- ENTRY_END -->\n\n")
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.splitlines()
        if not lines:
            continue

        # Parse tag line: [YYYY-MM-DD | TICKER | RATING | ...]
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            continue

        try:
            fields = [f.strip() for f in tag_line[1:-1].split("|")]
            if len(fields) < 3:
                continue

            entry = {
                "date": fields[0],
                "ticker": fields[1],
                "rating": fields[2],
            }

            # Extract DECISION and REFLECTION from body
            body = "\n".join(lines[1:]).strip()
            decision_match = re.search(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", body, re.DOTALL)
            reflection_match = re.search(r"REFLECTION:\n(.*?)$", body, re.DOTALL)

            entry["decision"] = decision_match.group(1).strip() if decision_match else ""
            entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""

            entries.append(entry)
        except (ValueError, IndexError):
            continue

    return entries


def _load_risk_events(risk_state_path: Path | str) -> list[dict]:
    """Load risk gate state JSONL file.

    Returns list of dicts with keys: type, ts, symbol, reason (for trade_skipped), etc.
    """
    p = Path(risk_state_path)
    if not p.exists():
        return []

    events = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except (IOError, OSError):
        pass

    return events


def export_hermes_memory(
    memory_log_path: Optional[Path | str] = None,
    risk_state_path: Optional[Path | str] = None,
    output_dir: Optional[Path | str] = None,
    hours_lookback: int = 168,  # 7 days
) -> str:
    """Export Monopoly memory and risk state to Hermes-compatible markdown.

    This function is idempotent: repeated calls with the same input produce
    the same output (no duplicate entries). Deduplication is based on
    (intent_id, timestamp) pairs for events and date/ticker for memory entries.

    Args:
        memory_log_path: Path to Monopoly's trading_memory.md.
                        Defaults to ~/.tradingagents/memory/trading_memory.md
        risk_state_path: Path to risk_gate_state.jsonl.
                        Defaults to ~/.tradingagents/risk_gate_state.jsonl
        output_dir: Directory to write output files.
                   Defaults to ~/.tradingagents/memory/
        hours_lookback: Only include events from last N hours (default 7 days).

    Returns:
        Path to the generated memory file as a string.

    Raises:
        ValueError: If output_dir cannot be created or written to.
    """
    # Set defaults
    home = Path.home()
    if memory_log_path is None:
        memory_log_path = home / ".tradingagents" / "memory" / "trading_memory.md"
    else:
        memory_log_path = Path(memory_log_path)

    if risk_state_path is None:
        risk_state_path = home / ".tradingagents" / "risk_gate_state.jsonl"
    else:
        risk_state_path = Path(risk_state_path)

    if output_dir is None:
        output_dir = home / ".tradingagents" / "memory"
    else:
        output_dir = Path(output_dir)

    # Create output directory
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except (IOError, OSError) as e:
        raise ValueError(f"Cannot create output directory {output_dir}: {e}")

    # Load data
    memory_entries = _parse_memory_log(memory_log_path)
    risk_events = _load_risk_events(risk_state_path)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_lookback)

    # Extract human override patterns (trade_skipped with reason)
    overrides = []
    seen_overrides = set()  # (symbol, reason, ts) for deduplication

    for event in risk_events:
        if event.get("type") != "trade_skipped":
            continue

        try:
            event_ts_str = event.get("ts", "")
            event_ts = datetime.fromisoformat(event_ts_str.replace("Z", "+00:00"))

            if event_ts < cutoff:
                continue

            symbol = event.get("symbol", "unknown")
            reason = event.get("reason", "unknown")
            intent_id = event.get("intent_id", "")

            # Deduplication key
            dedup_key = (symbol, reason, event_ts_str, intent_id)
            if dedup_key in seen_overrides:
                continue
            seen_overrides.add(dedup_key)

            overrides.append({
                "ts": event_ts,
                "ts_str": event_ts_str,
                "symbol": symbol,
                "reason": reason,
                "intent_id": intent_id,
            })
        except (ValueError, KeyError):
            continue

    # Sort overrides by timestamp (most recent first)
    overrides.sort(key=lambda x: x["ts"], reverse=True)

    # Extract recent decisions
    recent_decisions = []
    seen_decisions = set()  # (date, ticker) for deduplication

    for entry in memory_entries:
        try:
            # Try to parse date to filter by lookback
            entry_date_str = entry.get("date", "")
            entry_date = datetime.fromisoformat(entry_date_str + "T00:00:00+00:00")

            if entry_date < cutoff:
                continue

            ticker = entry.get("ticker", "unknown")
            dedup_key = (entry_date_str, ticker)

            if dedup_key in seen_decisions:
                continue
            seen_decisions.add(dedup_key)

            recent_decisions.append(entry)
        except (ValueError, KeyError):
            continue

    # Sort decisions by date (most recent first)
    recent_decisions.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Build markdown output
    lines = []
    lines.append("# Trading Patterns & User Overrides")
    lines.append("")
    lines.append(f"**Generated**: {now.isoformat()}")
    lines.append(f"**Lookback**: Last {hours_lookback} hours ({hours_lookback / 24:.0f} days)")
    lines.append("")

    # Section 1: Human Override Patterns
    lines.append("## User Override Patterns")
    lines.append("")
    if overrides:
        lines.append("### Rejected Signals (trade_skipped events)")
        lines.append("")
        for ov in overrides:
            lines.append(f"- **{ov['symbol']}** | {ov['ts_str']}")
            lines.append(f"  - Reason: {ov['reason']}")
            if ov['intent_id']:
                lines.append(f"  - Intent ID: `{ov['intent_id']}`")
            lines.append("")
    else:
        lines.append("*No signal rejections in this period.*")
        lines.append("")

    # Section 2: Recent Decisions Summary
    lines.append("## Recent Decision Summary")
    lines.append("")
    if recent_decisions:
        for entry in recent_decisions[:10]:  # Show top 10 recent
            date = entry.get("date", "unknown")
            ticker = entry.get("ticker", "unknown")
            rating = entry.get("rating", "unknown")
            decision = entry.get("decision", "")

            lines.append(f"### [{date}] {ticker} → {rating}")
            lines.append("")
            if decision:
                # Truncate long decisions for readability
                short_decision = decision[:200] if len(decision) > 200 else decision
                suffix = "..." if len(decision) > 200 else ""
                lines.append(f"**Decision**: {short_decision}{suffix}")
                lines.append("")
    else:
        lines.append("*No decisions in this period.*")
        lines.append("")

    # Section 3: Gate Rejection Statistics
    lines.append("## Gate Rejection Patterns (All Time)")
    lines.append("")
    rejection_stats = defaultdict(lambda: {"count": 0, "first_ts": None, "last_ts": None})
    for event in risk_events:
        if event.get("type") == "trade_skipped":
            reason = event.get("reason", "unknown")
            ts_str = event.get("ts", "")
            rejection_stats[reason]["count"] += 1
            if rejection_stats[reason]["first_ts"] is None:
                rejection_stats[reason]["first_ts"] = ts_str
            rejection_stats[reason]["last_ts"] = ts_str

    if rejection_stats:
        for reason, stats in sorted(rejection_stats.items(), key=lambda x: x[1]["count"], reverse=True):
            lines.append(f"- **{reason}**: {stats['count']} times")
            lines.append(f"  - First: {stats['first_ts']}")
            lines.append(f"  - Last: {stats['last_ts']}")
            lines.append("")
    else:
        lines.append("*No gate rejections recorded.*")
        lines.append("")

    # Write to file
    output_file = output_dir / "hermes_memory.md"
    output_text = "\n".join(lines)

    try:
        output_file.write_text(output_text, encoding="utf-8")
    except (IOError, OSError) as e:
        raise ValueError(f"Cannot write to {output_file}: {e}")

    return str(output_file)


def main():
    """CLI entry point for hermes_memory_adapter."""
    parser = argparse.ArgumentParser(
        description="Export Monopoly trading memory to Hermes skill format"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for hermes_memory.md (default: ~/.tradingagents/memory/)",
    )
    parser.add_argument(
        "--memory-log-path",
        type=Path,
        default=None,
        help="Path to Monopoly memory log (default: ~/.tradingagents/memory/trading_memory.md)",
    )
    parser.add_argument(
        "--risk-state-path",
        type=Path,
        default=None,
        help="Path to risk_gate_state.jsonl (default: ~/.tradingagents/risk_gate_state.jsonl)",
    )
    parser.add_argument(
        "--hours-lookback",
        type=int,
        default=168,
        help="Include events from last N hours (default: 168 = 7 days)",
    )

    args = parser.parse_args()

    try:
        output_file = export_hermes_memory(
            memory_log_path=args.memory_log_path,
            risk_state_path=args.risk_state_path,
            output_dir=args.output_dir,
            hours_lookback=args.hours_lookback,
        )
        print(f"✓ Exported to: {output_file}")
    except ValueError as e:
        print(f"✗ Error: {e}", flush=True)
        exit(1)


if __name__ == "__main__":
    main()
