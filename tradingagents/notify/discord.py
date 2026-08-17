"""Discord webhook sender for notifications."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class AlertLevel(Enum):
    """Alert severity level."""
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


def _get_dedup_dir() -> Path:
    """Get the deduplication directory path."""
    home = Path.home()
    return home / ".tradingagents"


def _demask_url(url: str | None) -> str:
    """Return demasked URL (show last 4 chars only) for logging."""
    if not url:
        return "(unconfigured)"
    if len(url) <= 4:
        return url
    return "..." + url[-4:]


def should_send_alert(
    dedup_key: str,
    *,
    dedup_file: Path | str | None = None,
    ttl_hours: int = 6,
) -> bool:
    """Check if an alert with the given key should be sent.

    Returns True if:
    - TTL is 0 (disabled)
    - Key not in dedup file (first time)
    - TTL has expired since last send

    Returns False if within TTL and not expired.

    Fail-open on any file read error (returns True, i.e., send the alert).
    """
    if ttl_hours == 0:
        return True

    if dedup_file is None:
        dedup_file = _get_dedup_dir() / "notify_dedup.json"
    else:
        dedup_file = Path(dedup_file)

    try:
        if not dedup_file.exists():
            return True

        with dedup_file.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if dedup_key not in data:
            return True

        last_sent_iso = data[dedup_key]
        last_sent = datetime.fromisoformat(last_sent_iso)
        now = datetime.now(timezone.utc)
        elapsed = now - last_sent

        if elapsed > timedelta(hours=ttl_hours):
            return True

        return False

    except Exception as e:
        logger.warning(f"Dedup check failed (fail-open): {e}")
        return True


def record_alert_sent(
    dedup_key: str,
    *,
    dedup_file: Path | str | None = None,
) -> None:
    """Record that an alert was sent."""
    if dedup_file is None:
        dedup_file = _get_dedup_dir() / "notify_dedup.json"
    else:
        dedup_file = Path(dedup_file)

    try:
        dedup_file.parent.mkdir(parents=True, exist_ok=True)

        data = {}
        if dedup_file.exists():
            try:
                with dedup_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}

        data[dedup_key] = datetime.now(timezone.utc).isoformat()

        with dedup_file.open("w", encoding="utf-8") as f:
            json.dump(data, f)

    except Exception as e:
        logger.warning(f"Failed to record dedup: {e}")


def format_decision_card(
    symbol: str,
    direction: str,
    leverage: float,
    position_size_pct: float | None,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    cycle: str,
    summary: str,
    risk_gate_status: str,
    risk_gate_reason: str | None,
    executor_mode: str,
    timestamp_utc: str,
) -> str:
    """Format a decision card as markdown for Discord.
    
    For Flat decisions (§4.1), position_size_pct can be None to omit position size.
    """
    try:
        dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = timestamp_utc

    gate_emoji = "✅" if risk_gate_status == "pass" else "🛑"
    gate_text = "通过" if risk_gate_status == "pass" else f"拒绝（{risk_gate_reason or 'unknown'}）"

    def fmt_price(p: float) -> str:
        return f"{p:,.0f}" if isinstance(p, (int, float)) else str(p)

    lines = [
        f"🎯 **{symbol} 决策** · {ts_display}",
    ]

    # For Flat decisions, omit position size
    if position_size_pct is not None:
        lines.append(f"**方向**: {direction} ｜ **杠杆**: {leverage}x ｜ **仓位**: {position_size_pct}%")
    else:
        lines.append(f"**方向**: {direction} ｜ **杠杆**: {leverage}x")

    lines.extend([
        f"**入场**: {fmt_price(entry_price)} ｜ **止损**: {fmt_price(stop_loss)} ｜ **止盈**: {fmt_price(take_profit)}",
        f"**周期**: {cycle}",
        f"**摘要**: {summary}",
        f"**Risk Gate**: {gate_emoji} {gate_text}",
        f"**执行**: {executor_mode}",
    ])

    return "\n".join(lines)


def format_alert_card(
    level: AlertLevel,
    window_hours: int,
    findings: list[str],
    timestamp_utc: str,
) -> str:
    """Format an alert card as markdown for Discord."""
    try:
        dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = timestamp_utc

    emoji_map = {
        AlertLevel.OK: "✅",
        AlertLevel.WARN: "⚠️",
        AlertLevel.CRITICAL: "🛑",
    }
    emoji = emoji_map.get(level, "❓")

    level_name = level.value.upper()

    header = f"{emoji} **{level_name}** · 扫描窗口 {window_hours}h · 截至 {ts_display}"

    findings_text = "\n".join(f"- {finding}" for finding in findings)

    return f"{header}\n{findings_text}"


def format_action_card(
    actions: list[str],
    timestamp_utc: str,
) -> str:
    """Format a position monitor action card as markdown for Discord."""
    try:
        dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = timestamp_utc

    header = f"🛑 **持仓监控动作** · {ts_display}"
    actions_text = "\n".join(f"- {action}" for action in actions)

    return f"{header}\n{actions_text}"


def send_discord(content: str, *, webhook_url: str | None = None) -> bool:
    """Send markdown content to Discord webhook.

    Args:
        content: Markdown text to send
        webhook_url: Discord webhook URL (if None, checks env var)

    Returns:
        True if all chunks sent successfully, False if any failed or unconfigured.
    """
    if webhook_url is None:
        webhook_url = os.environ.get("TRADINGAGENTS_DISCORD_WEBHOOK_URL")

    if not webhook_url:
        logger.warning("Discord webhook URL not configured (TRADINGAGENTS_DISCORD_WEBHOOK_URL); skipping notification")
        return False

    try:
        if not webhook_url.startswith("https://discord.com/api/webhooks/"):
            logger.error(f"Invalid Discord webhook URL format: {_demask_url(webhook_url)}")
            return False

        chunks = _split_chunks(content)

        logger.debug(f"Sending {len(chunks)} chunk(s) to Discord webhook {_demask_url(webhook_url)}")

        all_ok = True
        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                chunk_with_marker = f"({i+1}/{len(chunks)})\n{chunk}"
            else:
                chunk_with_marker = chunk

            success = _send_chunk(
                chunk_with_marker,
                webhook_url=webhook_url,
                chunk_num=i + 1,
                total_chunks=len(chunks),
            )

            if not success:
                all_ok = False

            if i < len(chunks) - 1:
                time.sleep(1)

        if all_ok:
            logger.info(f"Successfully sent {len(chunks)} chunk(s) to Discord")
        else:
            logger.warning(f"At least one chunk failed; check logs above")

        return all_ok

    except Exception as e:
        logger.error(f"Unexpected error in send_discord: {e}", exc_info=True)
        return False


def _split_chunks(content: str, chunk_size: int = 2000) -> list[str]:
    """Split content into chunks of at most chunk_size characters."""
    if len(content) <= chunk_size:
        return [content]

    chunks = []
    while content:
        chunks.append(content[:chunk_size])
        content = content[chunk_size:]

    return chunks


def _send_chunk(
    content: str,
    *,
    webhook_url: str,
    chunk_num: int,
    total_chunks: int,
    max_retries: int = 3,
) -> bool:
    """Send a single chunk to Discord webhook with retries."""
    payload = {"content": content}

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=10)

            if response.status_code == 204:
                logger.debug(f"Chunk {chunk_num}/{total_chunks} sent successfully (attempt {attempt + 1})")
                return True

            if response.status_code == 429:
                retry_after = _parse_retry_after(response)
                logger.warning(f"Chunk {chunk_num}/{total_chunks} rate limited; retry after {retry_after}s (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Chunk {chunk_num}/{total_chunks} failed after {max_retries} retries due to rate limit")
                    return False

            if 500 <= response.status_code < 600:
                wait_time = 2 ** attempt
                logger.warning(f"Chunk {chunk_num}/{total_chunks} got {response.status_code}; backoff {wait_time}s (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Chunk {chunk_num}/{total_chunks} failed after {max_retries} retries due to server error")
                    return False

            if 400 <= response.status_code < 500:
                logger.error(f"Chunk {chunk_num}/{total_chunks} got {response.status_code}: {response.text[:100]}")
                return False

            logger.error(f"Chunk {chunk_num}/{total_chunks} got unexpected status {response.status_code}")
            return False

        except requests.RequestException as e:
            wait_time = 2 ** attempt
            logger.warning(f"Chunk {chunk_num}/{total_chunks} network error: {e}; backoff {wait_time}s (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries:
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"Chunk {chunk_num}/{total_chunks} failed after {max_retries} retries due to network error")
                return False

        except Exception as e:
            logger.error(f"Chunk {chunk_num}/{total_chunks} unexpected error: {e}")
            return False

    return False


def _parse_retry_after(response: requests.Response) -> float:
    """Parse Retry-After from response header or JSON body."""
    if "Retry-After" in response.headers:
        try:
            return float(response.headers["Retry-After"])
        except (ValueError, TypeError):
            pass

    try:
        data = response.json()
        if "retry_after" in data:
            return float(data["retry_after"])
    except Exception:
        pass

    return 1.0
