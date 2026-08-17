# 推送层设计：Discord 通知（定时触发 + OpenClaw 交互双模式）

> 状态：设计文档，未实现。作为 Week 5 OpenClaw 阶段的实现输入。
> 参考项目：[ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（下称 DSA），2026-08-09 调研。

## 1. 背景与目标

Monopoly 部署到 Mac mini 后有两种交互方式：

1. **定时任务触发**（launchd）：定期跑分析/告警/持仓监控，把结论**推送**到 Discord。
2. **手动交互触发**：用户在 Discord 里跟 OpenClaw 对话，OpenClaw 调 CLI 后**在会话里直接回复**。

DSA 是一个 LLM 股票分析项目，其通知层做了 14 个渠道、路由策略、降噪、Web 设置页等完整产品化。对 Monopoly（单人、单渠道、本机部署）来说大部分是过度设计，但有几处工程决策直接可用。本文记录取舍与设计。

### 借鉴清单（来自 DSA）

| # | 借鉴点 | DSA 出处 |
|---|--------|----------|
| 1 | 双触发模式路由：定时触发走静态推送渠道；交互触发只回来源会话、跳过静态推送（防双推） | `docs/notifications.md` 通知路由策略 |
| 2 | Discord webhook 优先于 Bot API（配置简单、权限低） | `src/notification_sender/discord_sender.py` |
| 3 | 2000 字符分片 + 分页标记 + 片间 sleep；失败片不阻断后续片 | 同上 |
| 4 | 429 按响应 `retry_after` 重试、5xx 指数退避、最多 3 次 | 同上 |
| 5 | 推送失败隔离（fail-open）：只记日志，绝不中断主流程 | `docs/notifications.md` 聚合报告失败隔离 |
| 6 | emoji 决策仪表盘式 markdown 消息格式 | README 推送效果 |
| 7 | 告警去重 TTL（防止同一异常反复轰炸） | `NOTIFICATION_DEDUP_TTL_SECONDS` |
| 8 | `--check-notify` 只读配置诊断（不发送、不写入，退出码 0/1） | `main.py --check-notify` |
| 9 | 报告格式 fixture 测试防回归 | `tests/fixtures/notification_reports/*.md` |

### 不借鉴清单（写明理由，防止未来重推）

- **多渠道抽象（14 个 sender + 渠道枚举 + 路由配置）**：只用 Discord 一个渠道，抽象层是纯成本。未来真要加渠道（如邮件兜底）再提。
- **Web 设置页 / 一键测试面板**：单人部署，`.env` + `--check-notify` 足够。
- **markdown 转图片分享模板**：无分享需求。
- **交易日历判断（非交易日跳过）**：crypto 24/7，无此概念。
- **进程内降噪 dict**：DSA 是常驻进程，Monopoly 是 launchd 每次拉起新进程——进程内状态无效，需要状态文件（见 §5.3）。
- **每日摘要 / 静默时段 / 最低严重级别过滤**：验证期事件量小，先不做；若实际被告警轰炸，由复盘证据触发再加（符合花钱/堆功能原则）。

## 2. 路由规则（核心设计）

```
定时触发（launchd → CLI --notify）
    └─→ 结论格式化 → Discord webhook 推送

交互触发（Discord → OpenClaw → CLI，不带 --notify）
    └─→ JSON stdout → OpenClaw 格式化后在会话里回复
        （不碰 webhook，避免同一结论推两遍）
```

实现约定：

- CLI 相关命令（`analyze_json` / `trade_review`）加 `--notify` 标志，**默认关闭**。
- launchd 调用的脚本显式传 `--notify`。
- OpenClaw skills（`openclaw/skills/trade_*/SKILL.md`）调 CLI 时**不传** `--notify`——skill 文档中明确写出这一约定及理由。
- `alerts.py` / `position_monitor` 作为独立监控入口（只被 launchd 调用），同样用 `--notify` 控制，默认关闭以保持现有行为和测试不变。

选择"默认关闭、显式开启"而不是环境变量开关的原因：调用方（launchd plist vs OpenClaw skill）是路由的唯一决定者，用命令行标志让路由决策在调用点可见、可审计；环境变量会让 dryrun/回放等场景意外带上推送副作用（参照 6067bc0 修过的 EXECUTOR_MODE 环境变量越权教训）。

## 3. 推送触发点（定时模式，共 3 类）

| 触发点 | 入口 | 条件 | 消息 |
|--------|------|------|------|
| 分析决策完成 | `analyze_json --notify` | 每次跑完 | 决策卡（§4.1） |
| 告警 | `python -m tradingagents.futures.alerts --notify` | `level ∈ {warn, critical}`；ok 不推 | 告警卡（§4.2） |
| 持仓监控动作 | `python -m tradingagents.futures.position_monitor --notify` | 有实际动作（平仓、发现 naked/untracked 持仓）；无事不推 | 动作卡（§4.3） |

## 4. 消息格式（markdown 模板草案）

Discord webhook 原生渲染 markdown。级别 emoji 沿用 `alerts.py:24` TODO 里已定的映射：
`{"ok": ✅, "warn": ⚠️, "critical": 🛑}`。

### 4.1 决策卡

数据源：`FuturesDecision`（`tradingagents/agents/schemas.py:214`，经 `cli/json_output.py` `serialize_decision()`）+ Risk Gate 结果。

```
🎯 **BTC-USD 决策** · 2026-08-09 18:00 UTC
**方向**: Long ｜ **杠杆**: 2.0x ｜ **仓位**: 0.5%
**入场**: 61,500 ｜ **止损**: 60,200 ｜ **止盈**: 64,000
**周期**: swing_days
**摘要**: <executive_summary 原文>
**Risk Gate**: ✅ 通过 ／ 🛑 拒绝（<拒绝原因>）
**执行**: dryrun / testnet / 已提交订单 <id>
```

Flat 决策简化为一行方向 + 摘要，省略仓位字段。

### 4.2 告警卡

数据源：`AlertReport.to_dict()`（`tradingagents/futures/alerts.py`）。

```
⚠️ **WARN** · 扫描窗口 24h · 截至 2026-08-09 18:00 UTC
- Gate rejections exceed threshold: 5 in window (threshold 3)
- Naked position detected: BTCUSDT since ...
```

### 4.3 动作卡

数据源：`position_monitor` 的 JSON stdout（mode、positions_checked、positions_closed 等）。

```
🛑 **持仓监控动作** · 2026-08-09 18:00 UTC
- 平仓 BTCUSDT：交易所持仓与本地状态不一致（naked）
- 检查 3 个持仓，动作 1 个
```

### 通用规则

- 长内容（如 executive_summary 超长）交给分片逻辑，不在模板层截断——DSA 的教训是截断后"只收到前半段报告"比分片更糟。
- 每条消息头部带 UTC 时间戳，与 `risk_gate_state.jsonl` 事件时间对齐，方便回查审计日志。

## 5. 模块设计

### 5.1 sender：`tradingagents/notify/discord.py`（新建，约 150 行）

仅 webhook，不做 Bot API（交互归 OpenClaw）。对外一个函数：

```python
def send_discord(content: str, *, webhook_url: str | None = None) -> bool:
    """推送 markdown 到 Discord webhook。fail-open：任何失败只记日志返回 False。"""
```

内部行为（照搬 DSA `discord_sender.py` 的实测参数）：

- 按 2000 字符上限分片；多片时加 `(1/3)` 分页标记，片间 sleep 1s；失败片记日志、继续发后续片。
- 每片最多重试 3 次：429 按响应体 `retry_after` 或 `Retry-After` header 等待；5xx 指数退避（2^attempt 秒）；4xx（非 429）不重试直接失败。
- 网络异常同样指数退避重试。
- webhook_url 未配置 → 记 warning、返回 False，不抛异常。

### 5.2 配置

走 `tradingagents/default_config.py` 现有 `_ENV_OVERRIDES` 模式：

```python
"TRADINGAGENTS_DISCORD_WEBHOOK_URL": "discord_webhook_url",   # 默认 None
```

webhook URL 属于 capability URL（拿到即可发消息），按 secret 对待：只进 `.env`，不进仓库；日志与 `--check-notify` 输出脱敏（只显示末 4 位）。

### 5.3 告警去重（跨进程）

launchd 每次拉起新进程，DSA 的进程内 dict 方案无效。用状态文件：

- 路径 `~/.tradingagents/notify_dedup.json`：`{dedup_key: last_sent_ts}`。
- dedup_key = 告警 finding 的稳定标识（如 `naked_position:BTCUSDT`），**不含时间戳**，避免每次扫描生成新 key 击穿去重。
- TTL 默认 6h（同一告警 6h 内只推一次），`TRADINGAGENTS_NOTIFY_DEDUP_TTL_HOURS` 可调，0 关闭。
- 只对告警卡去重；决策卡、动作卡每次都推（本来就是低频、每条都该看）。
- 读写失败 fail-open：照常推送并记日志（宁可重复推，不可漏推 critical）。

### 5.4 `--check-notify` 诊断

挂在 `cli/main.py` 下的只读命令：检查 `discord_webhook_url` 是否配置、URL 形态是否合法（`https://discord.com/api/webhooks/` 前缀）、dedup 状态文件是否可写。不发送任何消息。退出码 0（可用）/ 1（配置缺失或非法），供部署脚本做前置检查。

### 5.5 失败隔离（不变量）

**推送层任何失败不得改变主流程的行为、输出和退出码。** 具体地：

- `analyze_json --notify` 推送失败 → JSON stdout 照常、退出码不变。
- `alerts.py --notify` 推送失败 → 退出码仍由 `AlertReport.exit_code()`（0/1/2）决定，launchd 升级链路不受影响。
- sender 内部捕获一切异常，只透出布尔返回值 + 日志。

这是推送层唯一的硬约束，测试必须覆盖（§6）。

## 6. 测试策略

- **sender 单测**（mock `requests.post`）：分片边界（1999/2000/2001 字符）、多片分页标记、429 带 `retry_after` 重试、5xx 退避、3 次耗尽后返回 False、webhook 未配置返回 False、片失败不阻断后续片。
- **fail-open 断言**：`requests.post` 抛任意异常时 `send_discord` 不向上抛；`alerts.py --notify` 在 sender 恒失败时退出码仍等于 `exit_code()`。
- **去重测试**：TTL 内同 key 不重发、过期重发、状态文件损坏时 fail-open 照常发送。
- **格式 fixture**（借鉴 DSA `tests/fixtures/notification_reports/`）：三类卡片各存一份固定输入 → 期望 markdown 的样例，格式化函数输出与 fixture 全文比对，防止改模板时无感知回归。

## 7. 实现排期与依赖

归入 Week 5 OpenClaw 阶段，建议顺序：

1. **sender + 配置 + `--check-notify`**（独立，可先行，不依赖 OpenClaw）。
2. **三个触发点接 `--notify`**（依赖 1；`alerts.py` 的 TODO 注释 `alerts.py:20-27` 即在此步兑现并删除）。
3. **OpenClaw skills 补路由约定**（依赖 T5 trade_review skill 落地；在 SKILL.md 写明"不传 `--notify`"及防双推理由）。
4. **launchd plist 样例**（walkthrough 已标 TODO 的部分）：定时调用三个入口并带 `--notify`。

步骤 1-2 与 OpenClaw 无耦合，若 Week 5 里 OpenClaw 链路受阻，可先独立交付定时推送模式。
