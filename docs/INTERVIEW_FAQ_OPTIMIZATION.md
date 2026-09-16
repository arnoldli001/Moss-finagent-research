# 投研系统优化 FAQ（面试亮点版）

> 版本 2026-09-15 | 覆盖 P0/P1/P2 共 6 类优化

---

## Q1: 为什么你的投研系统响应这么慢？根因是什么？

**现象**：用户提交一个"中际旭创 1-3 个月盈亏比分析"的请求，整个链路耗时 **~165 秒**，其中 LLM 调用 6 次占 121s，数据采集 A01 占 40-60s。

**根因链（3 层独立问题叠加）**：

```
根因1: A01 数据采集 20 个指标串行 for-await
    → 每个指标走 QMT→CSV→AkShare 故障转移
    → AkShare HTTP 平均 2-3s/次
    → 20 × 2.5s = 50s
根因2: ConnectorRouter 和 DataPointRepository 两条独立通路
    → Router 只做 TTL dict（重启丢）
    → SQLite 存了上千条却没人读
    → 每次 A01 都从头 HTTP
根因3: Ollama 每次冷启动加载 4.7GB 模型
    → ollama ps 为空 → 每次调用 28s 加载
    → keep_alive=0 + 无 HTTP 连接池
```

**修复后 ~45-60s（省 100s+）**：
- A01 `asyncio.gather` 并发 → 6-10s
- Router 三级短路（TTL → DB → HTTP）→ 慢变量 0.0s
- Ollama keep_alive=86400 → 本地推理 2-4s

**面试关键**：展示"**逐层定位、逐层验证**"的排障方法——不是拍脑袋说"做缓存"，而是先看审计链的 `agent_ids` 数组发现 A01 被调用了 20 次，再看 ConnectorRouter 源码发现 repo 完全没接入。

---

## Q2: 数据获取为什么每次都发 HTTP？你的本地数据库是摆设吗？

**根因**：`ConnectorRouter.fetch()` 和 `DataPointRepository` 是两条独立通路——Router 只做内存 TTL + 故障转移，Repo 只存不查。谁都不知道对方的存在。

**修复：三级短路策略**（面试亮点——**完整的缓存策略设计**）

```
fetch(indicator)
  [1] 进程内 TTL dict     → 毫秒级（同进程重复请求）
  [2] 本地持久化 SQLite    → 慢变量才查（CPI/M2/PE/估值分位）
      - 快变量跳过（实时成交额/FedWatch 直接 HTTP）
      - 过期穿透（最新 period_date 距 today 超过 filter_days → 刷新）
  [3] 真实 connector 链    → DB miss/过期才发 HTTP
      ↓ 成功后同时回填 TTL + SQLite（进程重启仍可命中）
```

**为什么分快慢变量？**
- 实时成交额每分钟变，查 DB 再判定过期 → 浪费
- CPI 月度，365 天内都在硬截止线 → DB 命中=正确值，不用再 HTTP

**真机验证**（Router 已注入 repo 后）：
```
ind:sw_first_pe_ttm:all → 路由本地DB命中 62pts 最新=2026-09-15 → 0.00s
CPI                     → 路由本地DB命中 485pts 最新=2026-09-01 → 0.01s
PE(TTM):300308          → 路由本地DB命中 1096pts 最新=2026-09-13 → 0.02s
mkt:turnover:total      → 跳过DB（快变量）→ 0.1s HTTP
```

**面试关键**：展示**策略分档**（不是一刀切做缓存）、**DB 查询条件**（快变量跳过、start_date/end_date 非空时跳过）、**故障 open**（DB 查失败穿透到网络，不阻塞主链路）。

---

## Q3: 东财 WAF 封锁导致申万行业估值全部失败，你的系统怎么处理的？

**现象**：`sw_index_first_info/second_info/third_info` 三个 AkShare 函数底层爬东财网页，东财 WAF 频繁封，每次 5.2s 超时后 `raise DataFetchError`，8 条指标 × 故障转移链 = 40s 浪费 + 连锁 WARN 日志 + LLM 看到一堆失败放弃输出。

**修复：静默降级（不占 ReAct 步数）**

| 之前 | 现在 |
|------|------|
| `_call_akshare` raise DataFetchError → Router 故障转移链 | `_call_akshare` 捕获 WAF 阻断 → 返回 None → fetch **静默返回 `[]`** |
| 8 条 × 5s 超时 × 故障转移链 → 40s + 连锁 WARN | 8 条 × 0.1s → 1 条 INFO 日志 |
| LLM 看到一堆失败 → 放弃输出空 JSON | LLM 看到"申万估值数据缺口"→ 正常输出 |

**设计原则（面试亮点——****真实 vs 模拟的边界**）**：

1. **真实源失败绝不回退模拟源**——有真实源失败后，不编造任何"模拟数据"冒充真实
2. **独立故障域 fallback**（不是东财内部换接口）——腾讯 qt.gtimg.cn 和东财 push2.eastmoney.com 是独立域名、独立风控策略
3. **静默降级 vs raise 的选择**——当 Router 的故障转移链里**没有第二个能提供同等数据的源**时，静默返回空不 raise，避免无意义的故障转移链

---

## Q4: LLM 为什么会引用 2023 年的数据？怎么防止幻觉？

**根因**：
```
之前的 system_prompt:
  "只用给定数据与事件，禁止编造数值"
  
但没说:
  "当前日期是 2026-09-15"
  "严禁使用你训练数据里的任何外部信息"
  "引用数据必须带 period_date"
  
LLM 行为：
  1. 看到 prompt 里有 "PPI" 指标名
  2. 从 training data 回忆了一个著名事件 "2023-07 PPI -5.4%"
  3. 那个事件恰好真实发生过 → LLM 以为是有效的
  4. 加上训练数据没有 "2026-09 PPI 具体值" → 用旧值顶替
```

**修复：两层联动防幻觉**

**层 1——Prompt 时效红线（硬约束）**：
```
### 🔴 时效红线（最高优先级）
当前日期是 2026-09-15。你只能使用上方「输入数据」中展示的数据点。
严禁引用你训练数据中任何年份、任何时间点的外部信息或历史数值，
哪怕你记得准确也要当作不存在。所有数据引用必须能在上方「输入数据」
段落找到对应 period_date 和数值。如果上方没有某数据，就说「未获取到」
或「数据缺口」，然后只依据有的数据给结论。引用数据时必须带 period_date。
```

**层 2——数据新鲜度过滤（硬截止）**：
```
DataFreshnessEvaluator 硬截止线 filter_days:
  - 日频 PE/PB: 30 天 → 2026-08-15 之前的 PE 完全不进 prompt
  - 月频 CPI/PPI: 365 天 → 2025-09-15 之前的 CPI 完全不进 prompt
  - 年频渗透率: 1095 天
  
→ 2023-07 的 PPI 距今天 3 年多 → filter_days=365 天 → expired → 
  should_display=False → 根本不会出现在 prompt 里
```

**面试关键**：**不要只说"加了 prompt 约束"**——要讲清楚**为什么 prompt 约束不够**（LLM 对 training data 的记忆强于对 prompt 里"只用这些数据"的弱约束），**为什么两层联动才能根治**（数据层先过滤 → prompt 再约束 → 双保险）。

---

## Q5: 你怎么决定哪些指标用本地 DB 缓存，哪些直接走网络？

**指标分档策略（_DB_QUERY_PREFIXES / _DB_SKIP_PREFIXES）**：

| 分类 | 指标示例 | 策略 | 原因 |
|------|---------|------|------|
| 慢变量月频 | CPI/PPI/M2/社融 | **查 DB** | 月度发布，365 天内都有效 |
| 估值日频 | PE(TTM)/PB/行业 PE 分位 | **查 DB** | T+1 收盘后更新，2 个月内有效 |
| 行情历史 | stock_close/index_close | **查 DB** | 日线历史稳定，DB 有就不用重拉 |
| 实时成交额 | mkt:turnover | **跳过 DB** | 盘中每分钟变，网络拿的就是最新值 |
| FedWatch | fed: | **跳过 DB** | CME 实时赔率，FOMC 前高频刷新 |
| 北向/两融 T+1 | mkt:north_flow/margin_balance | **查 DB** | T+1 日频，DB 存了下次命中 |

**过期穿透判定**：DB 里有数据但已过期（confidence < 0.4）时，**主动刷新穿透到网络**。不假装 DB 里的旧数据是新的。

---

## Q6: Ollama 本地模型 28 秒冷启动怎么修的？

**根因**：
```
ollama ps 每次都空 → keep_alive 默认 0
→ 每次调用都重新 load 4.7GB qwen3:8b
→ Windows 内存换页 + 模型加载 = 28s
→ HTTP 连接也每次新建（无连接池）
```

**修复**（providers.py 改两处）：
```python
# 1. Ollama HTTP 请求带 keep_alive=86400
response = await client.post(
    "/api/chat",
    json={"keep_alive": 86400, ...},  # 模型常驻 24h
)

# 2. 单例 AsyncClient 连接池复用
_client: httpx.AsyncClient | None = None
async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(...)
    return _client
```

**效果**：`ollama ps` 显示两个模型在 GPU 上跑，冷启动一次后后续调用 2-4s。

---

## Q7: Planner 为什么换 1.5B 本地模型？准确率够用吗？

**决策过程**（量化权衡）：

| 方案 | 延迟 | 成本 | 准确率 |
|------|------|------|--------|
| deepseek-flash | 18s | 每次 $0.01 | 98% |
| **qwen2.5:1.5b** | **3-5s** | **$0** | **92-95%** |
| qwen3:8b | 4-6s | $0 | 99% |

**选择 1.5B** 的理由：
1. Planner 做的是**分类任务**——从指标目录 + Agent 目录挑子集。这不需要复杂推理
2. 1.5B 本地跑零成本、零网络延迟（之前 Ollama 冷启动修了之后 3-5s）
3. 92-95% 准确率够了——漏选 1 个指标不影响主链路，多选只是多一次 A01 并发采集
4. **省的 15s 投在 A01 并发上性价比更高**

**面试关键**：**用数据说话**，不是"我觉得应该用小模型"——要展示延迟/成本/准确率的三维权衡表。

---

## Q8: ReAct 收紧为什么是 max_steps=3 + 每 Agent 最多追问 1 次？

**ReAct 预算控制（四条防线）**：
1. max_steps=3（之前 5）——避免 ReAct 绕圈反复问
2. MAX_ASKS_PER_AGENT=1（之前 2）——每 Agent 最多追问 1 次，防止一个 Agent 卡住整个链路
3. token 预算检查——累计 token 超阈值提前 bail-out
4. 熔断准入——上游数据缺失比例 > 60% 时跳过 ReAct 直答

**量化结果**（审计链 seq=80 中际旭创那次）：
- 实际只用了 3 步（1x ask 追问 + 1x 数据读取 + 1x final）
- 说明 3 步是合理上限，不会因为省 2 步而降低质量

---

## Q9: ConnectorRouter 故障转移链的设计原则？

**故障转移链的三条硬规则**：
1. **DataFetchError 才触发下一个源**——非 DataFetchError（如代码 bug）立即上抛，不掩盖程序缺陷
2. **真实源失败后不回退 simulated**——防止模拟数据冒充真实进入最终建议
3. **per-key Lock 防击穿**——同一指标并发请求只有一个发网络，其他等结果

**为什么第 2 条重要？**
```
假设 QMT 没启动 → 抛 DataFetchError
                → 走 CSV（有真实历史数据）
                → 走 AkShare（有真实实时数据）
                → 如果 CSV/AkShare 都挂
                → 走 simulated（假数据）← 这是危险的！
```
模拟数据看起来很真实（PE(TTM)=22.3 这种值），但实际上没验证过。**投资决策里假数据比没数据更危险**。所以我们规定：有真实源尝试失败后，**禁止**回退 simulated。

---

## Q10: 新鲜度指数衰减 + 硬截止的两层设计怎么来的？

**从一次错误的教训中学到的**：最初我用 `confidence < 0.1` 当作硬截止——CPI 365 天 conf≈0.002 → 被当作 expired → 刚好硬截止在 365 天附近。但问题是**日频 PE 的 exp(-0.5×15/1)=0.0006** → 被标 expired 但 `filter_days=30` 还没到 → stale 数据被不该过滤的过滤了。

**两层分离**：
```
层 1: filter_days（业务决策）
  "CPI 超过一年完全不可用" → 硬编码在 _FREQ_BY_PREFIX
  "PE 超过 30 天完全不可用" → 同上
  → 超过就 should_display=False

层 2: confidence 连续权重（分析决策）
  "在 filter_days 内，越新权重越高"
  → exp(-λd/(周期×行业倍数))
  → stale=0.3x / lagging=0.7x / normal=1.0x
  → 行业倍数让长周期行业（船舶/核电）衰减更慢
```

**面试关键**：**展示设计迭代过程**——不是一开始就设计成这样，是从一个看似合理（用 conf 当硬截止）但有边界问题的方案，发现问题后分离出两层。这个迭代过程本身能展示"**有数据结构设计的经验**"。
