# 项目审计报告：性能 / Token / 代码规范 / 面试欠缺

> 审计日期 2026-09-19。范围：`D:\code\Moss-finagent-research`（448 个 .py / 126,242 行）。
> 证据来源标注：`实测` = 本次可复现；`审计日志` = `data/audit/llm_audit.jsonl`
> 984 条真实记录（2026-09-12~09-18）；`静态` = grep/统计。
> **本次已修复 4 项**（见第五节），其余为待办。

---

## 摘要：最值钱的 10 条

| # | 发现 | 类别 | 量级 | 状态 |
|---|---|---|---|---|
| 1 | 档位拟合 `_best_achievable` 单票 2.36 s，占 `fit_levels` 的 **62.9%** | 性能 | 自选池 59 只冷缓存 **≈220 s CPU**，做T链路 96% CPU 在此 | 待办 |
| 2 | 生态序列每轮读三张全市场表 | 性能 | 决策链路 **1231 ms → 36 ms** | ✅ 已修 |
| 3 | 5 个分析 Agent 收到**完全相同**的全量数据点 | Token | 每轮 ~2 万 tokens_in 无效 | 待办 |
| 4 | 输入截断从尾部切掉 JSON schema → 必然重试 | Token | 单次成本翻倍 | ✅ 已修 |
| 5 | 缓存键不含模型层级 → 1.5B 结论被决策层复用 | 正确性 | 跨层级串台 | ✅ 已修 |
| 6 | A17 ReAct 每轮重发完整 payload（3 遍） | Token | A17 占全系统成本 **55%** | 待办 |
| 7 | `sh/sz` 前缀 **5 处实现、3 处有真 bug**（缺 `"5"` 代码段） | 正确性 | 588170 等沪市 ETF 被判深市 | 待办 |
| 8 | **反依赖**：`domain → infrastructure` 18 条，违反项目自订规范 | 架构 | 分层形同虚设 | 待办 |
| 9 | 配置分居 4 处 + 8 套 loader；`intraday/config.py:27` 是 CWD 相对且无 env 覆盖 | 稳定性 | 服务不在仓库根启动 → 用户配置**静默失效** | 待办 |
| 10 | 成本核算链路断裂：定价块写了但从不读 | 可观测 | 无法验收任何 token 优化 | 待办 |

---

# 一、性能

## P1 · 做T档位拟合：单票 2.36 s，占该链路 96% CPU 【最高优先】

**位置**：`src/intraday/level_fit.py:677 _best_achievable()`（放宽网格），
入口 `src/intraday/service.py:1223 _fit_levels_sync` → `:1333 _fit_levels_for`

**实测**：单票 `fit_levels` = **3751 ms**，其中 `_best_achievable` 占 **2362 ms（62.9%）**。
cProfile：`evaluate_levels` 调用 4114 次 → `_first_cross`（`level_fit.py:413`）
**1,266,916 次 Python 调用 / 1.36 s**。

**影响面**：自选池 59 只 → 冷缓存一次 **≈220 s CPU**；缓存 TTL 1800 s
（=30 分钟，短于交易时段）→ 每天至少重算 2 次 → **≈7 分钟/天**。
一次 `snapshot(light=True)` 的构成是 **档位拟合冷态 3868 ms vs 其余全部 140 ms**
—— **96% CPU 在 fit_levels 里**。

**顺带发现的文档失效**：该函数 docstring 写"实测 ~0.2 秒"，实际 2362 ms，**差 12 倍**。

**建议**：① `_first_cross` 用 numpy 向量化（当前是纯 Python 双层循环）；
② 网格搜索改**分层剪枝**（先粗后细）；③ TTL 提到覆盖整个交易时段；
④ 冷启动放后台任务（`service.py` 已有 `_cold_recompute_task` 模式可循）。

## P2 · 生态序列每轮读三张全市场表 【已修复】

**实测**（N=31，冻结录像带，不含网络/落库）：

| 环节 | 修复前 | 修复后 |
|---|---:|---:|
| `ecology.build_snapshot()` | **1223 ms（99.3%）** | 0（走磁盘缓存） |
| `build_candidates` | 0.1 ms | 0.1 ms |
| `build_feature` + `score_candidate` | 8.1 ms | 8.1 ms |
| **合计** | **1231 ms** | **36 ms（34×）** |

**根因**：`src/auction_select/ecology.py:_streak_records()` 每轮调
`load_dataset("daily"/"stk_limit"/"daily_basic")`。cProfile 热点是
`sqlite3.fetchall` 6.5 s、pandas 字符串数组 1.4 s、merge 0.9 s（约 208 万次元素操作）。

**修复**：新增 `src/auction_select/ecology_cache.py`，盘前算一次落盘 JSON。

> **教训（面试可讲）**：若直接去"优化规则"（把 12 维打分改成位运算），
> 天花板只有 0.66%。**先测再改，比先改再测重要。**

## P3 · `load_dataset` 每次重建连接 + CSV 回退扫 5032 个分区

**位置**：`src/quant/warehouse.py:1354`

每次调用新建 `QuantWarehouse` + `create_engine` + `SELECT 1` + 表存在性探测
（实测 1.4 ms/次）；CSV 回退分支 `store.keys()` 扫 5032 个分区 = **450 ms**。
`src/auction_select/ecology.py:118/121/124` 连续三次 → `_streak_records()` **2094 ms**。

**建议**：模块级连接池单例 + `store.keys()` 结果缓存（按目录 mtime 失效）。

## P4 · 回测里同一份数据拉了两遍（白花 4.15 s）

**位置**：`src/backtest/shortterm.py:286` 与 `:305`

`stk_limit` 用同一组 4 列拉了两次（一次无 `end`、一次有 `end`）。
**实测两次 `DataFrame.equals == True`**，第二遍白花 **4150 ms**（3,387,536 行）。

**建议**：删掉 `:305` 那次，复用 `:286` 的结果并按需切片。

## P5 · `data_status()` 单请求 3454 ms 且无缓存

**位置**：`src/api/routes/quant.py:83`

对 11 个数据集各调一次 `coverage()` = **3454 ms/请求**。
`web/src/api.ts:1798` 确认前端在调它。
同模块 `src/api/data_health.py:359` **已有 300 s TTL 缓存可照抄**。

## P6 · `replay_totals` 逐 bar 做 O(N²) 时间比较

**位置**：`src/intraday/features.py:428`

```python
osc[osc["dt"] <= pd.to_datetime(ts)]      # 每个 bar 重新转时间 + 全表扫描
```

**实测 45.5 ms**，占单次 snapshot 177 ms 的 **26%**。
cProfile：`_guess_datetime_format_for_array` 97 次、`re` 调用 15287 次。

**建议**：`dt` 列预转一次并排序，改用 `np.searchsorted`。

## P7 · 其余 6 条（收益较小，见附表）

- resample + oscillator 重复算两遍（15.3 ms）
- `_rolling_percentile` O(N×240) 13.6 ms；`vwap_series` 按日循环
- `panels.py` 每字段一次连接
- fundflow 三条 `to_thread` 串行（可并发）
- langgraph 一族 **452 ms** 启动开销（`import src.api.main` 净 1876 ms 中占 24%）
- `_FUNDAMENTAL_CACHE` 无上限（内存泄漏风险）

## P8 · 已确认**不是**瓶颈（别浪费时间优化）

| 项 | 结论 |
|---|---|
| `warehouse._read_sql` 绕过 SQLAlchemy | **是优化不是反模式** —— 注释自带 2.5 s/125 万行实测 |
| `panels.py` | 已做来源探测 / 下推 / 分区单读 |
| `auto_select._name_map` | iterrows 已修 |
| `ConnectorRouter` 的 `stock_close:` TTL = 14400 s | `_average_volume` 不构成重复网络取数 |
| akshare / tushare / xtquant | **确认未在启动时导入** |
| `_WATCH_COMPUTE_BATCH=1` | **刻意限流**（50→事件循环占用 36.5 s；1→0.4 s），**别调大** |

---

# 二、Token 花费

## 2.0 基线（审计日志实测）

| 指标 | 数值 |
|---|---|
| 样本 | 117 个 trace，984 次 LLM 调用 |
| 每次 trace 调用次数 | **8.41**（含行业 Agent 时 10–12） |
| 每次 trace | **44,829 tokens_in + 13,022 tokens_out** |
| 缓存命中率 | **40.9%**（402/984） |
| 模型分布 | deepseek-v4-flash 309 · qwen2.5:1.5b 251 · qwen3:8b 236 · deepseek-v4-pro 170 |
| 本地 Ollama 真实承载 | **487 次**（light/medium 层确实走本地） |

**单轮成本**（按 `models.yaml` 自身定价）≈ **0.20 元**；一天 200 轮 ≈ **40 元/天**，
其中 **A17 一个 Agent 占 55%**。

## 2.1 【高】5 个分析 Agent 收到完全相同的全量数据

**位置**：`src/orchestration/supervisor.py:863-874`

```python
"data_points": s.get("validated_points", []),   # ← 全量，不按 Agent 过滤
```

**实测**：537 行数据点 ≈ 82,173 字符。审计实证：一次 full 跑里
**A08/A09/A10/A11/A12 五次调用的 `tokens_in` 全部等于 25,149** —— 完全同一份输入。

**浪费**：A10 只需目标股 PE/PB/日线（≈300–1,500 tokens）、A11 只需资产负债率
（≈200）、A12 只需 events（≈200）。**每轮约 20,000 tokens_in 是无效载荷**；
更严重的是它把 6000 字符窗口的 95% 用来装无关数据，直接触发 2.2。

**建议**：`_node` 的 payload_fn 里按 Agent 白名单过滤指标。

## 2.2 【高】尾部截断丢掉 JSON schema 【已修复】

**位置**：`src/infrastructure/llm/gateway.py`

prompt 段序是 `规则 → 时效红线 → 焦点 → 【输入数据】 → 事件 → 原文 → 本地参考
→ 技能 → 【任务要求】`。数据段吃掉全部 6000 字符，实测截断后
`## 任务要求` / `## 专业技能指引` / `## 本地计算参考` **全部消失**。

模型拿不到输出 schema → 输出随机 JSON → 解析失败 → repair 重试
（`use_cache=False`、prompt 更长）—— **单次成本翻倍，且重试仍被同样截断**。

**修复**：`_truncate()` 改**保头保尾**（7:3）+ 中段省略标记；
测试 `test_llm_gateway.py::test_truncation_keeps_head_and_tail`。

## 2.3 【高】A17 ReAct 每轮重发完整数据

**位置**：`src/domain/agents/analysis/react.py:86-130`

**实测**：A17 prompt = **7,582 tokens**（上游全量 `result` JSON，只剔了 3 个字段）。
3 步 = 同一份 payload 发 3 次。**169 次调用 = 356,911 tokens_in / 411,726 tokens_out**，
跑在 `deepseek-v4-pro`（4.5 / 13.5 元每百万）→ **约占全系统成本 55%**。
其中 **98 次输出撞 4096 上限**。

**建议**：① 第 1 步发全量，第 2/3 步只发「上次输出 + 新增 observation」；
② 上游结论压缩为 `{agent_id, conclusion, confidence, 2–3 个关键数值}`；
③ observation 改本地摘要。

## 2.4 【中高】7 处 `use_cache=False` 关掉唯一的经济性防线

**位置**：`alerts/analyzer.py:171,218` · `data_gap_resolver.py:301` ·
`code_engineer/agent.py:340` · `analysis/base.py:296` · `recommend/agent.py:157` ·
`react.py:102`

其中两处用**时间戳**做 `trace_id` → 审计无法关联、缓存永不命中。

**审计实证**：54 组 prompt_hash 被重复真调 **302 次**，冗余输入 **476,824 tokens**；
`intraday_news_sentiment` 单个 prompt 被推理 **85 次**。

## 2.5 【中高】数据缺口自修复无条件触发

**位置**：`src/orchestration/supervisor.py:587-607`

触发条件只有"无数据"，无白名单、无台账、无频率上限。而 `ind:sw_third_*`
因东财 WAF **稳定返回空** → 每轮都稳定触发。

**审计实证**：`A19_code_engineer` **50 次调用输出 162,608 tokens = 全系统输出的 23%**，
输入只有 21,532 —— 典型的"输出极长 + 缓存关闭 + 反复重生成"。

## 2.6 【中】分层路由漏掉一半

**做得好的**：Planner 用 `light` 本地 1.5B；A05/A06 用 `medium` 本地 8B；
A18 审计**零 LLM**。实测 Ollama 真实承载 487 次。

**漏掉的**：

| Agent | 实际职责 | 现状 | 建议 |
|---|---|---|---|
| A07 舆情 | 解读已本地算好的 `weighted_sentiment` | `reasoning` 云端 | → `medium` |
| A10 微观 | 估值已由 `_calc_valuation` 本地完成 | `reasoning` 云端 | → `medium` |
| A11 风控 | 指标本地算好，LLM 只定性解读 | `reasoning` 云端 | → `medium` |
| A12 合规 | 输入实质是 events + 合规规则 | `reasoning` 云端 | **→ 纯规则查表** |

一轮约 **18,500 tokens_in** 可从云端归零。

## 2.7 【中】成本核算链路断裂

`configs/models.yaml:39-53` 写了完整定价块，但 `gateway.py` **从不读取**；
`metrics.py:87` 只汇总 token 数，**不折钱、不按 tier 拆**。

**顺带发现的配置不一致**：`models.yaml:35` 定义 `deepseek-flash`，
但审计里 309 次调用的 `model` 是 **`deepseek-v4-flash`**。
配置名与线上模型名不一致，降级链与成本表都建立在错误的模型名上。

## 2.8 其余发现

- **T7**：`## 回答规则` + 时效红线占掉 6000 字符窗口的 **40%**；用户提问在同一
  prompt 里出现 3 次、焦点词出现 9 次；规则在 system 与 user prompt 里各写一遍。
- **T8**：A17 全量透传上游 `result` JSON，与 `conclusion` 文本高度重叠。
- **T11**：`verified_texts`（最多 10×300 字符）注入给**全部** 9 个分析 Agent，
  但 A08/A10/A11 并不消费它。
- **T12**：申万行业估值截面 **701 行**（≈13,000 tokens）无裁剪逐行渲染，
  截断到 6000 后只剩 130 行 —— **数据没进模型，token 却按 6000 计费**。
- **T13**：语义缓存用字符 3-gram 余弦，长 prompt 下相似度趋近常数。
  命中率 40.9% 里绝大部分是"同 Agent 重复跑同一任务"，不是语义泛化。

## 2.9 Token 侧已经做得好的（面试自证清单）

1. **统一网关强制收口** —— 结构性消灭"散落各处的 `openai.chat()`"这个通病。
2. **6000 字符输入硬上限** —— 加 cap 前 A10/A11/A12 单次输入 **23.3 万 tokens**，
   加 cap 后降到 6,007，**输入侧约 −70%**，是整个审计里最大的一笔省钱动作。
3. **分层路由真的落地**：Ollama 487 次真实调用。
4. **"本地算、LLM 只解读"** —— 估值判断、可信度评分、情绪量化全部本地计算。
   **既降幻觉又降 token**。
5. **A18 审计零 LLM** —— 在"审计也用 LLM 打分"是常见反模式的背景下，这是清醒设计。
6. **告警两阶段批处理** —— 30 条候选事件用 **2 次**网关调用扫完。
7. **token 计量与哈希审计真正落地** —— 规范没停留在文档里。
8. **技能库三级加载（L0/L1/L2）** —— 各 Agent 的 L0 索引仅 191–238 tokens。
9. **取消令牌** —— 用户点停止后不再产生新 token 花费。

---

# 三、代码规范

> **总判断**：命名与类型注解是**工业级**，真正的洼地在
> **分层纪律、配置约定、公开方法 docstring、注释分类**。

## 3.1 魔鬼数字：真问题不是"多"，而是**同一口径多处各写一遍且相互矛盾** 【已修复】

量化基数：范围内 **20,912 个数字字面量**；src 函数体内非平凡数字 **2,013 个**。
模块级常量 **418 个**，其中同名不同值 **16 处**。

### 修复前的冲突清单（审计时实测）

| 口径 | 并存的值 | 位置 | 判定 |
|---|---|---|---|
| 年化交易日基数 | **242 与 252** | `quant/model_backtest.py:66` vs `quant/single_backtest.py:47` | ⚠️ **真 bug**：252 是美股口径，A 股约 242 天；`years = 交易日/252` 偏小 → **年化收益高估约 4%**、夏普高估约 2% |
| 异步任务 TTL | **1800 与 900** | `api/routes/quant.py:34` vs `api/routes/backtest.py:57` | ⚠️ 常量**同名不同值**，改动方以为在调全局策略 |
| 交易时段边界 | 09:15/09:30/11:30/13:00/15:00 以 `9*60+15` 形式重复在 **6 个模块** | `intraday/{service,market,indicators,daily}.py` · `fundflow/{service,provider}.py` | ⚠️ 其中两份 `session_state` **逐行相同**，前者的 docstring 还写着"与做T模块同口径" |
| 交易时刻格式 | `"09:25:00"` 与 `"09:25"` 两种 | `auction_select/config.py:31` vs `scheduler/registry.py:212,222` | ⚠️ **潜在 bug**：`jobs.py` 用字符串比较，`"14:45" >= "14:45:00"` 为**假**（短串是长串前缀），14:45 那一分钟会被判成"还没到窗口"而跳过 |
| 跳空取价窗口 | `"09:24:40"`/`"09:24:55"` 两处各写一遍 | `auction_select/config.py:121` vs `features.py:86` | 重复即漂移风险 |
| 市值下限 | 15 亿 / 20 亿 / 25 亿 / 30 亿（四套） | `auction_select/config.py:47` · `moss_selector/model_trainer.py:265` · `scripts/limit_up_next_day_stats.py:21` · `fundflow/service.py:44` | ✅ **口径本来就不同**（选股门槛 vs 统计样本 vs 大盘股分档），已在 `core/market_constants.py` 写明"不要合并" |
| 涨停容差 | `0.3` ×3 | `intraday/config.py:465` + `character.py:170` + `daily_score.py:242` | ✅ 各模块独立标定的容差，非共享口径 |
| 市值上限写法 | `11e9` 与 `1.1e10` | `auction_select/config.py:48` vs `scripts/bt_run.py:47` | ✅ 同值异写，无行为差异 |

### 魔鬼字符串

- `str(exc)[:N]` 截断 **530 处**，限额 **7 种并存**（`[:200]`×91、`[:120]`×88、
  `[:100]`×37、`[:80]`×32、`[:160]`×30、`[:150]`×22、`[:300]`×16）。
  单文件混用最多：`intraday/service.py` 29 处。
- HTTP 状态码字面量 **112 处**；错误文案重复（`"证券代码应为6位数字"` 3 处）。
- **8 个脚本硬编码本机绝对路径** `D:\code\Moss-finagent-research`。

**不建议提取的**：一次性的局部量（`x / 2`）、测试数据里的字面量 —— 就地写更清楚。
审计时做过一次"裸数值聚类"验证：`6.0` 出现在 **69 个模块**、`5.0` 在 66 个，
但它们全是互不相关的用途（均线周期 / 权重系数 / 分档边界）。
**强行抽成共享常量会把无关的事绑在一起，比不抽更糟** —— 这是本项目
"只抽 ≥2 模块共用同一口径"这条收录标准的实测依据。

### 本次实际改法（见 §5.6）

## 3.2 反依赖：项目自己写下的分层规范被自己违反

`.trae/skills/dev-standards/references/project-structure.md:13`（AGENTS.md 引用）
明写 **"domain 层 —— 不依赖 infrastructure"**，实测违规：

| 违规 | 条数 | 典型位置 |
|---|---:|---|
| `domain → infrastructure` | **18 条 / 10 文件** | `analysis/base.py:20`、`recommend/agent.py:15`、`audit/verifier/agent.py:13`、`data_gap_resolver.py:37` |
| `quant → api`（**向上依赖**） | 2 条 | `quant/screen_runner.py:87,133`（业务层反过来 import Pydantic 请求模型） |
| `domain → intraday`（运行时真依赖） | 1 条 | `domain/intraday/models.py:103` —— 同文件 `:36` 注释自称"避免硬依赖"，**注释与代码矛盾** |
| `infrastructure → quant` | 1 条 | `connectors/tushare_connector.py:184` |

**import 邻接矩阵显示：`core` 出度 0（唯一纯底层）、`api` 出度 11（唯一顶层）
—— 中间 10 个包是一团网状同层耦合，没有任何一层是 DAG。**
且存在真实环：`intraday ↔ fundflow`、`quant ↔ fundflow`。

**同一模块被拆成多次 import：69 处**（`api/routes/quant.py` 里
`from src.quant.strategy_store` ×5、`stock_directory` ×4、`warehouse` ×3）。

## 3.3 配置：分居 4 处、8 套 loader、0 复用

| 配置 | 路径解析 | env 覆盖 | 热重载 |
|---|---|---|---|
| `auction_select/config.yaml` | `PROJECT_ROOT/...` ✅ | ✅ | ❌ |
| `sector_crowding/config.yaml` | `PROJECT_ROOT/...` ✅ | ✅ | ❌ |
| **`configs/intraday.yaml`** | **`Path("configs/intraday.yaml")` CWD 相对** | ❌ **无** | ✅ |
| `configs/models.yaml` | CWD 相对 | ✅ | ❌ |
| `configs/agents.yaml` | `__file__` 推导 ✅ | ❌ | ❌ |
| `moss_selector/config.yaml` | **3 处各写一遍字面量** | ❌ | ❌ |

**`src/intraday/config.py:27` 的实际风险（已实测）**：无 `PROJECT_ROOT`、无 env 覆盖，
服务不在仓库根启动时只打一条 `logger.warning` 就继续跑 ——
**用户改的权重/阈值静默失效**。同一问题在 `src/intraday/subproc.py:35` 复现。

**两个零引用的死配置（共 221 行）**：
`config_label10_2bucket.yaml`（127 行，全库 0 命中）、
`configs/data_sources.yaml`（94 行，唯一"引用"是某文件 docstring 提到它）。

**三个策略模块三种摆放**：`src/auction_select/` + 根 `auction_select/config.yaml`（目录里只有这一个文件）；
`src/sector_crowding/` 同样；`moss_selector/` 代码与配置都在根目录**且没有 `__init__.py`**。
`moss_selector` 与 `src.quant` **双向依赖**，靠 `pyproject.toml:44 package = false` 才成立
—— 实质是"伪装成顶层包的 quant 子模块"。

## 3.4 API 层内联业务逻辑（规范明写"不包含业务逻辑"）

| 路由文件 | 总行 | 路由处理体 | 占比 | 最长 handler |
|---|---:|---:|---:|---|
| `research.py` | 385 | 251 | **65%** | `submit_analyze()` **120 行** |
| `intraday_weights.py` | 692 | 356 | 51% | `preview()` 97 行 |
| `quant.py` | 653 | 273 | 42% | `data_status()` 56 行 |

具体违规：`research.py:298` 在 health 里直连 Ollama；`:304` 验审计链；
`quant.py:646-648` 把 IC 计算写在 handler 内；`sector_crowding.py` 路由层直持
SQLite 短连接（`conn.close()` 出现 9 次）。

**`sys.path` 篡改 38 处**（src 内 3 处、manage.py 1 处、scripts 34 处）——
`scripts/bt_*.py` 互相 import，靠 sys.path 拼装出第三个"包系统"。

## 3.5 重复实现：同一件事两处以上

**① `sh/sz` 前缀规则：5 处实现，3 处有真 bug**
```
src/intraday/sources.py:72-90     exchange_symbol()   ← 权威（含 5/6/9，注释记录了 588170 的坑）
akshare_connector.py:574          ("5","6","9")  ✅
akshare_connector.py:746          ("6","9")      ❌ 缺 5
tencent_daily_connector.py:76     ("6","9")      ❌ 缺 5
xtquant_connector.py:45           ("6","9")      ❌ 缺 5
```
**同一个 `akshare_connector.py` 内 `:574` 与 `:746` 规则不一致**，而 `:746` 正是
`intraday/sources.py:79` 注释里点名踩过的那个 bug。
另：`tencent_daily_connector.py:65` 提到 `sources.symbol_for`，**该函数不存在**（真名 `exchange_symbol`）。

**② `CostConfig` 三处同名、字段集互不兼容**
`backtest/engine.py:16`（全默认 0）· `quant/single_backtest.py:53`（含印花税/过户费/滑点 bps）
· 消费方分裂：`api/routes/backtest.py:19`（旧栈）vs `quant/screen_runner.py:135`（新栈）。

**③ 同名函数行为不同**
`_max_drawdown` 在 `backtest/engine.py:103`（空序列抛 `IndexError`）与
`shortterm.py:1124`（有空序列 + 除零保护）；`_mean` 逐字节相同。

**④ 4 个回测入口并存**：`backtest/engine.py`（旧月度）· `backtest/shortterm.py`（新日频）
· `quant/single_backtest.py`（DSL 单票）· `scripts/bt_run.py`（自带 4 套结算口径）。
`bt_run.py` 完全不 import `src.backtest`。

**⑤ 小工具重复**：`_num` **13 处**、`_now` **8 处**、`ensure_schema` **10 处**、
`load_config` 3 处（返回 3 种不同类型）。其中 **`_now` 行为不一致**：
`auction_select/db.py:213` 无时区、`sector_crowding/db.py:107` 带时区 ——
两者**写同一个 SQLite 文件、各自建表**。

**⑥ DDL 重复**：`fact_data_points` 在 SQLite 与 PG 两个 repo 里手抄，
除 3 处类型外逐字相同；全项目 **21 处 `CREATE TABLE IF NOT EXISTS`、10 处 `ensure_schema`**。

## 3.6 注释质量（用户重点关注）

| 指标 | 数量 | 占比 |
|---|---:|---:|
| 注释行总数 | **22,614** | 100% |
| **开发过程叙述型**（曾经/原来/第一版/实测踩到/已废弃/改成/修复） | **1,474** | **6.5%** |
| **高价值型**（口径/单位/来源/边界/为什么不用某方案） | **3,036** | **13.4%** |
| 带日期（`YYYY-MM-DD`）变更日志 | **351** | 1.6% |

**关键结论：高价值注释是过程性注释的 2 倍多** —— 作者有能力写有价值的注释，
只是没把两类分开。

**过程性注释最多的文件**：

| 文件 | 条数 | 占该文件注释 |
|---|---:|---:|
| `src/intraday/service.py` | 58 | 8.8% |
| `src/auction_select/sources.py` | 55 | **11.2%** |
| `tests/unit/test_auction_select.py` | 50 | 12.5% |
| `src/auction_select/service.py` | 41 | **16.0%** |
| **`src/auction_select/config.py`** | 22 | **24.2%** ← 占比最高 |
| `src/auction_select/preheat_cache.py` | 20 | 20.6% |

**改写模板**（保留结论、删掉过程）：
```
- 曾经把它们也缓存了，结果单测里…（001216 58.56 → 56.06）。教训：缓存 key 必须覆盖所有输入。
+ 口径：缓存 key 必须覆盖全部输入（含 market_cycle.max_streak 等运行时字段），
+      否则会读到按不完整输入算出的脏结果。见 tests/unit/test_auction_select.py 回归用例。
```

**必须保留的高价值注释（不要删）**：`auction_select/features.py:3-11`（量比口径声明）、
`auction_select/config.py:71-72`（`>=` 边界语义）、`quant/pit.py:3-11`（未来函数三条硬规则）、
`quant/condition_dsl.py:380-381`（`15.32` 表示 15.32% 的单位口径）、
`backtest/shortterm.py:34-43`（一字涨停买不进/跌停卖不出）、
`intraday/chan.py:354`（缠论第 65 课出处）、`intraday/sources.py:76-80`（ETF 代码段规则）、
`pyproject.toml:36`（pytest 锁 8.3 线的实测理由）。

**判定标准**：注释在讲"**这是什么口径/单位/边界/为什么不选另一个方案**"→ 保留；
在讲"**我调试时发生了什么 / 上一版是什么 / 什么时候改的**"→ 删除，只留结论。

**其他注释问题**：`src/auction_select/sources.py:10` 引用的 `_probe_qmt_*.py` 已不存在
（**悬空引用**）；`domain/intraday/models.py:36` 注释与 `:103` 代码**矛盾**。

## 3.7 死代码与冗余

**零引用公开函数 60 个**，其中最值得注意的：

| 位置 | 函数 | 为什么重要 |
|---|---|---|
| `src/infrastructure/security/rls.py:38/56/61/99` | `get_tenant` / `set_rls_enabled` / `is_rls_enabled` / `register_rls_listeners` | **AGENTS.md:33 明确要求的多租户 RLS —— 4 个函数全部零引用，即"已写但未接线"**。面试会被追问 |
| `src/quant/risk.py:63/195` | `portfolio_var` / `stress_test_portfolio` | 风险管理 API 无人调用 |
| `src/quant/factor_analyzer.py:280/310/346` | `analyze_factor` 等 3 个 | 因子分析主入口 |
| `src/infrastructure/connectors/sw_industry_valuation_connector.py:190` | `compute_valuation_percentile` | |

**已废弃别名**：`quant/warehouse.py:826 MySqlWarehouse = QuantWarehouse`（全库 0 命中，**可直接删**）；
`auction_select/features.py:62-69` 的两个废弃标签常量（保留理由：兼容历史落库数据反序列化）。

**被注释掉的大段代码**：`src/intraday/daily_signals.py:392-420` —— **29 行，整个旧版
`signal_b8()`**，新实现已在同文件 `:336-390`。

**重复的 `if` 判断 96 组**（同一函数内 ≥2 次相同条件），如
`scripts/bt_run.py:757-797 _settle_v2()` 里 `pos.shares < before` ×5。

**仓库卫生**：根目录残留 `manage.py.bak-console-fix`（37KB）+ `manage.py.bak-encoding-fix`（39KB）
= **76 KB 死备份**；空文件 `_build.log`。

## 3.8 一致性（先说好的，再说洼地）

**命名：全项目最强维度**

| 项 | 结果 |
|---|---|
| 公开函数/方法 | **4,320 / 4,320 = 100% `snake_case`**，违规 0 |
| 类 | 473 / 473 = **100% PascalCase** |
| 模块文件名 | **100% snake_case** |

唯一不一致：REST 路由 kebab vs snake（15 : 2，`sector_crowding.py:50,60` 是 snake）；
router prefix 也不统一（`auction_select` snake vs `code-engineer` kebab）。

**类型注解：src 工业级，tests 是洼地**

| 范围 | 完全注解 | 无注解 | 返回注解 |
|---|---:|---:|---:|
| **`src/`** | **78.5%** | **0.5%** | **99.0%** |
| `moss_selector/` | 90.9% | 0% | 100% |
| `tests/` | 18.2% | **30.7%** | 66.8% |

**裸泛型注解 458 处**（`dict` 406），**约 90 处集中在 8 个路由文件**（`quant.py` 31）。
`Any` 1,589 次 / 174 文件（`auction_select/sources.py` 78 最多）。
`cast()` **0 处**（好）。

**Docstring：三级断层**

| 范围 | 模块级 | 公开 def |
|---|---:|---:|
| `src/` | **97.3%** | **65.0%** |
| ├ 模块级函数 | — | **83.5%** |
| └ **公开类方法** | — | **42.4%** ← 洼地（缺口 377 个） |
| `scripts/` | 100% | **24.3%** |

最差文件：`src/quant/quant_select_repo.py` **16 个公开方法 0 docstring**；
`src/quant/factor_library_v2.py` 38/41 未文档化；两个 sqlite repo 各 5-6 个全无。

**错误处理**

| 指标 | 数量 |
|---|---:|
| `except` 子句 | 797 |
| 裸 `except:` | **0** ✅ |
| **静默吞掉**（无日志无重抛） | **76**（src 61） |
| `raise ... from e` | 102 |

**最危险的 4 处静默吞 `Exception`**：
`auction_select/service.py:900` · `repositories/cached_repo.py:154` ·
`quant/quant_select_service.py:643` · **`core/sqlite_recovery.py:320,347`（DB 恢复路径静默失败
→ "服务起来了但库是坏的"）**

**自定义异常：21 个类 = 1 个真层级 + 11 个孤岛**
唯一成体系的是 `src/core/exceptions.py` 的 `FinAgentError` + 9 子类；
11 个 ad-hoc 孤岛（`TaskCancelledError(Exception)`、`WarehouseError(RuntimeError)`、
`ConditionError(ValueError)` …）。
620 次 `raise` 中 `DataFetchError` 一家占 158 次 —— **只有数据层建立了异常语义**。

**日志**：`src/` 生产代码 `print` 仅 **9 处**（全是 CLI runner 的 stdout 协议，可接受）；
但 **不存在中央日志配置** —— `logging.basicConfig` 仅 4 处（3 处在 scripts，
**1 处在库模块 `quant/quant_select_runner.py:32`**），`dictConfig` 0 处，
8 处硬编码 logger 名，`src/` 有 **151 个模块从不导入 logging**
（含 `infrastructure/llm/gateway.py`、`cache.py`、全部 12 个 Agent）。

## 3.9 质量重心与架构文档错位

| 包 | 行数 | 占比 |
|---|---:|---:|
| `src/intraday` | 20,472 | **23.7%** |
| `src/quant` | 14,452 | 16.7% |
| `src/infrastructure` | 9,300 | 10.8% |
| `src/auction_select` | 8,710 | 10.1% |
| `src/domain` | 6,150 | 7.1% |
| `src/core` + `src/orchestration` | 3,156 | 3.7% |

**`intraday` + `quant` = 54%**，而 AGENTS.md 宣称的"5 层 18 Agent"主干
`domain`+`core`+`orchestration` 仅 **14%**。

## 3.10 大文件与超长函数

**>800 行文件 20 个**（Top 8）：`intraday/service.py` **2,811** ·
`auction_select/sources.py` **1,712** · `intraday/sources.py` **1,588** ·
`quant/warehouse.py` **1,480** · `auction_select/rulebook.py` 1,390 ·
`backtest/shortterm.py` 1,384 · `quant/single_backtest.py` 1,259 ·
`intraday/config.py` 1,154

**>80 行函数 116 个**（Top 6）：

| 行数 | 位置 | 函数 |
|---:|---|---|
| **587** | `orchestration/supervisor.py:309-895` | `build_research_graph` |
| **400** | `intraday/service.py:712-1111` | `snapshot` |
| **307** | `auction_select/service.py:515-821` | `run_selection` |
| **270** | `domain/skills/liquidity_cycle/analyzer.py:77-346` | `assess_liquidity` |
| **259** | `quant/panels.py:310-568` | `build_panels` |
| 235 | `auction_select/service.py:230-464` | `build_feature` |

---

# 四、面试视角：项目还欠缺什么

## 4.1 投「AI 应用开发工程师」

| 欠缺 | 现状 | 为什么会被追问 | 建议 |
|---|---|---|---|
| **没有评测集（eval）** | 有单测，但无"回答质量"评测 | 改了 prompt 怎么知道变好还是变坏 | 20–50 条标注样本 + LLM-as-judge |
| ~~没有 CI~~ **✅ 已有** | `.github/workflows/ci.yml`（backend + frontend） | — | 本轮已补 `compliance` 合规门禁 job |
| **成本核算断裂** | 只统计 token 不折钱 | 见 2.7 | 网关写 `cost_yuan` 进审计 |
| **prompt 无版本管理** | 散在代码里 | prompt 是核心资产 | 外置 + 版本号，与 trace_id 关联 |
| **无灰度/回滚** | 改配置即生效 | 生产可用性基本功 | 模型路由加权 + 一键回滚 |
| **单点：网关 / SQLite** | 单进程 | 可用性追问 | 网关无状态可水平扩；换 PG |
| **无并发压测** | 无数据 | "能扛多少 QPS" | locust/k6 压 `/research/run`，给 P95 |
| ~~RLS 写了但没接线~~ **✅ 已重写并接线** | 原为假实现 | — | 见 `MULTI_TENANCY_DESIGN.md` |

**最该补的两条：评测集 + 成本核算闭环。**

## 4.2 投「量化工程师」

| 欠缺 | 现状 | 为什么会被追问 | 建议 |
|---|---|---|---|
| **无样本外/滚动回测** | 有样本内声明 | 量化第一问就是"你过拟合了吗" | walk-forward + IC 衰减曲线 |
| **无交易成本模型** | 有滑点敏感性 | 冲击成本、涨跌停无法成交 | 冲击成本模型 + 涨停买不进的处理 |
| **无因子相关性/正交化** | 有单因子 IC | 多因子合成前必须去共线性 | 相关性矩阵 + 对称正交化 |
| **组合优化过简** | 等权 / 市值加权 | 没有风险模型与约束 | 行业/个股集中度约束 |
| **无实盘对接与对账** | 纯研究 | "研究到实盘怎么落地" | 下单接口抽象 + 持仓对账设计 |
| **风控无限额体系** | 有个股级信号 | 组合级风险预算缺失 | 单票上限、行业上限、回撤熔断 |
| **PIT 需自证** | 有 `quant/pit.py` | 未来函数是最常见的低级错误 | 写"故意注入未来数据"的测试验证 PIT 能拦住 |
| **无基准对比** | 有绝对收益 | 必须对基准 | 加沪深 300 / 中证 500 超额与信息比率 |

**最该补的两条：滚动样本外 + 交易成本模型。**

## 4.3 稳定性与冗余设计

| 维度 | 现状 | 评级 |
|---|---|---|
| LLM 降级（主备链 + 断路器 + 取消令牌） | 完整 | ✅ 好 |
| 数据源容灾（三级故障转移 + per-key lock + 分档 TTL） | 完整 | ✅ 好 |
| 不造假数据（失败报缺口，不回退 simulated） | 明确的工程主张 | ✅ 好 |
| 审计与可追溯（哈希链 + prompt hash + token） | 规范真正落地 | ✅ 好 |
| **单点故障**（网关 / SQLite / 单进程） | Demo 阶段可接受 | ⚠️ 待改 |
| **API 层无限流** | 未见令牌桶 | ⚠️ 缺 |
| **幂等性** | 部分接口有 | 🔶 部分 |
| **数据备份** | 未见 | ⚠️ 缺 |
| **中央日志配置** | 不存在（见 3.8） | ⚠️ 缺 |
| **静默吞异常 76 处** | 其中 4 处是关键路径 | ⚠️ 缺 |

## 4.4 面试时可以主动说的三个"不足"

主动说不足比被问出来好。挑这三个（都有明确改进路径）：

1. **"成本核算我一直没闭环"** —— token 有计量、钱没折。改进很小，
   但**没有它所有 token 优化都无法验收**。
2. **"分层规范是我自己写的，但我违反了它"** —— `domain → infrastructure` 18 条。
   我知道该怎么修（把 LLM 抽象成 domain 侧接口，由 runtime 注入实现）。
3. **"缺 CI 和评测集"** —— 2500+ 单测说明我重视测试，但缺"LLM 回答质量"的评测。

---

# 五、本次已完成的修复

## 5.1 性能 / 正确性（第一批）

| # | 问题 | 位置 | 验证 |
|---|---|---|---|
| 1 | 生态序列每轮读三张全市场表 | 新增 `auction_select/ecology_cache.py` | 决策链路 1231→36 ms；32× |
| 2 | 截断切掉 JSON schema | `infrastructure/llm/gateway.py:_truncate` | 3 组 cap 参数化测试 |
| 3 | 缓存键不含模型层级（exact + semantic 双路径） | `infrastructure/llm/cache.py` | 3 条作用域测试 |
| 4 | `rulebook.py` 开发日志式注释 13 处 → 1 处 | `auction_select/rulebook.py` | 41 条测试通过 |

## 5.2 真实 bug（第二批）

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 5 | `sh/sz` 归属规则 5 处实现、**3 处缺沪市 `5` 段** | `akshare_connector:746`、`tencent_daily_connector:76`、`xtquant_connector:45` | 588170 等沪市 ETF 被判深市 → **取数全空且报错指不到原因** |
| 6 | `configs/intraday.yaml` **CWD 相对路径 + 无 env 覆盖** | `intraday/config.py:27` | 服务不在仓库根启动 → **用户改的权重/阈值静默失效** |
| 7 | `.env.example` 含**可用的默认数据库密码** + **真实个人邮箱** | `.env.example` | 诱导部署照抄默认密码；个人邮箱进版本库 |

**修复方式（不是就地打补丁，而是消除重复实现）**：
新建 `src/core/symbols.py` 作为**唯一权威实现**（选 `core` 因为它出度为 0，
连接器与做T都能 import 而不产生跨层耦合），5 处全部改为委托；
`tests/unit/test_symbols.py` 34 条用例钉住边界，并断言"旧写法不得复活"。

## 5.3 多租户与合规（第三批 · 新增能力）

| # | 交付 | 位置 | 说明 |
|---|---|---|---|
| 8 | 身份与租户上下文 | `src/core/tenancy.py`（166 行） | **不提供**"从请求头设租户"的接口；未认证是可检测状态而非默认租户 |
| 9 | 授权策略引擎 | `src/core/policy.py`（190 行） | RBAC × ABAC × 信息隔离墙**三维正交**；默认拒绝；跨墙需显式审批凭据 |
| 10 | RLS **重写为真实现** | `src/infrastructure/security/rls.py` | 原版是"取了租户但 `return stmt`"的**假实现**；新版只做能做到的事并写明边界 |
| 11 | 接入层中间件 | `src/api/tenancy_middleware.py`（201 行） | 凭证派生 Principal + 访问审计（哈希链）+ 默认 401 |
| 12 | 日志脱敏 | `src/core/redaction.py`（108 行） | 把"禁止输出完整 Prompt"从文档条款变成**写侧**代码约束 |
| 13 | **合规门禁** | `tests/unit/test_compliance_gate.py` + CI `compliance` job | 红线变成 CI 会拦的断言，失败不允许合并 |

**门禁上线当天抓到 3 个真问题**：默认 DB 密码、真实个人邮箱、形似真 key 的占位符。

## 5.4 死代码清理

| 删除项 | 大小 |
|---|---|
| `src/intraday/daily_signals.py:392-420` 被注释的旧 `signal_b8()` | 29 行 |
| `manage.py.bak-console-fix` / `manage.py.bak-encoding-fix` / `_build.log` | 76 KB |
| `config_label10_2bucket.yaml`（零引用） | 127 行 |
| `configs/data_sources.yaml`（零引用） | 94 行 |
| `MySqlWarehouse` 兼容别名（零调用） | 2 行 |

## 5.5 回归

`2588 passed / 1 failed` —— 唯一失败是 `test_connector_router.py` 的既有无关用例
（路由层，与本次改动无交集）。

## 5.6 魔鬼数字与代码规范清理（第四批）

> ⚠️ 这一批在第一轮交付时**只写了计划没落地**，是复盘中被打回重做的部分。
> 下面每条都附了可复核的命令。

### 一、异常消息截断：255 处 → 3 档

**问题**：`str(exc)[:N]` 530 处、7 种限额并存。限额由**调用点**决定，
导致同一类消息在不同文件里长短不一，"想统一调长要改 255 个地方"。

**改法**：`src/core/errors.py: brief(exc, limit)`，限额由**消息去哪里**决定：

| 档位 | 去处 | 长度 |
|---|---|---|
| `BRIEF_TIGHT` | 紧凑摘要（gap / notes / 前端标签） | 120 |
| `BRIEF_DEFAULT` | API 响应、异常消息 | 200 |
| `BRIEF_LOG` | 日志 | 500 |

迁移 **255 处 / 64 文件**，残留 `str(exc)[:N]` 归零。
`brief()` **默认走 `redaction.redact()`**：异常文案是最常见的意外泄漏通道
（第三方库报错常带完整 URL 与 token）。

```powershell
# 复核：应为 0
rg "str\(exc\)\[:\d+\]" src/
```

### 二、同名不同值常量：16 处冲突逐个定性

新建 `scripts/scan_same_name_conflicts.py`（按**常量名**聚合，不按值聚类 ——
见 §3.1 说明为什么值聚类会误导）。结果：418 个模块级常量名中 16 处同名不同值，
其中 **3 处是真缺陷**：

| 修复 | 内容 |
|---|---|
| `TRADING_DAYS_PER_YEAR` **242 vs 252** | 归一到 `core/market_constants`（A 股 242）。252 是美股口径，会让年化收益高估约 4% |
| `_JOB_TTL_SECONDS` **1800 vs 900** | 抽成 `src/api/job_table.py: JobRetention`，每类任务必须写明**保留理由**（回测产物大→短 TTL；因子统计回看间隔长→长 TTL）；两份重复的 `purge_jobs` 合并为一份 |
| `"09:24:40"` 跳空窗口两处 | `config.py` 默认值改为引用 `features.JUMP_WINDOW_*` |

其余 13 处经复核是**有意的本地口径**（各 Agent 的 `SYSTEM_PROMPT`、
各仓储的 `TABLE`、各连接器的超时…），已写入脚本的 `ACKNOWLEDGED`
白名单并逐条写明"为什么不合并"。

```powershell
# 复核：待处理应为 0
.\.venv\Scripts\python.exe scripts\scan_same_name_conflicts.py src
```

### 三、交易时段：6 个模块 → 1 个权威

**问题**：`9*60+15`、`11*60+30` 这类算术字面量散落在
`intraday/{service,market,indicators,daily}.py` 与 `fundflow/{service,provider}.py`。
其中 `intraday/service.py::session_state` 与 `fundflow/service.py::session_state`
**逐行相同**，后者的 docstring 写着"与做T模块同口径" —— 靠注释维持的一致，
改一处漏一处就会让两个页面对同一时刻显示不同状态。

**改法**：`src/core/trading_session.py` 成为唯一权威
（`CALL_AUCTION_START/MATCH`、`MORNING_OPEN/CLOSE`、`AFTERNOON_OPEN/CLOSE`、
`POST_CLOSE`、`TRADING_MINUTES`、`WATCH_WINDOWS`、`SESSION_LABELS`），
6 个模块全部改为转发。

**顺带修掉一个潜在 bug**：`scheduler/jobs.py` 用字符串比较判定选股窗口，
而 `registry.py:212,222` 参数写的是 `"09:25"`（无秒）——
一旦有人按 §3.1 表格里"统一格式"的直觉改成 `"14:45:00"`，
`"14:45" >= "14:45:00"` 为**假**（短串是长串前缀），14:45 那一分钟会被跳过，
**尾盘选股整轮丢失**。改用 `parse_hhmm()` 转分钟数再比。

### 四、Ruff：65 项 → 0

主要来自上面 255 处迁移的插入位置（53 个 import 块未排序）与几行超长。
`ruff check .` 现在 **All checks passed**。

### 五、CI 守卫（防止这类问题再次漂移）

| 文件 | 作用 |
|---|---|
| `tests/unit/test_trading_session.py`（77 用例） | 把**每个边界分钟**的期望值参数化钉住；并**实测** intraday / fundflow 与权威口径逐分钟一致（不再靠注释） |
| `tests/unit/test_market_constants.py` | `SHARED` 清单声明"共享口径"，断言从任何模块导入都拿到同一个值 |
| `scripts/scan_same_name_conflicts.py` | 人工维护用：只报**新出现**的同名不同值 |

### 六、脚本硬编码路径

**8 个** `scripts/limit_up_*.py` 的 `Path(r"D:\code\Moss-finagent-research")`
改为 `Path(__file__).resolve().parents[1]`，4 处重复的 `MIN_CIRC_MV = 25e8`
改为引用 `STATS_LIMIT_UP_MIN_CIRC_MV`。

### 七、仍然没做的（如实记录）

- `Any` 标注 1589 处、裸泛型 458 处 —— 量大但**不影响运行**，
  且有 1589 处的地方往往正是动态 payload（Agent 间 JSON），收紧类型收益低。
- 377 个公开方法缺 docstring、76 处静默吞异常、60 个零引用公开函数。
- `_load_industry_frame` 无缓存、`_FUNDAMENTAL_CACHE` 无上界 —— 属 §一/§六 的性能项。

---

# 六、优化优先级（按 收益/成本 排序）

| # | 动作 | 收益 | 成本 | 状态 |
|---|---|---|---|---|
| 1 | 生态序列盘前预算 | 决策链路 −97% | 小 | ✅ 已完成 |
| 2 | 截断保头保尾 | 省掉必败重试 | 小 | ✅ 已完成 |
| 3 | 缓存键加 scope | 正确性 | 小 | ✅ 已完成 |
| 4 | **删死代码/死配置/`.bak`** | 第一印象 | 极小 | ✅ 已完成 |
| 5 | **修 `sh/sz` 前缀 bug** | **真 bug** | 小 | ✅ 已完成 |
| 6 | **`intraday/config.py` 相对路径 → 绝对路径** | 修"配置静默失效" | 极小 | ✅ 已完成 |
| 7 | 分析层按 Agent 白名单过滤数据点 | 省 ~2 万 tokens_in/轮 | 中 | 待办 |
| 8 | 成本折算进审计（`cost_yuan`） | 让优化可验收 | 小 | 待办 |
| 9 | 7 处 `use_cache=False` 改回 + trace_id 规范化 | 8 天省 47.7 万 tokens | 小 | 待办 |
| 10 | `_best_achievable` 向量化 | 做T链路 96% CPU | 中 | 待办 |
| 11 | A17 上游结论压缩 + ReAct 不重发全量 | A17 占 55% 成本 | 中 | 待办 |
| 12 | 消掉 `domain → infrastructure` 18 条反依赖 | 分层纪律 | 中 | 待办 |
| 13 | `str(exc)[:N]` 统一 | 最大一类魔鬼数字 | 半天 | ✅ 已完成（255 处） |
| 14 | 同名不同值常量归一 + CI 守卫 | 消除口径漂移 | 半天 | ✅ 已完成 |
| 15 | 评测集 + CI | 工程闭环 | 中 | 待办 |
| 16 | 拆分 `intraday/service.py` | 可维护性 | 大 | 待办 |

**若只有半天：做 4 → 5 → 6 → 2（已完成）→ 排查 `auction_select/config.py` 注释**
（约 4 小时，全是低风险高可见度）。
