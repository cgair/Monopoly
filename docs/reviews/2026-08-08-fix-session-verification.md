# 2026-08-08 持仓周期回测问题 — 修复前证伪判定表

数据侧：直接从 `2026-08-08-horizon-replays.jsonl`（72 条）重算，不引用报告汇总值。
代码侧：按内容 grep 定位后逐文件读，行号为本次实际所见（基线 commit `62373d3`）。

## 判定表

### 1. 闸门对市价单跳过止损方向检查 — **CONFIRMED（严重性需修正一处）**

- **数据依据**（重算）：两笔记录 `gate_passed=True`、`gate_reason=None`，与报告一致：
  - `2026-05-27 r0` Short，`entry_price=None`，`stop_loss=68500.0`，`reference_price=75737.9`（止损在参考价下方 ⇒ 对空单是盈利侧）
  - `2026-06-28 r2` Short，`entry_price=None`，`stop_loss=60140.0`，`reference_price=60314.9`（同上）
- **代码依据**：`risk_gate.py:284` 确认为 `if decision.entry_price is not None:`，
  `stop_wrong_side` 两条分支（285/287 行）整体嵌在其中，市价单（entry=None）整条跳过。
- **分母修正**：报告说"15 笔市价单里 2 笔（13%）"。重算：非 Flat 43 笔中市价单 **17** 笔
  （报告的 15 是剔除这 2 笔后的有效市价单数，把被剔除的当成不在分母里了）。
  实际出现率 **2/17 ≈ 12%**。结论不变：不是偶发。
- **严重性修正（本次证伪新发现）**：executor 的 `compute_sizing`
  （`tradingagents/futures/executor.py:119-139`）对市价单会用真实参考价**再查一次**止损方向，
  反向即抛 `ValueError` → `success=False`，订单不会到交易所。所以缺口的准确表述是
  **"闸门（设计上的安全底线）错误放行，靠 executor 的兜底才没成交"**，而不是"会开出反向止损的实盘单"。
  回放只采集了闸门结论（`replay.py:145-148`），没采集 executor 结果，所以报告只看到了放行。
  修复仍然必要：闸门被文档定义为 safety floor，且 `gate_passed=True` 会污染所有下游统计与审批语义。

### 2. 根因"闸门跑在拿到 mark price 之前" — **CONFIRMED**

- **代码依据**：`tradingagents/graph/setup.py:170-172`：
  `PM → Risk Gate → Mark Price → Executor`。Mark Price 节点（`market_data.py:65-72`）
  读的是 `execution_intent`——闸门的**输出**——所以在现拓扑下它不可能先于闸门运行。
  `evaluate()` 签名（`risk_gate.py:179-188`）没有任何价格入参。
  按给定两个修法处理是成立的。

### 3. `time_horizon` 是 schema 示例回声 — **CONFIRMED**

- **数据依据**（重算）：51 笔填了该字段，其中 **46 笔 = `2-5 days`（90.2%）**，
  与示例字符串一字不差。其余 5 个值各出现 1 次。
- **代码依据**：`tradingagents/agents/schemas.py:261-264`，描述字面量确认：
  `"Optional intended holding period, e.g. '2-5 days' or 'intraday'."`（263 行）。

### 4. 决策不可复现（7/24 三次同向） — **CONFIRMED**

- **数据依据**（重算）：24 窗口中三次同侧 **7 个**（29%）；"3 次里 2 次 Flat"窗口 **10 个**；
  三次全 Flat **1 个**；`2026-02-06` 三次 = `[Short, Long, Short]`。全部与报告一致。

### 5. 41 笔有效决策 12 多 / 29 空（71% 做空） — **CONFIRMED**

- **数据依据**（重算）：非 Flat 43 笔，剔除 2 笔反向止损（判定 1 的两笔，且仅这两笔），
  余 41 笔 = **Long 12 / Short 29**。Flat 29 笔。72 = 41 + 29 + 2，账目闭合。

### 6. "只有分析师节点动态加入，下游全部无条件" — **CONFIRMED**

- **代码依据**：`setup.py:81-84` 仅按 `plan.specs`（来自 `selected_analysts`）动态加节点；
  `setup.py:87-94` Bull/Bear Researcher、Research Manager、Trader、三方风险辩论、
  Portfolio Manager 全部无条件 `add_node`。上一轮结论的适用范围声明成立：
  `selected_analysts=["market"]` 的回放跑的是完整决策链，只缺新闻/舆情分析师。

## 行号核对（本次实际所见 vs prompt 所引）

| 断言 | prompt 引用 | 实际位置 | 一致? |
|---|---|---|---|
| `if decision.entry_price is not None:` | risk_gate.py:284 | risk_gate.py:284 | ✅ |
| `time_horizon` 示例描述 | schemas.py:263 | schemas.py:263（字段声明 261-264） | ✅ |
| 图拓扑 PM→Gate→MarkPrice→Executor | setup.py（未给行号） | setup.py:170-172 | ✅ |

## time_horizon 修复的行为验证（修后实测）

`scripts/backtest_time_horizon_validation.py`：4 个窗口（2026-01-11 / 03-23 /
05-19 / 07-08）× 2 次 = 8 次图运行，全部成功、0 次结构化输出失败：

| 窗口 | r0 | r1 |
|---|---|---|
| 2026-01-11 | Flat / None | Long / `1-3 days` |
| 2026-03-23 | Flat / None | Long / `1-3 days` |
| 2026-05-19 | Short / `1-3 days` | Flat / None |
| 2026-07-08 | Short / `1-3 days` | Short / `1-3 days` |

判读（诚实版）：

- **回声消失**：`2-5 days` 不再出现（枚举里没有它，schema 层面不可能生成）。
- **枚举没有破坏结构化输出**：8/8 成功，方向性决策全部带合法桶值——
  coercion 防线没有被触发过。
- **但分布没有散开**：5 笔方向性决策全部选了 `1-3 days`。n=5 无法区分
  "真实意图一致"（止损均值 2.68% 确实量级上对应 1-3 天的不利波动）和
  "换了一个众数继续收敛"。值得注意的是：schema 一改，众数就从 `2-5 days`
  跳到 `1-3 days`——这本身说明该字段跟着 schema 走的成分仍然大于跟着行情走。
- **结论维持上一轮的警告**：在更大样本显示桶间变异之前，`time_horizon`
  仍不能当作意图证据使用；它现在的价值是"约束住了、可比较了、不会再污染
  解读"，不是"已经携带信息"。

顺带的小样本复现：4 窗口 × 2 次里只有 1 个窗口（2026-07-08）两次同向，
与问题 3 的 29% 一致率相符——降温/投票方案（见 proposals 文档）依然必要。

## 不成立 / 无法验证的部分

- 无整条不成立的断言。两处修正：市价单分母 15→17（出现率 13%→12%）；
  严重性从"放行反向止损单"降为"闸门层放行、executor 层兜底拒单"（见判定 1）。
- "首轮 7 笔里 1 笔"引用的是首轮数据，不在本 JSONL 里，本次未复核。
