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


# ---------------------------------------------------------------------------
# Chinese rendering. Cards are read on a phone by a Chinese-speaking
# operator; source strings (alerts.py messages, FuturesDecision literals)
# stay English because logs and tests grep them. Translation happens here,
# at the display edge, and unknown values fall back to the original text —
# an untranslated line beats a lost one.
# ---------------------------------------------------------------------------

_TIME_HORIZON_ZH = {
    "intraday": "日内",
    "1-3 days": "1-3 天",
    "1-2 weeks": "1-2 周",
    "2-4 weeks": "2-4 周",
    "1 month+": "1 个月以上",
}

_SIDE_ZH = {
    "Long": "做多",
    "Short": "做空",
    "Flat": "观望(不开仓)",
}

_MODE_ZH = {
    "dryrun": "dryrun — 人工下单",
    "testnet": "testnet — 测试网自动下单",
}

# Gate rejection reasons are stable strings (risk_gate.py REASON_* — log
# scrapers and tests grep them), so an exact-match table is safe here.
# Unknown reasons fall back to the original English text.
_GATE_REASON_ZH = {
    "side=Flat — no position requested": "观望决策——未请求任何仓位",
    "leverage missing on non-Flat decision": "非观望决策缺少杠杆",
    "position_size_pct missing on non-Flat decision": "非观望决策缺少仓位比例",
    "stop_loss missing on non-Flat decision": "非观望决策缺少止损",
    "leverage exceeds configured max_leverage": "杠杆超过配置上限",
    "leverage below 1x — not a valid futures position": "杠杆低于 1x——不是有效的合约仓位",
    "position_size_pct exceeds configured per_trade_risk_pct": "仓位比例超过单笔风险上限",
    "position_size_pct must be > 0": "仓位比例必须大于 0",
    "stop_loss must be a positive price": "止损必须为正数价格",
    "stop_loss is on the wrong side of entry": "止损在入场价的错误一侧",
    "no reference price to validate market-order stop side": "无参考价可校验市价单止损方向",
    "daily drawdown halt active until next UTC day": "日回撤停机生效中(至下一 UTC 日)",
    "cooldown window active after recent stop-out": "近期止损触发,冷却期生效中",
    "max_concurrent_positions already open": "并发持仓数已达上限",
    "high-impact macro event inside block window": "高影响宏观事件处于封锁窗口",
    "dangling order intent awaiting reconciliation": "存在悬置下单意图,等待对账",
}


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _breakdown_suffix(details: dict) -> str:
    breakdown = (details or {}).get("reason_breakdown") or {}
    if not breakdown:
        return ""
    items = ", ".join(f"{k}×{v}" for k, v in list(breakdown.items())[:3])
    return f"（{items}）"


_FINDING_ZH = {
    "gate_rejections": lambda d, m: "Risk Gate 拒绝次数超阈值" + _breakdown_suffix(d),
    "naked_position": lambda d, m: (
        f"检测到 {d.get('count', '?')} 起裸持仓事件——保护单缺失，需人工处理"
    ),
    "untracked_position": lambda d, m: (
        f"交易所侧发现 {d.get('count', '?')} 个未跟踪持仓——本地无记录，需人工处理"
    ),
    "pnl_backfill_failed": lambda d, m: (
        f"{d.get('count', '?')} 笔平仓的真实盈亏回填失败——回撤/冷却统计对其失明"
    ),
    "dangling_intents": lambda d, m: (
        f"{d.get('count', '?')} 个下单意图悬置待对账——Risk Gate 已拒绝一切新开仓"
    ),
    "malformed_lines": lambda d, m: (
        f"状态文件有 {d.get('count', '?')} 行损坏——重放可能缺事件，请检查文件"
    ),
    "executor_errors": lambda d, m: "Executor 错误次数超阈值——检查交易所连通性与下单参数",
    "consecutive_stops": lambda d, m: "连续止损次数达到阈值——复盘策略与当前行情的相性",
}


def render_finding_zh(
    finding_type: str | None, details: dict | None, message: str,
) -> str:
    """Chinese alert line for a finding; unknown types fall back to ``message``."""
    template = _FINDING_ZH.get(finding_type or "")
    if template is None:
        return message
    try:
        return template(details or {}, message)
    except Exception:
        return message


def format_decision_card(
    symbol: str,
    direction: str,
    leverage: float | None,
    position_size_pct: float | None,
    entry_price: float | str | None,
    stop_loss: float | None,
    take_profit: float | None,
    cycle: str,
    summary: str,
    risk_gate_status: str,
    risk_gate_reason: str | None,
    executor_mode: str,
    timestamp_utc: str,
    thesis: str | None = None,
) -> str:
    """Format a decision card as markdown for Discord.

    For Flat decisions (§4.1), position_size_pct can be None to omit
    position size. ``entry_price=None`` renders 市价 (market entry);
    ``stop_loss``/``take_profit``/``leverage`` of None render as —
    (absent is not the same as zero). ``thesis`` adds a 依据 section
    with the PM's investment thesis, truncated to keep the card inside
    one Discord message.
    """
    try:
        dt = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
        ts_display = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_display = timestamp_utc

    gate_emoji = "✅" if risk_gate_status == "pass" else "🛑"
    if risk_gate_status == "pass":
        gate_text = "通过"
    else:
        reason = risk_gate_reason or "unknown"
        gate_text = f"拒绝（{_GATE_REASON_ZH.get(reason, reason)}）"

    def fmt_price(p) -> str:
        if p is None:
            return "—"
        return f"{p:,.0f}" if isinstance(p, (int, float)) else str(p)

    entry_display = "市价" if entry_price in (None, "market") else fmt_price(entry_price)
    leverage_display = f"{leverage}x" if leverage is not None else "—"
    side_display = _SIDE_ZH.get(direction, direction)

    lines = [
        f"🎯 **{symbol} 决策** · {ts_display}",
    ]

    # For Flat decisions, omit position size. position_size_pct is the
    # decimal fraction from FuturesDecision (0.005 = 0.5% of equity) —
    # same *100 rendering as schemas.render_futures_decision.
    if position_size_pct is not None:
        lines.append(
            f"**方向**: {side_display} ｜ **杠杆**: {leverage_display} ｜ "
            f"**仓位**: {position_size_pct * 100:.2f}%"
        )
    else:
        lines.append(f"**方向**: {side_display} ｜ **杠杆**: {leverage_display}")

    lines.extend([
        f"**入场**: {entry_display} ｜ **止损**: {fmt_price(stop_loss)} ｜ **止盈**: {fmt_price(take_profit)}",
        f"**周期**: {_TIME_HORIZON_ZH.get(cycle, cycle)}",
        f"**摘要**: {_truncate(summary, 400)}",
    ])

    if thesis:
        lines.append(f"**依据**: {_truncate(thesis, 700)}")

    lines.extend([
        f"**Risk Gate**: {gate_emoji} {gate_text}",
        f"**执行**: {_MODE_ZH.get(executor_mode, executor_mode)}",
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


# Multi-chunk sends prepend a "(i/n)\n" pagination marker after splitting,
# so raw chunks must leave room for it — Discord rejects content over
# 2000 chars with a 400, and a full-size chunk plus marker goes over.
_MARKER_RESERVE = 12  # "(999/999)\n" and change


def _split_chunks(content: str, chunk_size: int = 2000) -> list[str]:
    """Split content into chunks that still fit after the pagination marker."""
    if len(content) <= chunk_size:
        return [content]

    effective = max(chunk_size - _MARKER_RESERVE, 1)
    chunks = []
    while content:
        chunks.append(content[:effective])
        content = content[effective:]

    return chunks


# Cap on honouring a 429's server-supplied retry_after (seconds).
_MAX_RETRY_AFTER_S = 30.0


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
                # Server-supplied value: a webhook under a long global limit
                # can say thousands of seconds, and honouring it would hang
                # the scheduled run (launchd sees neither output nor exit).
                # Better to fail visibly after capped waits than block.
                if retry_after > _MAX_RETRY_AFTER_S:
                    logger.warning(f"Chunk {chunk_num}/{total_chunks} retry_after {retry_after}s exceeds cap; using {_MAX_RETRY_AFTER_S}s")
                    retry_after = _MAX_RETRY_AFTER_S
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
