# Code Review Findings — 2026-08-02

> 审查基线：branch `dev`，覆盖 main..dev 全部 60 个提交中的资金路径代码。
> 审查时 HEAD ≈ `08e3024`（当前 `cfe5ab8`，其后仅 docs 提交）。**行号可能随后续提交漂移，修复时以描述定位为准。**
>
> 本文件是给"新 context 修复用"的自包含交接文档：每条 finding 含定位、缺陷、失败场景、建议修法。
> 已剔除的误报也记录在底部——**修复时不要重新报告/修复那些条目**。
>
> **修复流程（每条 finding 独立走一遍，先验证后修复）：**
> 1. **验证**：读当前源码确认缺陷仍在（行号可能漂移），然后写一个复现"失败场景"的测试并确认它失败（红）。
> 2. **无法验证则不修**：如果源码已不符合描述，或写不出失败测试（说明场景不成立），把该条状态标为 `✗ disputed` 并附一句理由，跳过——**不要为了"完成任务"硬修一个复现不了的问题**。
> 3. **修复**：最小改动让红测试转绿，全量回归（见底部验证命令）。
> 4. **登记**：在总表状态列标 `✓ fixed` 或 `✗ disputed`。
>
> 注意两轮 review 的 CRITICAL/HIGH 已经过一次源码核实（7 条子代理误报被剔除），但核实 ≠ 复现——红测试这一步就是最终验证，不要省。

## 审查范围与方法

- **第一轮**（T12 增量，`e0045dc..HEAD`）：code-reviewer + python-reviewer 两个子代理，13 文件 +1409/−162。
- **第二轮**（核心路径基座，`main..e0045dc`）：3 个并行 code-reviewer，分执行/审批链、风险层、支撑模块，~3.6k 行实现 + 3.8k 行测试。
- 所有 CRITICAL/HIGH 级 finding 均由主循环**对源码二次核实**；误报已剔除并记录理由。
- 未审：`tradingagents/dataflows/`（数据源抓取，非资金路径）、docs 类提交。
- 静态工具 ruff/mypy/bandit 未安装（venv 里没有），只跑了 `py_compile`（全过）。

## 修复优先级总表

| # | Sev | 文件 | 一句话 | 状态 |
|---|-----|------|--------|------|
| F1 | HIGH | `tradingagents/futures/risk_state.py` | `load_events` 坏行崩溃，与 alerts.py 容错不一致 | ✓ fixed 2026-08-02 |
| F2 | HIGH | `tradingagents/futures/executor.py` | Binance Client 无 timeout | ☐ |
| F3 | HIGH | `cli/main.py` + `approval.py` | decision 文件无重放保护 | ☐ |
| F4 | HIGH | `tradingagents/futures/position_monitor.py` | 多 dangling intent 收养选"最新"可能配错单 | ✓ fixed 2026-08-02 |
| F5 | HIGH | `tradingagents/futures/alerts.py` / `position_monitor.py` | `executor_errors`、`orphan_algo_orders_pending` 是死指标 | ☐ |
| F6 | MED | `position_monitor.py` | 收养仓位不校验 side | ✓ fixed 2026-08-02 |
| F7 | MED | `position_monitor.py` | outcome 推断：相对容差无下限 + PnL 含手续费/资金费 | ✓ fixed（费用半句 disputed，见注） |
| F8 | MED | `position_monitor.py` | income history 失败/intent 重叠只写 log 不走 alerts | ✓ fixed 2026-08-02 |
| F9 | MED | `executor.py` | 市价开仓不处理部分成交（记请求量非 executedQty） | ☐ |
| F10 | MED | `risk_gate.py` | 宏观日历异常静默吞掉，无痕放行 | ☐ |
| F11 | MED | `risk_state.py` / `alerts.py` | 事件回放假定文件行序=时间序，未按 ts 排序 | ☐ |
| F12 | LOW | `cli/main.py` | ticker 缺失默认 "UNKNOWN" 不 fail-fast | ☐ |
| F13 | LOW | `trade_review.py` | `pnl_usd` 缺失静默按 0 累加 | ☐ |
| F14 | LOW | `executor.py` | `_try_unwind` 不检查响应 status | ☐ |

修复顺序建议：F1 → F2 → F3 → F4+F6（同一函数）→ F5 → F7/F8 → 其余按复盘证据排。

> **2026-08-02 修复批次（F1/F4/F6/F7/F8，569 tests passed）**：
> - F1：`load_events` 改容错（坏行 skip + warning），新增 `load_events_with_errors` 供 alerts 计数，
>   坏行数 ≥1 → WARN finding（`TRADINGAGENTS_FUTURES_ALERT_MALFORMED_LINE_THRESHOLD`）。
> - F4+F6：收养仅在「恰好一个同 symbol dangling intent 且 side 与交易所仓位一致」时发生；
>   歧义（>1 匹配）或方向不符 → 拒收养，记 `position_untracked` + CRITICAL 告警交人工。
> - F7：**费用前提 disputed**——`get_realized_pnl` 本就按 `incomeType="REALIZED_PNL"` 过滤，
>   实弹 income 明细验证 COMMISSION 是独立类目（-1.72 vs REALIZED_PNL -2.96），不在求和内。
>   但容差问题反向成立：1% 相对容差（~632 点）淹没紧止损距离（89 点），保本手动平仓会被
>   误标 `stop` 白白触发 cooldown。已修：容差按候选取 `min(1%×close, 0.5×|entry−trigger|)`。
> - F8：`MonitorResult.dangling_remaining` 计数（monitor CLI 退出码 1），alerts 全量事件配对
>   检测 dangling（无视扫描窗口，3 天前的也报）→ WARN（`…_ALERT_DANGLING_INTENT_THRESHOLD`）。

---

## HIGH 详情

### F1. `load_events` 坏行崩溃（risk_state.py:119-125）
- **缺陷**：`load_events` 里 `json.loads(line)` 无保护。崩溃/断电写残的半行 JSON 会让 gate 评估和 position monitor **每次运行都崩**，对账停摆直到人工修文件。而 `alerts.py:194` 对 malformed 行是跳过不炸（spec 有此要求）——同一份 JSONL 两套容错策略。
- **连带**：`trade_review.py:112` 调 `load_events` 同样会崩，修上游即可一并解决。
- **修法**：对齐 alerts.py：坏行 skip + `logger.warning`（可计数，超阈值走告警）。补测试：文件尾部残行时 `derive_state` 仍正常。

### F2. Binance Client 无 timeout（executor.py:290 附近，`TestnetExecutor.__init__`）
- **缺陷**：`client_cls(api_key, api_secret, testnet=True)` 未传 `requests_params={"timeout": ...}`，python-binance 底层 requests 默认无超时。网络卡死时下单调用无限挂起：write-ahead 保证事后可对账（dangling intent 关 gate，方向保守），但进程悬死，仓位在 monitor 下次运行前无人监管。
- **修法**：构造时加 `requests_params={"timeout": 10}`（或配置项）。注意 `client_cls` 是可注入的（测试用 mock），只改真实分支。

### F3. decision 文件无重放保护（cli/main.py trade_execute ~1470-1580；approval.py）
- **缺陷**：同一份已批准的 decision JSON 重复执行会再开一笔同向仓（one-way 模式直接加仓翻倍）。gate 只有 `max_concurrent_positions=2` 总数上限（risk_gate.py:160 `REASON_MAX_POSITIONS`），挡不住同 symbol 重入。人工流程里误重跑 CLI 很现实。
- **修法**（择一）：执行成功后把 decision 文件改名（`.executed` 后缀）；或在 ledger 按 decision_ts+内容哈希查重，重复则拒绝并提示。测试：同文件执行两次，第二次拒绝。

### F4. 多 dangling intent 收养选"最新"（position_monitor.py:573-580）
- **缺陷**：交易所仓位匹配到多个同 symbol dangling intent 时取 `ts` 最新的收养。实际成交的可能是旧 intent → 收养事件复制**错误 intent 的 stop_loss/take_profit**，后续 outcome 推断（stop vs manual）判错，影响 cooldown。剩下的旧 intent 掉到 Pass 3 因"与已跟踪仓位重叠"永久搁置，gate 持续关闭。
- **修法**：同 symbol 匹配到 >1 个 dangling intent 时**不自动收养**，记 error 事件并升级告警（结合 F8），要求人工裁决。测试：两个 dangling intent + 一个交易所仓位 → 不收养、有告警。

### F5. 死指标：`executor_errors` 与 `orphan_algo_orders_pending`
- **缺陷 a**：`alerts.py:222` `EventStats.executor_errors` 初始化后**全文件无 append**，`alerts.py:376` 的 `executor_error_threshold` 永远不触发——executor 连续崩溃告警恒 OK。222 行注释也是复制错的（写着 "runs of consecutive stop-closes"）。
- **缺陷 b**：`position_monitor.py:496` `orphan_algo_pending = 0` 后从不递增（algo 撤单失败只 `logger.warning`），`MonitorResult.orphan_algo_orders_pending` 恒 0；546 行 TODO 自己承认 basic 撤单也只按 symbol 计数不按单数。
- **修法**：a) 在 `analyze_events` 里把 `trade_skipped`/error 类事件（按现有事件 schema 里的 executor 失败标记，如 `position_naked`、error reason）填入 `executor_errors`；b) algo 撤单失败时 `orphan_algo_pending += 1`。修完顺手更正注释。

---

## MEDIUM 详情

### F6. 收养不校验 side（position_monitor.py:591）
`side` 从 `positionAmt` 符号推断，不与 dangling intent 记录的 `side` 比对。不一致时（如用户在交易所手动反向）应 warning + 拒绝收养，而不是静默按交易所方向记账（intent_id 对不上方向，close price 推导整个反号）。与 F4 在同一函数，建议一起修。

### F7. outcome 推断容差与费用（position_monitor.py:353-360）
`close = entry ± pnl_usd/qty` 反推平仓价后用 `_OUTCOME_PRICE_TOLERANCE * abs(close)`（1% 相对容差）匹配 stop/TP。两个问题：① 容差无绝对下限；② income history 的 realized pnl 含 COMMISSION/FUNDING_FEE，小仓位下占比大，易把 stop-out 误判 "manual"（漏触发 cooldown）。修法：容差加绝对下限；或拉 income 时按 incomeType 只取 REALIZED_PNL。

### F8. 保守留置只写 log 不走告警（position_monitor.py:625-645）
`get_realized_pnl` 失败、dangling intent 与已跟踪仓位重叠——两处都正确地"留置 + gate 关闭"，但只 `logger.error`。launchd 跑的 monitor 没人盯日志，operator 可能几天不知道 gate 为何一直关。修法：这两种情形写入事件流（新事件类型或复用现有），让 `alerts.py` 的 evaluate 能看到并升级。

### F9. 市价开仓部分成交（executor.py:426-440）
`position_opened` 记请求量而非 `executedQty`，保护单也按请求量下。BTC 市价单几乎总全成，但部分成交时 reduceOnly 保护单会超量被拒。修法：从 open_resp 取 `executedQty` 作为真实仓量，用它下保护单和记事件。

### F10. 宏观日历异常静默（risk_gate.py:334-338）
`upcoming_high_impact()` 抛异常时按"无事件"放行且无痕迹。若启用 `macro_block_hours`，日历源挂掉=防线静默失效。修法：catch 时 `logger.warning` + 在 GateResult/snapshot 里带 `macro_check_failed` 标记（不阻断，只留痕）。

### F11. 事件未按 ts 排序（risk_state.py derive_state；alerts.py analyze_events）
多进程并发追加时文件行序可能与 ts 乱序：cooldown 起点（`last_stop_loss_close_ts`）、连续止损计数会算错。修法：`load_events` 返回前按 `ts` 稳定排序（一行）。

---

## LOW 详情

- **F12** cli/main.py:1478：`decision_json.get("analysis", {}).get("ticker", "UNKNOWN")` — ticker 缺失应直接报错退出（无效 symbol 最终会被交易所拒，但报错迷惑）。
- **F13** trade_review.py:180：`float(ev.get("pnl_usd", 0.0))` 静默按 0 累加，掩盖 schema 缺字段；缺失时 skip + warning。
- **F14** executor.py `_try_unwind`：市价 reduceOnly 单基本必成交，但可顺手检查响应 status 增强确定性。

## 测试观察（非缺陷）

- 保护单失败→unwind 全路径**只有 mock 测试**，未在 testnet 实弹演练（尤其部分成交后 unwind 超量被拒的分支）。下次 testnet 验证可专门演一次。
- 无测试覆盖：日回撤恰好等于阈值、cooldown 恰好到期时刻、乱序 JSONL 事件。
- 建议把 ruff/mypy/bandit 装进 dev 依赖（免费，符合验证期原则）。

## 已剔除的误报（勿再报/勿"修复"）

| 原报告 | 剔除理由 |
|---|---|
| "utcnow_iso 与 reconcile now_iso 时间戳不一致致超时判定漂移"（HIGH） | 两者都是 UTC 去微秒（risk_state.py:217 / position_monitor.py:444），所谓差异只是调用时刻自然流逝 |
| "equity_usd=0 → 回撤阈值 0，允许无限亏损"（CRITICAL） | 方向反了：阈值 0 时任何亏损即 halt，fail-closed |
| "unwind 只确认下单未确认成交"（CRITICAL） | MARKET reduceOnly 单不挂盘口，场景不成立（残余改进见 F14） |
| "market_data.py:70 float(entry_price) 崩溃"（CRITICAL） | entry_price 由内部 ExecutionIntent 构造，已是 float/None |
| "risk_per_unit 无 epsilon"（MEDIUM） | 失败终点是被交易所过滤器拒单，fail-safe |
| "qty 趋零数值爆炸" | pnl/qty 即单位价格变动，数学上正确；真实问题并入 F7 |
| "approved_by 回退 unknown 绕过审批"（HIGH） | CLI 入口已强制校验 --approved-by（cli/main.py:1547 附近） |

## 修复验证方式

```bash
cd "/Users/chenge/Desktop/AI/become rich/Monopoly"
.venv/bin/python -m pytest tests/test_futures_executor.py tests/test_futures_risk_gate.py \
  tests/test_futures_position_monitor.py tests/test_futures_t12_reconciliation.py \
  tests/test_trade_execute_cli.py tests/test_futures_trade_review.py -q
```

每条 finding 修复时先补一个复现该失败场景的测试（红），再修（绿）——红测试即验证，写不出红测试的条目标 disputed 不修。涉及交易所交互的（F2/F9）用现有 mock `client_cls` 注入模式，无需实盘。F2 例外：无 timeout 难以用测试"复现挂起"，验证方式改为读 python-binance `Client.__init__` 签名确认 `requests_params` 传递路径，测试断言构造参数即可。
