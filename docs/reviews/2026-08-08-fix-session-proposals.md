# 2026-08-08 修复 session — 问题 3/4/5 方案（只出方案，不改行为）

前提：本轮回测在 41 笔有效决策上**没有测出任何周期的方向优势**（全部 95% CI 宽度
≥ 25pp）。以下三条都会改变实盘行为，在没有优势证据的情况下直接改 = 在噪声上调参。
每条给出方案、代价、验证计划；实施留给单独 session，且必须配回测验证。

---

## 问题 3：决策不可复现（24 窗口仅 7 个三次同向，29%）

### 性质

不是 bug，是 LLM 采样方差。同一份冻结数据三次重跑，方向、开不开仓、止损价位
都会变（`2026-02-06` = 空/多/空；同窗止损离散度均值 1.71pp，最大 3.83pp）。
当前每一次实盘决策都只是分布里的一次抽样。

### 方案（按实施顺序推荐）

**方案 A：给决策链降温（先做，近零成本）**
- 对 `deep_think_llm`（Research Manager + Portfolio Manager 两个节点）设 temperature=0
  或接近 0；辩论节点（quick_think）保持默认温度以保留观点多样性。
- 代价：一次配置改动 + 一轮验证回放。无运行时开销。
- 风险：温度低≠确定性（Gemini 在 temp=0 下仍有非确定性）；且方向翻转可能主要
  发生在辩论阶段而不是 RM/PM 阶段——**回放数据没存 RM 的 rating，无法定位方差
  来源**。下一轮 sweep 应把 `ResearchPlan.recommendation` 也记进 JSONL。

**方案 B：k 次采样 + 方向一致才执行（A 不够时再上）**
- 每次决策跑 k=3 次完整图，方向 ≥2/3 一致才下单，否则 Flat。
- 代价：3× API 费用 + 3× 延迟。单次图运行 80–200s，串行 3 次 ≈ 4–10 分钟。
  实盘每天 1–2 次决策的节奏下延迟可接受；费用按当前 sweep 实测（72 次 ≈ 2.5h）
  线性外推。
- 附带收益：10 个"3 次里 2 次 Flat"的窗口会稳定落到 Flat——k 投票天然把
  "犹豫"折叠成不动，方向仓位只在模型稳定时出现。这既是特性也是代价
  （开仓频率会明显下降，41/72 → 预计 ~20/72 量级）。

### 验证计划

改完后重跑 24 窗口 × 3 次的一致性指标（复用
`scripts/backtest_horizon_sweep.py`，~2.5h）。指标：三次同向窗口数。
当前 7/24（29%）；目标 ≥ 17/24（70%）。达不到就叠加方案 B 再测。

---

## 问题 4：止损宽度与持仓周期不匹配

### 性质

止损距入场 0.39%–6.18%（均值 2.68%），而平均不利波动 6h=1.26% → 168h=3.82% →
720h=9.69%。到 168h 已有 8/15 笔市价单碰过自己的止损，其中 4 笔方向本来是对的。

### 方案

- 让 PM/Trader 把止损锚定在 **ATR 倍数**上而不是拍百分比：market report 已经带
  ATR（回放样本里就有"ATR of 711"字样），prompt 加一条"止损距离 = max(论点失效位,
  k × ATR)"，k 和 ATR 周期按 `time_horizon` 桶选择——该字段本轮已枚举化
  （`intraday`/`1-3 days`/`1-2 weeks`/`2-4 weeks`/`1 month+`），第一次有了可用的
  意图信号可以挂钩：
  - `intraday` → 1h ATR × ~2
  - `1-3 days` → 4h ATR × ~2
  - `1-2 weeks` 及以上 → 1d ATR × ~2
- 只改 prompt 层（L1 advisory），闸门不加 ATR 检查——闸门没有价格序列，加了就
  破坏它"纯函数、无数据依赖"的设计。

### 为什么现在不改

**没有任何周期显示方向优势，放宽止损不会让系统赚钱**，只会让"没有优势"更慢地
显形——同时每笔亏损变大（同样 risk_pct 下 qty 会因止损变宽而变小，单笔美元
风险不变，但碰止损的频率下降 = 亏损实现得更慢、回撤周期拉长）。
这是在改亏钱的速度曲线，不是在改期望。

### 验证计划

单独 session：改 prompt → 重跑 24 窗口 sweep（~2.5h）→ 对比 race 口径的期望 R
与"方向对但被止损打掉"的笔数（当前 168h 上是 4 笔）。后者应显著下降且期望 R
不恶化，才算改对。

---

## 问题 5：做空偏向（41 笔里 29 空，71%）—— 诊断

### 排除行情因素

窗口按 168h 前瞻涨跌 12:12 平衡选取，重算确认。偏向是系统自身的。

### 机制链（本次诊断结论，四段代码证据）

偏向不是某一个 prompt 里有"看空"字样，而是**一条把"谨慎"逐级翻译成"做空"的
流水线**：

1. **辩论框架不对称** —— `bear_researcher.py:15`：Bear 的任务是
   "making the case **against going long**"（列风险、挑战、负面指标），
   Bull 的任务是 "advocating for a long position"。辩论是"做多 vs 不做多"，
   不是"做多 vs 做空"。所有风险类论据都自动记在 Bear 名下。
2. **评级语义不对称** —— `research_manager.py:31-36` + `schemas.py` 的
   `PortfolioRating`：这是一套股票组合评级（Buy/Overweight/Hold/Underweight/Sell）。
   `Underweight` 的定义是 "**Cautious view**; recommend trimming exposure"——
   "谨慎"这个风险词直接映射到 Underweight。股票语境里 Underweight = 低配
   （少持有），最接近的期货动作是减仓或 Flat。
3. **Trader 映射把低配翻译成开空** —— `trader.py:64-69`：
   `Underweight → Short (lower conviction)`。于是"Bear 赢了'不要做多'的辩论"
   → RM 给出"谨慎"的 Underweight → Trader **开杠杆空单**。没有人论证过
   "价格会跌"，空单就产生了。
4. **风险辩论层强化下行词汇** —— `conservative_debator.py:19` 的整个人设是
   "protect assets, minimize volatility, …assessing potential losses"。它的输出
   进入 PM 的 prompt，把"风险/损失"词频推高，进一步把 PM 推向"谨慎"侧——
   而按 2+3，"谨慎"就是做空。

反向路径不存在：没有一个评级把"谨慎"翻译成"轻仓做多"，也没有一个辩手的任务
是"论证不要做空"。这就是 12:29 的来源。

### 可证伪预测（下一轮 sweep 验证）

若机制链成立，多数空单应来自 `Underweight` 而非 `Sell` 评级。当前回放 JSONL
**没有记录 RM 的 recommendation**，验证不了——下一轮 sweep 应把
`ResearchPlan.recommendation` 记进每条记录（也是问题 3 定位方差来源需要的）。

### 修法候选（均改变实盘行为，本轮不动）

- (a) 最小改动：Trader 映射改为 `Underweight → Flat`（或减仓语义），只有 `Sell`
  开空。预计直接消掉低置信空单。
- (b) 对称化辩论：Bear 的任务改成 "making the case for a short position"，
  让空单必须有主动的做空论据。
- (c) 评级去股票化：把 PortfolioRating 换成方向原生的五档
  （StrongLong/Long/Neutral/Short/StrongShort）。改动面最大（三个结构化 schema
  消费方 + 渲染 + 记忆日志）。

### 验证计划

任选其一实施后：重跑 24 平衡窗口 × 3 次，看多空比从 12:29 移向 ~1:1；同时确认
Flat 率没有失控（(a) 预计 Flat 率上升，需确认不是全部变 Flat）。加记
`recommendation` 字段后可直接检验上面的可证伪预测。
