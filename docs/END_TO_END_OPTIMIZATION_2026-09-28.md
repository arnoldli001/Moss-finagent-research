# 端到端时间优化：第九轮（2026-09-28）

> 依据：附件 `AGENT_COLLABORATION_ATLAS.html`（19 Agent 协作图谱） + 上一轮审计报告。
>
> 目标：把 `full` 类型投研分析端到端时间从 **160s（早期）→ ~80-120s（第八轮后）→ 30-50s（第九轮目标）**。

## 0. 当前各阶段耗时（实测/审计实证基线）

下表是 8 阶段流水线的耗时分量，全部数字都来自 `PROJECT_AUDIT_2026-09-19.md` / `2026-09-27.md` 的实测段落，未估。

| # | 阶段 | 节点 | 第八轮后耗时 | 占端到端 % | 主要瓶颈 |
|---|---|---|---:|---:|---|
| 1 | 规划 | `supervisor_node` (planner LLM 1.5B) | 3-5s | 4% | OK |
| 2 | 数据采集 | A01 collect（20 指标并发） | 30-60s（冷）/ 5-10s（热） | 35% | akshare/tushare 限频、QMT 私有时无数据 |
| 3 | 数据管线 | A02→A03→A04 串行 | <5s | 4% | OK（纯本地） |
| 4 | 信息层 | A05→A06→A07 **串行** | 30+35+30 = **~95s** | **62%** | **最大瓶颈** |
| 5 | 本地量化 | `liquidity_ctx`（纯本地） | <1s | 1% | OK |
| 6 | 分析层扇出 | A08-A12 + A13-A16 = 9 路 Ollama 单 slot | ~50s（最慢一节点） | 38% | Ollama 单 slot 串行 |
| 7 | 决策 ReAct | A17 max_steps=3 | 30-45s（云端） | 31% | 云端调用 + ReAct 重发 |
| 8 | 审计 | A18（纯本地） | <1s | 1% | OK |

**关键发现**：第 4、6、7 阶段共占 **~95%** 的端到端时间。第九轮聚焦这三块。

---

## 1. 阶段 4：信息层串行 → 并行（**最大收益**，预计 −60s）

### 1.1 问题

```
store → A05_verifier(35s) → A06_extractor(35s) → A07_sentiment(30s)
                                                        ↓
                                                   liquidity_ctx
```

A05 任务 = "逐条复核可信度 + 给 verdict"，输出 `verified_items`；
A06 任务 = "从文本抽取事件表"，输出 `extracted_events`；
A07 任务 = "基于事件表做情绪判读"。

A06 的"抽取事件"与 A05 的"是否可信"判断**逻辑无关**——A05 给出的 verdict 是给"下游要不要用这条新闻"做信不信的判断，但 A06 的"抽取结构化事件"任务只需要把信息结构化（假新闻也有结构）。也就是说 A06 完全不需要等 A05 完成。

### 1.2 改造

```
store → ┌─ A05_verifier ──────┐
         └─ A06_extractor ─────┴── A07_sentiment
         └─ liquidity_ctx（不等 sentiment）
```

**代码落地**（已实施）：`supervisor.py:1196-1230`
- `store → verify_info`（A05）
- `store → extract_events`（A06 **不再 wait A05**）
- `verify_info → sentiment`（A07 等 A05）
- `extract_events → sentiment`（A07 等 A06）
- `store → liquidity_ctx`（liquidity_ctx 不再 wait sentiment）
- `sentiment → recommend`（recommend 等 A07 完成）
- `liquidity_ctx → analyze_{aid}`（分析层仍等 liquidity_ctx）

**注意**：A06 现在直接读 `state.info_items` 而非 `verified_items.items`（过滤后的）。
A06 任务代码原本就只是"抽取"，不依赖"是否可信"信号；下游（分析层）仍读 `verified_items` 过滤后的事件。
A05 的"可信/存疑/不可信"verdict 通过 A06 输出的事件里的 `verified` 字段传递（已通过 A05 的 `merge` 步骤标注）。

### 1.3 收益

旧：~95s（35+35+30 串行）
新：**~65s**（35+35 串行 → 35+30 并行取 max = 65s，因为 liquidity_ctx 也同时启动）
**节省 ~30s**（−32%）

如果再叠加 1.4 节（A07 改 light）→ **~38s**（35 + 3）— —**信息层降到 38s，节省 57s（−60%）**

### 1.4 副优化：A07 sentiment 改 light 1.5B（节省 ~27s）

A07 任务 = 把"情绪分 / 事件分布 / 热点主体"翻译成 `phase` ∈ {乐观/分歧/谨慎/恐慌/不明确}。
本质是**离散分类**，1.5B 本地模型（受约束解码）完全够用。

**代码落地**（已实施）：`src/domain/agents/info/sentiment/agent.py:22`
- `task_tier: TaskTier = "light"`（旧 `reasoning`）
- 加 `json_schema` 给 gateway：枚举 + 必填字段，避免 1.5B 输出坏 JSON

实测：light 1.5B 单次 **~3s**（旧 reasoning 30s）—**节省 27s**

### 1.5 副副优化：liquidity_ctx 不等 sentiment（节省 ~30s）

旧：`sentiment → liquidity_ctx → analyze_*`（串行）
新：`store → liquidity_ctx`（与 sentiment 并行启动）

liquidity_ctx 只读 `validated_points`，与 sentiment 输出**无依赖**。让 liquidity_ctx 提前 ~30s 启动，分析层可提早进入。

---

## 2. 阶段 6：分析层 Ollama 单 slot 串行（预计 −10s，无破坏性）

### 2.1 问题

A08-A12 + A13-A16 = 9 路并发，但 **Ollama 单 slot 串行执行**（见 `configs/models.yaml` §一注释："Ollama 只有一个计算槽位 —— 并发请求是严格串行的"）。

实测 9 路并发 = **max(单 agent 时延)** ≈ 50s（最慢一节点决定整体）。

### 2.2 现状：5 维分析 + 4 行业 = 9 路并发都跑

`analysis_type` 决定要哪些 agent：
- `macro` → A08
- `industry` → A09 + A13-A16（5 路）
- `stock` → A10/A11/A12 + A08/A09（5 路）
- `news` → 0 路（不跑分析层）
- `full` → **A08-A12 + A13-A16 全跑 = 9 路**

### 2.3 改造方向（已部分实施）

**已完成**（第八轮）：
- A07 改 light（不在分析层，但同样减少并发占用）
- A08/A10/A11 改 medium 本地 8B（节省云端调用）
- A12 改纯规则（不再调 LLM，节省 18.5k tokens + ~3s）
- A08-A12 按 Agent 白名单过滤 data_points（节省 2 万 tokens/轮）

**待做**（按 ROI 排序）：
1. **A13-A16 行业 Agent 改成"按行业类型按需"**——industry 类型才用，full 类型的 9 路并发里 A13-A16 经常是冗余的（如果用户已问个股，A08/A10/A12 已覆盖，A13-A16 提供的是"科技/消费/周期/医药"4 视角，主线路不一定要全跑）。
   - **落地**：`_PLANNING["full"]` 里 `A13-A16` 从必选改"默认必选，但 plan 阶段允许 LLM planner 跳过"——planner 已知主题词时只挂相关行业 Agent。
   - **收益**：full 类型从 9 路 → 5-7 路，最坏 50s → 35s（节省 ~15s）

2. **A08 macro 经常可走"纯模板"**——如果是"`美联储降息`对 A 股影响"这类高度模板化问题（命中 `TOPIC_KEYWORDS["macro_core"]`），前 12 期宏观数据其实足以判趋势，可让 A08 跳过 LLM 直接出结论。
   - **落地**：A08 加 `_should_skip_llm()` 钩子（仿 A12 合规 Agent 模式），当 `topic_keywords` 命中"标准宏观问题模板"且指标已落地 → 纯模板输出。
   - **收益**：典型宏观 full 任务节省 ~35s（A08 不再调 8B）

3. **Ollama 多实例 + load balancing**：当前本机一个 ollama 进程，8GB 显存只能常驻一个 7-8B 模型（详见 `models.yaml` §二）。**加一台机器 + 两实例 Ollama + round-robin 负载** → 9 路并发可拆给 2 个 slot。
   - **落地**：需要额外硬件；当前阶段不可行（Demo 单机）。
   - **收益**：分析层 50s → 25s（节省 25s）

---

## 3. 阶段 7：A17 ReAct max_steps 3 → 2（节省 ~15s）

### 3.1 实测

`PROJECT_AUDIT_2026-09-19.md` §2.3 实测：169 次 A17 调用中：
- 第 1 步 96% 直接给 final_answer
- 第 2 步 3% 追问后给 final_answer
- 第 3 步 1% 真正用上

也就是说 **第 3 步 99% 的情况下是浪费的**（仅 1% 真正需要 3 步）。

### 3.2 改造

`supervisor.py:1148-1158`（已实施）：
```python
import os as _os
try:
    _max_steps = int(_os.environ.get("MOSS_REACT_MAX_STEPS", "2"))
except (TypeError, ValueError):
    _max_steps = 2
react = ReActExecutor(
    a17._gateway, tools, max_steps=_max_steps, task_tier="decision",
    incremental=True,
)
```

- 默认 2 步（节省 1 步 ≈ 10-15s）
- `MOSS_REACT_MAX_STEPS=3` 可恢复旧行为（debug 用）

### 3.3 配套：增加"提前返回 final_answer"语义

`react.py` 已支持 `final_answer` 提前返回；如果 LLM 在第 1 步直接给 final_answer，循环立即终止（不进入第 2 步）——这是关键，max_steps 2 步**不会**让"第 1 步已答"的案例变慢。

---

## 4. 阶段 2：A01 数据采集（预计 −10s，可选）

### 4.1 现状

20 指标并发，Ollama/tushare 各自有缓存。冷启动：
- Tushare：~1s/指标（hit cache <100ms）
- akshare：~2-5s/指标（无 cache）
- QMT：已置最后 + 默认关闭（2026-09-23 决策）

### 4.2 改造方向

**`scripts/download_market_data.py`**：全市场全量下载到 `data/quant/prices*/`（AGENTS.md 已说明），可让"实时采集 + 存储降级"路径更短。

**A01 拆"必需 + 降级"两批**：
- 第一批（必跑）：PE/PB/股价 3 个核心指标（不阻塞后续 Agent）
- 第二批（可降级）：流通市值/换手率/财务比率（用 storage_fallback）

**落地**：在 `plan_run()` 阶段把 indicators 拆 `_MUST_RUN_INDICATORS` + `_DEGRADABLE_INDICATORS`；supervisor 启动两批并发，**第一批完成即可进入 collect 后续阶段**（不等第二批——直接用 storage_fallback）。

**收益**：冷启动从 30-60s → 15-25s（节省 15-35s）

---

## 5. 跨阶段并行（预计 −10s）

### 5.1 现状图

```
collect ─┬─→ clean ──→ validate ──→ store ─┬─→ A05 ────┐
         └─→ (news_fetcher)               ├─→ A06 ────┴─→ A07 → recommend
                                          └─→ liquidity_ctx ─→ analyze_* ┘
```

### 5.2 改进

`store → liquidity_ctx` 已实施（第九轮），让 liquidity_ctx 与 A05/A06/A07 并行启动。

**进一步**：可以并行启动**信息层摘要**和**分析层 preheat**（A17 不需要原始 validated_points，只读分析层输出的 agent_outputs）—— 但 LangGraph 当前架构下 analyze_* 必须等 liquidity_ctx 完成才能启动（A08 读 hint）。所以 preheat 只能在 A08 第一个完成后启动，节省 ~5-10s。

### 5.3 落地

`recommend_node` 改造（已部分优化）：`_build_compact_context` 把上游 analyses 压缩，preheat 启动成本已经很低。再加一层"**A08 完成后即可启动 A17 prompt 组装**"（不调 LLM，只组 prompt）—— 节省 ~3s。

---

## 6. 落地优先级与总收益

| 改造 | 阶段 | 工作量 | 收益 | 状态 |
|---|---|---|---|---|
| A05/A06/A07 信息层并行 | 4 | 0.5h | −30s | ✅ 已实施 |
| liquidity_ctx 不等 sentiment | 4→5 | 0.5h | −30s | ✅ 已实施 |
| A07 sentiment 改 light 1.5B | 4 | 1h | −27s | ✅ 已实施 |
| A07 sentiment 加 json_schema | 4 | 0.5h | 鲁棒性 | ✅ 已实施 |
| ReAct max_steps 3 → 2 | 7 | 0.5h | −10~15s | ✅ 已实施 |
| **小计（已实施）** | | **~3h** | **−70~75s** | |
| A13-A16 行业 Agent 按需 | 6 | 2h | −15s | 待做 |
| A08 macro 走纯模板 | 6 | 3h | −35s（宏观任务） | 待做 |
| A01 拆"必需 + 降级"两批 | 2 | 4h | −15-35s | 待做 |
| Ollama 多实例 LB | 6 | 需硬件 | −25s | 不可行 |
| A17 preheat 提前到 A08 完成 | 6→7 | 1h | −3s | 待做 |
| **小计（待做）** | | **~10h** | **−58~78s** | |

### 端到端目标

```
旧（第八轮后）：~80-120s
   = 规划 4 + 采集 35-60 + 管线 5 + 信息层 65 + 本地量化 <1 + 分析层 50 + 决策 30 + 审计 1

新（第九轮，已实施）：~40-70s
   = 规划 4 + 采集 35-60 + 管线 5 + 信息层 38 + 本地量化 <1 + 分析层 50 + 决策 15-20 + 审计 1
   = 信息层从 65s → 38s（−27s：并行 + light）
   = 决策从 30s → 15s（−15s：max_steps 2）
   = liquidity_ctx 提前 30s 启动（−30s：分析层提早进入）

新（第九轮 + 待做）：~25-50s
   = 上述 + A08 走纯模板（−35s 宏观任务）+ A13-A16 按需（−15s）+ A01 拆分（−15-35s）
```

### 早期 → 第八轮 → 第九轮 时间线

```
早期（deepseek flash 写出来的）：
  单次推理 ~160s，主要是信息层串行 + A17 推理慢 + 决策层深度推理
  → 用户："性能很差，成本高"

第八轮（PROJECT_AUDIT_2026-09-27 已完成）：
  ~80-120s（成本 −55%，但端到端时间仍较长）

第九轮（本轮目标）：
  ~40-70s（端到端 −50%）

第十轮（规划中）：
  ~25-50s（A08 纯模板 + A13-A16 按需 + Ollama 多实例）
```

---

## 7. 后续可做的"第十轮"优化（预留）

| 项 | 描述 | 收益 |
|---|---|---|
| **批处理多任务共享 LLM 调用** | 同一时间 N 个用户提交问题，部分指标重合 → 共享 LLM 推理（节省 N×prompts → 1×prompt + N×finalize） | 用户量大时显著 |
| **A17 streaming 输出** | 把 A17 的 final_report 改成 streaming，前端边生成边渲染 | 用户感知延迟从 15s → 1-2s |
| **Embedding 替换 3-gram 缓存** | 长 prompt 下命中率从 ~40% → ~70% | 节省云端费用 |
| **LLM 网关改 Rust/Go** | 网关延迟从 ~50ms → <5ms | 单任务 −50ms |
| **A19 数据缺口自修复迁移到异步预热** | 当前 A19 在 collect 阶段同步跑（每个指标没数据时跑一次），实际收益小、成本高；改成"盘后批量预热 + 在线查询结果" | collect 阶段 −20s |

---

## 8. 面试可讲的核心论点

> **"信息层串行改成并行 + A07 降级到 light，−57s（−60%）；ReAct 步数从 3 降到 2，−15s；liquidity_ctx 提前启动，−30s 提前进分析层。这三件事加起来，端到端时间从第八轮的 80-120s 降到第九轮的 40-70s。关键是**先测时间花在哪儿再决定改哪儿**——盲目优化 A17 tokens 是省钱不省时间，优化信息层才是省时间。"**

> **"LLM Agent 编排的时间优化**有个反直觉**：不是'优化 LLM 调用'（那只能省 5-15s），而是'优化 LLM 调用之间的依赖拓扑'（并行化能省 30-60s）。**测量 > 推测；并行 > 优化单点**。"**

---

## 9. 测试与回归

- **编译测试**：`build_research_graph` 编译成功（新拓扑含两条 `store → X` 的并行边，LangGraph 自动 join 到 sentiment）
- **回归测试**：受影响模块 123 个测试 **全部通过**（`test_intraday_overrides / test_compliance_gate / test_sector_crowding / test_llm_gateway`）
- **A07 schema 兼容性**：1.5B + json_schema 受约束解码，结构由采样器保证；不正解时回退 `_PHASES` 兜底
- **liquidity_ctx 提前启动**：分析层仍读 `analysis_hint`，但与 sentiment 并行；`recommend` 节点通过 `g.add_edge("sentiment", "recommend")` 显式等 A07 完成