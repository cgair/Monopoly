# Monopoly Trading Agent 代码走读

> **基准**：`dev` 分支（PLAN.md T0–T4 已合并：加固提交 / position monitor / algo 撤单 / Reddit 修复 / OpenClaw CLI）  
> **生成日期**：2026-06-24 · **更新**：2026-07-19（T1–T4 落地后同步，新增 §10.5–§10.6，修订 §12）  
> **读者画像**：熟悉 Python，但**未读过 LangGraph 文档**

---

## 0. 怎么读这份文档

本文按"自顶向下，再自底向上"的顺序展开：

1. **LangGraph 速通**（§1）——你不需要完整看官方文档，掌握本节几个核心概念就够看懂全部代码。
2. **系统鸟瞰**（§2）——Monopoly 的两层风控设计哲学 + 完整决策图。
3. **入口与生命周期**（§3）——从 `main.py` 到一次完整 run 的代码路径。
4. **状态对象**（§4）——LangGraph 里所有节点共享的"信使" `AgentState`。
5. **图拓扑**（§5）——四张 mermaid 图，分别展开 analyst 自循环、研究员辩论、风险辩论、futures tail。
6. **节点详读**（§6–§10）——按图的执行顺序逐节点讲。
7. **横切关注点**（§11）——structured output、JSONL 事件日志。
8. **上线前盲点**（§12）——已修复 / 未修复对照表。
9. **LangGraph 文档链接**（§13）。

所有代码引用格式：`path/to/file.py:LINE` 或 `:LINE-LINE`。

---

## 1. LangGraph 速通

[LangGraph](https://langchain-ai.github.io/langgraph/) 是 LangChain 团队推出的"用图结构编排 LLM agent"的库。它解决一个具体问题：当一个 agent 需要**多轮工具调用 + 多个子 agent 协作 + 有状态共享 + 可恢复**时，普通的 Python 函数链不够。LangGraph 把这种工作流抽象成一张有向图（带循环），节点是函数，状态在节点间流动。

### 1.1 五个必须懂的概念

#### 1.1.1 StateGraph

[`StateGraph`](https://langchain-ai.github.io/langgraph/concepts/low_level/#stategraph) 是顶层构造器。声明一个 state schema（一般是 `TypedDict`），然后往里加节点、加边、加条件边、编译。

```python
from langgraph.graph import StateGraph, START, END
workflow = StateGraph(AgentState)
workflow.add_node("Bull Researcher", bull_node_fn)
workflow.add_node("Bear Researcher", bear_node_fn)
workflow.add_edge(START, "Bull Researcher")
workflow.add_conditional_edges("Bull Researcher", router_fn, {...})
graph = workflow.compile()
```

#### 1.1.2 State + Reducer

[State](https://langchain-ai.github.io/langgraph/concepts/low_level/#state) 是流经图的字典对象。每个节点接收当前 state，返回一个**部分 dict**，LangGraph 把它和当前 state 合并。

合并方式由字段的 **reducer** 决定：

- 不指定 reducer → **直接覆盖**（你 return 这个字段就替换旧值）。
- `Annotated[list, operator.add]` → 列表追加。
- `Annotated[list[Message], add_messages]` → 消息列表用 LangGraph 的智能合并器（去重、按 id 替换）。

Monopoly 的 `AgentState`（§4）大量使用"直接覆盖"模式：每个节点写自己负责的字段，互不重叠。

#### 1.1.3 节点（Node）

节点就是普通 Python 函数，签名 `def node(state) -> dict`。返回值是 state 的部分更新。

```python
def my_node(state):
    last_msg = state["messages"][-1]
    return {"my_report": f"Processed {last_msg}"}
```

LangGraph 自动调度：根据边的连接，决定哪个节点接力。

#### 1.1.4 条件边（Conditional Edges）

[条件边](https://langchain-ai.github.io/langgraph/how-tos/branching/) 是路由函数：接收 state，返回**下一个节点的名字**。

```python
def router(state) -> str:
    if state["count"] > 3:
        return "Manager"
    return "KeepDebating"

workflow.add_conditional_edges("Bull", router, {
    "Manager": "Research Manager",
    "KeepDebating": "Bear",
})
```

Monopoly 用条件边实现三种循环：① analyst ↔ ToolNode 工具调用循环，② Bull/Bear 辩论循环，③ 三方风险辩论循环。

#### 1.1.5 ToolNode

[`ToolNode`](https://langchain-ai.github.io/langgraph/reference/agents/#langgraph.prebuilt.tool_node.ToolNode) 是 LangGraph 内置的"工具执行器"。给它一组 `@tool` 装饰的函数：

```python
tool_node = ToolNode([get_market_data, get_news])
workflow.add_node("tools_market", tool_node)
```

LangGraph 会自动：检查 `messages[-1].tool_calls` → 调对应工具 → 把结果包成 `ToolMessage` 追加进 `messages`。

### 1.2 MessagesState 与消息合并

[`MessagesState`](https://langchain-ai.github.io/langgraph/concepts/low_level/#messagesstate) 是一个预置的 TypedDict 子类，自带：

```python
messages: Annotated[list[AnyMessage], add_messages]
```

[`add_messages`](https://langchain-ai.github.io/langgraph/reference/graphs/#langgraph.graph.message.add_messages) reducer 会：
- 节点返回的 `{"messages": [new_msg]}` 自动追加到现有列表
- 如果新消息 id 已存在，替换而非重复

Monopoly 的 `AgentState` 继承 `MessagesState`，所以你看到很多节点返回 `{"messages": [AIMessage(...)]}`——这是在追加，不是覆盖。

### 1.3 编译与执行

`workflow.compile()` 把图固化成可执行对象。运行有两种姿势：

- `graph.invoke(initial_state, config={...})` —— 一把跑到 END，返回最终 state。
- `graph.stream(initial_state, config={...})` —— 每个节点完成时 yield 一个增量 chunk。Monopoly 在 `debug=True` 时用这个，便于打印中间态。

### 1.4 Checkpointer（持久化）

[Checkpointer](https://langchain-ai.github.io/langgraph/concepts/persistence/) 让图能**中断后恢复**。常用的是 `SqliteSaver`：

```python
from langgraph.checkpoint.sqlite import SqliteSaver
saver = SqliteSaver.from_conn_string("checkpoints.db")
graph = workflow.compile(checkpointer=saver)
graph.invoke(state, config={"configurable": {"thread_id": "run-001"}})
```

每个节点完成后，state 被序列化进 SQLite，按 `thread_id` 索引。下次用同 `thread_id` 调用，会从最近的检查点续跑。Monopoly 把 `thread_id` 设成 `f"{ticker}-{trade_date}"`——同 ticker 同日的 run 会续跑，换日就是新 thread。

### 1.5 与 LangChain 的关系

LangChain 提供 `BaseChatModel`、`@tool` 装饰器、`with_structured_output` 等"模型层"原语；LangGraph 提供"编排层"。Monopoly 用 LangChain 来 bind LLM 和 schema，用 LangGraph 来串编排——两者各管一摊。

---

## 2. 系统鸟瞰

### 2.1 Monopoly 是什么

Monopoly 是 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) 的 fork，把目标从美股改成 **Binance 永续合约**。整体哲学（spec §1.2）：

> **LLM 只分析、不下单。每笔订单必须经过：（a）硬编码风控门 + （b）Discord 人工确认（OpenClaw bot）。**

人工确认落在 OpenClaw 层（PLAN.md T5，未完成）——**T5 落地前 executor 保持 dryrun/testnet，不切 mainnet**。

代码里的体现是 §10 的 futures tail——一段确定性的 Python 代码守在 LLM 决策和 Binance API 之间。

### 2.2 全图鸟瞰

整体 LangGraph 图分两段：

- **共用主链**（stock + crypto 都跑）：4 个 analyst → Bull/Bear 辩论 → Research Manager → Trader → 三方风险辩论 → Portfolio Manager。
- **Futures tail**（仅 crypto）：Risk Gate → Mark Price → Executor。stock 模式从 PM 直接到 END。

![整体决策流程](diagrams/full-graph.svg)

### 2.3 两层风控

| 层 | 实现 | 性质 | 谁能改 |
|---|---|---|---|
| L1 | PM prompt 教学（`portfolio_manager.py:159-208`） | LLM 自律 | LLM 自己 |
| L2 | Schema `Field(description=...)`（`schemas.py:371-381`） | LLM 自律 | 提示工程 |
| L3 | `risk_gate.evaluate`（`risk_gate.py:149-296`） | **硬编码** | 仅代码改 |
| L4 | 持仓对账已落地（`position_monitor.py`，§10.5）；告警脚本 TODO（PLAN T6） | 运维侧 | — |

**关键设计选择**：L2 的 Field constraint **不是 Pydantic 的 `le=0.01` 这种硬约束**，只是 description 里的自然语言。原因：Pydantic 硬约束会让 `structured_output` 调用抛 `ValidationError`，触发 free-text fallback，反而失去结构化重试。决策放在 L3。

---

## 3. 入口与生命周期

### 3.1 进程入口

两个入口：

- `main.py:1-16` —— 程序化入口。**默认 `asset_type="stock"`，不会走 futures tail**。
- `cli/main.py` —— CLI 入口，根据 ticker 自动选 `asset_type`。

### 3.2 `TradingAgentsGraph.__init__` —— `tradingagents/graph/trading_graph.py:55-137`

构造函数做 9 件事，按顺序：

1. **`self.config = config or DEFAULT_CONFIG`**（line 71）。
2. **`set_config(self.config)`**（line 75）—— 注入 dataflows 全局态，让数据源 fetcher 共享同一份配置。**这是全局可变状态**，多实例并跑时要小心。
3. **创建缓存/结果目录**（line 78-79）。
4. **构造两个 LLM**（line 88-99）——`deep_thinking_llm`（用于 Research Manager、Portfolio Manager）和 `quick_thinking_llm`（用于 analysts、debators、Trader）。provider 由 `config["llm_provider"]` 决定，工厂在 `llm_clients/factory.py`。
5. **`TradingMemoryLog`**（line 104）—— 跨 run 反思记忆。
6. **工具节点**（line 107，`_create_tool_nodes` line 161-197）—— 每个 analyst 对应一组 LangChain `@tool`，被包成 LangGraph `ToolNode`。`social` analyst 只有 `get_news`（Twitter 已 deferred；Reddit 侧完整降级链：OAuth（需凭证，T3b）→ www/old `.json` → `search.rss` → `new.rss`+客户端过滤 → `data_gap`。2026-07 实况：DC IP 的匿名 `.json` 被 Reddit 封锁，RSS 未封——数据经 RSS 恢复，但 RSS 无 score/评论数，engagement 字段为 0）。
7. **`ConditionalLogic`**（line 110-113）—— debate 循环的路由器。
8. **`GraphSetup.setup_graph(selected_analysts)`**（line 135）—— 构图（§5 详解）。
9. **`workflow.compile()`**（line 136）—— 编译。

### 3.3 `propagate` —— `trading_graph.py:300-339`

```python
def propagate(self, company_name, trade_date, asset_type="stock"):
```

签名注意：**`asset_type` 默认 "stock"**。必须显式传 `"crypto"` 才会触发 futures tail。

执行序：

1. **`_resolve_pending_entries`**（line 313）—— 把上一次同 ticker 的 pending decision（还没"知道结果"的）拉出来，用 yfinance 查持仓后 N 天的 raw/alpha return，扔给 reflector 生成反思，回写 memory log。crypto 路径目前会走这条但 yfinance 不一定能给 crypto 数据，行为待 Week 6 改造。
2. **checkpointer 可选启用**（line 316-321）—— 如果 `config["checkpoint_enabled"]`，用 `SqliteSaver` 持久化中间态。
3. **`_run_graph`**（line 334）—— 真正跑图。
4. **`finally`**（line 335-339）—— 退出 checkpointer 上下文，**重新编译一份无 checkpointer 的 graph**，避免下次调用复用脏状态。

### 3.4 `_run_graph` —— `trading_graph.py:341-390`

```python
init_agent_state = self.propagator.create_initial_state(
    company_name, trade_date, asset_type=asset_type, past_context=past_context
)
```

`create_initial_state`（`propagation.py:18-60`）只填了空字符串的报告槽位、空的 debate 状态、`messages=[("human", company_name)]`。**futures tail 用到的字段（`final_decision_structured`、`mark_price`、`execution_intent`、`execution_result`、`risk_gate_rejection_reason`、`equity_usd`）从未在初始化时填入**。

这意味着：
- gate 节点用 `state.get("equity_usd", starting_equity)` 兜底（`risk_gate.py:309`，默认 1000 USD）
- executor 节点同样兜底（`executor.py:567`）
- **当前没有任何代码动态把"账户实际余额"塞进 state**——所有 run 都按 starting_equity=1000 算 sizing。这是个 Week 5 OpenClaw 需要补的洞。

后续：
- `debug=True` 用 `graph.stream`（line 357），每节点一个 chunk，便于看中间态。
- 非 debug 用 `graph.invoke`（line 369），一把跑到底。
- `_log_state`（line 392-432）—— 把 final_state 落 `results_dir/{ticker}/TradingAgentsStrategy_logs/full_states_log_{date}.json`，**但只 dump stock 字段**，没有 dump `execution_result` / `execution_intent` / `risk_gate_rejection_reason`。**crypto run 的执行明细只在 JSONL event log 里**，复盘时要同时查 JSON（决策）+ JSONL（执行）。

### 3.5 checkpointer 的 thread_id 规则

`trading_graph.py:351-353`：

```python
tid = thread_id(company_name, str(trade_date))
args["config"]["configurable"]["thread_id"] = tid
```

`thread_id` 函数（`graph/checkpointer.py`）把 ticker + date 拼成稳定 id。同 ticker 同日的 run 续跑，换日就是新 thread——**回测时按日重跑是天然新 thread，不会续旧检查点**。

---

## 4. 状态对象 `AgentState`

文件：`tradingagents/agents/utils/agent_states.py`。

### 4.1 类型本质

```python
class AgentState(MessagesState):
    company_of_interest: Annotated[str, "..."]
    asset_type: Annotated[str, "..."]
    # ...
```

继承 `langgraph.graph.MessagesState`，所以**自带 `messages` 字段 + `add_messages` reducer**。其余字段都用 `Annotated[type, "description"]`——**只是类型注释，没有指定 reducer，所以是直接覆盖**。

**陷阱**：如果未来加并行节点同时写同一字段，会**静默丢失**——LangGraph 的默认覆盖语义就是后写覆盖先写，没有冲突告警。当前所有写入字段都是单写者模式，但加新节点时要保持。

### 4.2 字段清单

**Stock 主链**（line 47-74）：

| 字段 | 写入者 | 用途 |
|---|---|---|
| `company_of_interest` | initial | ticker |
| `asset_type` | initial | "stock" 或 "crypto" |
| `trade_date` | initial | 决策日 |
| `sender` | Trader/analysts | "谁说了上一句" |
| `market_report` / `sentiment_report` / `news_report` / `fundamentals_report` | 4 个 analyst | 各自分析报告 |
| `investment_debate_state` | Bull/Bear/Research Manager | 嵌套字典：bull_history / bear_history / count 等 |
| `investment_plan` | Research Manager | 5-tier rating + thesis |
| `trader_investment_plan` | Trader | TraderProposal 或 FuturesProposal 渲染后的 markdown |
| `risk_debate_state` | 3 个 debator + PM | 嵌套字典 |
| `final_trade_decision` | PM | markdown 决策 |
| `past_context` | initial | memory log 注入 |

**Futures 扩展**（line 81-92，仅 crypto path 写）：

| 字段 | 写入者 | 类型 |
|---|---|---|
| `final_decision_structured` | PM | **`Any`**（实际是 `FuturesDecision` Pydantic 对象，或 `None`） |
| `mark_price` | mark price 节点 | float 或 None |
| `equity_usd` | 当前未填，gate/executor 兜底 | float |
| `execution_intent` | Risk Gate | `asdict(ExecutionIntent)` dict 或 None |
| `execution_result` | Executor | `asdict(ExecutionResult)` dict 或 None |
| `risk_gate_rejection_reason` | Risk Gate | str 或 None |

### 4.3 `final_decision_structured: Any` 的坑

这个字段持有 **Pydantic 对象**，是 PM → Gate 之间唯一传结构化数据的通道。`Any` 类型让它能任意透传，但：

- **LangGraph state 在 checkpointer 启用时会序列化**。Pickle 兼容 Pydantic，但 JSON serializer 不一定。**上线启用 checkpoint 前必须验证 crypto path 能恢复**。
- 如果有人 refactor 时改成 `dict`，PM → Gate 之间需要重新 parse，破坏当前简洁链路。

---

## 5. 图拓扑

### 5.1 `GraphSetup.setup_graph` —— `tradingagents/graph/setup.py:43-188`

构图逻辑分四段：

1. **Analyst plan**（line 55-58 + `build_analyst_execution_plan`）：根据 `selected_analysts` 列表，给每个 analyst 生成 `(agent_node, tool_node, clear_node)` 三件套。
2. **节点注册**（line 82-96）：所有 analyst、Bull/Bear、Research Manager、Trader、3 个 risk debator、Portfolio Manager。
3. **边连接**（line 99-164）：见下面 4 个子拓扑图。
4. **Futures tail 条件连接**（line 166-186）：根据 `self.config` 是否 None 决定是否注册 Risk Gate / Mark Price / Executor 节点。**`config=None` 时，stock 模式 PM 直接到 END**（用于 stock-only 测试图）。

### 5.2 Analyst 自循环

每个 analyst 节点输出后，`ConditionalLogic.should_continue_*` 检查 `messages[-1].tool_calls`：

- 有 tool call → 跳到对应 `ToolNode`
- 没有 → 跳到 `Msg Clear *` 节点（清空消息历史）

`setup.py:114`：`workflow.add_edge(current_tools, current_analyst)` 把 ToolNode 连回 analyst —— 这就是循环。

![Analyst 工具循环](diagrams/analyst-loop.svg)

**关键认识**：循环没有显式上界，由 LLM 自己决定何时停止调工具。`Propagator.max_recur_limit=100`（`propagation.py:14`）是 LangGraph 兜底——超了抛 `GraphRecursionError`。

### 5.3 研究员辩论循环

`setup.py:122-138`：Bull 和 Bear 互相用条件边指向对方，直到 `ConditionalLogic.should_continue_debate` 返回 `"Research Manager"`。

`conditional_logic.py:52-61`：

```python
if state["investment_debate_state"]["count"] >= 2 * self.max_debate_rounds:
    return "Research Manager"
if state["investment_debate_state"]["current_response"].startswith("Bull"):
    return "Bear Researcher"
return "Bull Researcher"
```

`count` 每次 Bull/Bear 说完话 +1。`max_debate_rounds * 2` = 总轮数。**判断"上一个说话的是谁"用字符串前缀匹配**——脆弱：如果 researcher 的输出格式改了，会进死循环。

![研究员辩论](diagrams/researcher-debate.svg)

### 5.4 风险辩论循环

`setup.py:140-164`：Aggressive → Conservative → Neutral 三方轮转，直到 `should_continue_risk_analysis` 返回 `"Portfolio Manager"`。

`conditional_logic.py:63-73`：

```python
if state["risk_debate_state"]["count"] >= 3 * self.max_risk_discuss_rounds:
    return "Portfolio Manager"
if state["risk_debate_state"]["latest_speaker"].startswith("Aggressive"):
    return "Conservative Analyst"
if state["risk_debate_state"]["latest_speaker"].startswith("Conservative"):
    return "Neutral Analyst"
return "Aggressive Analyst"
```

同样的字符串前缀脆弱性。

![风险辩论](diagrams/risk-debate.svg)

### 5.5 Futures Tail

`setup.py:166-186`：

```python
if self.config is not None:
    workflow.add_node("Risk Gate", create_risk_gate_node(self.config))
    workflow.add_node("Mark Price", create_mark_price_node())
    workflow.add_node("Executor", create_executor_node(self.config))

    def _branch_after_pm(state):
        return "Risk Gate" if state.get("asset_type") == "crypto" else END

    workflow.add_conditional_edges("Portfolio Manager", _branch_after_pm, ...)
    workflow.add_edge("Risk Gate", "Mark Price")
    workflow.add_edge("Mark Price", "Executor")
    workflow.add_edge("Executor", END)
```

![Futures Tail](diagrams/futures-tail.svg)

---

## 6. Analyst 层

四个 analyst（market / social / news / fundamentals）行为同构：

1. 节点函数 = LangChain chain（prompt + `quick_thinking_llm.bind_tools(tools)`）
2. 输出 `AIMessage`，可能带 `tool_calls`
3. 有 tool_calls → `ToolNode` 执行 → 结果以 `ToolMessage` 追加进 `messages` → 回到 analyst → 再次 invoke
4. 无 tool_calls → 写自己的 `*_report` 字段 → 转到 `Msg Clear *` 节点
5. `Msg Clear *`（`create_msg_delete()`）清空消息历史 → 转到下一个 analyst

**为什么清消息**：四个 analyst 串行执行，共享 `messages` 列表。不清空，下一个 analyst 会看到上一个的 tool call 记录，污染 prompt。

**工具清单**（`trading_graph.py:161-197`）：

| Analyst | 工具 |
|---|---|
| market | `get_market_data`、`get_funding_rate`、`get_open_interest`、`get_indicators` |
| social | `get_news`（Twitter deferred；Reddit 数据经 RSS fallback 恢复，engagement 为 0，见 §3.2） |
| news | `get_news`、`get_global_news`、`get_insider_transactions` |
| fundamentals | `get_fundamentals`、`get_balance_sheet`、`get_cashflow`、`get_income_statement` |

`get_market_data` / `get_funding_rate` / `get_open_interest` 是 crypto-specific，stock 模式 fallback 到 yfinance 兼容包装。

---

## 7. Researcher → Manager → Trader

### 7.1 Bull / Bear Researcher

`agents/researchers/bull_researcher.py` 和 `bear_researcher.py`。结构：

- 读 4 份 analyst 报告 + 上一轮对方发言 + debate history
- 直接 `llm.invoke(prompt)`，**没有 structured output**——这两个就是辩手，输出自然语言
- 写 `investment_debate_state["bull_history"]` 或 `["bear_history"]`，把 `count += 1`
- `current_response` 设为本轮发言（前缀 "Bull Analyst:" 或 "Bear Analyst:" 用于路由判断）

### 7.2 Research Manager —— `agents/managers/research_manager.py:1-67`

第一个用 `structured_output` 的节点：

```python
structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

investment_plan = invoke_structured_or_freetext(
    structured_llm, llm, prompt, render_research_plan, "Research Manager",
)
```

`ResearchPlan`（`schemas.py`）是 5-tier rating（Buy / Overweight / Hold / Underweight / Sell）+ executive summary + thesis 的 Pydantic 模型。

写 state：
- `investment_plan`（markdown）
- `investment_debate_state["judge_decision"]`

### 7.3 Trader —— `agents/trader/trader.py:1-150`

根据 `asset_type` 分流：

- **stock**：bind `TraderProposal`（Buy/Hold/Sell + entry/stop/sizing 文本）
- **crypto**：bind `FuturesProposal`（`schemas.py:259-317`）

`FuturesProposal` 字段：`side`（Long/Short/Flat）、`reasoning`、`entry_price?`、`stop_loss?`、`take_profit?`、`leverage?`、`position_size_pct?`。

**和 PM 的 `FuturesDecision` 的区别**：
- `Proposal` 是 Trader 的初稿
- `Decision` 是 PM 综合风险辩论后的最终版
- 两者字段几乎相同，但 `Decision` 额外有 `executive_summary` / `investment_thesis` / `time_horizon`
- 风控门只看 `Decision`，不看 `Proposal`

**Trader 的 prompt**（line 106-145）含一张映射表，把 Research Manager 的 rating 转 side：

| Rating | Side |
|---|---|
| Buy | Long（高 conviction） |
| Overweight | Long（低 conviction） |
| Hold | Flat |
| Underweight | Short（低 conviction） |
| Sell | Short（高 conviction） |

---

## 8. 风险辩论 → Portfolio Manager

### 8.1 Aggressive / Conservative / Neutral —— `agents/risk_mgmt/*_debator.py`

三个文件结构同构，以 `aggressive_debator.py` 为例：

- 读 `risk_debate_state["history"]`、对方上轮发言、4 份 analyst 报告、Trader 的 proposal
- 拼 prompt，**`llm.invoke(prompt)`** 直接调用，不用 structured
- 输出文本，prepend `"Aggressive Analyst: "`
- 写 state：
  - `history += "\n" + argument`
  - `aggressive_history += "\n" + argument`
  - `latest_speaker = "Aggressive"`（驱动下一步路由）
  - `current_aggressive_response = argument`
  - `count += 1`

**关注点**：三个 debator 共用 quick_thinking_llm，**每轮调一次**，所以 `max_risk_discuss_rounds=1` 时总共 3 次 LLM 调用。

### 8.2 Portfolio Manager —— `agents/managers/portfolio_manager.py:42-123`

**这是 stock/crypto 分叉点**。

```python
def create_portfolio_manager(llm):
    structured_stock = bind_structured(llm, PortfolioDecision, "Portfolio Manager")
    structured_futures = bind_structured(llm, FuturesDecision, "Portfolio Manager")
```

工厂在 closure 外层 bind 两份 structured LLM。返回 `portfolio_manager_node(state)`。

节点函数体（line 46-121）：

1. **读 asset_type 分流**（line 47）。默认 "stock"。
2. **`build_instrument_context`**：渲染 ticker + asset_type 的 prompt 头部。
3. **拼 prompt 上下文**（line 50-60）：
   - `risk_debate_state["history"]`：三方辩论
   - `investment_plan`：Research Manager 的 plan
   - `trader_investment_plan`：Trader 的 proposal
   - `past_context`：memory log 历史经验
4. **`structured_decision = None`**（line 62）——默认值。crypto path 的 try 会赋值，stock path 保持 None。
5. **crypto 分支**（line 63-94，T0 已提交）：

```python
if asset_type == "crypto":
    prompt = _build_crypto_prompt(...)
    if structured_futures is None:
        # provider 不支持 structured output（如老 Ollama 模型）→ 直接 free text
        response = llm.invoke(prompt)
        final_trade_decision = response.content
        structured_decision = None
    else:
        try:
            structured_decision = structured_futures.invoke(prompt)
            final_trade_decision = render_futures_decision(structured_decision)
        except Exception as exc:
            logger.warning("...")
            response = llm.invoke(prompt)
            final_trade_decision = response.content
            structured_decision = None
```

**这处改动的意图**（T0 提交，`portfolio_manager.py`）：之前 try/except 把"provider 不支持"和"调用失败"混在一起，都会触发"failed" warning，对前者不准确。现在分开：

- `structured_futures is None`（bind 时就失败）→ 直接走 free text，不打 warning（`bind_structured` 已经在构造时告警过）
- `structured_futures.invoke` 抛异常 → 打 "failed" warning，再 fallback

**为什么 PM 不直接用 `invoke_structured_or_freetext` helper**：helper 只 return string，但 risk gate 需要**结构化对象本身**（`FuturesDecision`）才能读 `leverage` / `position_size_pct`。PM 手动 catch 保留对象引用，写到 `state["final_decision_structured"]`。

6. **stock 分支**（line 95-108）：用通用 helper。
7. **回填 risk_debate_state**（line 110-121）。
8. **return**：写三个状态字段（line 123-127）。

### 8.3 `_build_crypto_prompt` —— `portfolio_manager.py:159-208`

PM 的 L1 防线（教学层）。逐段意图：

- **Decision shape**（line 173-184）：自然语言重述 schema 字段。**反复举例 0.003 / 0.005 / 0.010 三档**，警告 `0.1 = 10% = REJECTED`。
- **How to ratify or adjust**（line 185-189）：三种行为模板（支持 Trader / 反对 Trader / 平衡）。希望 PM 不要无理由推翻 Trader。
- **Hard rules**（line 190-196）：5 条硬规则。第 3、4 条直接对应 gate 的天花板。
- **Context**（line 199-204）：注入研究 / 交易 / 记忆 / 辩论历史。

`position_size_pct ≤ 0.01` 这条约束在 prompt 里**重复了 4 次**（schema description + 3 处 prompt）。这是 commit `3bfa053` 强化的——LLM 对一次性约束容易忽略，多次重复显著降低出错率。

---

## 9. Structured Output 模式

文件：`tradingagents/agents/utils/structured.py`。

### 9.1 `bind_structured(llm, schema, agent_name)` —— line 31-45

```python
try:
    return llm.with_structured_output(schema)
except (NotImplementedError, AttributeError) as exc:
    logger.warning("%s: provider does not support ...", agent_name, exc)
    return None
```

LangChain 把 schema 注入 provider 的 native structured output 机制：

- OpenAI: `response_format={"type": "json_schema", ...}`
- Anthropic: tool-use 工具调用强制返回 JSON
- Google: `response_schema`

失败返回 None，agent 后续走 free-text。**只在 agent 构造时调用一次**，结果缓存在 closure 里。

### 9.2 `invoke_structured_or_freetext(...)` —— line 48-73

```python
if structured_llm is not None:
    try:
        result = structured_llm.invoke(prompt)
        return render(result)
    except Exception as exc:
        logger.warning(...)
response = plain_llm.invoke(prompt)
return response.content
```

三层防线：
1. bind 时支持结构化
2. invoke 时未抛异常
3. 都失败时降级 free-text

return 类型永远是 `str`（render 后的 markdown 或 free-text）。**结构化对象本身没被传出来**——所以 PM crypto path 不能用这个 helper。

---

## 10. Futures Tail（关键路径）

### 10.1 Risk Gate

文件：`tradingagents/futures/risk_gate.py`。结构：纯函数 `evaluate` + 节点工厂 `create_risk_gate_node`。

#### 10.1.1 `RiskGateConfig` —— line 43-93

dataclass，frozen=True。字段及默认：

| 字段 | 默认 | 含义 |
|---|---|---|
| `max_leverage` | 3.0 | 杠杆上限 |
| `per_trade_risk_pct` | 0.01 | 单笔 risk 上限（1%） |
| `daily_drawdown_halt_pct` | 0.03 | 日内回撤熔断（3%） |
| `cooldown_after_loss_minutes` | 60 | 止损出场后冷静期 |
| `max_concurrent_positions` | 2 | 并发仓位上限 |
| `state_path` | `~/.tradingagents/risk_gate_state.jsonl` | 事件日志 |

`from_config(config: dict)`（line 78-93）从 Monopoly config dict 构造，所有字段都从 `futures_*` 前缀读取。`default_config.py:137-142` 是默认值的源头。可由 `TRADINGAGENTS_FUTURES_*` 环境变量覆盖（`default_config.py:21-26`）。

#### 10.1.2 `evaluate(...)` 详读 —— line 149-296（**已更新**）

签名：

```python
def evaluate(
    decision: FuturesDecision, *,
    symbol: str,
    equity_usd: float,
    config: RiskGateConfig,
    now: Optional[datetime] = None,
    snapshot: Optional[RiskGateSnapshot] = None,
) -> GateResult:
```

**关键字参数强制**（`*` 后），调用点不会传错位置。`now` 和 `snapshot` 可注入——便于测试。

执行序：

```
180-183  时区检查：now 必须 tz-aware UTC
185-186  snapshot 为 None 时从 JSONL 派生
188-191  规则 1: side==Flat → REASON_FLAT
194-200  规则 2: 必填字段（leverage / position_size_pct / stop_loss）
202-228  规则 2.5（新加）: 正性 / 下界
            - leverage < 1.0 → REASON_LEVERAGE_BELOW_MIN
            - position_size_pct <= 0 → REASON_RISK_NON_POSITIVE
            - stop_loss <= 0 → REASON_STOP_NON_POSITIVE
230-244  规则 3: 天花板
            - leverage > max_leverage → REASON_LEVERAGE_OVER_CAP
            - position_size_pct > per_trade_risk_pct → REASON_RISK_OVER_CAP
246-251  规则 4: 止损方向（仅当 entry_price 已设时）
            - Long & stop >= entry → REASON_STOP_WRONG_SIDE
            - Short & stop <= entry → REASON_STOP_WRONG_SIDE
253-261  规则 5: 日内回撤熔断
            - snapshot.daily_realised_pnl_usd <= -drawdown_halt_pct * equity → halt
263-274  规则 6: 冷静期
            - last_stop_loss_close_ts + cooldown_minutes > now → halt
276-282  规则 7: 并发仓位上限
            - snapshot.open_positions >= max_concurrent_positions → halt
284-294  通过：构造 ExecutionIntent (uuid4 + 当前时间)
```

**T0 新加的"规则 2.5"**：原本 gate 只校验上界，零或负值会"通过所有上界检查"再到 executor 爆。例如 `leverage=0` 会让 `compute_sizing` 里 `margin = notional / leverage` 抛 `ZeroDivisionError`，负 `risk_pct` 会让 qty 是负数。Gate 是 floor，应该当场拒绝。

**规则 4 的限制**：止损方向**只在 `entry_price` 已设时校验**。市场单（`entry_price=None`）gate 不知道实际成交价，无法校验。**但 executor 里的 `compute_sizing` 现在补了这条**（§10.3.1）。

**规则 5、6、7 依赖 `snapshot`**：snapshot 由 `derive_state` 从 JSONL 事件派生（§10.4.2）。`position_closed` 事件现在由 position monitor 对账写入（§10.5，T1 已落地，2026-07-18 testnet 实弹验证），并发仓位计数不再单调增长。**两个已知限制**：① 对账写入的 `position_closed` 带 `pnl_usd=0.0`——日回撤熔断（规则 5）看不到这些亏损；② outcome 由启发式推断，默认 `"stop"`——会触发冷静期（规则 6），方向上偏保守而非漏防，可接受但要知情。

#### 10.1.3 `create_risk_gate_node(config)` —— line 304-353

LangGraph 节点工厂。读 state：

- `state["asset_type"]`（非 crypto 直接 no-op）
- `state["final_decision_structured"]`（None → 跳过并写 trade_skipped）
- `state["company_of_interest"]` → symbol
- `state.get("equity_usd", starting_equity)` → 兜底 1000

调用 `evaluate`，写 state：
- `execution_intent`: `asdict(intent)` 或 None
- `risk_gate_rejection_reason`: str 或 None

拒绝时调 `_log_skip`（line 356-360）写 `trade_skipped` 到 JSONL。

### 10.2 Mark Price —— `tradingagents/futures/market_data.py:52-74`

简单。读 `state["execution_intent"]`：

- `None` → 写 `mark_price=None`
- `intent["entry_price"]` 已设（限价单）→ 复用 entry_price，不发 HTTP
- 市场单 → `fetch_mark_price(symbol)` 命中 Binance fapi `premiumIndex`（5s 超时，**mainnet endpoint，不需要 API key**）

失败返回 None，executor 看到 None 会写 `trade_skipped`。

**关注点**：Mark Price 用 **mainnet**，executor 是 **testnet**。两者价差在大波动时几个点，影响 sizing 精度。短期可接受，上 mainnet 后建议同源。

### 10.3 Executor

文件：`tradingagents/futures/executor.py`（**改动最大的文件，差不多重写了 testnet 路径**）。

#### 10.3.1 `compute_sizing` —— line 95-150（**已更新**）

```python
def compute_sizing(intent, *, equity_usd, mark_price) -> tuple[float, float, float, float]:
    reference_price = intent.entry_price if intent.entry_price is not None else mark_price

    # 新加：止损方向校验（市场单的最后防线）
    if intent.side == FuturesSide.LONG and intent.stop_loss >= reference_price:
        raise ValueError(f"long stop_loss {...} >= reference price {...}")
    if intent.side == FuturesSide.SHORT and intent.stop_loss <= reference_price:
        raise ValueError(f"short stop_loss {...} <= reference price {...}")

    risk_usd = intent.risk_pct * equity_usd
    risk_per_unit = abs(reference_price - intent.stop_loss)
    if risk_per_unit <= 0:
        raise ValueError("stop_loss equals entry — risk per unit is zero")
    qty = risk_usd / risk_per_unit
    notional = qty * reference_price
    margin_required = notional / intent.leverage
    return reference_price, qty, notional, margin_required
```

**核心 sizing 公式**：

```
qty × |entry − stop|  =  equity × risk_pct
```

即"按 entry/stop 间距分配数量，使被止损时损失恰好 = 配置 risk 比例"。

**新增的方向校验**：之前提到 gate 规则 4 只对限价单生效，市场单方向反了会"开仓后 Binance 拒绝挂止损（-2021 immediately trigger）→ 仓位裸奔"。现在 `compute_sizing` 拿到 reference_price（市场单时 = mark_price）后立刻校验方向，错了就抛 ValueError，两个 adapter 的 except 路径都会把它降级成 `success=False`。**这是 §12 风险表里 #5 的补丁**。

**leverage 只影响 margin，不影响 qty**——所以 leverage=3 vs 1 时，相同 qty 占用 1/3 保证金，风险不变，资金占用减少。

#### 10.3.2 DryRunExecutor —— line 161-247

简单：算 sizing → 校验 margin ≤ equity → 拍一条 JSON 进 `orders_log_path` → 返回 success=True。**不 mutate cross-run state**。

但 `executor_node`（line 502-635）会在 dryrun 成功后**也写 `position_opened`**——所以 dryrun 跑多了 `open_positions` 计数也会顶满 ceiling。**dryrun 测试时要清空 `~/.tradingagents/risk_gate_state.jsonl`**（或跑一遍 dryrun 模式的 position monitor：`DryrunExchange` 返回空持仓，reconcile 会把 JSONL 里所有 open 仓位补写 `position_closed`，见 §10.5），否则后续 run 全被 `REASON_MAX_POSITIONS` 拒绝。

#### 10.3.3 TestnetExecutor —— line 252-490（**重写**）

**最大变化**：原来一个 try 块包了"开仓 + 止损 + 止盈"三步，中途任一失败 → 返回 success=False，但开仓单已成交，仓位裸奔。**现在两阶段 + 紧急平仓**。

##### 阶段 0：守门 + sizing —— line 263-294

```python
# 1. validation-phase: LIMIT entry 禁用
if intent.entry_price is not None:
    return self._error_result(intent, error="LIMIT entry disabled in validation phase ...")
```

**为什么禁用 LIMIT**：在 fill-then-protect monitor 还没做出来之前，reduceOnly 的 stop/TP 在 LIMIT 单未成交时**不能挂**（Binance 拒绝），所以无法在挂单时就把保护单贴上。LIMIT 成交瞬间到挂 stop/TP 之间有个时间窗，仓位裸奔。**Week 5 OpenClaw + position monitor 落地前，只支持市场单**。

```python
# 2. sizing
reference_price, qty, _notional, _margin = compute_sizing(intent, ...)

# 3. 向下取整 + leverage 向下取整
leverage_int = max(1, int(intent.leverage))   # 2.7 → 2
qty_rounded = self._round_qty(qty)             # math.floor(qty / 0.001) * 0.001
if qty_rounded <= 0:
    return self._error_result(...)  # qty 被 floor 到 0 → 拒

# 4. 重新算 notional / margin（用 rounded 值），再校验 margin ≤ equity
notional_rounded = qty_rounded * reference_price
margin_required = notional_rounded / leverage_int

if margin_required > equity_usd:
    return self._error_result(...)
```

**为什么用 `_round_qty` 改成 `math.floor`**（line 482-498）：原本是 `round`，可能向上凑。如果向上 round 后 margin 超 equity 但 check 用未 round 的 qty 算（更小），会通过 check 但真实下单超额。改成 floor + 用 rounded 值重算 margin = 双保险。

**为什么 leverage 用 `int(intent.leverage)` 而非 `int(round(...))`**：原本 round 会把 2.7 凑成 3，比 PM 意图更激进。floor 成 2 更保守。

##### 阶段 1：开仓 —— line 296-313

```python
try:
    self.client.futures_change_leverage(symbol=binance_symbol, leverage=leverage_int)
    open_resp = self.client.futures_create_order(
        symbol=binance_symbol, side=binance_side,
        type="MARKET", quantity=qty_rounded,
    )
    open_order_id = _extract_order_id(open_resp, "open")
except Exception as exc:
    return self._error_result(intent, error=f"open failed: ...")
```

**注意**：这个 try 块**只包阶段 1**。失败 = 仓位没开 = 干净退出。`futures_change_leverage` 失败也安全（不改仓位）。

##### 阶段 2：保护单 —— line 315-385

```python
try:
    stop_resp = self.client.futures_create_order(
        symbol=binance_symbol, side=close_side,
        type="STOP_MARKET",
        stopPrice=str(intent.stop_loss),
        quantity=qty_rounded,
        reduceOnly=True,
        workingType="MARK_PRICE",
    )
    stop_order_id = _extract_order_id(stop_resp, "stop_loss")

    tp_resp = None
    tp_order_id = None
    if intent.take_profit is not None:
        tp_resp = self.client.futures_create_order(... type="TAKE_PROFIT_MARKET" ...)
        tp_order_id = _extract_order_id(tp_resp, "take_profit")

except Exception as exc:  # 保护单失败 → 仓位裸奔，紧急 unwind
    unwound, unwind_err = self._try_unwind(binance_symbol, close_side, qty_rounded)
    if unwound:
        return self._error_result(
            intent,
            error=f"protective order failed, position unwound: ...",
            ...
        )
    return self._error_result(
        intent,
        error=f"protective order failed AND unwind failed — POSITION NAKED, manual intervention required. ...",
        position_naked=True,   # ⭐
        ...
    )
```

**这是最关键的修复**。流程：
1. 保护单失败时，**立刻调 `_try_unwind`** 用反向 MARKET reduceOnly 紧急平仓
2. unwind 成功 → 返回 success=False（仓位干净退出，钱可能小亏一点）
3. unwind 也失败 → 返回 `position_naked=True`，让上层节点写 `position_naked` 事件 + 错误日志

##### `_try_unwind` —— line 387-407

```python
def _try_unwind(self, binance_symbol, close_side, qty):
    try:
        self.client.futures_create_order(
            symbol=binance_symbol,
            side=close_side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True,
        )
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
```

简洁。`reduceOnly=True` 保证不会反向开新仓。

#### 10.3.4 `executor_node` 对 naked 的处理 —— line 502-635（**新加**）

```python
if result.success:
    append_event(state_path, {"type": "position_opened", ...})
elif result.position_naked:
    # 即使 success=False，仓位仍然在交易所里活着
    # 1. 仍然写 position_opened（让 gate 的 open_positions 计数包含它，阻止新仓）
    # 2. 额外写 position_naked 事件（告警）
    # 3. logger.error 同步打日志
    append_event(state_path, {"type": "position_opened", ...})
    append_event(state_path, {"type": "position_naked", ...})
    logger.error("NAKED POSITION on %s ...", ...)
else:
    append_event(state_path, {"type": "trade_skipped", ...})
```

**关键认识**：naked 时**必须写 `position_opened`**——否则 gate 永远以为这仓位不存在，会继续接受新仓，把账户拉爆。同时 `position_naked` 是给运维的告警事件，可以被外部脚本扫到后推 Discord（T6 告警落地后接通）。

#### 10.3.5 `_extract_order_id` —— line 642-665

```python
def _extract_order_id(response, label) -> str:
    if not isinstance(response, dict):
        raise RuntimeError(f"{label} order response not a dict: ...")
    for key in ("orderId", "algoId"):
        if key in response:
            return str(response[key])
    raise RuntimeError(f"{label} order response missing orderId/algoId: ...")
```

接受 `orderId`（普通单）或 `algoId`（条件单/算法单）。**失败显式抛**——commit `204b6ad` 的 fail-loud 哲学。在此之前可能静默吞掉 → 仓位裸奔。

**algo 单的程序化撤销已解决**（T2）：当前安装的 python-binance 原生暴露 `futures_get_open_algo_orders` / `futures_cancel_algo_order`（需 symbol + algoId）/ `futures_cancel_all_algo_open_orders`，封装在 position monitor 的 `TestnetExchange` adapter 里（§10.5），2026-07-18 testnet 实测一发清掉 4 张 algo 单。**注意**：executor 的 naked-position unwind 路径目前**没有**接 algo 撤单——只有对账路径会清孤儿 algo 单。

### 10.4 JSONL 事件日志

文件：`tradingagents/futures/risk_state.py`。

#### 10.4.1 `append_event` —— line 53-67（**docstring 已更新**）

```python
def append_event(path, event):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":"), sort_keys=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
```

文档解释了并发原子性：

> Each event is one line written with a single `write()` to a file opened in append mode. On local POSIX filesystems an `O_APPEND` write of a small buffer is effectively atomic — the kernel resolves the file offset and copies the buffer under the inode lock — so concurrent appenders do not interleave partial lines. (This is a filesystem property, not the PIPE_BUF guarantee, which is about pipes/FIFOs.)

之前的 docstring 引用 `PIPE_BUF` 是错的（那是 pipe 语义）；改成正确的 `O_APPEND` + inode lock 解释。

#### 10.4.2 `derive_state` —— line 86-122

```python
def derive_state(events, *, now) -> RiskGateSnapshot:
    today_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    open_count = 0
    daily_pnl = 0.0
    last_stop_close_ts = None

    for ev in events:
        if ev["type"] == "position_opened":
            open_count += 1
        elif ev["type"] == "position_closed":
            open_count -= 1
            ts = _parse_ts(ev["ts"])
            if ts >= today_start:
                daily_pnl += float(ev.get("pnl_usd", 0.0))
            if ev.get("outcome") == "stop":
                if last_stop_close_ts is None or ts > last_stop_close_ts:
                    last_stop_close_ts = ts
        # trade_skipped / position_naked 被忽略

    return RiskGateSnapshot(
        open_positions=max(0, open_count),
        daily_realised_pnl_usd=daily_pnl,
        last_stop_loss_close_ts=last_stop_close_ts,
    )
```

**几个细节**：

- **窗口边界**：今天 00:00 UTC 起的 PnL 才计入 daily_pnl。**回撤熔断按 UTC 日**。
- **`max(0, open_count)`**：防御性下限，closed > opened 时不抛错。
- **`outcome == "stop"`**：只有止损出场才进入冷静期。手动平仓 / 止盈不触发。
- **`trade_skipped` 被忽略**——它只为审计存在。
- **`position_naked` 也被忽略**——它和 `position_opened` 同时被写（§10.3.4），open_count 已经 +1，naked 事件本身只用于外部告警。

**关键认识（2026-07 更新）**：`position_closed` 现在由 position monitor 对账写入（§10.5，T1 已落地）。open_positions 计数在对账后正常回落，`last_stop_close_ts` 会被填上（outcome 启发式默认 `"stop"` → 冷静期生效）。**残留缺口**：对账事件的 `pnl_usd=0.0`，所以 daily_pnl 仍看不到交易所侧止损/止盈带来的真实盈亏——回撤熔断（规则 5）对这类平仓依旧失明。这是验证阶段知情接受的限制，精确 PnL 需查订单历史（未做）。

### 10.5 Position Monitor（T1+T2 新增，2026-07-14 合并）

文件：`tradingagents/futures/position_monitor.py`。补上 §10.4.2 曾标记的最高优先级缺口：stop/TP 在 Binance 触发后，交易所侧仓位已平，但本地 JSONL 没人写 `position_closed` → gate 按 `max_concurrent_positions` 误拒新仓 + 孤儿保护单累积（下次下单 `-4130` 错误）。

**设计**：
- **独立于 LangGraph**——可被 launchd 周期调用，也可作为每次 run 前的 pre-step。
- 核心函数 `reconcile_positions(jsonl_path, exchange)`：从 JSONL 回放出 open 仓位集合（`position_opened` 减 `position_closed`）→ `futures_position_information` 拉交易所实况 → 对「本地 open、交易所已平」的仓位补写 `position_closed` 事件，并撤掉该 symbol 的孤儿单：Basic 单走 `futures_cancel_all_open_orders`，algo 单走 `futures_cancel_all_algo_open_orders`（先 `futures_get_open_algo_orders` 拿准确数量）。
- **`ExchangeAdapter` protocol 双实现**：`TestnetExchange`（真实 python-binance client）/ `DryrunExchange`（返回空持仓，单测不联网）。工厂 `create_monitor(config)` 按 `MONITOR_MODE` env > `config["futures_monitor_mode"]` 选择，默认 dryrun。
- **平仓来源推断** `_infer_close_outcome`：启发式——交易所已无仓位时默认 `"stop"`（保守：触发 gate 冷静期）；精确区分 stop / tp 需查订单历史，未做。

**2026-07-18 testnet 实弹验证**：开仓（带 stop/TP）→ reduceOnly 平仓模拟交易所侧关闭 → reconcile 正确写入 `position_closed`、孤儿单清零、账户状态干净。已知限制（`pnl_usd=0.0`、outcome 启发式）见 §10.1.2 / §10.4.2 注记。

### 10.6 OpenClaw 侧接口（T4 新增，2026-07-14 合并）

Week 5 方案 A 的 Monopoly 侧已完成，OpenClaw 侧安装是手工步骤：

- `python -m cli.main analyze_json`：**强制 dryrun** 跑完整分析，JSON 决策打到 stdout（日志全走 stderr），序列化在 `cli/json_output.py`。
- `python -m cli.main trade_review --symbol --hours`：纯读 memory log + risk JSONL 的历史汇总（`tradingagents/futures/trade_review.py`：近期决策 / 持仓 / gate 拒绝原因统计），**无 LLM 调用**。
- `openclaw/skills/trade_analyze`、`openclaw/skills/trade_review` 两份 SKILL.md，待拷到 Mac mini 的 OpenClaw skill 目录并配置 **Discord bot**（交互渠道已定为 Discord，非 Telegram）。
- `trade_execute` + 人工审批闭环是 **T5，未做**——mainnet 的硬前置。

---

## 11. 端到端数据流

下图展示一次成功的 crypto run 中，关键字段如何在节点间流动：

![数据流时序](diagrams/data-flow.svg)

---

## 12. 上线前盲点（已修 vs 未修）

对比上一版走读笔记的 6 条阻断/高风险项：

| # | 原描述 | 当前状态 | 证据 |
|---|---|---|---|
| 1 | `position_closed` 没人写，3 条 cross-run 规则失效 | **已修**（T1，7-18 实弹验证） | `position_monitor.py` 对账写入；残留：`pnl_usd=0.0` → 回撤熔断仍看不到交易所侧平仓的真实亏损（§10.5） |
| 2 | 开仓成功后保护单失败 → 仓位裸奔，无 rollback | **已修** | `executor.py:315-385` two-phase + `_try_unwind` + `position_naked` 事件 |
| 3 | gate 频繁拒绝无告警（L4 缺位） | **未修**（PLAN T6） | 仍需 JSONL 日扫脚本 + Discord 推送 |
| 4 | mark_price 用 mainnet，executor 用 testnet | **未修** | `market_data.py:25` 仍是 `fapi.binance.com` |
| 5 | `final_decision_structured` 在 LangGraph state 透传，checkpointer 序列化未验证 | **未验证** | 建议跑一遍 `checkpoint_enabled=True` 的 crypto path |
| 6 | `_round_qty` 硬编码 step=0.001 | **未修但更稳健** | 改成 floor 而非 round（`executor.py:482-498`），BTC/ETH 可接受；扩 symbol 前必须改 |

**T0 提交的新增防御**（上一版未列出的潜在风险）：

| 改动 | 文件 | 风险 |
|---|---|---|
| gate 加 `leverage < 1 / risk_pct ≤ 0 / stop_loss ≤ 0` 正性检查 | `risk_gate.py:205-228` | 防 LLM free-text fallback 给负值或零值，避开上界检查后在 executor 爆 |
| `compute_sizing` 加止损方向校验（含市场单） | `executor.py:117-133` | 修复 gate 规则 4 对市场单失效的盲区 |
| testnet LIMIT entry 直接拒 | `executor.py:266-273` | 在没有 fill-then-protect monitor 之前避免裸奔 |
| leverage / qty 一律向下取整 + 用 rounded 值重算 margin | `executor.py:280-294` | 避免向上 round 导致 margin 超额 |

---

## 13. LangGraph 文档地址清单

按文中提到的顺序：

- **首页**：<https://langchain-ai.github.io/langgraph/>
- **概念：StateGraph、State、Node、Edge、Reducer**：<https://langchain-ai.github.io/langgraph/concepts/low_level/>
- **MessagesState 与 `add_messages` reducer**：<https://langchain-ai.github.io/langgraph/concepts/low_level/#messagesstate>
- **条件边（Conditional Edges）**：<https://langchain-ai.github.io/langgraph/how-tos/branching/>
- **ToolNode**：<https://langchain-ai.github.io/langgraph/reference/agents/#langgraph.prebuilt.tool_node.ToolNode>
- **持久化（Checkpointer）**：<https://langchain-ai.github.io/langgraph/concepts/persistence/>
- **`SqliteSaver` 参考**：<https://langchain-ai.github.io/langgraph/reference/checkpoints/#langgraph.checkpoint.sqlite.SqliteSaver>
- **流式输出（streaming）**：<https://langchain-ai.github.io/langgraph/concepts/streaming/>
- **LangChain 的 `with_structured_output`**：<https://python.langchain.com/docs/how_to/structured_output/>

---

## 附录 A：建议的走读顺序（按风险加权）

如果你时间紧，按这个顺序读：

1. **§10.3** Executor（最近改动最大、money 最近）
2. **§10.1** Risk Gate（last line of defense）
3. **§8.2** Portfolio Manager（LLM 最容易出错的节点）
4. **§3.3** propagate（entry / asset_type 分流）
5. **§5.5** Futures tail 拓扑（确认条件边没旁路）
6. **§10.4–10.5** JSONL 事件 + position monitor 对账（重点看 `pnl_usd=0.0` 的残留影响）
7. 其余按需

## 附录 B：上线前必跑的 testnet 演练

1. ~~**正常路径**：crypto run 让 LLM 输出 Long → 看到 Binance testnet 真实开仓 + 止损 + 止盈三单挂出~~ ✅ 已覆盖（06-02 完整 graph 实弹 + 07-18 开仓/平仓/对账验证）
2. **gate 拒绝路径**：手动改 prompt 让 LLM 输出 `position_size_pct=0.5`，看到 gate 拒绝 + JSONL 写 `trade_skipped`
3. **方向反路径**：手动改 prompt 让 LLM 输出 Long 但 stop > entry，看到 executor 在 `compute_sizing` 拒绝（而不是在 Binance API 上失败）
4. **naked 模拟路径**：用 mock 让 stop 单的 `_extract_order_id` 抛 RuntimeError，验证 `_try_unwind` 真的发了反向 MARKET 单
5. **checkpointer 路径**：`checkpoint_enabled=True` 跑一个 crypto run，中途 Ctrl+C，重启验证能从最后节点恢复（重点看 `final_decision_structured` 反序列化是否完整）
