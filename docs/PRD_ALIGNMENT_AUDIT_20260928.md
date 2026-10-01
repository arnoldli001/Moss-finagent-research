# PRD ↔ 仓库现实 · 对账审计（2026-09-28）

> **这份文件是什么**：`docs/PRD.md`（用户 2026-09-12 提供的原始需求，409 行）
> 与**仓库当前现实**（`AGENTS.md` / `configs/` / `src/` / `web/` / `tests/`）的逐条对账。
>
> **谁在用**：`docs/REQUIREMENT_CHANGELOG.md`（变更台账）里每条 `待办 / 待确认`
> 的证据基座 —— 台账只写结论，证据在本文。
>
> **怎么复跑**：
>
> ```bash
> uv run python scripts/prd_sync_check.py --keyword "<关键词>"   # 三态：PRD / 台账 / 仓库
> uv run python scripts/prd_sync_check.py --list-sections        # 43 节 → PRD-n.m
> uv run python scripts/prd_sync_check.py --ledger               # 台账完整性（含"落 PRD 才算闭环"）
> ```
>
> **⚠️ 行号口径**：A 表里的 `PRD 行号` 是**对账当时（修订前）**的行号。本次已就地落的
> 4 条见 §E；`docs/PRD.md` 修订后行号整体下移，**引用一律用章节 ID（`PRD-n.m`）**，
> 不要用行号。
>
> **类型口径**：
> - `PRD有仓库无` = 写了但从未实现 / 已废弃 → 看该不该废止
> - `仓库有PRD无` = ★ 实现了但需求文档里查不到 → **补写 PRD**（用户要的"没有的补充进去"）
> - `口径冲突` = 两边都有但数值/名称/选型不一致 → **改写 PRD（留废止痕迹）**（"有差异的改动"）

---

## A. 确认漂移表（56 条）

| # | PRD 章节 / 行号 | PRD 原文（≤40 字，原样截断） | 仓库现实 | 证据 | 类型 |
|---|---|---|---|---|---|
| 1 | PRD-2.1 / 32 | 「数据层 \| 统一数据总线 \| PostgreSQL + SQLite + Redis + Neo4j（可选）」 | 默认 **SQLite 零依赖**；PG/Redis 可选；**Neo4j 全仓库 0 处** | `AGENTS.md:8`；`.env.example:27-38`；`src/core/config.py:242-243`；`docker-compose.yml:4,16` | 口径冲突 |
| 2 | PRD-2.1 / 34 | 「认证层 \| Keycloak（Demo可简化） + OPA \| SSO、MFA、RBAC+ABAC」 | 自研 HMAC 载荷 + 自研 RBAC×ABAC×隔离墙；**Keycloak/OPA 0 处** | `src/core/tenancy.py:37,49,69`；`src/core/policy.py:78,110`；`docs/SECURITY_COMPLIANCE.md:119` | 口径冲突 |
| 3 | PRD-2.1 / 31 | 「Agent层 \| 核心Agent（6-8个优先）」 | 已实现 **19 个**（A01–A19） | `src/api/runtime.py:285-303`；`docs/AGENT_REGISTRY.md:54` | 口径冲突 |
| 4 | PRD-2.1 / 31 | 「Agent层 \| … \| LangChain + FastAPI \| 各自独立任务处理」 | LangChain 仅声明依赖、代码未见调用；Agent 是**进程内模块** | `pyproject.toml:18-19`；`src/orchestration/supervisor.py:19`；`src/core/base_agent.py:11`；`AGENTS.md:32` | 口径冲突 |
| 5 | PRD-2.2 / 48-49 | A05「信息去伪Agent \| 信息层 \| P1」；A06「信息提取Agent \| 信息层 \| P1」 | `agents.yaml` 为 **P0**；`AGENT_REGISTRY.md` 为 P1（仓库内部也不一致） | `configs/agents.yaml:38,45`；`docs/AGENT_REGISTRY.md:18-19` | 口径冲突 |
| 6 | PRD-2.2 / 55 | A12「合规爆雷Agent \| 分析层 \| P2」 | `agents.yaml` 为 **P1**、名称为「合规风险Agent」 | `configs/agents.yaml:85-88`；`docs/AGENT_REGISTRY.md:34` | 口径冲突 |
| 7 | PRD-2.2 / 50-55 | A07 P2；A13–A16「微调金融模型 / 云端API」P2 | `agents.yaml`：A07 **P1**、A13–A16 **P1**、模型 **deepseek-flash** | `configs/agents.yaml:52,96,103,110,117`；`configs/models.yaml:139` | 口径冲突 |
| 8 | PRD-2.2 / 56-59 | 「微调金融模型 / 云端API」 | **无任何微调金融模型**（无 adapter/gguf 等产物） | `configs/agents.yaml:97,104,111,118` | PRD有仓库无 |
| 9 | PRD-2.2 / 38 | 「Agent全景清单（18个Agent + 2个基础模块）」 | 「2 个基础模块」**未指名**；实际 A19 是第 19 个 Agent | `docs/PRD.md:38`（表内 A01–A18）；`src/api/runtime.py:303` | 内部矛盾 |
| 10 | PRD-2.2 / 60 | A17「云端API（DeepSeek-V4-Pro / Claude Opus 5）」 | 路由实际 **deepseek-flash**；v4-pro 仅登记未接路由；**Claude 全库 0 处** | `configs/models.yaml:96-109,203-213` | 口径冲突 |
| 11 | PRD-2.3 / 65 | 「MCP（Model Context Protocol）：作为工具调用层」 | **未实现**：自研 `ToolRegistry`（4 个工具） | `src/domain/agents/analysis/react.py:1365-1434` | PRD有仓库无 |
| 12 | PRD-2.3 / 67 | 「内部消息格式：JSON，包含 message_id、sender、receiver、message_type、timestamp、payload、metadata」 | 实际字段为 **content / reply_to**，无 payload/metadata/audit_id | `src/orchestration/message_bus.py:42-50` | 口径冲突 |
| 13 | PRD-2.3 / 66 | 「A2A（Agent-to-Agent）」 | ✅ 已实现（`ask_agent` 问答总线 + state 落库） | `src/orchestration/message_bus.py:106-145` | 已核实一致 |
| 14 | PRD-3.1 / 81 | 「券商研报 \| 慧博投研 \| www.hibor.com.cn 订阅 \| P1」 | **无连接器**；仅文档一行（`hibor` 仅出现在主线的宏观敏感性配置里） | `docs/DATA_SOURCE_INTEGRATION.md:13`；`configs/mainline_macro_sensitivity.yaml` | PRD有仓库无 |
| 15 | PRD-3.1 / 82 | 「大宗商品 \| 生意社 \| www.100ppi.com 网页 \| P2」 | **无连接器**；仅文档一行 | `docs/DATA_SOURCE_INTEGRATION.md:14` | PRD有仓库无 |
| 16 | PRD-3.1 / 79 | 「厄尔尼诺 \| NOAA \| psl.noaa.gov 文件下载」 | **无连接器**；仅一处提及该词 | `configs/mainline_ground_truth.yaml:158` | PRD有仓库无 |
| 17 | PRD-3.1 / 75,77 | 「宏观经济 \| 国家统计局 \| data.stats.gov.cn API/网页」「央行数据 \| www.pbc.gov.cn 网页」 | **无直连连接器**，实际经 AkShare 封装 | `src/infrastructure/connectors/akshare_connector.py:453`；`real_industry_connector.py:221` | 口径冲突 |
| 18 | PRD-3.1 / 80 | 「A股行情 \| AkShare / Tushare Pro / BaoStock」 | 实际 **5 源链**：AkShare → 腾讯 → Tushare → baostock → 本地CSV → [QMT 开关最末] | `AGENTS.md:12`；`docs/DATA_SOURCE_ROUTING.md:18` | 口径冲突 |
| 19 | PRD-3.1（全表缺） | —（PRD 无 QMT 一词） | 仓库 **QMT 实现面 440 处**（终端失权限、默认关闭、排链尾） | `configs/intraday.yaml:228-231`；`AGENTS.md:9-25`；`--keyword QMT` → PRD **0** / 实现 **440** | 仓库有PRD无 |
| 20 | PRD-3.1（全表缺） | — | 新增且 PRD 查不到的外部源：知识星球、同花顺快讯、财联社、巨潮 cninfo、百度/乐咕、IDC/Gartner、CME FedWatch、中汽协、CDE、中电联 CECI、WSTS | `configs/indicators.yaml`（55 条 `- id:`、33 种 primary_source）；`src/infrastructure/connectors/intel_sources.py:20,92,94` | 仓库有PRD无 |
| 21 | PRD-3.2 / 92 | 「实时行情 \| 实时（延时≤500ms）」 | 实测腾讯快照中位 **54ms**（达标），但无 SLA 声明 | `docs/DATA_SOURCE_ROUTING.md:110,313` | 已核实一致 |
| 22 | PRD-3.3 / 101-104 | 「核心数据 \| … \| 本地化存储、双人审批、物理隔离」 | `DataClass` 四级已实现；「双人复核」是**声明式标注**；「物理隔离」无实现 | `src/core/tenancy.py:37`；`src/core/policy.py:21,110,240`；`docs/SECURITY_COMPLIANCE.md:120` | 口径冲突 |
| 23 | PRD-4.1 / 110 | 「data_id、source_name、source_url、…、raw_content_hash、…、verified」 | ✅ 12 字段齐；⚠️ 但**情报/研报链路刻意剔除 source_url** | `src/core/schemas.py:116-127`；`intel_sources.py:108-125`（`FORBIDDEN_PUBLIC_KEYS`） | 口径冲突（业务） |
| 24 | PRD-4.3 / 118-123 | 「参考 EDV（Execute-Distill-Verify）范式」「蒸馏」「经验库」「更新Skill Hub」 | **全库 0 处**；只有 A19 自修复 + 数据缺口补取 | `src/orchestration/supervisor.py:52,820`；`src/scheduler/registry.py:30` | PRD有仓库无 |
| 25 | PRD-4.5 / 133-154 | 「Skill 1–20」表（政策解读…投研建议生成） | 实际 **20 个技能目录**，结构与命名都不同；实测无 `scripts/`、无 `assets/` | `skills/`（20 目录）；`src/domain/skills/library.py:3-10` | 口径冲突 |
| 26 | PRD-4.5 / 139,142,152 | 「Altman Z-Score、Beneish M-Score」「风险平价、Black-Litterman」 | `Altman`/`Beneish`/`DCF`/`Black-Litterman`/「美林时钟」/「剪刀差」**全库 0 处** | `src/quant/strategy_cases.py:77`（唯一相关命中） | PRD有仓库无 |
| 27 | PRD-4.6 / 158 | 「四级缓存：L1内存 → L2本地文件 → L3 Redis → L4数据库」 | 实际：Redis 装饰层 + 文件/内存缓存，不存在"四级层叠" | `repository_factory.py:44-46`；`cached_repo.py`；`llm/cache.py:142` | 口径冲突 |
| 28 | PRD-4.6 / 161 | 「综合命中率目标≥85%」 | 无该指标监控（另有 fresh 97.8%，口径不同） | `docs/DATA_INDEX_REQUIREMENTS_AUDIT.md:191` | PRD有仓库无（未度量） |
| 29 | PRD-4.7 / 166 | 「分布式调度框架（Demo可用Celery Beat），禁止硬编码crontab」 | Demo 默认是**进程内 CronScheduler**；Celery 仅生产备选 | `src/api/main.py:571,634-635`；`src/scheduler/service.py:67`；`celery_app.py:1-4` | 口径冲突 |
| 30 | PRD-4.8 / 172 | 「所有Prompt必须经过压缩，上限4000 token」 | 硬上限是 **6000 字符（约 2000 token）** | `src/core/config.py:192-193`（`llm_input_char_hard_cap=6000`） | 口径冲突 |
| 31 | PRD-4.8 / 173 | 「保留最近3轮完整对话 + 更早摘要」 | 未找到该裁剪实现 | grep `最近3轮` / `对话历史` → NOT FOUND | PRD有仓库无 |
| 32 | PRD-4.8 / 176 | 「语义缓存：命中率目标≥50%」 | 语义缓存**已实现**（n-gram 余弦，阈值 0.85），但**无命中率监控** | `src/core/config.py:160`；`llm/cache.py:111-141` | 口径冲突 |
| 33 | PRD-4.8 / 177 | 「记录 input_tokens、output_tokens、cache_hit、cost_estimate」 | 实际 `tokens_in/tokens_out/cached/prompt_hash/response_hash`；无 `cost_estimate` | `llm/audit.py:128-137`；`core/budget.py:152` | 口径冲突 |
| 34 | PRD-4.9 / 181 | 「简单查询0.5-2秒，中等2-8秒，复杂5-10秒」 | 实测端到端：热缓存 **~25s**、冷启动 **~33-38s**；10s 明确不可达 | `docs/PR_CHECKLIST_LATENCY_AND_MODEL_20260928.md:154-163,413`；`docs/LATENCY_OPTIMIZATION.md:13` | 口径冲突 |
| 35 | PRD-4.9 / 184 | 「向量检索优化：使用向量数据库，响应时间目标50ms」 | **无向量数据库**；用 n-gram 余弦内存索引；50ms 未度量 | `llm/cache.py:78-141` | PRD有仓库无 |
| 36 | PRD-4.9 / 185 | 「性能监控：记录P50/P95/P99延迟，慢查询日志」 | P50/P95/P99 **已实现**；"慢查询日志"未确认 | `src/infrastructure/observability/metrics.py:80-82` | 部分一致 |
| 37 | PRD-4.10 / 194 | 「Design Token系统、深色模式」 | 有 CSS 变量令牌；**只有暗色主题**，无"深色模式"开关（无浅色） | `web/src/styles.css:2-13`；grep `dark` → NOT FOUND | 口径冲突 |
| 38 | PRD-4.10 / 192 | 「色盲安全调色板」「三端适配」 | `色盲` 0 处；响应式仅桌面断点（1780/1920/2048/2300 + max-width 900） | `web/src/styles.css:117-120,141` | PRD有仓库无 |
| 39 | PRD-5.2 / 209 | 「单元测试覆盖率≥80%，集成测试≥60%，E2E关键路径100%」 | CI 无覆盖率门禁/报告 | `.github/workflows/ci.yml`；`pyproject.toml:62-65` | PRD有仓库无（未度量） |
| 40 | PRD-5.1 / 201 | 「单文件≤300行，单函数≤50行」 | 大量超限，`supervisor.py` **1815 行**；最大 `intraday/service.py` 209,577 B | `src/orchestration/supervisor.py`；`src/intraday/service.py` | 口径冲突 |
| 41 | PRD-6.1 / 231 | 目录结构列出 `configs/data_sources.yaml` | **不存在** | `configs/` 实际清单 | PRD有仓库无 |
| 42 | PRD-6.1 / 240 | 目录结构列出 `docs/DEPLOYMENT.md` | **不存在** | docs 实际清单 | PRD有仓库无 |
| 43 | PRD-6.1 / 214-243 | 顶层结构（src 5 层 + configs + tests 3 目录 + .trae） | 多出 `web/`、`skills/`、`quant/`、`auction_select/`、`moss_selector/`、`sector_crowding/`、`bin/`、`data/` | 仓库根目录清单；`pyproject.toml:85-89` | 仓库有PRD无 |
| 44 | PRD-6.2 / 249 | 「domain/skills：{domain}-{action}/，包含 SKILL.md、scripts/、references/、assets/」 | 实际两级且**无 scripts/、无 assets/** | `src/domain/skills/library.py:3-10` | 口径冲突 |
| 45 | PRD-6.2 / 250 | 「infrastructure/connectors：{source}.py，如 akshare.py」 | ✅ 路径已对齐；但 `DATA_SOURCE_INTEGRATION.md:18` 仍写旧路径 | `src/infrastructure/connectors/`（32 文件） | 文档漂移 |
| 46 | PRD-7.2 / 275-276 | 「Qwen3.5-4B（Q4量化）」「Qwen3.5-4B 或 Gemma 4 E4B」 | 实际 **qwen2.5:1.5b-instruct-q4_K_M / qwen3:8b-q4_K_M**；**gemma4 被显式排除** | `configs/models.yaml:39-41,114,121,130-135`；`AGENTS.md:7` | 口径冲突 |
| 47 | PRD-7.3 / 286-287 | 「ollama pull qwen3.5:4b」「ollama pull gemma4:e4b」 | 两条 tag 对应的模型在本机/仓库判定中**不存在或不可用** | 同 A#46 | PRD有仓库无 |
| 48 | PRD-7.4 / 302-303 | 「极简方案（全本地）：50-100元/月」「推荐方案（混合）：100-200元/月」 | 仓库口径 **日预算 20 元**（≈600 元/月）；实测单月付费调用 **617.71–618.16 元** | `src/core/budget.py:348-349,370`；`docs/INTERVIEW_LLM_COST_GOVERNANCE.md:36-37,103` | 口径冲突（3–6 倍） |
| 49 | PRD-7.4 / 298 | 「Claude Opus 5 \| $5/百万token \| $25/百万token」 | 无该模型登记、无 provider | `configs/models.yaml`；`src/infrastructure/llm/providers.py`（仅 ollama/deepseek/OpenAI 兼容） | PRD有仓库无 |
| 50 | PRD-8.1 / 312 | 「数据存储：PostgreSQL + SQLite + Redis，全部本地运行」 | 默认 SQLite；PG/Redis 需显式开启 | `.env.example:27,35`；`AGENTS.md:8` | 口径冲突 |
| 51 | PRD-9 / 323 | 「第一阶段…本地环境搭建（Ollama + PostgreSQL + Redis）」 | 里程碑与仓库状态不匹配；`DEVELOPMENT_ROADMAP.md` 的 P0 集合与 PRD 2.2 不一致 | `docs/DEVELOPMENT_ROADMAP.md:7-10,87-89` | 口径冲突 |
| 52 | PRD-10.1 / 338-347 | 内嵌模板：「模型：qwen3.5:4b」「本地运行PostgreSQL + Redis」「按 MCP 协议调用工具」「每个Agent封装为独立FastAPI微服务」 | 现行 `AGENTS.md` 四处全不同 | `AGENTS.md:7-9,30-32` vs `docs/PRD.md` §10.1 | 口径冲突 |
| 53 | PRD-10.2 / 376；PRD-10.3 / 380 | 「使用 /Spec 模式」「在单条消息中调用多次Task工具」 | 现行规则是 `preflight_check.py`、`prd_sync_check.py` + skill 体系 | `AGENTS.md`「需求闭环与影响半径」「需求—PRD 对账」两段 | 口径冲突 |
| 54 | PRD-12 / 396 | 「用户规模 \| 个人使用（Demo）」 | 实际是**多租户平台**（RLS + 角色/清关 + 计费分账 + 对外试点实例） | `src/core/tenancy.py`；`src/infrastructure/security/rls.py:57,75`；`src/domain/platform/llm_cost.py` | 口径冲突 |
| 55 | PRD-12 / 401 | 「数据源 \| AkShare + Tushare + BaoStock + 官方源」 | 与 3.1 同窄（缺 QMT/腾讯/东财/新浪/eltdx/zsxq 等） | 同 A#18/#19/#20 | 口径冲突 |
| 56 | PRD-12 / 402 | 「移动端 \| 响应式Web + 微信小程序（后续）」 | 微信小程序 0 处 | grep `小程序` → NOT FOUND | PRD有仓库无 |

### A2. PRD 内部自相矛盾（3 条）

| # | 位置 | 矛盾内容 | 证据 |
|---|---|---|---|
| S1 | PRD-2.2 | 标题说「18个Agent + 2个基础模块」，表内 A01–A18 共 18 行；§10.1 模板的层级描述（4+3+5+4+1+1=18）与"+2个基础模块"从未定义 | `docs/PRD.md` §2.2 标题与表；§10.1 |
| S2 | PRD-2.1 vs PRD-10.1 | 2.1 写"Agent 各自独立任务处理"，10.1 写"每个Agent封装为独立FastAPI微服务" | `docs/PRD.md` §2.1、§10.1 |
| S3 | PRD-2.1 vs PRD-8.1 | 2.1 表列"Neo4j（可选）"，8.1 又要求"PostgreSQL + SQLite + Redis 全部本地运行"，取舍未说明 | `docs/PRD.md` §2.1、§8.1 |

### A3. 仓库侧文档 ↔ 代码的漂移（7 条，对账时必须一起处理）

| # | 漂移 | 证据 | 影响 |
|---|---|---|---|
| D1 | `docs/ARCHITECTURE.md` **整段照抄 PRD 过时技术栈**（PG+SQLite+Redis+Neo4j / Keycloak+OPA / LangChain+FastAPI） | `docs/ARCHITECTURE.md:11-13` | 过时口径的持续传播源 |
| D2 | `docs/DATA_SOURCE_ROUTING.md` 写「`intraday_sources` 里 qmt 仍写在最前」，与配置**正好相反** | `docs/DATA_SOURCE_ROUTING.md:27` vs `configs/intraday.yaml:228` | ★ 文档与配置直接冲突，下一位读者会改错顺序 |
| D3 | `README.md` 架构图把 QMT 画在链首 | `README.md`（数据连接器路由节）vs `AGENTS.md:12` | 同上 |
| D4 | `README.md` 写「19 个 Agent」，`AGENTS.md` 写「18个专业Agent」 | `README.md` vs `AGENTS.md:30` vs `src/api/runtime.py:285-303` | 数字口径不一致 |
| D5 | `docs/DATA_SOURCE_INTEGRATION.md` 整篇是 PRD 3.1 的复制，含未实现的慧博/生意社/NOAA 与旧路径 | `docs/DATA_SOURCE_INTEGRATION.md:5-14,18` | 让人误以为 4 个源已接入 |
| D6 | `README.md` 称「16 个路由模块」，实际 `src/api/routes/` 有 23 个 | `src/api/routes/` | 数字口径 |
| D7 | `docs/CACHE_DESIGN.md` 亦为 PRD 4.6 复制（四级缓存 / 85% / 键格式），无实现对照 | `docs/CACHE_DESIGN.md:5-45` | 需求与实现脱节 |

---

## B. 已核实一致（防重复劳动 —— 下一轮不要重查这些）

- **ARCH-1** LangGraph StateGraph 编排已实现 —— `src/orchestration/supervisor.py:19,1701`
- **ARCH-2** A01–A18 全部落地，分层与 PRD 2.2 表一致 —— `src/api/runtime.py:285-302`、`docs/AGENT_REGISTRY.md:54`
- **ARCH-3** Agent 间标准 JSON 消息通信已实现 —— `src/core/message.py`、`src/orchestration/message_bus.py:42-50`
- **ARCH-4** A2A 互操作层已实现（`ask_agent`） —— `src/orchestration/message_bus.py:106-145`
- **ARCH-5** Agent 统一入口齐备（`execute`/`get_capabilities`/`health_check`） —— `src/core/base_agent.py:22,26,30`
- **ARCH-6** 编排层依赖 LangGraph（依赖已声明） —— `pyproject.toml:18`
- **DS-1** 国家统计局 / 央行数据确有取数（经 AkShare 封装） —— `akshare_connector.py:453`、`real_industry_connector.py:221`
- **DS-2** FRED 已接入 —— `src/orchestration/supervisor.py:480`、`src/scheduler/jobs.py:228`
- **DS-3** AkShare / Tushare / BaoStock 三源连接器均在 —— `src/infrastructure/connectors/`
- **DS-4** 实时行情延迟 ≤500ms 达标（实测快照中位 54ms） —— `docs/DATA_SOURCE_ROUTING.md:110`
- **SEC-1** 四级数据分级实现为 `DataClass` 四级 —— `src/core/tenancy.py:37`、`docs/SECURITY_COMPLIANCE.md:21-28`
- **SEC-2** RBAC + ABAC + 默认拒绝已实现 —— `src/core/policy.py:7,78,198-201`
- **SEC-3** 多租户 RLS（应用层 + PG 原生 DDL）已实现 —— `src/infrastructure/security/rls.py:57,75,101`
- **SEC-4** 审计日志独立存储 + 哈希链防篡改 —— `src/infrastructure/repositories/audit_chain.py:3,23,94`
- **SEC-5** 留存期达成（实际 ≥5 年，严于 PRD 的 ≥3 年） —— `docs/SECURITY_COMPLIANCE.md:91`
- **SEC-6** 硬编码敏感信息扫描门禁在 CI 强制 —— `.github/workflows/ci.yml`、`docs/SECURITY_COMPLIANCE.md:71`
- **TRACE-1** 溯源 12 字段齐备 —— `src/core/schemas.py:116-127`
- **TRACE-2** 推理路径 Trace 已实现 —— `src/core/schemas.py:55-63`
- **TRACE-3** LLM 推理记录 `prompt_hash` 与 token 数 —— `src/infrastructure/llm/audit.py:128-131`
- **TOKEN-1** 模型路由（本地/云端分层） —— `configs/models.yaml:58-109`
- **TOKEN-2** 工具 Schema 按需加载（PTD 三级披露） —— `src/domain/skills/library.py:7-10`
- **PERF-1** P50/P95/P99 延迟监控 —— `src/infrastructure/observability/metrics.py:80-82`
- **PERF-2** 无依赖 Agent 并行执行（gather fan-out，墙钟 = max） —— `docs/PR_CHECKLIST_LATENCY_AND_MODEL_20260928.md:30,188`
- **PERF-3** 渐进式响应已实现 —— `src/orchestration/supervisor.py:760`；`docs/LATENCY_OPTIMIZATION.md:16`
- **DEV-1** Python 3.10+ 与类型注解 —— `pyproject.toml:6`
- **DEV-2** 无裸 `except:`（0 处） —— `Select-String src\**\*.py -Pattern "except\s*:"` 计数 0
- **DEV-3** 免责声明存在且有测试 —— `docs/SECURITY_COMPLIANCE.md:73`
- **MIL-1** 4 阶段 / 6-8 周里程碑与 `docs/DEVELOPMENT_ROADMAP.md:5-10` 一致（仅 P0 集合不同，见 A#51）

---

## C. 待人工确认（15 条 —— 机器判不了，不许替人拍板）

| # | 要谁确认什么 | 关联 A 表 |
|---|---|---|
| C1 | **用户**：PRD 是"历史基线"还是"活文档"？ ✅ **已答（2026-09-28，本会话）**：PRD 作为需求基线，缺的补进去、冲突的就地改 —— 见台账 CHG-0000 | 全部 |
| C2 | **用户/业务**：情报与研报链路"出接口不带 `source_url`"（保密）与 PRD 4.1「一键溯源」冲突 —— 哪个优先？边界写进哪一节？ | A#23 |
| C3 | **用户**：PRD 2.2 标题的「+ 2个基础模块」指哪两个？ | A#9、S1 |
| C4 | **项目负责人**：A05/A06 优先级 P0 还是 P1？`agents.yaml`(P0) / `AGENT_REGISTRY.md`(P1) / PRD(P1) 三方需定权威值 | A#5 |
| C5 | **项目负责人**：A12 取「合规爆雷Agent」还是「合规风险Agent」？P1 还是 P2？ | A#6 |
| C6 | **用户**：性能指标改 PRD 还是改实现？（实测 25–38s vs PRD 5–10s；10s 已确认不可达） | A#34 |
| C7 | **用户**：成本口径 100–200 元/月 vs 日预算 20 元 + 实测 618 元/月 —— PRD 写低了，还是预算配置该收紧？ | A#48 |
| C8 | **项目负责人**：「单文件≤300行 / 单函数≤50行」是否仍生效？（`supervisor.py` 1815 行）若改为"仅新文件适用"，需给可执行判据 | A#40 |
| C9 | **项目负责人**：测试覆盖率门禁（80/60/100%）是否落地？CI 现在完全不测覆盖率 | A#39 |
| C10 | **项目负责人**：Celery Beat 是否还保留为生产路径？（当前 Demo 全走进程内 CronScheduler） | A#29 |
| C11 | **用户**：慧博 / 生意社 / NOAA 三源是"计划未实现"还是"已放弃"？ | A#14、A#15、A#16 |
| C12 | **项目负责人**：`docs/ARCHITECTURE.md`、`DATA_SOURCE_INTEGRATION.md`、`CACHE_DESIGN.md` 三份"PRD 副本"是否一并作废？ | D1、D5、D7 |
| C13 | **项目负责人**：`DATA_SOURCE_ROUTING.md:27` 与 `configs/intraday.yaml:228` 的 QMT 顺序冲突，以配置为准修文档，还是还需复核 `health.rank` 先验逻辑？ | D2、D3 |
| C14 | **用户**：EDV 自迭代（蒸馏/经验库/Skill Hub）明确放弃还是仍要建？ | A#24 |
| C15 | **用户**：微信小程序是否仍要做？（PRD 标注"后续"，仓库 0 处） | A#56 |
| C16 | **项目负责人**：本护栏的脚本 `.trae/skills/requirement-prd-sync/SKILL.md`、`scripts/prd_sync_check.py` 都在 `.gitignore` 排除范围内（`/.trae/`、`/scripts/*`），**是否加入发布白名单**？现行处置：测试加 `pytest.skip(..., allow_module_level=True)` 守卫做"知名降级" | 见 `tests/unit/test_shipped_deps.py` |

---

## D. 章节 ID 体系（台账引用口径）

**章节级一律用 `PRD-n.m`**，由 `--list-sections` 从 `docs/PRD.md` 现读现算
（H2 → `PRD-n`，H3 → `PRD-n.m`，**跳过围栏代码块**，避免 §10.1 内嵌模板产生幻影章节）。
共 **43 节（12 个 H2 / 31 个 H3）**。

条目的语义 ID 前缀（只作可读性补充，**不作台账主键**）：
`OVW` 概述 · `ARCH` 架构 · `AGT` Agent 清单 · `PROTO` 协议 · `DS` 数据源 · `FRQ` 频率 ·
`CLASS` 分级 · `TRACE` 溯源 · `AUDIT` 推理路径 · `EVO` 自迭代 · `TEN` 多租户 ·
`SKILL` 技能 · `CACHE` 缓存 · `SCHED` 调度 · `TOK` Token · `PERF` 性能 · `FE` 前端 ·
`DEV` 开发规则 · `DIR` 目录 · `MODEL` 模型 · `COST` 成本 · `DEPLOY` 部署 · `MILE` 里程碑 ·
`TRAE` Trae 指导 · `INTV` 面试 · `CONF` 待确认信息

---

## E. 本次已落到 PRD 的 4 条（2026-09-28，CHG-0001 ~ CHG-0004）

| CHG | 对应 A 表 | 落点（章节 ID） | 落法 |
|---|---|---|---|
| CHG-0001 | A#19 | PRD-3.1 | **补写**：PRD 原文无 QMT → 在数据源清单下补 QMT 降位/关闭/恢复条件 |
| CHG-0002 | A#1、A#50、S3 | PRD-2.1、PRD-8.1 | **改写**：原文保留不动，就地加「⚠️ 已废止（CHG-0002）」注，给出 SQLite 零依赖现行口径 |
| CHG-0003 | A#46、A#47 | PRD-7.2、PRD-7.3 | **改写**：加「⚠️ 已废止（CHG-0003）」，给出 `configs/models.yaml` 现行模型名与被排除的两个 tag |
| CHG-0004 | A#52、S2 | PRD-10.1 | **标注废止**：整节内嵌模板标为历史留档，指向仓库根 `AGENTS.md` |

> **其余 52 条 A 项 + 3 条 S + 7 条 D** 目前是台账里的 `待办 / 待确认`
> —— 它们要么等 C 表的人判断，要么等后续轮次逐条落 PRD。
> **"等"不等于"可以忘"**：`--ledger` 会一直把它们算作未闭环，直到处置写成
> `补写PRD / 改写PRD` 且 `PRD 已更新=是`。

---

免责声明：本文是对账记录，不构成任何投资建议。PRD 原文引用一律原样截断，未改写语义。
