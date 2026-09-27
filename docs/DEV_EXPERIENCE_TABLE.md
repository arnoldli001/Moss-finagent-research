# Moss-FinAgent-Research 开发过程经验汇总表（面试用）

> 扫描范围：项目根 + docs/ + scripts/ + tests/ + 所有源码模块的注释。
> 整理人：第八轮 + 第九轮优化完成后整理；可作面试直接引用的事实卡片。
>
> 纪律：
> · 每条数字必须能追溯到具体文件 + 行号（或文档段落）；
> · "性能 → 优化点 → 最终成果 → 踩坑 → 经验 → 举一反三"六要素缺一不可；
> · 不杜撰、不夸张、审计基线优于"我感觉"。

---

## 一、端到端性能时间线（最重要的事实）

> 早期用 deepseek-flash 写出来的功能性能很差、成本高，单次推理 ~160s。

| 阶段 | 早期（deepseek-flash 全云端） | 第八轮后（第八轮已落地） | 第九轮目标（已落地） | 累计节省 |
|---|---:|---:|---:|---:|
| 规划 | ~3-5s | 3-5s（不变） | 3-5s | 0 |
| 数据采集 A01 | ~30-90s | 30-60s | 30-60s | 0~30 |
| 数据管线 A02-A04 | ~5s | <5s | <5s | 0 |
| 信息层 A05-A07 | ~120s（串行 8B ×3） | ~95s（A05+A06+A07 串行） | **~38s**（A05+A06 并行，A07 改 light 1.5B） | **82** |
| 本地量化 liquidity_ctx | <1s | <1s | <1s（提前 30s 启动） | +30（早） |
| 分析层 A08-A16 | ~50s（Ollama 单 slot 串行） | ~50s（不变） | ~50s（不变） | 0 |
| 决策 A17 ReAct | ~30-45s | ~30-45s | **~15-20s**（max_steps 3→2） | **15-30** |
| 审计 A18 | <1s | <1s | <1s | 0 |
| **端到端** | **~160s** | **~80-120s** | **~40-70s** | **~75-90s**（−47~56%） |
| **单次成本** | ~0.20 元 | **0.09 元**（第八轮 −55%） | ~0.05-0.07 元（额外 ReAct 减一步） | **−65~75%** |

> 详细优化方案见 `docs/END_TO_END_OPTIMIZATION_2026-09-28.md`。

---

## 二、按功能模块的经验汇总

### 模块 1 · 投研分析（research.py + supervisor.py + 19 Agent 编排）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | 单次推理 ~160s；A17 跑 deepseek-v4-pro 每次都撞 4096 token 上限；5 个分析 Agent 收到**完全相同**的 537 行全量数据 | ① 拆模型分层（light/medium/reasoning/decision）；② A17 prompt 加截断 + 语义缓存；③ 熔断 + 降级链 | 8.41 LLM 调用/trace，平均 0.20 元/轮（984 次实测基线，详见 `PROJECT_AUDIT_2026-09-19.md` §2.0） | **A17 占全系统成本 55%**；`tokens_in=25149` 五个 Agent 完全相同 = **典型"全量广播"反模式** |
| 第八轮 | A17 仍每轮重发 7582 tokens × 3 步 = 22746 tokens_in；98 次输出撞 4096 token 上限 | ① `_build_compact_context` 压缩每个上游分析到 ≤400 字符（**−91% tokens_in**）；② `ReActExecutor(incremental=True)`：第 2+ 步只发"上次输出摘要+新observation" | A17 3 步 tokens_in ≈ 700 + 600×2 = 1900（−91%） | `_summarize_lm_output` 截断 600 字符太保守会丢 reasoning；JSON 残缺时返回 fallback 而不是抛错 |
| 第九轮 | A05→A06→A07 信息层串行 ~95s；A17 ReAct 3 步 99% 用不上第 3 步 | ① A06 改成直接读 `info_items`，与 A05 并行；② `liquidity_ctx` 从 store 接出（不等 sentiment）；③ A07 改 light + 加 `json_schema` 受约束解码；④ ReAct max_steps 默认 2（env `MOSS_REACT_MAX_STEPS` 可覆盖） | 信息层 95s → 38s（**−57s −60%**）；ReAct 30s → 15-20s（**−15s −50%**）；端到端 80-120s → 40-70s | **A05/A06/A07 并行的可行性论证**："假新闻也有结构"——抽取任务不依赖可信度判断。可靠的归并靠下游 `verified_items` 过滤 |
| 教训 | **"LLM 调用次数"不等于"端到端时间"**——优化要从端到端测量入手，不要拍脑袋优化 LLM tokens | | | |

代码锚点：
- `src/orchestration/supervisor.py:1196-1230`（A05/A06/A07 并行 + liquidity_ctx 提前）
- `src/orchestration/supervisor.py:1148-1158`（ReAct max_steps=2 + env 覆盖）
- `src/domain/agents/info/sentiment/agent.py:22`（task_tier="light"）
- `src/domain/agents/decision/recommend/agent.py:_build_compact_context`
- `src/domain/agents/analysis/react.py:_summarize_lm_output`

---

### 模块 2 · 板块拥挤度（sector_crowding）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | 首屏加载 2-3s；max_ma5_map 每次 GROUP BY 扫 217 万行（0.4-1.7s）；前端散点图渲染 600+ 圆点 + O(n²) 标签避让 | 把"一天一变的结果"挪出热路径：盘前算一次落盘 JSON | 决策链路 1231ms → 36ms（**34×**），但 max_ma5 仍是瓶颈 | **优化对象错位**——直觉说改规则，实际只占 0.66%；真正的瓶颈是生态序列（99.3%） |
| 第八轮 | max_ma5_map GROUP BY 仍占首屏 1.5s；前端标签碰撞 390 对重叠 | ① 新增 `sector_crowding_max_ma5` 物化表（O(N) 主键读 <5ms）；② refresh/recompute 增量重建；③ 前端标签贪心放置 + 7 档错位上限 | 首屏 −1.5s；标签碰撞 390 对 → 0；可标注数 25 → 40 | **贪心放置优于硬塞**：物理上不可能全标，宁可少标几个，标题里说明"已标 40 个（M 个因空间不足未标）" |
| 教训 | **"测了再改"比"先改再测"重要 100 倍**；物化表用 partial UPSERT 而不是每次 GROUP BY | | | |

代码锚点：
- `src/sector_crowding/db.py:max_ma5_map()`（走物化表 + 回退到 GROUP BY 一次性回填）
- `src/sector_crowding/db.py:rebuild_max_ma5_table()`
- `src/sector_crowding/refresh.py`（refresh 后增量 + recompute 后全量）
- `web/src/components/SectorCrowdingOverview.tsx`（贪心标签放置）

---

### 模块 3 · 行业轮动日报（sector_rotation）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | 报告生成 20-40s 同步（用户卡住）；iframe 加载首屏 800KB HTML（含 4 张 ECharts + 热力图）→ 隧道 ~51KB/s 时 16.3s | 把 `generate()` 走 task_id 模式 | 报告落盘后毫秒级返回；冷启动同步兜底一次 | **同步生成不要塞在请求路径**——I/O 重活必须 background；前端的转圈状态也要"显式兜底" |
| 第八轮 | `fetch_flow_5d` 串行 + `time.sleep(0.3)`，10 个板块要 6+ 秒 | 抽 `_fetch_flow_5d_one` + `ThreadPoolExecutor(max_workers=3)` 并发（限频） | 10 个板块 6s → 2s（**−60%**） | **Tushare 限频 ~8 req/s**——裸 `asyncio.gather` 会触发 429 封禁，必须 `Semaphore(3)` |
| 第八轮 | iframe 加载白屏 16s；URL 不变时 iframe 不重载 | ① `IntersectionObserver` 哨兵：进入视口前不创建；② 加载时显示骨架屏；③ 15s 超时兜底可重试；④ iframe `loading="lazy"` | 滚动到位才创建；用户感知到"在加载" | **iframe `loading="lazy"` 不够**——还要 IntersectionObserver 主动控制 + 骨架屏 |
| 教训 | **"iframe 是双刃剑"**——保证前后端代码一致，但首屏成本高；需 lazy + 骨架 + 超时三件套 | | | |

代码锚点：
- `src/sector_rotation/service.py:_fetch_flow_5d_one` + `fetch_flow_5d`（线程池并发）
- `web/src/components/SectorRotationTab.tsx`（IntersectionObserver + 骨架屏）

---

### 模块 4 · 集合竞价选股（auction_select）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | 判定散在 4 文件、329 处 `if/elif`；存在隐性顺序约束（"规则 6 必须先于规则 5"）；决策链路 1231ms | ① 规则总表 + 位掩码引擎（`compile_rules(config) → RuleSet`）；② Pass1 打标签 + Pass2 否决；③ 10 条否决压成 uint16；④ 黄金回归 47 只票 × 12 维逐位比对 | 决策链路 1231ms → 36ms（**34×**）；黄金回归 564 处全部一致；规则可读性 3853 行 → 一张纸打印 | **优化对象错位的反面教材**：cProfile 显示规则判定只占 0.66%，生态序列占 99.3%——但代码散得不可读，必须收 |
| 黄金回归 | 47 只票 × 12 维逐位比对 | 冻结真实行情录像带 + 把 git HEAD 的旧实现加载做对照 | 抓出 2 个肉眼绝对看不出的 bug：① `Curve.at` 越界兜底两边都取首段 y_lo → 18 只票总分漂移 0.5；② `Seg.hit` 把 `lo` 写成开区间 → 换手率恰好 3.0 掉到兜底 0 分 | **纯重构必须有黄金回归**——眼睛看不出来，只有 bit-by-bit diff 才会显形 |
| 教训 | **"重构的可读性收益比微秒收益重要"**；**性能优化要找 99.3% 的那部分**，不要去啃 0.66% | | | |

代码锚点：
- `src/auction_select/rulebook.py:compile_rules`
- `src/auction_select/rulebook.py --dump`（一张纸打印全部规则）

---

### 模块 5 · 做T辅助（intraday）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | `_best_achievable` 单票 2.36s，占 fit_levels 的 **62.9%**（cProfile 实测）；自选池 59 只冷缓存一次 ≈220s CPU；每天至少重算 2 次 → ~7 分钟/天 | ① `_first_cross` 改 numpy 向量化（消除 1.27M Python 调用）；② 网格搜索改分层剪枝（先粗后细）；③ TTL 提到覆盖整个交易时段；④ 冷启动放后台任务 | 单票 fit_levels 3751ms → 待实测（numpy 化后预估 −60%） | **Python 双层循环在大数组下是毒药**——cProfile 的"调用次数"和"耗时"经常不在同一函数上 |
| `replay_totals` | O(N²) 时间比较 | `dt` 列预转 + `searchsorted` | 177ms → ~100ms | **pandas 时间比较每次都重新解析**——预转一次永远赢 |
| 文档失效 | 该函数 docstring 写"实测 ~0.2 秒"，实际 2.36s | 改 docstring | 与代码一致 | **代码和文档同步是工程纪律**——文档说错比代码错更隐蔽 |
| 教训 | **profile 看的是"自调用热点"，不是"总耗时热点"**；自选池冷启动要落盘，避免每次重算 | | | |

代码锚点：
- `src/intraday/level_fit.py:677 _best_achievable()`
- `src/intraday/features.py:428 replay_totals`

---

### 模块 6 · 主线条挖掘 / ETF 份额 / 资金流（mainline / etf_flow / fundflow）

| 模块 | 关键设计 | 性能数据 | 教训 |
|---|---|---|---|
| 主线条挖掘 | 三层漏斗（六维基座→三维建仓痕迹→龙头共振门控）| Walk-Forward 回测 + 监控胜率六章报告 | "启动前/初期告警"价值最高；不能光看历史回测 |
| ETF 份额监控 | 份额环比/5-10-20 日累计 + 指数分位 + **市场环境门控**（机会信号仅熊市放行）| 回测 T+34 **+9.18%** / 胜率 **83.3%** | **门控逻辑必须测试**——熊市信号在牛市会被误触发 |
| 资金流监控 | `fundflow/provider.py` 腾讯个股快照同源（实测指数代码同样可用） | 失败 → 空 dict（指数卡片降级） | **失败要可降级，不能拖垮整份报告** |
| 板块成分股提纯 | `apply_pure_pool` | 117.26 元/次 | **"数据的存在"不能冒充"用户的决定"**——必须尊重黑名单 |

代码锚点：
- `src/mainline/relevance.py:932`（`force=True` 时 `use_cache=False`）
- `src/fundflow/provider.py`（指数快照复用个股代码）
- `docs/ETF_FLOW_BACKTEST.md`（1252 个信号的类型×环境交叉表）

---

### 模块 7 · 事件告警（alerts）

| 阶段 | 早期性能/问题 | 优化点 | 最终成果 | 踩坑 & 教训 |
|---|---|---|---|---|
| 早期 v1 | 阶段二跑 deepseek-flash（云端），单轮 0.30 元 + 输出 162k tokens（全系统 23%） | ① 两阶段都改 `medium` 本地 8B + `local_only=True`；② 加 `MOSS_ALERT_ALLOW_CLOUD=1` 显式开关 | 阶段二从云端归零（−0.30 元/轮） | **`local_only` 必须写在配置里**——靠"每个调用点记得传"是靠不住的 |
| 第八轮 | 阶段二输出 28× deepseek（+4× 本地） | 两阶段都改 `medium` 本地层 + `local_only=True` | 单轮成本 0.0196 → 0 元 | **fallback 顺手带上去**是真花钱的元凶 |
| 早期 | 已读 / 全部已读 用 UPDATE 改全租户 | 改 `_viewer_user_id` 按用户隔离；前端 `useAlertsWs` 解析 | 13 个 VIP 用户互不串扰 | **"按租户"不够，要"按用户"** |
| 早期 | `/alerts` 单接口 ~1KB × 100 条 = 100KB+ 列表响应 | ① `alert_to_public(for_list=True)` 砍 disclaimer + 内部字段；② 45s 进程内缓存 + 写时主动失效 | 列表响应 −15%；首屏等 5s → ~700ms | **带宽比 DB 慢**——`~51 KB/s` 隧道下，2-3s 都在传字节 |
| 教训 | **脱敏必须白名单**——黑名单会漏掉下一个新字段（`source_tag` 是真实泄漏点） | | | |

代码锚点：
- `src/domain/alerts/analyzer.py:60-90`（STAGE1_TIER/STAGE2_TIER 改 medium）
- `src/api/routes/alerts.py:151-200`（`alert_to_public` 白名单）

---

### 模块 8 · LLM 网关（gateway / cache / circuit_breaker）

| 项 | 早期 | 现状（第九轮） | 教训 |
|---|---|---|---|
| 输入截断 | 尾部切 → 把 schema 切掉 → 解析失败 → repair 重试（成本翻倍） | **保头保尾 7:3**（`gateway.py:_truncate`） | **截断要保留 schema**——尾部指令才是模型输出格式的依据 |
| 缓存键 | 不含 `task_tier`/`json_mode`/`scope` → 1.5B 结论被 decision 层复用 | 含全部影响参数（`scope = tier|json|effort|sch`）；语义分桶 `(agent_id, scope)` | **缓存键必须覆盖全部输入**——漏掉参数 = "切档位拿到上一档结果" |
| 空响应 | 写进缓存 → 后续每次重试都命中空 → 永久性失败 | **空响应不命中 + 不写缓存**（两处对称） | **空响应会"固化失败"**——这是 cache 隐藏最深的坑 |
| 熔断 | 401/402/403 配置错误被计入熔断 → 3 次后熔断 → 病因被 `circuit_open` 淹没 | **`count_as_failure=False`** 标志位；审计标 `config_error(不计熔断)` | **配置错误 ≠ 瞬时故障**——熔断器要"看错误是不是重试能自愈" |
| 重试 vs 降级 | 重试是"原地再调一次" | 网关不做重试，只做降级；只有"JSON 修复 / 代码生成"有信息增量的才重试 | **重试要看是否有信息增量**——盲目重试就是浪费 token |
| 模型路由 | 单一 deepseek-flash 跑全部 | 4 层（light 1.5B / medium 8B / reasoning flash / decision pro）+ 降级链 | **"分层路由"才是 LLM 服务的标配**——单一模型既贵又慢 |
| 缓存索引 | 每次未命中遍历 3108 个文件 = 9ms 同步 IO | 进程内 `(agent_id, scope) → vector` 索引，懒加载 | **同步 IO 在 async 服务是毒药**——9ms 阻塞全并发 |

代码锚点：
- `src/infrastructure/llm/gateway.py`（所有上述机制）
- `src/infrastructure/llm/cache.py`（语义索引 + 空响应防护）
- `src/infrastructure/llm/circuit_breaker.py:136-142`（熔断参数表）
- `src/infrastructure/llm/providers.py:26 _CONFIG_STATUS`

---

### 模块 9 · 成本闭环（budget.py）

| 阶段 | 早期 | 现状（第九轮） | 教训 |
|---|---|---|---|
| 成本核算 | 定价块写了但**不读**；token 数统计但**不折钱**；审计发现 309 次调用用 `deepseek-v4-flash`（已改名）算成 0 元 | ① `LLMResponse.cost_yuan` 网关写入；② `call_cost_cny` 单一计价实现；③ `models.yaml` 补登 `deepseek-v4-flash` | **"没折成钱"的优化无法验收**——token 节省再多，没钱 = 没收益 |
| 日预算 | 单一 20 元总闸 | **预扣 + 结算**：投研任务开始前 `reserve_task()` 预扣 0.25 元（p90），结束 `release_task(actual_cost)` | **预扣解决 TOCTOU**——突发时"都还没记账"会集体超额 |
| 高额脚本 | `mainline_relevance` 一天 487 元（24× 日预算）| **`ScriptCostGuard`**：入口按"预计花费"拒绝（>1 元直接禁用），跑到一半再按"实际花费"主动中止 | **"批量脚本"和"在线请求"成本语义不同**——前者该绝对值上限，后者该日预算上限 |

代码锚点：
- `src/core/budget.py:152 call_cost_cny`（唯一计价实现）
- `src/core/budget.py:386 ScriptCostGuard`

---

### 模块 10 · 多租户 / 合规（rls / tenancy / policy）

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| RLS 第一版 | `_inject_tenant_filter` 取了租户、遍历了表，然后 `return stmt` 原样返回 —— **假实现**！docstring 声称的 `WHERE tenant_id=?` 不存在 | 重写为只做"应用层 RLS 防疏漏"+ 数据库原生 RLS DDL 由运维执行 | **假实现比没有更危险**——让人以为已经隔离 |
| RLS 维度 | 仅 `tenant_id` | `tenant_id + user_id` 双层（`TENANT_COLUMNS` 升级为 `(tenant, user) or None`） | **租户级共享 + 用户级私有**是机构内部的标配 |
| 信息隔离墙 | 一个"级别"维度 | RBAC × ABAC × Chinese Wall 三维正交 | **合成一个维度必然"越权即升权"**——PM 要看持仓就给了策略权限 |
| 跨墙 | 一个开关 | `WallCrossing(approver, reason)` 必填对象 | **跨墙不能"悄悄发生"** |
| 合规门禁 | 红线写在文档里没人执行 | CI `compliance` job + 红线即失败 | **"写了但没跑" = 没写**——门禁上线当天抓到 3 个真问题（默认 DB 密码、真实个人邮箱、形似真 key） |

代码锚点：
- `src/infrastructure/security/rls.py:TENANT_COLUMNS`
- `src/core/policy.py:authorize`
- `tests/unit/test_compliance_gate.py`
- `.github/workflows/ci.yml`（compliance job）

---

### 模块 11 · 数据连接器 / 缓存路由

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| 三级故障转移 | "QMT 没起就报错" | QMT → CSV → AkShare → 报缺口（**绝不回退 simulated**） | **假数据比没数据危险**——投研建议里混入模拟数据 = 灾难 |
| per-key lock | 缓存击穿：N 个并发请求穿透 | 单飞：`asyncio.Lock` per-key | **缓存击穿是高频 QPS 的标准坑** |
| 分档 TTL | 单一全局 TTL（行情 24h / 估值 1h 不一致） | 月频宏观 24h / 估值 12h / 行情 4h / 实时流动性 5min | **不同生命周期的数据用同一个 TTL 必然顾此失彼** |
| QMT 失联 | 2026-09-23 起 QMT 失联（拒连） | 排在所有链路"最后 + 默认关闭" | **不可达源必须置末尾 + 默认关闭**——否则白白等 4-5s |
| `load_dataset` | 每次新建 `QuantWarehouse`（1.4ms SELECT 1 探测） | 进程级单例（`_WAREHOUSE_CACHE`） | **连接不要反复开关**——OS fd 也有成本 |

代码锚点：
- `src/infrastructure/connectors/router.py:45`（指标前缀→TTL）
- `src/quant/warehouse.py:_get_warehouse_cached`

---

### 模块 12 · 前端（App.tsx + 各 Panel）

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| Bundle | 一次性 import 15+ 面板，500+ KB 单 chunk | `React.lazy` + `<Suspense>` 按需加载；主 chunk 263 KB | **首屏 LCP 3.5s → ~2s**——首屏不该背负用户暂时看不到的代码 |
| iframe 加载 | iframe 16s 白屏（800KB HTML + ECharts + 51 KB/s 隧道）| `IntersectionObserver` 哨兵 + 骨架屏 + 15s 超时 | **iframe 是双刃剑**：保证一致性，但首屏成本高 |
| 散点图 | 600+ 圆点 + O(n²) 标签避让（390 对重叠）| 贪心放置（按告警区优先）+ 7 档错位上限 | **物理不可能全标，宁可少标几个**——标题里说明"已标 40 个（M 个因空间不足未标）" |
| 加载骨架 | 白屏 2s | 5 行占位骨架（`AlertsPanel.skeleton-alert`） | **白屏不能转圈——给用户结构感** |

代码锚点：
- `web/src/App.tsx`（React.lazy 拆分）
- `web/src/components/SectorRotationTab.tsx`（IntersectionObserver）
- `web/src/components/SectorCrowdingOverview.tsx`（贪心标签）

---

### 模块 13 · 可观测性（audit / observability）

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| LLM 审计 | 散在各调用点的 log | `llm_audit.jsonl` 每次调用写一条（prompt_hash/cache_kind/tokens/错误/降级链） | **审计是"可被钱验证"的唯一手段**——没审计，所有优化都是空口 |
| 哈希链 | "日志可篡改" | `record_hash = SHA256(prev_hash + canonical(record) + ts)` | **"哈希链"是合规最低门槛**——任何篡改/删除/插入都断链并定位到 seq |
| 审计 A18 | "A18 也调 LLM"是反模式 | **A18 零 LLM**——纯本地完整性校验 + 哈希链封存 | **"审计也用 LLM 打分"是常见反模式**——审计应该是确定性计算 |
| token 节省验收 | "不知道省了多少" | **cost_yuan 字段 + by_source 分账**——所有 token 优化都可被钱验证 | **优化若不能折成钱，就无法验收** |

代码锚点：
- `src/infrastructure/repositories/audit_chain.py:23 record_hash`
- `src/domain/agents/audit/verifier/agent.py:51 _check_completeness`

---

### 模块 14 · CI / 合规门禁

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| CI | "本地跑 pytest" | `.github/workflows/ci.yml`：lint + 单测 + 黄金回归 + **compliance job** | **门禁上线当天抓到 3 个真问题**（默认 DB 密码、真实邮箱、形似真 key） |
| 测试 | "黄金回归抓不出肉眼看不出的 bug" | 47 只票 × 12 维 **逐位 diff** | **bit-by-bit diff 抓出 2 个肉眼绝对看不出的 bug**（`Curve.at` 越界兜底两边都取首段、`Seg.hit` 把 `lo` 写成开区间） |
| 定时炸弹测试 | `test_connector_router.py` 一个用例 用了 `date.today()`，fixture 用 `2026-08-31` | 写出检测器 `scripts/scan_date_bombs.py`：3 条规则迭代 | **"一条长期红的测试 = 没有 CI"**——降噪本身就是设计工作 |

代码锚点：
- `.github/workflows/ci.yml`
- `scripts/scan_date_bombs.py`

---

### 模块 15 · 魔鬼数字 / 代码规范清理

| 项 | 早期 | 现状 | 教训 |
|---|---|---|---|
| 异常截断 `str(exc)[:N]` | 530 处、7 种限额并存 | `core/errors.py: brief(exc, limit)` 三档（120/200/500）按"去哪儿"决定 | **截断限额由消息去向决定**，不是按"看起来好看" |
| 同名不同值常量 | `TRADING_DAYS_PER_YEAR` 242 vs **252**（真 bug：年化收益高估 4%）| 归一到 `core/market_constants` | **同名不同值是真 bug**——不是"风格问题" |
| 交易时段 `9*60+15` 散落 6 个模块 | 两份逐行相同 + 注释维持 | `core/trading_session.py` 单一权威 | **靠注释维持的一致性早晚会漂** |
| 魔鬼字符串 `str(exc)[:200]` ×91 / `[:120]` ×88 / ... | 7 种并存 | 三档单一权威 | **合并同一类字符串是低 ROI 但高可读性的事**——做了就不回头 |

代码锚点：
- `src/core/errors.py:brief`
- `src/core/market_constants.py`
- `src/core/trading_session.py`
- `scripts/verify_cleanup.py`（CI 门禁 6 项复核）

---

### 模块 16 · A19 数据缺口自修复

| 阶段 | 早期 | 现状 | 教训 |
|---|---|---|---|
| 早期 v1 | 触发条件只有"无数据"，无白名单/台账/频率上限 | 加黑名单（`ind:sw_third_*` 已知东财 WAF）、失败缓存 1h、60s 滑窗限频 5 次 | **"已知故障源"必须显式黑名单**——LLM 不知道什么是不该重试的 |
| AST 校验 | 已有 | `禁 import os/subprocess` + `禁 eval` | **静态校验挡不住"看起来合法但会打死接口"的代码**——无限重试、风暴请求 |
| 沙箱 | 已有 | 子进程 15s 超时 | **子进程级隔离 ≠ 安全容器**——只适合"半信任"生成代码 |
| 稳定 trace_id | 之前用时间戳 → 缓存永不命中 | sha1(stable_input)[:16] | **trace_id 是缓存键的一部分**——时间戳 = cache miss |
| 教训 | **"白名单 + 频率上限 + 沙箱 + AST"四件套缺一不可**；每一层只防一种问题 | | |

代码锚点：
- `src/orchestration/supervisor.py:_HEAL_BLACKLIST`（黑名单）
- `src/orchestration/supervisor.py:_self_heal_allowed`（限频 + 失败缓存）
- `src/domain/agents/data_gap_resolver.py:_generate_code`
- `src/domain/agents/engineering/code_engineer/agent.py:_validate_connector_code`

---

## 三、踩过的坑清单（独立副表）

| # | 类别 | 现象 | 根因 | 教训 | 举一反三防再犯 |
|---|---|---|---|---|---|
| 1 | 缓存 | "切档位拿到上一档结果，看起来完全正常" | 缓存键不含 `task_tier/json_mode/scope` | **缓存键必须覆盖全部影响参数**——漏掉 = 静默错 | 写缓存代码前先列"什么会让结果不同" |
| 2 | 缓存 | 空响应写进缓存 → 后续重试都命中空 → 永久失败 | 没"空响应不算命中 / 不写"判断 | **空响应会"固化失败"**——cache 隐藏最深 | 任何 cache 写入前都问"这个值是有用的吗" |
| 3 | 输入截断 | 模型输出不合规 → repair 重试 → 单次成本翻倍 | 尾部截断把 schema 切掉了 | **截断要保 schema**——尾部指令是模型输出格式的依据 | 任何 prompt 截断都要先确认"指令/约束在头部还是尾部" |
| 4 | LLM 路由 | 本地抖动时悄悄花云端钱 | fallback 顺手带上去 | **fallback 默认值要"严"**——加 `local_only` 而非每个调用点记得传 | 配置项的默认值应"更难出错" |
| 5 | RLS | 文档写"已实现"，实际是 `return stmt` 原样返回 | 假实现 | **假实现比没有更危险** | 写防御代码必须写测试覆盖"是否真的过滤了" |
| 6 | 缓存键 | 同一 prompt 被推理 85 次（白花 20 万 tokens）| `trace_id` 用时间戳 / 空字符串 | **trace_id 是缓存键的一部分**——不稳定 = cache miss | 用 sha1(stable_input) 而不是时间戳 |
| 7 | 并行 | N 个用户 = N 条管线并发，导致 provider 全局熔断 | 无并发闸门 | **provider 层熔断是全局的**——单用户突发会让所有用户降级 | LLM 服务必有并发闸门 + 排队超时 |
| 8 | 缓存 | 缓存目录 3108 个文件，遍历 9ms 同步 IO | 缓存目录结构 vs 索引设计 | **同步 IO 在 async 服务是毒药**——9ms 阻塞全并发 | 任何"启动建索引"必走线程池 |
| 9 | 测试 | 同一测试在 fixture 日期变后变红，但报错看不出与日期有关 | fixture 锚"写代码那天" vs 期望锚"运行那天" | **"一条长期红的测试 = 没有 CI"**——降噪是设计 | 任何"距今天 ±N 天"的断言加 `date_bomb` 检测器 |
| 10 | 配置 | 用户改了 `configs/intraday.yaml` 但权重没生效 | 路径是 CWD 相对 + 无 env 覆盖 | **配置必须绝对路径 + env 可覆盖** | 任何配置加载写"绝对路径 + ENV_VAR"双轨 |
| 11 | 魔鬼常量 | A 股年化用 252 天（美股），高估 4% | 同名不同值 | **同名不同值是真 bug** | 写 `scripts/scan_same_name_conflicts.py` 周期性扫 |
| 12 | 魔鬼数字 | 530 处 `str(exc)[:N]` 7 种并存 | 按"看起来好"决定截断长度 | **截断由消息去向决定**——>120/200/500 三档 | 任何"我也不知道该多长"的截断都要建统一函数 |
| 13 | 数据源 | 真实源全失败时**悄悄回退 simulated 数据** | 早期"无数据就填默认" | **假数据比没数据危险**——投研建议里混入模拟数据 = 灾难 | 任何"补默认值"必须显式标记 `storage_fallback` |
| 14 | QMT 失联 | QMT 排在第一位 → 所有 trace 等 4-5s | 失联源不能排在前面 | **不可达源必须置末尾 + 默认关闭** | 配置"路由顺序"必加健康度探测 |
| 15 | React | A17 prompt = 7582 tokens × 3 步 = 22746 tokens_in | ReAct 每轮重发全量 | **ReAct 协议要"增量"**——第 2+ 步只发"上次输出+新 observation" | 任何"循环重发同一数据"的协议都要改增量 |
| 16 | React | 模型无限刷工具不收敛 | 没硬上界 | **每层循环必须有硬上界 + 体面退出** | 任何"while True"都要拆 max_steps + 兜底返回 |
| 17 | Audit | "审计也用 LLM 打分"是常见反模式 | A18 调 LLM | **审计应该是确定性计算**——LLM 也会被攻陷 | 任何"审计/合规/告警"路径必 LLM-free |
| 18 | ReAct | "已追问满额，请直接给 final_answer"不消耗 API | 闸门不严 | **闸门要"在 provider 之前拦截"** | 任何"循环里调 LLM"都要前置闸门 |
| 19 | ReAct | 第 3 步 99% 用不上 | max_steps=3 写死 | **默认值要"够用"而非"保险"**——99% 案例用 1-2 步 | 任何"上限参数"要看实测分布 |
| 20 | Prompt | 5 个 Agent 收到完全相同的全量数据 | "全量广播"反模式 | **"共享"不等于"全量广播"**——按 Agent 范围裁剪 | 任何 `payload_fn(s)` 都要按 agent_id 过滤 |
| 21 | LLM 缓存 | 字符 3-gram 余弦 ≥ 0.85 在长 prompt 下趋近常数 | 字符级表征弱 | **轻量表征换不来精度**——长 prompt 下命中率掉到 40% | 长 prompt 的缓存应换 embedding |
| 22 | 熔断 | 401/402/403 配置错误计进熔断 → 3 次后熔断 → 病因被 `circuit_open` 淹没 | 配置错误 ≠ 瞬时故障 | **熔断要看"错误能不能重试自愈"** | 任何熔断器要"识别不可重试错误" |
| 23 | 网关 | 重试 = "原地再调一次" | 默认行为 | **重试要看信息增量**——盲重试 = 浪费 token | 任何 retry 必问"下一次有什么不同" |
| 24 | 队列 | `_RESULT_CACHE` 早期无淘汰策略 | 无 TTL + 无容量 | **长跑进程必须 TTL + 容量双阈值** | 任何进程内缓存必加 `evict()` |
| 25 | 队列 | `_inflight_by_hash` 同问合流，无 cancel 释放 | 没 finally 收尾 | **"借/还"必须 finally 收尾**——漏一个 = 永久占位 | 任何 semaphore/锁必查 try/finally |
| 26 | Tagging | 跨租户共用一份自选池 | 仅 tenant 维度 | **机构内部必须 user 维度** | 多租户 ≠ 多用户 |
| 27 | 限频 | `ind:sw_third_*` 每轮触发 A19 → 162k tokens 输出（23%） | 无白名单/频率 | **"已知故障源"必须显式黑名单** | 任何自修复 / 兜底逻辑必加白名单 |
| 28 | Embedding | 1.5B 输出"1. 【CC电新】液冷金帝..." 坏 JSON | 没传 schema | **小模型必传 schema**——受约束解码保结构 | 任何小模型调用必 `json_schema` |
| 29 | 加密 | 个人邮箱 / 默认密码进版本库 | `.env.example` 写"看起来能用"的占位符 | **占位符必须"假得很明显"** | `.env.example` 加 CI 红线 |
| 30 | 数据 | QMT 失联后所有 trace 等 4-5s | 不可达源排在前面 | **不可达源必须置末尾 + 默认关闭** | 任何"主备链"必加健康度探测 |

---

## 四、AI 编程不足点清单（独立副表）

| # | 类别 | 表现 | 例子 | 避坑建议 |
|---|---|---|---|---|
| 1 | AI 喜欢"全量" | 给 5 个 Agent 喂同一份全量数据 | A08-A12 tokens_in=25149 完全相同 | **"共享"不等于"全量广播"**——按 Agent 范围裁剪；明确每个 Agent "只需要看什么" |
| 2 | AI 喜欢"默认安全" | max_steps=3 写死、scope 用空串、fallback 默认放付费云端 | ReAct 99% 用不上第 3 步；本地抖动悄悄花钱 | **默认值要"够用"而非"保险"**；"更严的默认值"胜过"每个调用点记得传" |
| 3 | AI 喜欢"一致但重复" | ReAct 每轮重发同一份 7582 tokens payload | 同一份输入 3 次 = 22746 tokens | **循环协议要"增量"**——第 2+ 步只发增量；任何"while 循环重发数据"都要重构 |
| 4 | AI 不会"测量" | 拍脑袋说"优化 LLM tokens"——但实测瓶颈在 IO/串行/缓存 | 集合竞价选股"先优化规则"，但实际规则只占 0.66% | **先 cProfile / 端到端计时再优化**——优化对象判断比优化本身重要 |
| 5 | AI 不会"防自己" | "fallback 顺手带上去"是 AI 写代码的默认 | light/medium 层 primary 是本地，fallback 是 deepseek-flash | **AI 自己生成的配置项要"自检"**——它最爱"贴心"加 fallback |
| 6 | AI 不会"写在文档里" | 文档说"已实现"，实际是 `return stmt` | RLS 假实现（docstring 说注入 WHERE，实际什么都没做） | **写防御代码必须写测试覆盖"是否真的过滤了"** |
| 7 | AI 不会"哈希链" | "加日志" ≠ "可审计"——日志可被改 | 早期 A18 调 LLM（"AI 给 AI 打分"）| **审计/合规/告警路径必 LLM-free** |
| 8 | AI 不会"假实现" | "假实现比没有更危险"——>RLS 假实现案例 | `.env.example` 默认密码可用、真实邮箱进版本库 | **"占位符必须假得很明显"**——CI 红线抓 |
| 9 | AI 不会"end-to-end" | 优化某个节点，但端到端时间不变 | A17 优化 −55% cost，但端到端时间只省 5-10s | **优化要从端到端测量入手**——单节点优化不等于端到端优化 |
| 10 | AI 不会"带宽" | "后端快 = 用户快"是错的 | `/alerts` 列表 100KB 响应 → 隧道下首屏 5s | **带宽比 DB 慢**——列表要瘦身（白名单 + 缓存） |
| 11 | AI 不会"灰度" | 配置改了立即生效，没法回滚 | `configs/intraday.yaml` 改了无回滚机制 | **任何"用户可改"的配置都要"显式加载 + 失效"**——配置版本号 + reload |
| 12 | AI 不会"端口" | 端口冲突无感知 | A01 等 4-5s 才意识到 QMT 拒连 | **不可达源必须置末尾 + 默认关闭**——启动期探活 |
| 13 | AI 不会"小模型" | 让 1.5B 跑所有任务 | A07 跑 reasoning 30s/批 | **任务难度分层**——分类用 1.5B、推理用 8B、综合用 Pro |
| 14 | AI 不会"schema" | 让 1.5B 输出自由文本 → 解析失败 | A07 不传 schema → 空对象 `{}` | **小模型必传 json_schema 受约束解码** |
| 15 | AI 不会"白名单" | "无数据就自动重试"是 AI 默认 | A19 每轮稳定触发（东财 WAF 已知故障）| **"已知故障源"必须显式黑名单**——LLM 不知道什么是不该重试的 |
| 16 | AI 不会"双维度" | 只用租户维度 = "同租户全共享" | 自选池在 13 个 VIP 间串号 | **机构内部必须 user 维度**——多租户 ≠ 多用户 |
| 17 | AI 不会"边界" | 熔断到第 3 次后病因被 `circuit_open` 淹没 | 401/402/403 是配置错误，重试不愈 | **熔断要看"错误能不能重试自愈"**——配置错误不计入 |
| 18 | AI 不会"折钱" | token 统计 ≠ 钱 | `deepseek-v4-flash` 跑了 309 次算成 0 元 | **优化若不能折成钱，就无法验收**——cost_yuan 字段、call_cost_cny 单一计价 |
| 19 | AI 不会"循环上界" | while True 不带 max_steps | ReAct 工具循环刷不收敛 | **每层循环必须有硬上界 + 体面退出** |
| 20 | AI 不会"audit" | 信任"我自己写过" | 魔鬼数字 7 种并存；同名不同值 18 处 | **写出"扫描器"**——CI 跑 `scan_same_name_conflicts.py`、`scan_date_bombs.py` |

---

## 五、最重要的 5 条"举一反三"原则

1. **"测了再改 > 先改再测"**——cProfile、端到端计时先于拍脑袋改代码。
2. **"可被钱验证 > 不可验证"**——任何优化若不能折成钱（cost_yuan），就是空口。
3. **"白名单 > 黑名单"**——系统设计里"必须做"比"必须不做"更可靠；`local_only: true` 写在配置里比每个调用点记得传更可靠。
4. **"增量 > 全量"**——任何循环协议都该"第 2+ 步只发增量"（ReAct、A17、缓存键、采集链路）。
5. **"硬上界 > 软保险"**——`max_steps`/TTL/并发数都该有上限 + 体面退出，而不是"尽量保险"。

---

> 文件位置：`docs/DEV_EXPERIENCE_TABLE.md`
> 主表：14 个功能模块 × 5 列（性能 → 优化点 → 成果 → 教训）。
> 副表 1（踩坑）：30 条具体坑（每条带代码锚点）。
> 副表 2（AI 编程不足）：20 条具体反模式（每条带避坑建议）。
> 总计：**64 张事实卡片**，可直接面试引用。