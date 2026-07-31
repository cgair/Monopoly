# Monopoly 开发计划(PLAN.md)

- **更新**: 2026-07-31(新增 **T9–T11 数据源增强**任务卡;此前 2026-07-19:**T0–T7 全部完成**并合并回 `dev`,512 passed;剩:用户在 Mac mini 配 OpenClaw+Discord、T3b Reddit OAuth(可选);Hermes 与 T8 On-Chain 均搁置/观察)
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
搁置:Hermes 侧集成(代码已在库,用户决定现有工作流够用,暂不配置)
      T8 On-Chain analyst(观察后再决定:testnet 复盘发现链上信号盲区才启动)

新增(2026-07-31,数据源增强,三者互相独立、可并行,全部 $0):
      T9  Binance 免费多空比端点 → Market Analyst(Coinglass 购买降级为 wait-and-see,
          触发条件:testnet 复盘发现止损被插针/清算瀑布类亏损)
      T10 快讯源(BlockBeats/Odaily)→ News Analyst + Sentiment twitter_block
      T11 经济日历(ForexFactory)→ 前瞻宏观事件风控上下文

新增(2026-07-31,对账闭环加固,QuantDinger 调研引出):
      T12 对账闭环补全:intent 先行落盘 + 反向对账 + 真实 PnL 回填(P1,mainnet 前置)
      T13 单写者锁:monitor 与 graph run 并发防护(P3,可选)
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
> **T3b OAuth 已实现(2026-07-19)**:`crypto_reddit.py` 支持 app-only client_credentials
> 授权——设置 `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` 后优先走 `oauth.reddit.com`,
> token 缓存 + 401 自动失效重取,无凭证或 token 失败时回落公开端点(www → old → data_gap),
> 行为不变。测试 +5(共 16),模板见 `.env.example`。
> **⚠️ 2026-07-19 实测受阻**:Reddit 已启用 Responsible Builder Policy,自助创建 app 被关闭,
> 需先走工单审批(个人项目被拒率高)。**当前推荐路径改为:换代理出口节点**(公开端点对住宅
> IP 正常放行,零代码改动);OAuth 代码保留,审批若批下来即可用。审批入口见
> <https://support.reddithelp.com/hc/en-us/articles/14945211791892>。

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

## T8 — On-Chain Analyst 节点(观察后再决定,2026-07-19)

**状态**: **非必须,暂不开发**。现有三 analyst(技术面/新闻/情绪)+ 完整链路已闭环,On-Chain
是分析质量增强项而非结构依赖。决策方式:testnet 阶段用三 analyst 配置攒决策样本,复盘时若
发现"链上早有信号但系统看不到"的亏损类别(如巨鲸砸盘打止损),再启动本任务——届时数据源
选型(Glassnode/CryptoQuant 付费 vs 链上 RPC 自建)也有了实际依据;若决策质量够用则永久搁置。
**决策后的工作**:
- 新建 `dataflows/crypto_onchain.py`(交易所净流入、巨鲸地址)。
- 新建 `agents/analysts/onchain_analyst.py`,注册 `AnalystType.ONCHAIN`(`cli/utils.py::filter_analysts_for_asset_type`)。
- DAG Analyst Team 阶段追加并行节点(`graph/setup.py`)。

**注意**: 不要改造 `fundamentals_analyst.py` — fork 已在 crypto 模式下过滤掉它,On-Chain 是全新节点(spec §2.3 修正)。

---

## T9 — 合约情绪信号接线到 Market Analyst(2026-07-31 新增;2026-07-31 改免费方案)

**优先级**: P2
**触碰文件**: `dataflows/crypto_binance.py`(新增免费多空比端点)、`agents/analysts/market_analyst.py`
+ 对应测试
**并行安全**: 与 T10/T11 零交集

**决策(2026-07-31)**: **不买 Coinglass**——项目 testnet 验证期无收入,$29/月是纯烧钱,且无基线
无法衡量其贡献。改用 Binance 免费公开端点(`/futures/data/*`,无需 auth):
- `globalLongShortAccountRatio` — 全市场多空人数比
- `topLongShortPositionRatio` — 大户持仓多空比
- `takerlongshortRatio` — 主动买卖量比
单所数据,但系统只在 Binance 交易,够用。实现风格对齐 `market_data.py::fetch_mark_price()`
(unauth GET)或 python-binance 对应方法。

**Coinglass 购买 → wait-and-see(与 T8 同款决策模式)**: 唯一无免费替代的是**清算数据**
(清算密集区→止损摆放/连环清算预警;Binance 强平 REST 端点已下线)。触发条件:testnet 复盘
发现「止损频繁被插针扫掉」或「亏损集中在清算瀑布时段」的模式 → 买 Hobbyist($29/月,
**按月付**,年付折扣虚标)。届时 `crypto_coinglass.py` 已就绪,只需设 `COINGLASS_API_KEY`
+ 把 `get_liquidations` 补进 analyst 工具表。
**限额结论存档(spec §5 可关闭)**: Hobbyist 30 req/min ≫ 双标的 5min 轮询(≈4 req/5min),够用。

**步骤**:
1. `crypto_binance.py` 加 `get_long_short_ratio()`(内部聚合上面 3 个免费端点,复用现有
   cache/Response 模式;与 coinglass 版同名工具冲突的话,vendor 路由优先 binance)。
2. `market_analyst.py` 工具列表加该工具,prompt 增加使用指引(多空比极值→反向/拥挤信号,
   大户 vs 散户背离),风格对齐现有 4 个工具说明。
3. 端点失败优雅降级(复用 Response ok=False 模式),单测不联网。

**验收**: 完整 run 报告含多空比分析;端点失败时报告注明 data gap;零新增费用。

---

## T10 — 快讯源接入:BlockBeats/Odaily → News + Sentiment(2026-07-31 新增)

**优先级**: P2
**触碰文件**: `dataflows/crypto_news.py`(`_FEEDS` 扩充)或新建 `dataflows/crypto_newsflash.py`、
`agents/analysts/sentiment_analyst.py`(`twitter_block` 填充)+ 测试
**并行安全**: 与 T9/T11 零交集

**背景**: 两个缺口一次补。① `crypto_twitter.py` 是占位符,Sentiment Analyst 的 `twitter_block`
恒为 "data unavailable"——X API 太贵($100/月起且配额小)。② News 源只有 CoinDesk/CoinTelegraph,
宏观数据(FOMC/CPI/非农)覆盖是文章级、慢。BlockBeats/Odaily 快讯编辑全天盯 crypto Twitter,
KOL 重要推文几分钟内变快讯("某某发推表示…"),宏观数据公布后几分钟内出数字快讯——等于免费、
无 key、官方支持的「X 消息面 + 宏观快讯」聚合(follow-builders 同款中心化 feed 模式)。

**端点**(官方 GitHub repo 提供):
- BlockBeats 快讯: `https://api.theblockbeats.news/v2/rss/newsflash?lang=en`(repo: BlockBeatsOfficial/RSS-v2)
- Odaily: repo ODAILY/RSS(快讯 + 文章)
- 可选: CryptoPanic `https://cryptopanic.com/news/rss`

**设计要点**:
- **语言选 `lang=en`**——现有 symbol 关键词过滤是英文 regex(`crypto_news.py` 的 pattern 表),
  中文内容会漏筛;LLM 读中文没问题,但过滤层先行。
- News Analyst 路径:快讯进 `_FEEDS`,`get_news`/`get_global_news` 自动生效,无 prompt 改动。
- Sentiment 路径:从快讯里筛「KOL 动态」类条目(标题含发推/tweet/表示 等模式,或全量近 24h)
  填进 `twitter_block`,替换占位符;`crypto_twitter.py` 的对外签名保持,内部换实现,
  block 头注明来源是编辑转述而非推文原文(无 engagement 数据)。
- 快讯条目短、量大,注意 `hours` 窗口与条数上限,别把 prompt 撑爆。

**验收**: `get_news` 结果含快讯条目;宏观数据日(如 CPI)当天能拉到数字快讯;Sentiment 的
`twitter_block` 不再 "data unavailable";RSS 拉取失败时优雅降级(复用现有 fetch_errors 模式)。

---

## T11 — 经济日历前瞻风控(ForexFactory)(2026-07-31 新增)

**优先级**: P3
**新建文件**: `dataflows/econ_calendar.py` + 测试;接线点在 PM 上下文 + risk gate(可选策略)
**并行安全**: 与 T9/T10 零交集;触碰 `risk_gate.py` 时注意与未来任务的冲突

**背景**: 现有宏观覆盖全是**回顾式**(新闻发出来才知道)。系统不知道「明天凌晨 2 点 FOMC」——
对杠杆 + 止损单系统是真实风险:数据公布瞬间的插针足以扫掉止损。手动交易者「数据日不开仓」
的直觉,系统目前没有。

**数据源**: ForexFactory 免费周历 JSON `https://nfs.faireconomy.media/ff_calendar_thisweek.json`
(经典免费源,含事件时间/货币/影响等级)。无 key;注意 UA 和缓存(周历,TTL 可以很长)。

**设计要点**(遵守 defense layering,spec §8 locked):
- 过滤:`currency == USD` 且 `impact == High`(FOMC/CPI/NFP 自然命中)。
- L1/L2:未来 N 小时(默认 12h,env 可配)内有高影响事件 → 在 PM 上下文注入警示文本
  (事件名 + 倒计时),让 LLM 自己权衡降杠杆/观望。
- L3(可选,默认**只提示不硬拒**):risk gate 新增 `macro_event_caution` 策略,
  `TRADINGAGENTS_FUTURES_MACRO_BLOCK_HOURS` env 开启后,事件前 N 小时内拒绝**新开仓**
  (写 `trade_skipped`,reason 带事件名);默认关闭,攒样本复盘后再决定要不要开。
- 日历拉取失败 → 静默降级为无警示(fail-open,这是提示层不是安全层),但要 log。

**验收**: mock 日历单测(事件在窗口内 → PM 上下文含警示;窗口外/拉取失败 → 无警示);
env 开启硬拒后 gate 拒绝并写审计事件;真实拉取一次确认 schema 没变。

---

## 手工验证清单(2026-07-18 更新:1–3 已由 Claude 实弹完成,剩 4–5 需用户)

1. ~~Position monitor testnet 实测~~ ✅ 2026-07-18:开仓→平仓→对账→`position_closed` 写入、孤儿单清零、账户干净。
2. ~~Algo 撤单 endpoint 实测~~ ✅ 2026-07-18:修正 endpoint 后一发清掉 4 张 algo 单(详见 T2 任务卡注记)。
3. ~~Reddit 真实代理实测~~ ✅ 2026-07-18:降级路径正常;**代理出口 IP 被 Reddit 封禁**。
   T3b OAuth 代码已于 2026-07-19 实现,但 Reddit 新政策关闭了自助注册(需工单审批,见 T3 注记)。
   **2026-07-19 定位修正**:不是 IP 全封——封的是「DC IP + 匿名 .json 端点」组合(HTML 200 /
   .json 403,浏览器正常即此因);现有 3 节点 .json 全被封(US×2 = 403/tarpit,Tokyo = TLS 黑洞)。
   **✅ RSS fallback 已实现并实弹验证**:`search.rss` → `new.rss`+客户端过滤,RSS 未被封,
   r/Bitcoin 实测拿回 10 条真实帖子(`ok=True`)。限制:RSS 无 score/评论数(engagement 全 0)。
   OAuth 审批仍建议提交(长期解);新增住宅节点可选(能恢复 .json 的 engagement 数据)。
4. **OpenClaw 侧安装**(T4,需用户在 Mac mini 上操作):拷 `openclaw/skills/trade_analyze`、`trade_review`
   到 OpenClaw skill 目录,配置 **Discord bot**(交互渠道已定为 Discord,非 Telegram),
   手机 Discord 跑 `/trade_analyze` 和 `/trade_review`。
5. `git push origin dev` 备份(本地已领先若干 commit)。

## 其他挂起项(不单开任务)

- **Twitter 数据源**:已主动推迟(option f:先用 News + Reddit)→ **2026-07-31 起由 T10 以
  快讯转述形式部分覆盖**;推文原文 + engagement 数据仍无免费解,永久搁置直到 X API 降价。
- **Coinglass 计费 tier**:~~待验证~~ → **已在 T9 任务卡结论**:Hobbyist 30 req/min 够双标的
  5min 轮询;但**购买本身降级为 wait-and-see**(2026-07-31,理由:验证期无收入不烧订阅费,
  复盘发现清算盲区亏损再买)。
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
