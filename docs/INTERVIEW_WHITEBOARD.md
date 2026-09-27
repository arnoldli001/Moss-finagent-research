# 面试白板速查 · 3 个核心模块（可手绘 + 接口契约 + 性能指标）

> 用途：面试现场被要求"画一下 / 讲一下"时直接照着画、照着说。
> 纪律：**所有契约与数字都从项目实际代码/审计报告抄出**，不是编的。
> 路径对照：`src/infrastructure/llm/circuit_breaker.py`、`src/domain/agents/analysis/react.py`、`src/core/state.py`、`src/orchestration/supervisor.py`

---

# 模块 1 · 熔断状态机（TimeWindowCircuitBreaker）

## 1.1 手绘版（白板/纸上照这个画）

```
                    ┌─────────────────────────────────────────┐
                    │          CLOSED（正常放行）              │
                    │  · allow_request() → True                │
                    │  · record_failure() 记入 deque[时间戳]    │
                    │  · _gc_failure_window() 清窗口外旧失败    │
                    └─────────────────────────────────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │ 窗口内失败数 ≥ failure_threshold ?   │
                    │  deepseek: 3 次 / 60s                │
                    │  ollama:   5 次 / 60s                │
                    └──────────────────┬──────────────────┘
                             是 │              │ 否
                                ▼              └──► 留在 CLOSED
                    ┌─────────────────────────────────────────┐
                    │           OPEN（熔断中）                  │
                    │  · allow_request() → False               │
                    │  · 跳过 API 调用，立即降级备模型           │
                    │  · total_rejected += 1                   │
                    └─────────────────────────────────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │ now - last_failure_ts ≥ cooldown ?  │
                    │  deepseek: 30s   ollama: 15s        │
                    └──────────────────┬──────────────────┘
                             是 │
                                ▼
                    ┌─────────────────────────────────────────┐
                    │        HALF_OPEN（探测阶段）              │
                    │  · allow_request() → True（放行探测）     │
                    │  · half_open_successes = 0（重置）        │
                    └─────────────────────────────────────────┘
                          │                          │
              record_success()              record_failure()
                          │                          │
        ┌─────────────────┴──────────┐              ▼
        │ successes ≥ needed ?        │     ┌──────────────┐
        │ deepseek: 2  ollama: 1      │     │ 立即回 OPEN   │
        └────────────┬────────────────┘     └──────────────┘
                是 │        否
                   ▼         └──► 留在 HALF_OPEN 继续探测
        ┌──────────────────────────┐
        │ CLOSED + _failure_ts.clear() │
        └──────────────────────────┘
```

## 1.2 接口契约（精确签名）

```python
@dataclass
class CircuitState:
    """熔断器运行时状态快照。"""
    name: str
    state: str = "CLOSED"                    # CLOSED / OPEN / HALF_OPEN
    last_failure_ts: float = 0.0
    last_state_change_ts: float
    half_open_successes: int = 0
    total_failures: int = 0
    total_successes: int = 0
    total_rejected: int = 0                  # 被熔断拒绝的次数


class TimeWindowCircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,          # 失败阈值
        failure_window_sec: float = 60.0,    # 滑动窗口（秒）
        recovery_cooldown_sec: float = 30.0, # OPEN → HALF_OPEN 冷却
        half_open_success_needed: int = 2,   # 半开需连续成功数
    ) -> None: ...

    def allow_request(self) -> bool:
        """是否允许请求通过。False = 被熔断拒绝，应立即降级。"""

    def record_success(self) -> None:
        """记一次成功。HALF_OPEN 下累计，达阈值转 CLOSED。"""

    def record_failure(self) -> None:
        """记一次失败。HALF_OPEN 下立即回 OPEN；CLOSED 下判阈值。"""

    def snapshot(self) -> dict[str, object]:
        """可观测快照（供 /health 与运维页）。"""
```

**内部实现要点**（面试加分细节）：
- 失败时间戳存 `deque[float]`（不是 list）——`popleft()` O(1) 清理窗口外
- `threading.Lock` 保证线程安全（单进程多线程环境）
- `_gc_failure_window(now)` 在 `record_failure` 与 `snapshot` 中各调一次
- `_transition(new_state, reason)` 统一状态切换 + 记录 `last_state_change_ts`

## 1.3 性能指标（实测/配置值）

| 指标 | 值 | 来源 |
|---|---|---|
| deepseek 熔断参数 | **3 次 / 60s / 30s / 2** | `circuit_breaker.py:135-141` |
| ollama 熔断参数 | **5 次 / 60s / 15s / 1** | `circuit_breaker.py:142-147` |
| 熔断后行为 | **跳过 API 调用**，直接降级（0 延迟 + 0 token） | `gateway.py:353-368` |
| 配置类错误（401/402/403） | **不计入熔断** | `providers.py:26 _CONFIG_STATUS` |
| 慢 ≠ 坏 | 单跳超时**不计入**熔断 | `gateway.py:400-420` |
| snapshot 字段数 | 8 个（state / failures_in_window / …） | `circuit_breaker.py:116-129` |

## 1.4 面试话术（60 秒讲完）

> "熔断器是三态状态机——CLOSED 正常放行、OPEN 熔断拒绝、HALF_OPEN 探测恢复。
>
> **三个关键设计**：
> ① **按 provider 隔离，不按 model**——同一 provider 的不同 model 是**同一个故障域**（同一 endpoint、同一 key），按 model 隔离会让熔断变假象
> ② **配置类错误不计入**（401/402/403）——它们是**确定性错误**，重试永远不自愈；计进去会让熔断器打开、把真正的病因（KEY 没配）淹没
> ③ **慢 ≠ 坏**——单跳超时不计入熔断，因为超时说明'不该在这个预算内等'，不是'这个 provider 坏了'
>
> **参数差异也有讲究**：deepseek 3 次/60s 就熔断（云端，快速止损），ollama 5 次/60s（本地，给抖动留更多容错）。
>
> **熔断的语义是"立即降级"，不是"重试"**——跳过一次 API 调用，省一次 token 和时间。"

## 1.5 现场追问预案

| 面试官会问 | 你的回答 |
|---|---|
| "为什么用 deque 不用 list？" | "窗口清理是 `popleft()` — O(1)；list 要 `pop(0)` O(n)，高频失败下会退化" |
| "多线程安全吗？" | "`threading.Lock` 保护全部状态变更；单进程多 worker 场景需要考虑共享状态，但当前是单进程" |
| "熔断恢复为什么要 2 次成功？" | "1 次可能是偶然——2 次成功才说明探针稳定；ollama 用 1 次是因为本地成本低、恢复快" |
| "配置错误怎么识别？" | "provider 层抛 `LLMGatewayError(count_as_failure=False)`；网关按 `getattr(exc, 'count_as_failure', True)` 判定" |
| "熔断后用户看到什么？" | "看不到熔断——网关**静默降级**到备模型；审计里记 `circuit_open`，运维页能看到" |

---

# 模块 2 · ReAct 增量协议（ReActExecutor）

## 2.1 手绘版

```
Step 1（全量）                          Step 2+（增量）
┌────────────────────────────┐         ┌────────────────────────────┐
│ system: 工具描述（固定）      │         │ system: 同上（不变）          │
│ prompt:                    │         │ prompt:                    │
│   ## 任务                   │         │   ## 续推（增量）             │
│   {compact context}        │         │   原始任务已在第 1 步提供      │
│   （每块 ≤400 字）           │         │   ### 上一次输出              │
│   ## 上游分析结论            │         │    推理：{thinking[:300]}    │
│   ## 任务要求（schema）      │         │    动作：{name}({args[:200]}) │
│                            │         │   ### 本步新观察              │
│ ≈ 700 tokens                │         │    {observation[:800]}      │
└────────────────────────────┘         └────────────────────────────┘
            │                                       │
            ▼                                       ▼
    LLM 输出二选一：                        LLM 输出二选一：
    ① {"action": {name, args}}             ① {"action": {...}}
    ② {"final_answer": {...}}              ② {"final_answer": {...}}
            │                                       │
            └──► 有 final_answer？ → 立即返回（提前终止）
            └──► 有 action？ → 执行工具 → 观察截断 800 字 → 进下一步
            └──► 第 N 步（最后一步）→ 强制追加：
                 "这是最后一轮，必须直接输出 final_answer，禁止再调用工具"
            └──► 达上限仍无 → 返回最后一次输出（兜底，不抛错）
```

## 2.2 接口契约

```python
class ToolRegistry:
    def register(
        self, name: str,
        func: Callable[..., Coroutine[Any, Any, str]],
        description: str,
    ) -> None: ...

    def has(self, name: str) -> bool: ...

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """工具不存在 → 返回提示文本（不抛错）；执行异常 → 返回失败说明（不阻断循环）"""

    def describe_all(self) -> str:
        """返回 'name: description' 多行文本，注入 system prompt"""


class ReActExecutor:
    def __init__(
        self, gateway: LLMGateway, tools: ToolRegistry, *,
        max_steps: int = 3,
        task_tier: str = "reasoning",
        incremental: bool = True,        # ★ 增量协议开关
    ) -> None: ...

    async def run(
        self, system: str, prompt: str, *,
        agent_id: str, trace_id: str,
        json_mode: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """返回 final_answer 的 JSON（dict）"""


# 模块级常量
_OBS_TRUNCATE = 800          # 单步观察截断字符数
_LAST_LM_SNIPPET = 600       # 上次输出摘要的兜底截断


def _summarize_lm_output(data: dict[str, Any]) -> str:
    """上次 LLM 输出 → 短摘要
    优先取 thinking[:300] + action(name, args[:200])；
    兜底整段 json[:600]
    """
```

## 2.3 性能指标（审计实证）

| 指标 | 旧（每轮重发） | 新（compact + incremental） |
|---|---|---|
| **Step 1** tokens_in | 7,582 | **~700**（compact ≤400 字/块） |
| **Step 2** tokens_in | 7,582 | **~600**（上次输出摘要 + 新 observation） |
| **Step 3** tokens_in | 7,582 | **~600** |
| **3 步合计** | **22,746** | **~1,900（−91%）** |
| 3 步 tokens_out | 411,726 | ~60,000 |
| 占系统成本 | **55%**（云端 deepseek-v4-pro） | **~30%** |
| max_steps | 3 | **2**（env `MOSS_REACT_MAX_STEPS` 可覆盖） |
| 单步观察截断 | 1,500 字 | **800 字** |
| 撞 4096 输出上限 | **98 次**（实测） | 已提至 8192 |

**成本换算**：
- 单轮成本：0.20 元 → **0.07 元**（−65%）
- 一天 200 轮：40 元/天 → **14 元/天**

## 2.4 4 道追问护栏（面试重点）

| 护栏 | 实现 | 代码位置 | 防的问题 |
|---|---|---|---|
| ① receiver 白名单 | 只允许 A08-A16 | `supervisor.py:807-810` | LLM 问数据层（契约不匹配） |
| ② 同问题幂等 | `normalize_question()` 后命中缓存 | `supervisor.py:811-815` | 换个措辞重问同一件事 |
| ③ 单 Agent 追问数 | `MAX_ASKS_PER_AGENT = 1` | `supervisor.py:816-819` | LLM 绕着重问同一个 Agent |
| ④ 工具存在性 | `tools.has(name)` 检查 | `react.py:120-125` | LLM 调用不存在的工具 |

**关键细节（面试加分）**：
- 护栏在 **provider 调用之前** —— 拒绝时 **0 token 消耗**
- `MAX_ASKS = 1`（不是 prompt 里写的 2）—— 是被"LLM 绕着重问"实测逼出来的收紧

## 2.5 面试话术（90 秒讲完）

> "ReAct 循环的经典问题是**每轮重发同一份上下文**——我实测过：A17 的 prompt 是 **7,582 tokens**，3 步就是 **22,746 tokens_in**，占全系统成本的 **55%**。
>
> **两个改造**：
> ① **compact context**——第 1 步把上游每个 Agent 的结论压缩到 **≤400 字**（只留结论摘要 + 2-3 个关键数值），prompt 从 7,582 → **~700 tokens**
> ② **增量协议**——第 2+ 步**不再重发上游分析**，只发『上次输出摘要（≤600 字）+ 本步新观察（≤800 字）』
>
> **结果：3 步 tokens_in 从 22,746 降到 ~1,900，−91%**；单轮成本 0.20 元 → 0.07 元。
>
> **还有 4 道追问护栏**：receiver 白名单、同问题幂等、单 Agent 追问上限 1 次、工具存在性检查——**全部在 provider 调用之前拦截，拒绝时 0 token**。
>
> 最后 max_steps 从 3 降到 2——因为实测 **99% 的案例 1-2 步就够了**，第 3 步是浪费。"

## 2.6 现场追问预案

| 面试官会问 | 你的回答 |
|---|---|
| "增量之后 LLM 会不会丢上下文？" | "不会——第 1 步已给全量 compact context；第 2+ 步的任务是'基于已知信息继续推理'，只需知道**自己上次说了什么** + **新观察**" |
| "observation 为什么截断 800？" | "3 步 × 800 = 2,400 字符，保证总 prompt 不超输入硬上限 6,000 字符；原 1,500 会撑爆" |
| "如果第 2 步还没出 final_answer 呢？" | "第 N 步强制注入'必须直出 final_answer，禁止调用工具'；仍无 → 返回最后一次输出兜底，**不抛错**（避免整链路中断）" |
| "为什么 max_steps 是 2 不是 3？" | "实测 99% 案例 1-2 步完成；第 3 步几乎用不上却要多付一次云端调用" |
| "JSON 解析失败怎么办？" | "追加'输出非合法 JSON，重新推理'到 scratchpad，**continue 下一步**（不抛错）；每步都有机会自我修复" |
| "工具执行失败呢？" | "`ToolRegistry.execute` 捕获异常 → 返回'（工具 X 执行失败：…）'文本作为 observation，**不阻断循环**" |

---

# 模块 3 · LangGraph fan-out + reducer

## 3.1 手绘版

```
                    START
                      │
                ┌─────▼─────┐
                │ supervisor │  规划：本次要哪些 Agent
                └─────┬─────┘
                      │
                ┌─────▼─────┐
                │  collect   │  A01 并发采集（≤20 指标）
                └─────┬─────┘
                      │
                ┌─────▼─────┐
                │   clean    │  A02
                └─────┬─────┘
                      │
                ┌─────▼─────┐
                │  validate  │  A03
                └─────┬─────┘
                      │
                ┌─────▼─────┐
                │   store    │  A04
                └─────┬─────┘
                      │
      ┌───────────────┼───────────────┐
      │ (fan-out)     │               │
┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼──────┐
│ verify_info│   │extract_ev.│   │liquidity_ctx│
│   A05      │   │   A06      │   │  本地量化   │
└─────┬─────┘   └─────┬─────┘   └─────┬──────┘
      │               │               │
      └───────┬───────┘               │
              ▼                       │
        ┌───────────┐                 │
        │ sentiment  │  A07            │
        │  (等两者)  │                 │
        └─────┬─────┘                 │
              │                       │
              │   ┌───────────────────┘
              │   │  (fan-out 9 路)
      ┌───────┴───┴───────────────────────────────┐
      │       │       │       │       │            │
┌─────▼──┐┌───▼───┐┌──▼───┐┌──▼───┐┌──▼───┐  ┌────▼────┐
│ A08    ││ A09   ││ A10  ││ A11  ││ A12  │… │ A13-A16 │
│ macro  ││ meso  ││micro ││ risk ││compl.│  │ 行业×4   │
└─────┬──┘└───┬───┘└──┬───┘└──┬───┘└──┬───┘  └────┬────┘
      │       │       │       │       │            │
      └───────┴───────┴───┬───┴───────┴────────────┘
                          │ (fan-in)
                    ┌─────▼─────┐
                    │ recommend  │  A17 ReAct
                    └─────┬─────┘
                          │
                    ┌─────▼─────┐
                    │   audit    │  A18 哈希链封存（零 LLM）
                    └─────┬─────┘
                          │
                         END
```

## 3.2 reducer 契约（最关键的 9 个字段）

```python
from typing import Annotated, Any, TypedDict
import operator

class ResearchState(TypedDict):
    # ============ 列表字段：operator.add 累加（并行安全）============
    raw_points:       Annotated[list[dict[str, Any]], operator.add]  # A01 写
    cleaned_points:   Annotated[list[dict[str, Any]], operator.add]  # A02 写
    validated_points: Annotated[list[dict[str, Any]], operator.add]  # A03 写
    agent_outputs:    Annotated[list[dict[str, Any]], operator.add]  # ★ 9 路并行都写
    data_refs:        Annotated[list[str], operator.add]
    trace_ids:        Annotated[list[str], operator.add]
    errors:           Annotated[list[str], operator.add]
    agent_messages:   Annotated[list[dict[str, Any]], operator.add]  # A17 追问写
    progress:         Annotated[list[str], operator.add]             # 前端轮询读最后一项

    # ============ 覆盖语义字段（无 reducer）============
    info_items:        list[dict[str, Any]]     # 任务注入 / collect 抓新闻
    verified_items:    dict[str, Any]           # A05 写
    extracted_events:  dict[str, Any]           # A06 写
    analysis_hint:     dict[str, Any]           # liquidity_ctx 写
    validation_report: dict[str, Any]           # A03 写
    storage_stats:     dict[str, Any]           # A04 写
    final_report:      str | None               # recommend 写
```

**fan-out 的代码（3 行搞定 9 路）**：

```python
# src/orchestration/supervisor.py:1249-1270
for aid in ALL_INSIGHT_AGENTS:                    # 9 个 Agent
    g.add_node(f"analyze_{aid}", _node(aid, _build_analyze_payload_fn(aid)))
    g.add_edge("liquidity_ctx", f"analyze_{aid}")  # 扇出边
    g.add_edge(f"analyze_{aid}", "recommend")      # 扇入边
```

## 3.3 ⚠️ 最大的坑：手动还原 reducer 语义

**问题**：`graph.astream()` 的 chunk 是 `{node_name: node_update}` —— 直接 `final.update(chunk)` 会**丢失 add 语义**，`agent_outputs` 被后一个节点**整体覆盖**，最终只剩最后一个 Agent 的输出。

**解决**（`src/api/routes/research.py:84-88`）：

```python
_ACCUMULATE_LIST_KEYS = frozenset({
    "agent_outputs", "data_refs", "trace_ids", "raw_points",
    "cleaned_points", "validated_points", "errors", "progress",
})

# 流式聚合时手动还原 reducer 语义
for key, value in node_update.items():
    if (key in _ACCUMULATE_LIST_KEYS
            and isinstance(value, list)
            and isinstance(final.get(key), list)):
        final[key] = final[key] + value      # ★ 累加，不是覆盖
    else:
        final[key] = value
```

> **为什么用 `astream` 不用 `ainvoke`**：`astream` 让每个节点的产出**实时更新 live_state** → 前端能轮询到"正在执行 A10…"。`ainvoke` 只在最后返回。

## 3.4 性能指标（实测）

| 指标 | 值 | 来源 |
|---|---|---|
| 图规模 | **16 节点 + 23 边** | `supervisor.py:1215-1290` |
| 最大扇出 | **9 路**（A08-A12 + A13-A16） | `ALL_INSIGHT_AGENTS` |
| 并行累加 | `operator.add` reducer，**无需锁** | `state.py:43-52` |
| 实际并发 | Ollama **单 slot 串行** → ≈ max(单 Agent) | `configs/models.yaml` §一 |
| 单 Agent 耗时 | 35-50s（本地 8B） | 审计实测 |
| 空跳过 | 节点 `return {}` → **不算失败、不消耗 LLM** | `supervisor.py:840-847` |
| 信息层（改后） | 95s → **38s**（−60%） | 第九轮实测 |
| 端到端 | 160s → **40s**（−75%） | 八轮 + 九轮累计 |

## 3.5 面试话术（90 秒讲完）

> "编排用 LangGraph StateGraph，**16 节点 + 23 边**。
>
> **核心机制是 reducer**：`ResearchState` 里的列表字段用 `Annotated[list, operator.add]` —— 9 路分析 Agent 并行 `return` 各自输出，LangGraph **自动累加**，不需要任何锁、Agent 之间也不互相知道。
>
> **fan-out 只有 3 行代码**：
> ```python
> for aid in ALL_INSIGHT_AGENTS:
>     g.add_node(f"analyze_{aid}", ...)
>     g.add_edge("liquidity_ctx", f"analyze_{aid}")   # 扇出
>     g.add_edge(f"analyze_{aid}", "recommend")       # 扇入
> ```
>
> **踩过一个坑**：流式聚合时我一开始用 `final.update(chunk)` —— 结果 **`agent_outputs` 被后一个节点整体覆盖**，最终只剩最后一个 Agent 的输出。因为 `astream` 的 chunk 是 `{node: update}`，**不带 reducer 语义**。修法是维护一张 `_ACCUMULATE_LIST_KEYS` 白名单，手动做 `final[key] + value`。
>
> **为什么用 astream 不用 ainvoke**：astream 让每个节点的产出实时更新 `live_state`，前端能轮询到'正在执行 A10'——这是**用户能看见的进度**。"

## 3.6 现场追问预案

| 面试官会问 | 你的回答 |
|---|---|
| "9 路并行不会有竞态吗？" | "不会有——`operator.add` 是纯函数累加；LangGraph 在节点边界做 reduce，节点内不共享可变状态" |
| "Ollama 单 slot，9 路并行有意义吗？" | "**有意义但收益递减**——单机 8GB 显存只能常驻一个 7-8B 模型，Ollama 单 slot 串行执行。所以实际耗时 ≈ max(单 Agent)。**但换成多实例 LB 后立刻线性加速**——这是架构上的预留" |
| "节点失败会怎样？" | "`_node()` 内 `try/except` → 写 `state.errors` → **继续整图**，不中断。最后 A18 审计会列出 errors" |
| "不在 plan 里的 Agent 怎么办？" | "节点内 `return {}` **空跳过**——不算失败、不消耗 LLM。这让**一张图承载 5 种任务形态**（macro/industry/stock/news/full）" |
| "状态怎么传给前端？" | "`cancel_token.live_state` 旁路——把 state 引用挂在 token 上，节点内 `push_progress()` 直接改，绕过 LangGraph 的值传递" |
| "为什么不用 AutoGen？" | "AutoGen Group Chat 是**黑盒**——控不了 fan-out 并发度、控不了失败边界、控不了'哪个 Agent 什么时候说话'。我需要显式的边" |

---

# 附 · 现场白板 3 分钟速成（背这 3 句）

| 模块 | 一句话核心 | 关键数字 |
|---|---|---|
| **熔断状态机** | 三态 + 按 provider 隔离 + **配置错误不计入** | deepseek **3次/60s/30s/2** |
| **ReAct 增量协议** | 第 1 步全量 + **第 2+ 步只发增量** | **22,746 → 1,900 tokens（−91%）** |
| **LangGraph reducer** | `Annotated[list, operator.add]` —— **并行累加无需锁** | **16 节点 / 9 路扇出** |

---

# 附 · 通用应对策略（万一现场画不出）

> **不要硬撑**，坦诚 + 转向设计思路：

> "具体代码细节我需要回忆（几个月前写的）——但我可以**讲清楚设计思路和取舍**：
>
> 比如熔断器，核心是**三态转换 + 三个判定条件**：CLOSED 下窗口内失败数达阈值 → OPEN；OPEN 冷却期满 → HALF_OPEN；HALF_OPEN 连续成功 → CLOSED、任意失败 → 回 OPEN。
>
> **关键的取舍有两处**：① 按 provider 不按 model 隔离 ② 配置类错误不计入熔断。
>
> 如果需要看代码，我可以打开项目给您看——`circuit_breaker.py` 只有 170 行。"

---

> **文件位置**：`docs/INTERVIEW_WHITEBOARD.md`
> **数据来源**：`circuit_breaker.py`（170 行）、`react.py`（251 行）、`state.py`（57 行）、`supervisor.py`（1331 行）、`PROJECT_AUDIT_2026-09-19.md`、`END_TO_END_OPTIMIZATION_2026-09-28.md`
> **建议**：打印出来，面试前 30 分钟过一遍；3 个图各手绘 2 遍。
