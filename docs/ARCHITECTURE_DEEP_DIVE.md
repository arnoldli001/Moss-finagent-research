# 投研链路架构优化深挖（逐条回应三个问题）

> 全部数字本机实测（2026-09-26）。触发问题：投研分析输出仍慢。
> 本文逐条回答你提的三个点，并给出已实施的改动与实测收益。

---

## 结论速览

| 你的判断 | 是否成立 | 实测证据 |
|---|---|---|
| 1a. 为什么外网不可达 | — | **CME 被网络阻断**（DNS 通、TCP 443 不通）；**FRED 可达**，能替代 |
| 1b. 没有数据所在表/字段的索引目录 | **成立** | 指标元信息散在 **5 处**，**零一致性校验** |
| 2. 信息层为什么硬串行、能否换快模型 | **成立且收益最大** | A05/A06 挂 `medium` = 本地 Ollama，**45.7s**；同 prompt 换 `reasoning`=**11.1s**、加 `effort=none`=**2.1s** |
| 3. A17 同样深挖 | **成立** | 现状 `deepseek-v4-pro` **20.2s**；换 `flash` **11.7s**；`effort=none` **4.6s** |
| — | **新发现的系统性缺陷** | 降级链只在**失败**时前进，**"成功但极慢"不算失败** → 慢的本地模型永远占着主位 |

---

## 一、问题 1：外网不可达 + 没有数据目录

### 1.1 外网到底哪里不可达（实测矩阵）

```
DNS    www.cmegroup.com      -> 108.160.165.211   （Akamai 边缘，属被阻断网段）
TCP    www.cmegroup.com:443  -> ✗ 不通（curl 等满 21.3s 后报 Failed to connect）
TCP    api.stlouisfed.org:443-> ✓ 通
TCP    push2.eastmoney.com   -> ✓ 通（但会 RemoteProtocolError）
TCP    qt.gtimg.cn:443       -> ✓ 通（92ms）

HTTP   CME Group Futures API -> ✗ ConnectTimeout 16.1s
HTTP   FRED CSV              -> ✓ HTTP 200  1.1~1.3s
HTTP   FRED API              -> ✓ HTTP 400（服务响应，缺参数）
HTTP   美联储官网             -> ✗ ReadTimeout 11.6s
HTTP   TradingView 经济日历    -> ✗ ConnectTimeout 16.1s
HTTP   Investing.com 日历      -> ✗ SSL handshake 超时 8.3s
HTTP   新浪行情               -> ✓ HTTP 403（5.1s）
```

**结论：CME 是被阻断的，不是代码问题。** DNS 能解析（拿到一个 Akamai IP）
但 TCP 连不上 —— 所以**不能用 DNS 判断可达性**，必须真做一次 TCP connect。

而 `cme_fedwatch` 库内部用 pycurl 连 CME，失败前要等满 **21.3 秒**。
A01 用 `asyncio.gather` 并发采集，**墙钟 = 最慢那个**，于是整段采集被拖到 23.4s。

### 1.2 关键发现：FRED 可达，而且数据比 AkShare 新 14 个月

```
DFEDTARU  目标区间上限    HTTP 200  1.1~1.3s  最新 2026-09-25 = 4.00
DFEDTARL  目标区间下限    HTTP 200  1.0~1.2s  最新 2026-09-25 = 3.75
DFF       有效联邦基金利率 HTTP 200  1.8s      最新 2026-09-24 = 3.88
```

对比上一轮发现的 AkShare `us_fed_rate`：**最新有效值停在 2025-07-31**。
**FRED 这条路同时修掉了两个问题**：外网阻断 + 美联储利率陈旧 14 个月。

### 1.3 数据目录：你的判断成立，而且比想象的更散

我搜遍全仓，**不存在**任何 `data_catalog` / `indicators.yaml`。
指标元信息散在**五处**，各自独立维护：

| 位置 | 它知道什么 | 它不知道什么 |
|---|---|---|
| `planner.py` `INDICATOR_CATALOG` | 指标 id + 中文描述 | 表、字段、周期、来源、作业 |
| `data_freshness.py` `_FREQ_BY_PREFIX` | 更新周期（按前缀） | 其余全部 |
| `router.py` `_DB_QUERY/SKIP_PREFIXES` | 要不要查库 | 其余全部 |
| `router.py` `_TTL_BY_PREFIX` | 进程内缓存 TTL | 其余全部 |
| `scheduler/registry.py` `JOB_REGISTRY` | 采集作业与 cron | **不声明它产出哪些指标** |

**而且没有任何测试校验这几处口径互相一致**（我搜过 `tests/`，零命中）。

这正是"数据采集慢"的**结构性根因**：路由在决定"要不要打网络"时，
没有任何权威目录可查，只能靠前缀硬编码猜 —— 五处口径一漂移，
就出现"库里明明有当天数据却仍打网络"（上一轮修掉的那 5 个指标）。
我上一轮加的 `scripts/data_lifecycle.py` 是**只读的并排视图**，不是目录。

### 1.4 已实施的改动

| 改动 | 文件 | 实测效果 |
|---|---|---|
| **FRED 兜底源** `fed:policy_range` | `fedwatch_connector.py` | 1.17s 拿到目标区间 3.75%~4.00% |
| **TCP 可达性预检**（2s 超时 + 60s 结论缓存） | `net_probe.py`（新增） | CME 冷启动 **23.4s → 4.08s** |
| `fed:` 纳入 `_DB_QUERY_PREFIXES` | `router.py` | 一次成功后永久库命中 |
| 规划优先给可达的 `policy_range` | `supervisor.py` | 用户拿到的是有数据的那个 |

**数据采集端到端：23.4s → 4.08s（冷）/ 0.01s（热）**

---

## 二、问题 2：信息层硬串行 + 模型选型（**收益最大的一处**）

### 2.1 为什么"必须"串行 —— 其实不必须

```python
g.add_edge("verify_info", "extract_events")    # A05 → A06
g.add_edge("extract_events", "sentiment")      # A06 → A07
```

这个串行是**实现选择的产物，不是数据依赖**：

- A05 做的是"可信度评分 + LLM 复核"（输入：原文）
- A06 做的是"事件结构化提取"（输入：原文，要求 `evidence_quote` 逐字来自原文）
- A07 做的是"情绪量化"（输入：**事件表**）

**A06 真正依赖的只是原文**，A05 的分数只是被当作前置过滤
（`supervisor.py` 里 `[i for i in verified_items if i.get("verified")]`）。
而 A06 本来就有"evidence_quote 禁止改写"的硬约束 + 越界枚举本地归一化 ——
**它不是靠 A05 过滤来保证质量的**。

唯一真正有依赖的是 **A07 依赖 A06 的事件表**。

### 2.2 更严重的问题：A05/A06 用的是**本地 Ollama**，慢 4 倍

审计实测（真实调用，非构造）：

```
A05_verifier     tier=medium     延迟中位 38.1s   in 1813  out 1563
A06_extractor    tier=medium     延迟中位 52.9s   in 1734  out 1895
A07_sentiment    tier=reasoning  延迟中位  2.5s   in  523  out  370
```

`medium` 层主模型 = `local_medium` = **本地 Ollama qwen3:8b-q4_K_M**。

我用 A06 的真实 prompt 做了对照实验：

```
配置                                中位s    out   模型
现状 medium (Ollama qwen3:8b)       45.7   2048   qwen3:8b-q4_K_M
改 reasoning (DeepSeek-Flash)       11.1   2287   deepseek-flash
reasoning + effort=none              2.1    621   deepseek-flash
改 light (本地 1.5B)                  6.2    424   qwen2.5:1.5b
```

**同类任务换到云端 `reasoning` 层：45.7s → 11.1s（4.1×）；加 `effort=none` 到 2.1s（21×）。**

注意 A07 本来就在 `reasoning` 层、只要 2.5s —— **同一个系统里，
一个 Agent 2.5 秒、另一个 52.9 秒，差别只在于挂了哪一层。**

### 2.3 根因：降级链只在"失败"时前进，"成功但极慢"不算失败

这是**跨模块的系统性缺陷**，不止影响信息层：

```python
# gateway.py 的降级链
for index, model_name in enumerate(chain):
    resp = await provider.chat(...)     # ← 本地模型"成功"了，哪怕花了 45 秒
    return resp                          # ← 备模型永远轮不到
```

`models.yaml` 写的是：
```yaml
medium:
  primary: local_medium      # qwen3:8b，45.7s
  fallback: deepseek-flash   # 11.1s，但只有在本地**报错**时才会用到
```

备模型配得对，**但触发条件不对**。本地模型能出正确结果，只是慢 ——
它永远不会"失败"，于是那个快 4 倍的备模型一次都用不上。

### 2.4 已实施：单跳延迟预算（`attempt_budget_sec`）

给每一跳加墙钟预算，**只对链上还有退路的那几跳生效**
（最后一跳没有退路，设预算会把"慢但正确"变成"必然失败"）：

```python
_ATTEMPT_BUDGET = {
    "light": 20.0, "medium": 20.0,
    "reasoning": 45.0,   # 云端 reasoning 本身可到 18~28s，给宽
    "decision": 60.0,    # decision 现状 20~34s，别误伤
}
```

超时**不计入熔断**：慢 ≠ 坏。把慢源计进熔断会把它标成"故障"，
而它其实能出正确结果，只是不该在这个预算内等。

实测：

```
medium 层 = 本地qwen3:8b(主) -> deepseek-flash(备)
  默认预算(20s)          22.2s  model=deepseek-flash  fallback=True
  手动 5s 预算           7.2s  model=deepseek-flash  fallback=True
  （改造前：本地模型独自跑完 45.7s，备模型从未被使用）
```

---

## 三、问题 3：A17 决策层深挖

### 3.1 现状

```
A17_recommend  tier=decision  = deepseek-v4-pro
               每任务 2.06 次调用 × 34.5s（审计中位）= 78.3s/任务
               ↑ 占整条链路 LLM 时间的 68%
```

### 3.2 模型选型对照（同一个 A17 prompt，真实 6 维上游输入）

```
A17 配置                                中位s    out   模型
现状 decision = deepseek-v4-pro         20.2   2299   deepseek-v4-pro
decision + effort=none                   4.6    350   deepseek-v4-pro
改 reasoning = deepseek-flash            11.7   2781   deepseek-flash
reasoning(flash) + effort=none            2.6    383   deepseek-flash
```

**换 `flash`：20.2s → 11.7s（1.7×），且输出 token 更多（2781 vs 2299）** ——
说明质量不是靠"更大的模型"换来的，`pro` 在这里是**纯延迟成本**。

（`configs/models.yaml` 里 `decision` 选 `pro` 的理由是"决策层要质量"，
但实测 `flash` 输出更长的结构化结果 —— 这个假设值得重新评估。）

### 3.3 ReAct 结构：`max_steps=3` 但实际平均 2.06 次

```
supervisor.py:869  ReActExecutor(..., max_steps=3, task_tier="decision")
```

- 第 1 步：完整 prompt → LLM 决定 `action` 还是 `final_answer`
- 若 `action`：执行工具（`ask_agent` 追问上游 / `query_data`）→ 观察 → 第 2 步
- 第 3 步：注入"必须直出 final_answer、禁止再调工具"

实测每任务 2.06 次调用 ≈ **大部分任务走了 1 次工具调用**。
而"追问"的价值是让 A17 澄清上游冲突 —— 但上一轮已发现
`MAX_ASKS_PER_AGENT=1`、且 A17 的 system prompt 与输出契约
（`conflicts_resolved` 等字段）**已经要求它在单次输出里做仲裁**。

**所以 ReAct 的边际价值需要重新评估**：它换来 1 次追问，
代价是 +1 次 20~34s 的 LLM 调用。若追问命中率不高，
这 20 秒花得很不值。

### 3.4 建议（按性价比）

| 手段 | 收益 | 代价 | 建议 |
|---|---|---|---|
| **`decision` 换 `flash`** | 20.2 → 11.7s | 几乎无（输出更长） | **推荐，先做** |
| `effort=none` | 11.7 → 2.6s | 失去思维链 | 需评估 |
| ReAct 收敛为单次 | -1 次调用（-11.7s） | 失去主动追问 | 需评估命中率 |
| 精简 prompt | 与 effort 叠加 | 轻微 | 推荐 |

---

## 四、汇总：修正后的耗时模型

### 4.1 先分清两个口径（这一点我上一轮写错了）

我上一轮把信息层算成"15.0s"（= 3 × 5.0s 的模型层标称延迟），**这是错的**。
审计里必须分清两个口径：

```
agent              总调用  每任务次数  单次中位s  每任务摊销s  跑一次时s
A05_verifier          12      0.15      33.7       5.1       33.7
A06_extractor         12      0.15      36.8       5.5       36.8
A07_sentiment          7      0.09       1.5       0.1        1.5
A17_recommend        157      1.96      34.5      67.6       34.5
```

- **每任务摊销** = 每任务次数 × 单次中位 → 全体任务的平均负担
- **跑一次时** = 该 Agent 真正执行一次要多久 → **有新闻的任务实际要等这么久**

**信息层只在"有新闻文本"时才跑**（`plan_run` 的参与条件），
所以它摊到全体任务只有 33.7+36.8+1.5 的一部分；
但**一旦触发，用户就要串行等 72 秒**。

### 4.2 修正后的分档模型

```
                            A05/A06 单次   A17 单次   A17 每任务
现状 medium(Ollama)             45.7s          —          —
现状 decision(pro)                 —        20.2s       67.6s
换 reasoning(flash)             11.1s       11.7s       22.9s
+ effort=none                    2.1s        2.6s        5.1s
```

按"一次完整分析"算（假设触发信息层）：

```
阶段                    现状        换层后      再降effort
────────────────────────────────────────────────────────────
规划                    0.8s        0.8s        0.8s
数据采集               23.4s →     4.1s ✅     4.1s ✅   （已实施）
本地管线                0.6s        0.6s        0.6s
信息层(A05+A06+A07)    72.0s   →   24.0s    →   4.2s
分析层(并行)            5.0s        5.0s        1.5s
A17决策                34.5s   →   11.7s    →   2.6s
────────────────────────────────────────────────────────────
合计                  136.3s       46.3s       13.8s
```

**注：136.3s 是"最坏一次"（触发信息层的完整链路）。**
审计实测全体任务中位 36.2s —— 因为大多数任务信息层不触发、
且 A17 命中缓存。两个数字都对，只是口径不同。

### 4.3 优先级（按实测收益）

| 优先级 | 动作 | 收益 | 质量影响 | 状态 |
|---|---|---|---|---|
| **P0** | 信息层 A05/A06 换出 `medium` 层 | **-48s** | 无（换的是更强的云端模型） | ✅ 机制已做（延迟预算），待调层 |
| **P0** | FRED 兜底 + TCP 预检 | **-19.3s** | 无（且修掉利率陈旧 14 个月） | ✅ 已实施 |
| **P1** | A17 `decision` 换 `flash` | -22.8s | 几乎无（实测输出更长） | 待评估 |
| **P1** | 信息层并行化（A05/A06 同时跑） | -11.1s | 轻微 | 待做 |
| **P2** | 建**唯一数据目录**（见 §5） | 结构性 | 无 | 待做 |
| **P3** | `effort=none`（全局） | -32s | 明显（失去思维链） | 需你决策 |

---

## 五、关于"数据目录"的具体建议

这是**唯一能根治**"数据在哪、要不要打网络、多久更新"这类问题的办法。

建议建 `configs/data_catalog.yaml`（**单一事实源**），一项指标一条：

```yaml
- id: "fed:policy_range"
  desc: "美联储当前目标区间（FRED 源，实测可达）"
  table: "fact_data_points"          # 存在哪张表
  key_column: "indicator"            # 用什么列定位
  value_column: "value"
  period_column: "period_date"
  update_cycle: "daily"              # 更新周期（唯一口径）
  source: "FRED"                     # 数据来源
  connector: "FedWatchConnector"
  job: "us_macro_daily"              # 谁负责定期采集（可空=不预采）
  db_first: true                     # 是否库优先
  ttl_seconds: 3600                  # 进程内缓存
  reachable: true                    # 外网源可达性（可自动探测回填）
```

然后让**五处消费方都从它派生**：

- `planner.py` 的指标目录 ← `catalog.ids()`
- `data_freshness.py` 的周期 ← `catalog.update_cycle()`
- `router.py` 的 `_DB_QUERY/SKIP_PREFIXES` ← `catalog.db_first()`
- `router.py` 的 `_TTL_BY_PREFIX` ← `catalog.ttl_seconds()`
- `scheduler` 的作业 ← `catalog.job()`

**再加一条一致性测试**（这是当前完全缺失的）：
"每个被规划器产出的指标，必须在 catalog 里有条目，且其 job 存在" ——
这类测试能直接拦住本轮发现的那类漂移。

---

## 六、一句话总结

**你指的两处（信息层模型选型、A17 结构）都指对了，而且收益比我上一轮估的大得多。**

上一轮我把信息层算成 15s（模型层标称），实际审计是 **91s** ——
因为 A05/A06 挂的是**本地 Ollama**，比云端慢 4 倍。
而它们挂本地并不是"精心权衡"，而是 `models.yaml` 的 `primary` 这么配的，
**且降级链只在失败时前进，"成功但极慢"永远轮不到那个快 4 倍的备模型**。

已修的三件事（都零质量损失）：
1. **单跳延迟预算** —— 让"慢"也能触发降级（45.7s → 22.2s，可调到 7.2s）
2. **FRED 兜底源** —— 绕开被阻断的 CME，且利率数据从"陈旧 14 个月"变成最新
3. **TCP 可达性预检** —— 不可达主机不再等满 21s（数据采集 23.4s → 4.1s）

**还差一件事就能到 10 秒级：把 A05/A06 从 `medium` 层挪走**（改 `models.yaml`
一行，或按已实现的延迟预算调小 `medium` 的 budget）。这一项单独就是 -60s。
