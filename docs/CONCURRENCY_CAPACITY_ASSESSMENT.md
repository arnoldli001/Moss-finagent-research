# 投研分析并发能力评估（多用户同时调用）

> 评估对象：`POST /api/v1/research/analyze` 及 LangGraph 投研链路
> 方法：代码走查 + 实测（本机真实缓存目录 3108 文件 / 审计链 85 条）
> 结论先行：**结构上支持并发，单进程内有正确的隔离与去重；但没有准入控制，
> 且语义缓存会在事件循环上同步阻塞。当前形态的安全并发量约 2~4 个同时进行的
> full 分析，超出后不是"变慢"，而是"所有请求一起变慢"。**

---

## 一、结论摘要

| 维度 | 现状 | 判定 |
|---|---|---|
| 任务状态隔离 | 每请求独立 `ResearchState`，`operator.add` reducer 聚合 | ✅ 安全 |
| 并行写竞态 | A01 并发采集走 `asyncio.Lock`；LangGraph add reducer 自带聚合 | ✅ 安全 |
| 审计哈希链并发 | 实测 200 并发封存，链完整、seq 无冲突 | ✅ 安全 |
| 数据源去重 | `ConnectorRouter` per-key `asyncio.Lock` 防击穿 | ✅ 已优化 |
| 同问去重 | 结果缓存 + 数据层 TTL + per-key 锁，**但无 in-flight 合流** | ⚠️ 部分 |
| **准入控制** | **完全没有**。N 个请求 → N 条完整管线 | ❌ **硬缺口** |
| **语义缓存** | 事件循环上同步扫目录，实测单次阻塞 9ms（冷启 682ms） | ❌ **硬缺口** |
| **横向扩容** | 结果缓存/TaskStore/预算全在进程内 | ❌ **架构限制** |
| **内存回收** | `TaskStore._tasks` / `_tokens` 永不淘汰，实测 200 请求残留 200 份 | ❌ **泄漏** |
| 成本上限 | token 预算是**单任务** 20 万，无全局/租户级上限 | ⚠️ 缺护栏 |

---

## 二、实测数据

### 2.1 并发的"结构性安全"已验证

```
哈希链 并发   1:      0.8 ms | valid=True 记录=1   唯一seq=1   末seq=1
哈希链 并发  10:      8.2 ms | valid=True 记录=10  唯一seq=10  末seq=10
哈希链 并发  50:     12.5 ms | valid=True 记录=50  唯一seq=50  末seq=50
哈希链 并发 200:     55.6 ms | valid=True 记录=200 唯一seq=200 末seq=200
```

200 个任务同时封存，哈希链**仍然是完整链**（`valid=True`），200 条记录 seq 全部唯一。
这说明 `AuditChainWriter` 的 `threading.Lock` + `_resume()` 续链逻辑是对的 ——
`append()` 全同步无 `await`，在单事件循环下天然原子。

TaskStore 200 并发建/查/清：**6.3 ms**，无异常。同时暴露了残留问题（见 3.4）。

### 2.2 语义缓存：事件循环上的同步阻塞

```
LLM 缓存 cold 未命中扫描:  682.2 ms   (进程重启后第一次)
LLM 缓存 warm 未命中扫描:    8.9 ms   (此后每次未命中)
缓存文件数:                 3108
事件循环最大停顿:           22.3 ms   (协程内同步调用语义缓存期间)
```

`LLMCache._get_semantic()`（`src/infrastructure/llm/cache.py:149`）在**缓存未命中时**
遍历 `data/llm_cache/*.json` **全部 3108 个文件**，逐个 `read_text` + `json.loads`
+ 3-gram 向量化。全程同步、直接在事件循环上跑。

- 冷启动（进程重启后第一次未命中）：**682 ms**
- 稳态：**约 9 ms/次未命中**

这是本项目**最尖锐的并发瓶颈**，因为它是**串行累加**的：
N 个用户并发分析，总阻塞 ≈ N × 9ms（且冷启动那次 N 个请求要各自付 682ms）。
一次 full 分析有 7~15 次 LLM 调用，大部分会未命中 ——
**单用户的 10 次未命中就已经给所有其他请求累计 90ms 延迟**。

### 2.3 其他链路代价（参照系）

```
审计链全链校验:   85 条  ->  1.8 ms   (每次任务 A18 都跑)
plan_run(full):          0.078 ms     (纯本地规则, 无LLM)
plan_run(industry):      0.037 ms
```

这些**不是**瓶颈。规则规划是微秒级；审计链 85 条时 1.8ms 可忽略
（但它 O(N) 读全文件，链长到几万条时需重新评估）。

---

## 三、逐个瓶颈分析

### 3.1 【严重】没有任何准入控制

`submit_analyze` 里就是 `asyncio.create_task(_run())` 直接起，**没有信号量、没有队列、没有并发上限**：

```python
# src/api/routes/research.py:263
handle = asyncio.create_task(_run())
store.register_handle(task_id, handle)
```

单条 full 管线的资源画像：
- **7~15 次 LLM 调用**（其中 A17 decision 层是云端大模型）
- **最多 20 个指标并发采集**（`plan_run` 的 indicators 上限）
- **单任务 20 万 token 预算**
- 一次审计链写入 + 入库存点

于是 **N 个用户 = N 条完整管线同时跑**，直接后果：

1. **LLM API 配额**。DeepSeek 有并发/RPM 限制。没有本地排队，
   超限的请求会在 provider 层失败 → 触发**三态熔断器**（deepseek: 60 秒内 3 次失败即 OPEN）
   → 所有用户一起降级到备模型。**一个用户的突发流量会污染所有人的服务质量。**
2. **成本无上限**。`llm_token_budget_per_task=200000` 是**单任务**预算。
   10 个并发任务 = 200 万 token 潜在消耗，没有任何全局闸门。
3. **默认线程池被占满**。`asyncio.to_thread` 用事件循环默认执行器
   （`min(32, cpu+4)`）。20 指标并发 × N 个任务会瞬间打满。
   `core/executors.py` 的注释就是为这个场景写的 ——
   他们当时发现 `/health` 要 144.5 秒才返回，只因为池子被业务任务占满。
   **健康检查已经隔离到独立池（`INFRA_WORKERS=4`），但投研链路自己没有隔离也没有上限。**

### 3.2 【严重】LLM 语义缓存在事件循环上同步阻塞

见 2.2 实测。补充两点设计层面的问题：

- 扫描是 **O(缓存文件数)** 而非 O(该 agent 的条目数)。
  虽然 `_load()` 会把命中项放进 `self._memory`，但**每个文件仍要被 `open` + `json.loads` 至少一次**
  才能读到 `agent_id` / `scope` 做过滤 —— 过滤发生在解析**之后**。
- `_get_semantic` 里对最佳条目又调了一次 `_load(best_key)`（第 180 行），
  已有 `_memory` 时是白跑一趟。

稳态 9ms 看似不大，但它是**同步阻塞 + 串行累加**的，
而且缓存会随时间**单调增长**：3108 文件 = 10MB，只增不减（过期文件仅在
被再次访问时才 `unlink`）。这个数字会自己变大。

### 3.3 【架构限制】无法多 worker / 多实例横向扩容

三个关键状态都是**进程内**：

| 状态 | 位置 | 多 worker 后果 |
|---|---|---|
| 结果缓存 `_RESULT_CACHE` | `routes/research.py:91` 模块级 dict | 命中率降为 1/N；同一问题在 N 个 worker 各算一遍 |
| `TaskStore`（任务/令牌/live_state） | `api/tasks.py:47` 实例字典 | worker A 提交，轮询打到 worker B → **404 task not found** |
| token 预算 `_token_usage` | `gateway.py` 实例字典 | 单任务预算失效（被分摊） |
| 熔断器状态 | `circuit_breaker.py` 模块级单例 | 每个 worker 独立计数，实际阈值变成 N× |

所以 `manage.py` 启动命令**没有 `--workers`**（`manage.py:790`），
这是**有意为之而非疏漏** —— 加 `--workers 4` 会立刻出现"任务提交后查不到"的诡异 bug。
（`core/idempotency.py:21` 的注释也明确写了"`uvicorn src.main:app` 单 worker 完全够用"。）

代价是：**这套系统当前只能垂直扩容（换更强的机器），不能水平扩容。**

### 3.4 【内存泄漏】TaskStore 永不淘汰

实测 200 个请求完成后：

```
TaskStore 并发 200: 任务残留=200  live残留=0  token残留=200
```

- `_tasks`（`TaskRecord`）—— 每个任务留一份完整 `agent_outputs` + `final_report` 字符串，
  **没有 TTL、没有容量上限、没有淘汰**。
- `_tokens`（`CancellationToken`）—— `cancel_task` 和 `_run` 的 finally 都只清
  `_live_state`，**从不清 `_tokens` 和 `_handles`**（只有 lifespan 关停时的
  `cancel_all()` 会 clear）。
- `_handles` 同理 —— 完成的 asyncio Task 对象一直被引用。

按每次分析产出几十 KB 报告估算，**1 万个任务就是几百 MB 常驻内存**，
且进程永不释放。这是长时间运行后必然出问题的地方。

### 3.5 【缺护栏】成本与速率没有"系统级"上限

现有护栏都是**单任务**口径：
- `llm_token_budget_per_task = 200000`（按 `trace_id` 计）
- `llm_max_tokens_hard_cap = 32768`（单次调用）
- `llm_input_char_hard_cap = 6000`（单次输入）

**没有**：租户级日配额、全局 QPS 上限、同时在飞任务数上限、全局日成本上限。
多租户下这意味着**一个用户可以耗尽所有人的 LLM 配额**。

（注：`src/domain/quota/` 与 `src/domain/auth/ratelimit.py` 存在，
但经走查未发现投研接口挂载它们 —— 用于认证/平台侧，不是投研链路的护栏。）

### 3.6 【次要】结果缓存的击穿窗口

```python
# routes/research.py:139-144
if not force_refresh:
    cached = _RESULT_CACHE.get(qhash)
    if cached and cached[0] > time.time():
        return cached[1]     # 命中
    elif cached:
        _RESULT_CACHE.pop(qhash, None)
# ... 中间没有任何"占位"写入 ...
# 直到 _run() 完成才写 _RESULT_CACHE[qhash]（第 248 行）
```

从"检查未命中"到"写入缓存"之间有**整个管线执行时间**（数十秒）。
这期间**任何**相同 `qhash` 的请求都会重新起一条完整管线。
10 个用户同时点同一个热门问题 → **10 条完整管线**，而不是 1 条 + 9 次等待。

同样地，`_RESULT_CACHE` 无上限、无淘汰（虽然条目比 TaskStore 小得多）。

---

## 四、已经做对的部分（值得保留）

不只是找问题 —— 这套代码在并发上有多处**主动优化**，改动时不要破坏：

1. **`ConnectorRouter` per-key 锁防击穿**（`router.py:360`）
   ```python
   lock = self._per_key_locks.setdefault(indicator, asyncio.Lock())
   async with lock:
       # 二次检查（持锁期间其他协程可能已写入 TTL）
       hit = self._ttl_hit(indicator, now, demanded)
   ```
   多用户同时要同一个指标时，**只有一次网络请求**，其余等锁后直接拿缓存。
   这是标准的 single-flight 模式，做得很正确。

2. **数据降级三级链**：实时 → 入库快照 → LLM 自修复连接器。
   并发压力下源站变慢时，后面的请求能直接吃快照。

3. **熔断器全局共享**（虽然是单进程内）：一个 provider 挂了，
   所有用户**同时**快速降级，而不是各自慢慢超时。

4. **WAL + busy_timeout + 分级锁等待**（`event_sqlite_base.py`）：
   `CONNECT_TIMEOUT_S=20.0` 给关键写排队，`FAST_TIMEOUT_S=2.0` 给顺带写快速降级，
   `EXPIRE_TIMEOUT_S=0.4` 让懒过期不拖慢列表接口。
   注释里记录了"把清理写也设成 20s 导致线程池被打满，比原来 500 更糟"的实测教训。

5. **关键路径独立线程池**（`core/executors.py`）：`/health` 不再被业务任务挤死。

6. **AST 白名单 + 15s 沙箱**：自修复生成的代码不会在并发下把进程拖死。

7. **每个请求独立的 `CancellationToken`**：用户 A 点停止不会影响用户 B。

---

## 五、改进建议（按性价比排序）

### P0：改动小、收益最大的四项

#### ① 全局准入控制（约 30 行）

在 `submit_analyze` 入口加信号量，把"无限并发"变成"排队"：

```python
# src/api/routes/research.py
import asyncio
from src.core.errors import BriefError  # 或在 error_codes 里加一个 SPEC

#: 同时在飞的投研任务上限。按 LLM 配额与机器资源定，不按用户数定。
_MAX_INFLIGHT = int(os.environ.get("MOSS_RESEARCH_MAX_INFLIGHT", "4"))
_INFLIGHT = asyncio.Semaphore(_MAX_INFLIGHT)
#: 排队超时：等不到名额就明确告诉用户"前面还有 N 个任务"，而不是无限等
_QUEUE_TIMEOUT_S = float(os.environ.get("MOSS_RESEARCH_QUEUE_TIMEOUT", "30"))

async def _run() -> None:
    try:
        await asyncio.wait_for(_INFLIGHT.acquire(), timeout=_QUEUE_TIMEOUT_S)
    except TimeoutError:
        store.update(task_id, status="failed",
                     error=f"分析队列已满（当前 {_MAX_INFLIGHT} 个任务进行中），请稍后重试")
        return
    try:
        ...  # 原有逻辑
    finally:
        _INFLIGHT.release()
```

要点：**上限按 LLM 配额定，不按用户数定**。DeepSeek 的实际 RPM/并发限制是多少，
`_MAX_INFLIGHT` 就设多少除以"单任务平均 LLM 调用数"。先设 4，压测后再调。

#### ② 同问在飞合流（约 20 行）

把 3.6 的击穿窗口堵上 —— 让 `qhash` 在**开始执行时**就占位：

```python
#: 正在计算的 qhash -> list[Future]。后到的相同请求挂上去等结果，不重跑。
_INFLIGHT_RESULTS: dict[str, list[asyncio.Future]] = {}

async def submit_analyze(...):
    ...
    if not force_refresh:
        cached = _RESULT_CACHE.get(qhash)
        if cached and cached[0] > time.time():
            return cached[1]
        waiting = _INFLIGHT_RESULTS.get(qhash)
        if waiting is not None:
            # 已经有人在算同一个问题：登记一个 future 等它，然后复用同一个 task_id
            fut = asyncio.get_running_loop().create_future()
            waiting.append(fut)
            return {"task_id": (await fut), "status": "queued",
                    "deduplicated": True, "plan": plan["agents"]}
        _INFLIGHT_RESULTS[qhash] = []
    # ... _run() 完成时：写缓存 + 唤醒所有等待者 + pop 占位
```

对热门问题的效果是**数量级**的：10 个并发相同问题从 10 条管线降到 1 条。

#### ③ 语义缓存移出事件循环（约 15 行）

最小改动 —— 把扫描卸载到线程，事件循环不再被阻塞：

```python
# src/infrastructure/llm/cache.py
async def aget(self, system, prompt, agent_id="", scope=""):
    """异步版 get：语义扫描走线程池，不阻塞事件循环。"""
    key = cache_key(system, prompt, scope)
    hit = self._get_exact(key)          # 精确命中是单文件读，很快，可留在循环上
    if hit is not None:
        return self._mark(hit, "exact")
    return await asyncio.to_thread(
        self._get_semantic, system, prompt, exclude=key, agent_id=agent_id, scope=scope)
```

更彻底的做法（推荐，但要改多点）：**把 L2 索引进内存**
（文件名 → `{agent_id, scope, vector}`），进程启动时建一次、
`put()` 时增量更新。这样未命中扫描变成纯内存计算，9ms → 微秒级，
并且天然消除"文件数增长"的退化。

⚠️ 注意 `gateway.py:196` 的 `self._cache.get(...)` 要同步改成 `await self._cache.aget(...)`。

#### ④ TaskStore 的回收（约 25 行）

```python
# src/api/tasks.py
from collections import OrderedDict
import time

#: 完成任务保留数量与时长（超出先按容量淘汰，再按 TTL 淘汰）
_MAX_TASKS = int(os.environ.get("MOSS_TASK_KEEP", "200"))
_TASK_TTL_S = float(os.environ.get("MOSS_TASK_TTL", "3600"))

class TaskStore:
    def __init__(self):
        self._tasks: OrderedDict[str, TaskRecord] = OrderedDict()
        ...

    def _evict(self) -> None:
        """淘汰已完成任务与全部令牌/句柄：避免长跑后内存单调增长。"""
        now = time.time()
        for tid, rec in list(self._tasks.items()):
            if rec.status in ("completed", "failed", "cancelled"):
                if now - _parse_ts(rec.created_at) > _TASK_TTL_S:
                    self._tasks.pop(tid, None)
                    self._tokens.pop(tid, None)
                    self._handles.pop(tid, None)
        while len(self._tasks) > _MAX_TASKS:
            tid, _ = self._tasks.popitem(last=False)
            self._tokens.pop(tid, None)
            self._handles.pop(tid, None)
```

**关键**：`_tokens` / `_handles` 必须跟着 `_tasks` 一起清，
现在这三者只在 lifespan 关停时统一 clear。

### P1：要动架构，但为多用户必需

| 项 | 做法 | 解锁的能力 |
|---|---|---|
| 结果缓存 → Redis | `_RESULT_CACHE` 换成 Redis + TTL | 多 worker 共享命中 |
| TaskStore → Redis/DB | 任务与 live_state 外置 | **可加 `--workers N`**、可多实例 |
| 数据层 → PostgreSQL | `DATA_BACKEND=postgres`（工厂已支持） | 脱离 SQLite 单写者 |
| 租户级配额 | 在 `submit_analyze` 挂 `domain/quota` 的日配额 | 防单用户耗尽全局配额 |
| 预算改租户维度 | `_token_usage` 的 key 从 `trace_id` 改 `tenant_id:日` | 成本可预测 |

### P2：可观测性（先能看见，才能调）

现在**没有**任何并发相关指标。建议最少加四个：

- `research_inflight`（当前在飞任务数）与 `research_queue_wait_ms`
- `llm_cache_hit_rate` / `llm_cache_scan_ms`（直接盯 3.2 的退化）
- **事件循环滞后**（event loop lag）—— 这是发现"同步阻塞"的唯一直接信号
- `taskstore_size`（直接盯 3.4 的泄漏）

---

## 六、给当前部署的实操建议

如果你现在就要让一批用户用起来，**在上面的 P0 做完之前**：

1. **把 `_MAX_INFLIGHT` 当作唯一的容量旋钮**，从 4 开始，压测后调整。
2. **前端不要让用户空转**：`GET /research/{task_id}` 已经返回 `progress`，
   队列等待时返回"前面还有 N 个任务，预计等待 X 秒"比转圈好。
3. **业务上引导收窄 `analysis_type`**：`stock` 比 `full` 少一个数量级的 LLM 调用
   （plan_run 实测 agents 数：full=10, stock=8, macro=3）。
   大部分"这只票怎么样"的问题不需要 full。
4. **给 `DEEPSEEK_API_KEY` 配好余额监控**：token 预算只防单任务失控，
   不防多人累加。
5. **定期重启或加 TaskStore 淘汰**：长跑必然内存增长（3.4）。
6. **不要加 `--workers`**，直到 TaskStore/结果缓存外置完成。

---

## 七、一句话总结

**链路的"并发正确性"是过关的 —— 状态隔离、reducer 聚合、per-key 去重、哈希链并发
都经得起实测；缺的是"并发控制"。**

这套系统当前的设计前提是
"**单进程、少量任务、任务本身就是重活**"，
所以它把工程精力花在了"单任务内部的健壮性"（熔断、降级、自修复、防幻觉）上，
而没有花在"多任务之间的资源仲裁"上。

要支撑多用户，最小改动是 **P0 的四项（准入控制 + 同问合流 + 语义缓存卸载 + TaskStore 回收）**，
这四项都在单文件内、不动架构、不破坏已有的正确性保证，
预计能把可支撑的并发从 2~4 提升到 15~20，同时消除内存泄漏。

再往上就必须做 P1 的状态外置 —— 那是"从 Demo 到服务"的分界线。
