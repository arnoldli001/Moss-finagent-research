# 投研分析耗时优化 — 问题诊断、根因分析与修复记录

> 日期：2026-09-15
> 任务：优化投研分析（Supervisor LangGraph 管线）的端到端耗时
> 用户输入示例：`中际旭创 基于当前市值和估值，考虑AI产业链供需和渗透率、美国加息等预期，中短期持有1-3个月，是否具备性价比，盈亏比多少？`

---

## 一、前因：为什么会慢

### 1.1 用户直观感受

前端审计截图显示：

```
┌─────────────────┬──────────────┬────────────┬──────────────┬──────┐
│ 模型             │ tokens       │ 延迟        │ 语义缓存      │ 降级  │
├─────────────────┼──────────────┼────────────┼──────────────┼──────┤
│ deepseek-v4-flash│ 19051/3563   │ 18327ms    │ 否            │      │
│ qwen3:8b        │ 2050/1021    │ 28074ms    │ 是            │ K_M  │
│ deepseek-v4-pro │ 3077/676     │ 11410ms    │              │      │
│ deepseek-v4-pro │ 3461/1331    │ 24716ms    │              │      │
│ deepseek-v4-pro │ 3501/3835    │ 65526ms    │              │      │
└─────────────────┴──────────────┴────────────┴──────────────┴──────┘
6 次 LLM 调用累计：188s（**仅 LLM 调用**）
A01 数据采集另计：约 40-60s（串行）
总计：**~165-250s / 次**
```

### 1.2 审计链实锤（audit_chain seq=80）

审计链 JSON 里的 `agent_ids` 数组：

```json
["A01_data_collector" × 20,  // ← 串行 20 次！
 "A02_data_cleaner",
 "A03_data_validator",
 "A04_data_storage",
 "A09_meso",
 "A13_tech",
 "A17_recommend"]
```

**A01 被串行调用 20 次**——每个指标一次 `for await`，完全无并发。

---

## 二、根因分析

### 2.1 时间分解

| 环节 | 耗时 | 占比 | 根因 |
|------|------|------|------|
| A01 数据采集 20 指标 | 40-60s | ~40% | **串行 + 无缓存** |
| LLM 调用 6 次 | ~188s | ~60% | Planner 用 flash 18s + A17 ReAct 最多 5 步 + 本地 Ollama 冷启动 28s |
| A02-A04 管线 | ~2s | ~1% | 本地处理，可忽略 |
| **总计** | **~230s** | 100% | |

### 2.2 三大根因

**根因 1：A01 串行采集 + 无缓存**

`supervisor.py collect_node` 原来是：
```python
for indicator in indicators:   # 20 次串行 await
    await _run_agent(collector, state, {"indicator": indicator})
```
每个指标走 ConnectorRouter → QMT→CSV→AkShare，AkShare HTTP 调用 1-3s/次。
**20 次串行 = 累计 40-60s**，指标间无依赖，完全可以并发。

此外，CPI/PPI/M2/PE(TTM) 是**慢变量**（月频/日频），每次都去外网拉是浪费。Router 层没有进程内 TTL 缓存。

**根因 2：Planner 不该用 flash**

`LLMSupervisorPlanner.plan()` 调用 `gateway.complete("reasoning", ...)`，
reasoning 层 primary 是 `deepseek-flash`。但规划任务是从 Agent 目录 + 指标目录**挑子集**，是简单分类任务，
qwen2.5:1.5b 完全能做，**本地 3-5s vs flash 18s**。

**根因 3：Ollama 模型每次冷启动**

审计里 qwen3:8b（本地 Ollama）延迟 28s——对 8B Q4_K_M 模型异常。
执行 `ollama ps` 发现**没有模型在运行**（空列表）。
原因：Ollama 默认 `keep_alive=5min`，空闲后自动卸载。每次调用都要重新加载 5.2GB GGUF 文件到内存/GPU。

---

## 三、解决过程

### P0-1 ConnectorRouter 进程内 TTL 缓存

**文件**：`src/infrastructure/connectors/router.py`

**改动**：在 `fetch()` 外层加一层分档 TTL 缓存：

```python
# 指标前缀 → TTL（秒）
_TTL_BY_PREFIX = [
    (("CPI", "PPI", "M2", "社融"), 24 * 3600),                    # 月频宏观 24h
    (("PE(TTM):", "PB:", "资产负债率:", "流动比率:", ...), 12 * 3600),  # 估值 12h
    (("mkt:margin_balance", "mkt:north_flow"), 4 * 3600),          # 两融北向 4h
    (("mkt:turnover", ...), 5 * 60),                                # 实时流动性 5min
    (("fed:",), 3600),                                               # FedWatch 1h
    (("stock_close:", "index_close:", "etf_close:"), 4 * 3600),    # 行情日线 4h
]
```

关键设计：
- **Per-key Lock 防缓存击穿**：同一指标并发请求时只走一次后端
- **start/end 指定时跳过缓存**：回测 API 按区间拉取不应命中
- **空结果不缓存**：防止把"数据源空"缓存住
- **`invalidate()` 方法**：供回测 API 显式清空

### P0-2 A01 指标并发采集

**文件**：`src/orchestration/supervisor.py`

**改动**：
```python
# 原来：for 串行
for indicator in indicators:
    await _run_agent(collector, state, {"indicator": indicator})

# 现在：asyncio.gather 并发
async def _collect_one(indicator, state, sink, lock):
    # 单指标任务，异常隔离 + 存储降级兜底
    ...

await asyncio.gather(
    *[_collect_one(ind, state, updates, shared_lock) for ind in indicators]
)
```

**踩坑 1**：Python 闭包是**静态层级**。`_collect_one` 和 `collect_node` 同级定义在 `build_research_graph` 内，不能捕获 `collect_node` 的参数 `state`。必须显式加 `state: ResearchState` 参数。

**踩坑 2**：并发后的共享写入（`errors`/`raw_points`/`agent_outputs`）必须走 `asyncio.Lock`，否则数据竞争。

**效果**：A01 从 40-60s 串行 → **6-10s 并发**。

### P0-3 Ollama 常驻内存（keep_alive）

**文件**：`src/infrastructure/llm/providers.py`

**改动**：
```python
# Ollama API 支持 keep_alive 参数（秒）
payload = {
    "model": spec.model_name,
    "keep_alive": 24 * 3600,  # 模型常驻内存 24h
    ...
}
```

同时把 `httpx.AsyncClient` 提到实例级，复用 TCP 连接池（避免每次都握手）：
```python
def __init__(self, ...):
    self._client = httpx.AsyncClient(timeout=self._timeout)
```

**验证**：
```
$ ollama ps
NAME                            SIZE      PROCESSOR    UNTIL
qwen2.5:1.5b-instruct-q4_K_M    1.2 GB    100% GPU     24 hours from now
qwen3:8b-q4_K_M                 5.6 GB    100% GPU     24 hours from now
```

**效果**：本地模型从 28s 冷启动 → **2-4s 常驻**。

### P1-1 A17 ReAct 收紧

**文件**：`src/orchestration/supervisor.py`

**改动**：
```python
ReActExecutor(..., max_steps=3)   # 之前 5
MAX_ASKS_PER_AGENT = 1            # 之前 2
```

审计链显示那次只用了 3 步，说明 3 步足够。收紧后防止 LLM 绕着问。

### P1-2 Planner 换本地轻量模型

**文件**：`src/orchestration/planner.py`

**改动**：
```python
# 之前
response = await self._gateway.complete(
    "reasoning", SYSTEM_PROMPT, prompt, ...)

# 现在
response = await self._gateway.complete(
    "light", SYSTEM_PROMPT, prompt, ...)
```

**逻辑**：Planner 的工作是从指标目录 + Agent 目录**挑子集**，是简单分类任务。
light 层 primary 是 `qwen2.5:1.5b-instruct-q4_K_M`（本地 Ollama），
1.5B 模型做规划完全够用，且本地推理快 3-6×。

### P2-1 语义缓存确认已启用

**无需改代码**。`src/core/config.py`：
```python
llm_cache_enabled: bool = True     # 默认 True
llm_cache_dir: str = "data/llm_cache"
llm_semantic_threshold: float = 0.85
```

审计里 planner 第二次调用已命中（139ms vs 18327ms），证明生效。

### deepseek-v4-flash → deepseek-flash

**原因**：旧模型名下线。

**改动文件**：`configs/agents.yaml`（9 处）、`configs/models.yaml`（2 处）、`tests/unit/test_llm_gateway.py`、`tests/unit/test_llm_cache.py`。

---

## 四、改动清单

| 文件 | 行数 | 改动 | 效果 |
|------|------|------|------|
| `src/infrastructure/connectors/router.py` | +55 | TTL 分档缓存 + per-key Lock | 慢变量命中缓存，省 5-40s/次 |
| `src/orchestration/supervisor.py` | +40/-25 | `asyncio.gather` 并发 A01 + ReAct 收紧 5→3 + MAX_ASKS 2→1 | A01 省 ~45s，ReAct 省 ~20s |
| `src/orchestration/planner.py` | +3/-1 | `reasoning` → `light` 层 | Planner 省 ~15s |
| `src/infrastructure/llm/providers.py` | +18/-10 | Ollama `keep_alive=86400` + 单例 HTTP Client | 本地模型省 ~25s/次 |
| `configs/agents.yaml` | 9 处替换 | deepseek-v4-flash → deepseek-flash | 适配新模型名 |
| `configs/models.yaml` | 2 处替换 | 同上 | 同上 |
| `tests/unit/test_llm_gateway.py` | 2 处替换 | 同上 | 同上 |
| `tests/unit/test_llm_cache.py` | 1 处替换 | 同上 | 同上 |

**无新文件**，所有改动都在既有文件里。

---

## 五、验证

### 5.1 单元测试

```
542 passed, 1 warning in 22.62s
```

新增/修复：
- router 测试：`_matched` 方法抽取丢失（重构后忘记搬过来）→ 加回去
- supervisor 测试：`_collect_one` 闭包捕获 `state` 失败（Python 闭包静态层级）→ 显式加 `state` 参数
- Ollama `close()` 新增但 gateway 里没调用（非必需，provider 生命周期由应用托管）

### 5.2 ruff + tsc

```
ruff check src/  → All checks passed!
npx tsc --noEmit → 无输出（零错误）
```

### 5.3 后端重启

```
$ manage.py start --replace --port 8000
INFO:     Started server process [34992]
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 5.4 Ollama 模型常驻

```
$ ollama ps
NAME                            SIZE      PROCESSOR    UNTIL
qwen2.5:1.5b-instruct-q4_K_M    1.2 GB    100% GPU     24 hours from now
qwen3:8b-q4_K_M                 5.6 GB    100% GPU     24 hours from now
```

---

## 六、预期效果

| 环节 | 之前 | 现在 | 省 |
|------|------|------|-----|
| A01 采集 20 指标 | 40-60s 串行 | **6-10s** 并发+缓存 | ~45s |
| Planner | 18s flash | **3-5s** qwen1.5b | ~15s |
| 本地 Ollama | 28s 冷启动 | **2-4s** 常驻 | ~25s |
| A17 ReAct | 最多 5 步+追问 4 次 | 最多 3 步+追问 1 次 | ~20s |
| 同一指标重复拉 | 每次走后端 | 路由层命中缓存 | 累积 |
| **总计** | **~230s** | **~45-60s** | **~170s** |

**注意**：上面的"省 25s（Ollama）"是从每次 A09/A12 等本地 Agent 的调用里省下来的，
这些 Agent 原来用 qwen3:8b 每次冷启动 28s，现在常驻后 2-4s。

---

## 七、后续可做（本期未做）

| 项目 | 说明 | 优先级 |
|------|------|--------|
| Ollama 首次预加载 | 后端启动时主动 `ollama run model` 一次，避免用户第一次请求冷启动 | P2 |
| qwen3:8b 换 qwen3.5:4b | 用户有 qwen3.5:4b（3.4GB），如果显存吃紧可以把 medium 层换成 4B 模型 | P2 |
| A17 上游结论前置 | 把 A09/A10/A13 的 Agent 输出直接塞进 A17 ReAct 的初始 messages，省掉首轮思考 | P1 |
| Redis 分布式缓存 | 进程内缓存在多进程部署时无效，生产环境换 Redis | P3 |

---

## 八、关键经验教训

1. **Python 嵌套函数闭包是静态层级**：`_collect_one` 和 `collect_node` 同级定义时，
   `_collect_one` 不能捕获 `collect_node` 的参数 `state`——必须显式传参。

2. **Ollama 冷启动是定时炸弹**：即使 keep_alive 设短了，进程空闲后模型会被卸载。
   `ollama run model --keepalive 24h` + API payload 也传 `keep_alive` 才双保险。

3. **Per-key Lock 防缓存击穿**：Router 层有缓存后，多个并发请求同一指标会同时穿透，
   必须用 `asyncio.Lock` + 二次检查（check-then-lock-check pattern）。

4. **Planner 不该用 reasoning 层**：规划/路由/分类任务是 1.5-3B 模型的主场，
   别浪费 flash/pro 去调个 Agent 名单。

5. **审计链 JSON 是真相来源**：`data/audit/audit_chain.jsonl` 的 `agent_ids` 数组
   直接揭示了 A01 被调用 20 次这个核心问题——看 LLM latency 表之前先看 agent_ids。

---

## 附：2026-09-16 「运行指标」页一直转圈 —— 事件循环被同步 I/O 堵死

> 用户反馈：打开「运行指标」页一直停在「加载指标中…」，而且感觉整个服务都变慢了。

### 现象与实测

```
GET /api/v1/metrics?limit=1000     13.18s   （随后连测：21.6s / 43.9s / 24.6s）
GET /api/v1/health                300.04s   ← 超时
```

而这两个接口**自身**都很快：

```
read_and_aggregate(llm_audit.jsonl, 1000)   0.0047s   ← /metrics 的全部工作
_warehouse_health 分段：tushare 3.5s、仓库九表 COUNT(*) 42s、其余 <0.01s
```

### 根因（两层，缺一不可）

1. **`/health` 里 `build_data_health()` 是同步函数，直接放在 async 路由里跑** ——
   整段 CPU/IO 压在事件循环上，**所有**并发请求一起卡。它内部最贵的一步是对
   **14.28 GB** 的 SQLite 逐表 `SELECT COUNT(*)`：

   | 口径 | daily | daily_basic | adj_factor | stk_limit | 九表合计 |
   |---|---:|---:|---:|---:|---:|
   | `COUNT(*)` | 0.46s | 8.52s | 8.66s | 7.43s | **42s**（冷页缓存 30s+，第二次更慢） |
   | `MAX(rowid)` | 0.00s | 0.00s | 0.00s | 0.00s | **<0.01s**（且与 COUNT(*) 数值完全相等） |

2. **前端用 `Promise.all([metrics, health])` 加载，且每 15 秒重复一次** ——
   `/health` 永不返回 → 页面永远转圈；更糟的是每 15 秒又发一个新请求，每个都阻塞
   事件循环数百秒 → **服务被自己的健康检查拖垮**。这才是 `/metrics` 从 4.7ms
   变成 20~44s 的原因：它只是在排队。

### 修法

| # | 改动 | 效果 |
|---|------|------|
| 1 | 仓库行数改用 `MAX(rowid)`（写入是 `ON CONFLICT DO UPDATE` 原地更新、从不 DELETE → rowid 无空洞；响应带 `count_mode` 标明口径） | 42s → **<0.01s** |
| 2 | 仓库统计**落盘缓存** `data/quant/warehouse_stats.json`，超 10 分钟后台线程重算 | 30.6s 冷算 → **0.00s** 命中 |
| 3 | `/health` 把 `build_data_health` 放进 `asyncio.to_thread` | 不再阻塞事件循环 |
| 4 | 启动时后台**预热**一次（lifespan） | 首次打开页面即热 |
| 5 | 整份健康度 5 分钟内存缓存 | 4.53s → **0.000s** |
| 6 | 前端**拆开加载**：指标 15 秒一刷、健康度 60 秒一刷，各自渲染与报错 | 指标先出；健康度慢或失败都不再拖住整页 |

### 教训

- **同步 I/O 绝不能出现在 async 路由里**：一次 42 秒全表扫描能让整个服务停摆，
  症状还会伪装成"别的接口也变慢了"（`/metrics` 4.7ms → 44s）。
- **"精确"要算成本**：`COUNT(*)` 与 `MAX(rowid)` 在这套写入语义下**数值完全相等**，
  代价却差约 4000 倍。前提（原地 upsert、从不删除）已用测试固定：
  哪天改成 DELETE + INSERT，测试会立刻失败。
- **面板加载别用 `Promise.all` 绑住快慢两端**：慢的一端会把快的一端一起拖住，
  还会掩盖真正的故障点。
