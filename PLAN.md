# Monopoly 开发计划(PLAN.md)

- **更新**: 2026-07-19(**T0–T7 全部完成**并合并回 `dev`,512 passed;剩:用户在 Mac mini 配 OpenClaw+Discord、T3b Reddit OAuth(可选)、T8 On-Chain(等数据源决策))
- **来源**: `~/Desktop/AI/become rich/docs/monopoly-spec.md` §3 / §5 / §8 + 工作区未提交改动盘点
- **用法**: 每个任务设计为可由一个独立 Claude Code 实例(worktree 或新会话)冷启动执行。
  开工前先读任务卡里的「上下文锚点」,完成后勾选并在 spec §8 追加恢复点。

## 当前基线

- Week 1–4 已完成:数据层替换、3 个 Analyst 改造、futures 层(schemas → Trader/PM → risk_gate → executor → graph wiring)。
- 2026-06-02 testnet 实弹验证 + 完整 graph 闭环已跑通。
- 分支 `dev`,本地领先 `origin/dev`;工作区有 ~460 行**未提交**的加固改动(见 T0)。
- 测试基线:296 passing(排除 socksio 相关 LLM-client 测试;`.venv/bin/pip install 'httpx[socks]'` 可转绿)。

## 任务依赖图

```
T0 ✅ ── T1 ✅ ── T2 ✅ ── T3 ✅ ── T4 ✅ ── T5 ✅ ── T6 ✅ ── T7 ✅
剩余:[用户] Mac mini OpenClaw + Discord bot 配置(验证清单 4)
      T3b Reddit OAuth(可选,数据缺口恢复)
      T8 On-Chain analyst(阻塞:数据源选型)
搁置:Hermes 侧集成(代码已在库,用户决定现有工作流够用,暂不配置)
```

**T0 必须最先在主仓库完成**(worktree 从已提交的 commit 分出,未提交改动不会带过去)。
之后 T1+T2、T3、T4、T6、T7 可各开一个 worktree 并行。T5 依赖 T1/T2/T4 合并。

---

## T0 — 提交未提交的加固改动 ✅ 完成(2026-07-14,`dbf15e3`…`a3c5b9b`)

**优先级**: P0(阻塞所有并行任务)
**触碰文件**: 工作区现有 9 个 modified 文件 + `docs/walkthrough/`

工作区有 2026-06 下旬做的一批加固,尚未提交:

- `futures/executor.py`:naked-position 逃生处理(开仓成功但 stop 挂失败 → 自动 unwind,再失败则记 `position_naked` 事件)、市价单 stop 方向校验、qty 按 step size 向下取整后重算 margin、验证阶段禁用 LIMIT 入场。
- `futures/risk_gate.py`:下界检查(leverage < 1x / size ≤ 0 / stop ≤ 0 直接拒),新增 3 个 REASON 常量。
- `futures/risk_state.py`:append 原子性注释修正。
- `agents/managers/portfolio_manager.py`:`structured_futures is None`(provider 不支持结构化输出)时直走 free-text,不打误导性 warning。
- `agents/utils/rating.py`:解析 crypto 的 `**Side**: Long/Short/Flat` → Buy/Sell/Hold 映射。
- `default_config.py`:`-USD` 后缀 → BTC-USD benchmark 映射。
- 对应测试更新(3 个测试文件,+218 行)。

**步骤**:
1. `pytest` 全量跑,确认基线绿(预期 ≥296 passing)。
2. 按逻辑拆成 2–4 个 commit(executor 加固 / gate 下界 / PM+rating / config)。
3. `docs/walkthrough/` 决定去留:是项目文档就 `git add`,是一次性产物就进 `.gitignore`。
4. `.DS_Store` 加进 `.gitignore`。
5. push 到 `origin/dev`。
6. 更新 spec §8 恢复点(当前 §8 停在 6-02,落后实际进度)。

**验收**: `git status` 干净,`origin/dev` 与本地同步,测试全绿。

---

## T1 — Position Monitor / 持仓对账 ✅ 完成(2026-07-14,`943558a` 合并)

> **✅ 2026-07-18 testnet 实弹验证通过**:开仓(带 stop/TP)→ reduceOnly 平仓模拟交易所侧关闭 →
> `reconcile_positions()` 正确写入 `position_closed`、清理孤儿单、账户状态干净。
> **遗留(验证阶段限制)**:
> ① 对账写的 `position_closed` 事件 `pnl_usd=0.0` — 日回撤熔断看不到这些亏损;outcome 默认
> 推断为 `"stop"` 会触发 cooldown,方向上过度保守而非漏防,可接受但要知情。
> ② stop vs TP 的精确区分需查订单历史,当前是启发式。

**优先级**: P1
**新建文件**: `tradingagents/futures/position_monitor.py` + `tests/test_futures_position_monitor.py`
**只读依赖**: `futures/risk_state.py`(事件 schema)、`futures/executor.py`(TestnetExecutor 的 client 用法)、`futures/market_data.py`
**并行安全**: 全新文件,与 T3/T4/T6/T7 无冲突

**问题**:stop 或 TP 在 Binance 触发后,持仓在交易所侧已关闭,但本地
`~/.tradingagents/risk_gate_state.jsonl` 不会自动写 `position_closed` 事件。后果:
1. gate 下次运行仍认为持仓存在 → `max_concurrent_positions` 误拒;
2. 孤儿算法单(另一个未触发的 stop 或 TP)累积 → 下次下单 `-4130` 错误回归。

**设计要点**(spec §8 已定方向):
- 独立于 LangGraph graph,做成可被 launchd 周期调用的脚本 / 也可作为每次 run 前的 pre-step 调用。
- 轮询 Binance testnet 持仓状态(`futures_position_information`),与 JSONL 里 open 状态的持仓 diff。
- 对已关闭的持仓:写 `position_closed` 事件(带平仓来源推断:stop / tp / manual / unknown)。
- 撤掉该 symbol 的孤儿保护单(Basic 单用 `futures_cancel_all_open_orders`;algo 单依赖 T2,T2 未完成前先在日志里 fail-loud 提示手工撤)。
- dryrun 模式下也要能跑(读 JSONL、mock exchange 状态),保证单测不联网。

**验收**:
- 单测覆盖:开仓→交易所侧关闭→monitor 跑一遍→JSONL 出现 `position_closed`、gate 重新放行。
- testnet 手工验证一轮:真实 stop 触发后 monitor 能对账。

---

## T2 — Algo 单程序化撤销 ✅ 完成(2026-07-14,与 T1 同 commit 合并)

> **✅ 2026-07-18 testnet 实弹验证通过(修正了两处 agent 调研错误)**:
> ① 列表端点是 `GET /fapi/v1/openAlgoOrders`(不是 `algoOpenOrders`,后者仅作 DELETE 全撤);
> ② 当前安装的 python-binance 已**原生暴露** `futures_get_open_algo_orders` /
> `futures_cancel_algo_order`(需 symbol+algoId)/ `futures_cancel_all_algo_open_orders`,
> 无需手拼 `_request_futures_api` 路径(手拼还会双重前缀 `/fapi/v1//fapi/v1/...`)。
> spec §8 里"1.0.36 没有此 API"的信息已过时。实测一发清掉 4 张 algo 单。

**优先级**: P1(与 T1 同一 worktree 做,顺序:先 T1 后 T2)
**触碰文件**: `tradingagents/futures/executor.py`(或新建 `futures/binance_algo.py`)+ 测试
**背景**: python-binance 1.0.36 不暴露 conditional/algo 单(STOP_MARKET / TAKE_PROFIT_MARKET 返回
`algoId` 而非 `orderId`)的 cancel API;`futures_cancel_all_open_orders` 只撤 Basic 单。
Binance UI 的 "Cancel All" 能撤,说明有对应 fapi endpoint。

**步骤**:
1. 调研 Binance fapi 算法订单 endpoint(查官方 fapi 文档 algo order 部分;必要时抓 UI 请求路径)。
2. 用 python-binance 的底层签名请求方法(`client._request_futures_api` 一类)直接调该 endpoint,封装成
   `cancel_algo_order(symbol, algo_id)` + `cancel_all_algo_orders(symbol)`。
3. 接进 T1 的孤儿单清理路径,以及 executor 的 naked-position unwind 路径。
4. testnet 验证:挂一对 stop/TP → 程序化撤掉 → UI 确认消失。

**验收**: testnet 上能程序化撤销 algo 单;T1 的对账流程不再需要手工 UI 撤单。

---

## T3 — Sentiment Analyst Reddit 403 排查 ✅ 完成(2026-07-14,`a9c54b9` 合并)

> 实现:合规 UA(`REDDIT_USERNAME` env 可覆盖)+ 403/429 指数退避 + `old.reddit.com`
> fallback + 全失败时 `data_gap` 优雅降级。
> **2026-07-18 真实代理实测**:降级路径工作正常(`ok=False` + `data_gap`,无裸异常),但
> www 和 old 两端点均 403(合规 UA 也一样)→ **代理出口 IP 被 Reddit 封禁,代码层无解**。
> 数据恢复的两条路:换代理出口节点重测,或注册 Reddit script app 走 OAuth API(免费 tier,
> oauth.reddit.com 对 DC IP 通常放行)——后者可开小任务 T3b。

**优先级**: P2(独立,随时可并行)
**触碰文件**: `tradingagents/dataflows/crypto_reddit.py` + 对应测试
**并行安全**: 与其他任务零交集

**问题**: 完整 graph 跑时 4 个 crypto subreddit 全部 HTTP 403。疑似 User-Agent 缺失/太泛,或代理出口 IP 被 Reddit 拉黑。

**步骤**:
1. 复现:直连 vs 走代理分别请求,定位是 UA 还是 IP 问题。
2. 修复优先级:合规 UA(Reddit 要求 `platform:app-id:version (by /u/username)` 格式)→ 退避重试 →
   如仍 403,评估切换到 old.reddit JSON / RSS 端点或官方 OAuth API(免费 tier)。
3. 失败时优雅降级:返回带 `data_gap` 标注的空结果,别让 analyst 拿到裸异常。

**验收**: 4 个 subreddit 拉取成功(或有明确降级路径),graph 跑 sentiment 阶段无 403。

---

## T4 — OpenClaw skill: trade_analyze + trade_review ✅ 完成(2026-07-14,`b5c6049` 合并)

> 实现:`python -m cli.main analyze_json`(强制 dryrun,JSON 到 stdout / 日志到 stderr)、
> `python -m cli.main trade_review --symbol --hours`(纯读 memory log + risk JSONL,无 LLM 调用)、
> `cli/json_output.py` 序列化、`futures/trade_review.py` 汇总、`openclaw/skills/*/SKILL.md` 两份。
> **OpenClaw 侧安装 + Discord bot 配置是用户手工步骤**(见 SKILL.md 内说明)。

**优先级**: P2
**产出位置**: OpenClaw 的 skill 目录(在 OpenClaw 侧新建,Monopoly 仓库内可能需要补一个稳定的 CLI 入口/JSON 输出)
**Monopoly 侧触碰文件**: `cli/`(如需加 `--json` 输出模式)——与 T1/T2 无冲突

**内容**(spec §3 Week 5,方案 A):
- `trade_analyze`:调 Monopoly CLI 跑完整分析,拿回 JSON 决策(不执行)。需要确认 CLI 有机器可读输出;没有就先加。
- `trade_review`:读 `~/.tradingagents/memory/` 的 memory log + `risk_gate_state.jsonl`,汇总近期决策/持仓/gate 拒绝原因。
- Discord bot 配置(OpenClaw 侧)。

**验收**: 手机 Discord 发消息 → OpenClaw 触发 `trade_analyze` → 返回决策摘要;`trade_review` 能报当前持仓与最近一次 run 的结果。

---

## T5 — OpenClaw skill: trade_execute + 人工审批闭环 ✅ 完成(2026-07-19,`cf8e515` 合并)

> 实现:`futures/approval.py`(staleness / 审批元数据 / gate 复评)+ `cli/main.py trade_execute`
> + `openclaw/skills/trade_execute/SKILL.md`。审批语义:`--approved --approved-by` 缺一不可、
> 决策超 15 分钟即过期(env 可配)、**缺时间戳 fail-closed**、执行前用当前 JSONL 重跑 gate、
> 拒绝/过期/未批准全部写 `trade_skipped` 审计事件。dryrun 端到端四路径已实测。
> **实弹修复 5 处 agent 遗留 bug**(`cf8e515`,函数级测试全绿但 CLI 本体全断的教训):
> 命令注册在 `app()` 之后(顺带修好 T4 的 analyze_json/trade_review 同款问题)、typer 连字符
> 化与文档不符、`datetime` 模块名遮蔽导致所有路径首行崩溃、强制 dryrun 使审批形同虚设、
> 硬编码 60000 假价格 → 改用 `fetch_mark_price()` 且无价格时拒绝下单。
> **待用户**:OpenClaw/Discord 侧按 SKILL.md 配置后,跑一次真实审批链路。
**背景**: 人工审批当初有意跳过 Monopoly graph 内的 Discord 节点,决定落在 OpenClaw 层。这是核心约束
「最终下单前必须人工确认」目前唯一缺失的防线,**T5 完成前不得切 mainnet**。

**内容**:
- `trade_execute`:接收 `trade_analyze` 的 JSON 决策 → Discord 推送决策卡片(方向/杠杆/size/SL/TP/gate 快照)→ 用户 Yes/No → Yes 才调 executor。
- 审批超时策略:超时自动取消(spec §5.2 开放问题,默认建议 15 分钟超时取消,做成可配)。
- 拒绝/超时都要写 `trade_skipped` 事件进 JSONL(带 reason)。

**验收**: 手机端完成一次「分析 → 推送 → 确认 → testnet 下单」全链路;拒绝和超时路径各验一次。

---

## T6 — L4 监控告警 ✅ 完成(2026-07-19,`01aa45e` 合并)

> 实现:`futures/alerts.py`,`python -m tradingagents.futures.alerts` 入口。规则:24h 窗口内
> gate 拒绝 ≥3 → warn;任何 `position_naked` → critical;executor error ≥5 → warn;连续
> 止损 ≥3 → warn。退出码 0/1/2,阈值 `TRADINGAGENTS_FUTURES_ALERT_*` env 可配。
> 已用真实 JSONL 实测(空 → ok/0;3 条拒绝 → warn/1)。launchd plist 示例在 agent 报告中,
> Discord 推送留 TODO(复用 T5 的 bot)。

**优先级**: P3
**新建文件**: `tradingagents/futures/alerts.py`(或并入 position_monitor)+ 测试
**背景**: Defense layering 设计(spec §8,已锁定):L1 prompt 教学 / L2 schema description / L3 risk gate /
**L4 monitor** — gate 正常应几乎不触发,触发频率超阈值说明上游(prompt/schema/LLM)坏了。

**内容**:
- 读 `risk_gate_state.jsonl`,统计窗口期内 gate 拒绝次数/原因分布、`position_naked` 事件、executor error。
- 超阈值(如 24h 内拒绝 ≥3 次,或出现任何 position_naked)→ 告警。通道:先落地本地日志 + 退出码(launchd 可挂),Discord 推送等 T5 的 bot 通了以后复用。
- 顺带覆盖 spec §5.4:Mac mini 离线、API 连续报错、连续亏损告警,能做多少做多少,做不完的列 TODO 注释。

**验收**: 单测构造异常 JSONL → 告警触发;正常 JSONL → 静默。

---

## T7 — Hermes memory adapter ✅ 完成(2026-07-19,`d242d0e` 合并)

> 实现:`futures/hermes_memory_adapter.py`,`python -m tradingagents.futures.hermes_memory_adapter
> --output-dir <dir>` 汇出 `hermes_memory.md`(决策摘要 + 人工否决记录 + gate 拒绝模式),
> 按 intent_id 去重幂等,默认 7 天窗口。Hermes 记忆体系为 markdown-based(MEMORY.md/USER.md/
> skill 文件,来源:NousResearch/hermes-agent)。
> **Hermes 侧集成已搁置(2026-07-19 用户决定:现有工作流已够用)**——adapter 代码保留在库中,
> 将来要启用时只需配置 Hermes 挂载 + launchd 周期任务,无需再开发。

**优先级**: P4(低,不阻塞任何任务)
**新建文件**: `tradingagents/futures/hermes_memory_adapter.py` + 测试

**内容**(spec §3 Week 6):
- memory log → Hermes skill 格式转换器。
- `TRADINGAGENTS_MEMORY_LOG_PATH` 指到 Hermes 可读目录。
- 配置 Hermes User Modeling 学习人工 override 模式(用户否决信号后,下次给出反馈)。

**验收**: 否决一次信号后,Hermes 能在下次分析时引用该次否决。

---

## T8 — On-Chain Analyst 节点(被阻塞)

**优先级**: 阻塞中 — **先要人工决策数据源**(spec §5.1):Glassnode(付费)/ CryptoQuant(付费)/ 链上 RPC(免费但开发量大)。
**决策后的工作**:
- 新建 `dataflows/crypto_onchain.py`(交易所净流入、巨鲸地址)。
- 新建 `agents/analysts/onchain_analyst.py`,注册 `AnalystType.ONCHAIN`(`cli/utils.py::filter_analysts_for_asset_type`)。
- DAG Analyst Team 阶段追加并行节点(`graph/setup.py`)。

**注意**: 不要改造 `fundamentals_analyst.py` — fork 已在 crypto 模式下过滤掉它,On-Chain 是全新节点(spec §2.3 修正)。

---

## 手工验证清单(2026-07-18 更新:1–3 已由 Claude 实弹完成,剩 4–5 需用户)

1. ~~Position monitor testnet 实测~~ ✅ 2026-07-18:开仓→平仓→对账→`position_closed` 写入、孤儿单清零、账户干净。
2. ~~Algo 撤单 endpoint 实测~~ ✅ 2026-07-18:修正 endpoint 后一发清掉 4 张 algo 单(详见 T2 任务卡注记)。
3. ~~Reddit 真实代理实测~~ ✅ 2026-07-18:降级路径正常;**代理出口 IP 被 Reddit 封禁**,数据恢复见 T3 注记
   (换出口节点 / T3b OAuth)。
4. **OpenClaw 侧安装**(T4,需用户在 Mac mini 上操作):拷 `openclaw/skills/trade_analyze`、`trade_review`
   到 OpenClaw skill 目录,配置 **Discord bot**(交互渠道已定为 Discord,非 Telegram),
   手机 Discord 跑 `/trade_analyze` 和 `/trade_review`。
5. `git push origin dev` 备份(本地已领先若干 commit)。

## 其他挂起项(不单开任务)

- **Twitter 数据源**:已主动推迟(option f:先用 News + Reddit),不阻塞。
- **Coinglass 计费 tier**:双标的 5min 轮询是否够用,待验证(多标的化之前不急)。
- **Week 3 验证项**「跑出 BTC-USD 完整 Analyst Team 报告」:6-02 全链路验证实际已覆盖,T0 更新 spec 时顺手勾掉。

---

## Worktree 操作指引

```bash
cd "/Users/chenge/Desktop/AI/become rich/Monopoly"

# T0 完成并 push 后,再开 worktree:
git worktree add ../mono-t1-monitor  -b feat/position-monitor dev   # T1+T2
git worktree add ../mono-t3-reddit   -b fix/reddit-403       dev   # T3
git worktree add ../mono-t4-openclaw -b feat/openclaw-skills dev   # T4
git worktree add ../mono-t6-alerts   -b feat/l4-alerts       dev   # T6
git worktree add ../mono-t7-hermes   -b feat/hermes-adapter  dev   # T7

# 每个 worktree 里开一个 Claude Code 实例,开场白建议:
#   「读 PLAN.md 的 T<n> 任务卡和 docs/monopoly-spec.md §8,完成该任务。」

# 合并顺序建议:T1+T2 先回 dev(T5 依赖),其余完成即合;
# 每个分支合并前跑全量 pytest,合并后删 worktree:
git worktree remove ../mono-t1-monitor && git branch -d feat/position-monitor
```

**冲突提示**: T1/T2 会改 `futures/executor.py`,T6 若并入 monitor 也可能碰它——T6 建议等 T1 合并后再 rebase;其余任务文件集互不相交,可放心并行。

---

## 硬约束提醒(所有任务共同遵守)

- LLM 只做分析建议,**绝不直接下单**;最后一道关口必须是确定性代码。
- T5 完成(人工审批闭环)之前,executor 保持 dryrun/testnet,**不切 mainnet**。
- API Key 禁提币 + IP 白名单 + 单笔金额硬上限。
- 「硬约束放进 schema 但绕过 retry 机制」是反模式(spec §8 locked)——校验放 risk gate,不放 Pydantic。
