# Moss-FinAgent-Research 需求开发设计文档

> 项目代号：Moss-FinAgent-Research
> 原始需求文档存档（用户提供，2026-09-12）

## 一、项目概述

Moss-FinAgent-Research 是一个基于多Agent协作的AI辅助投研分析系统，面向二级市场投资决策场景，覆盖产业政策研究、产业链研究、个股研究三大核心模块，支持宏观、中观、微观三层分析，具备数据溯源、推理路径可回溯、多租户隔离、自迭代学习等能力。

系统采用 Supervisor分层调度 + 专业Agent协同 架构，参考 TradingAgents 的虚拟投研团队范式，将复杂投研决策拆解为可编排、可追踪、可复盘的Agent协作流程。整体架构采用 LangGraph StateGraph 搭建 Supervisor 多智能体协同架构，Supervisor 作为总调度，通过 LLM 动态决策任务执行顺序，Agent 间通过共享状态自动传递上下文。

### 核心目标

- 智能化：多Agent协作完成从数据采集到投研建议的全链路分析。
- 可溯源：每个数据点携带来源标签，每个结论记录推理路径。
- 可审计：所有操作日志独立存储、防篡改，满足金融监管要求。
- 可扩展：模块化设计，支持新增Agent、Skill、数据源，支持水平扩展。
- 高性能：满足高并发查询，响应时间分级控制，Token消耗优化。
- 安全合规：多租户数据隔离，四级数据分级，RBAC+ABAC权限模型。
- Demo可运行：能在本地RTX 4060 8GB + 32GB内存环境流畅运行，月成本控制在100-200元。

## 二、系统架构设计

### 2.1 整体架构

Demo阶段采用"本地为主、云端为辅"的混合架构：

| 层级 | 组件 | 技术选型（Demo版） | 职责 |
|------|------|------|------|
| 编排层 | Supervisor Agent | LangGraph | 任务分解、Agent调度、结果聚合、冲突仲裁 |
| Agent层 | 核心Agent（6-8个优先） | LangChain + FastAPI | 各自独立任务处理 |
| 数据层 | 统一数据总线 | PostgreSQL + SQLite + Redis + Neo4j（可选） | 数据存储、检索、事件传递 |
| 模型层 | 混合模型网关 | Ollama + 云端API | 本地处理基础任务，云端处理核心推理 |
| 认证层 | 身份认证与权限 | Keycloak（Demo可简化） + OPA | SSO、MFA、RBAC+ABAC |
| 审计层 | 溯源与审计 | 本地文件 + SQLite + 哈希链 | 数据溯源、推理路径记录 |
| 应用层 | 前端与API | React + FastAPI | 用户交互、报告展示 |

> ⚠️ **已废止（CHG-0002，2026-09-28 · 改写）**：本表「数据层 = PostgreSQL + SQLite +
> Redis + Neo4j（可选）」与「认证层 = Keycloak（Demo可简化） + OPA」两行**不再代表现行实现**。
> - 存储：默认 **SQLite 零依赖**；PostgreSQL / Redis 为**可选**
>   （`DATA_BACKEND=postgres` / `REDIS_CACHE_ENABLED=true`）；**Neo4j 全仓库 0 处**
> - 认证：自研 HMAC 载荷 + RBAC×ABAC×隔离墙（`src/core/tenancy.py`、`src/core/policy.py`）；
>   **Keycloak / OPA 未接入**，降为演进目标
> - Agent 层：**不是** FastAPI 微服务，而是进程内模块（见 `AGENTS.md`「架构规范」最后一条）
>
> 依据：`AGENTS.md`「环境配置」「架构规范」· 全量对账见
> `docs/PRD_ALIGNMENT_AUDIT_20260928.md`（A#1、A#2、A#4）

### 2.2 Agent全景清单（18个Agent + 2个基础模块）

Demo阶段优先实现P0级Agent，P1/P2逐步扩展：

| 编号 | 名称 | 层级 | 优先级 | 核心职责 | 推荐模型（Demo版） |
|------|------|------|--------|----------|------|
| A01 | 数据采集Agent | 数据层 | P0 | 从各数据源爬取原始数据 | 本地轻量模型 / 无需LLM |
| A02 | 数据清洗Agent | 数据层 | P0 | 数据标准化、去重、格式转换 | 本地轻量模型 / 无需LLM |
| A03 | 数据校验Agent | 数据层 | P1 | 数据准确性校验、异常值检测 | 本地轻量模型 |
| A04 | 数据存储Agent | 数据层 | P1 | 数据入库、版本管理、血缘追踪 | 本地轻量模型 |
| A05 | 信息去伪Agent | 信息层 | P1 | 信息真实性验证、来源可信度评分 | 本地模型（Qwen3.5-4B） |
| A06 | 信息提取Agent | 信息层 | P1 | 从非结构化文本中提取结构化信息 | 本地模型（Qwen3.5-4B） |
| A07 | 舆情分析Agent | 信息层 | P2 | 市场情绪量化、情绪周期定位 | 云端API（DeepSeek-V4-Flash） |
| A08 | 宏观分析Agent | 分析层 | P0 | 宏观经济周期定位、流动性分析 | 云端API（DeepSeek-V4-Flash） |
| A09 | 中观分析Agent | 分析层 | P0 | 产业链分析、行业周期定位 | 云端API（DeepSeek-V4-Flash） |
| A10 | 微观分析Agent | 分析层 | P0 | 个股深度研究、估值计算 | 云端API（DeepSeek-V4-Flash） |
| A11 | 财务风险Agent | 分析层 | P1 | 财务排雷、造假预警 | 云端API（DeepSeek-V4-Flash） |
| A12 | 合规爆雷Agent | 分析层 | P2 | 合规风险、爆雷风险预警 | 云端API（DeepSeek-V4-Flash） |
| A13 | 科技行业Agent | 行业层 | P2 | 科技行业深度分析 | 微调金融模型 / 云端API |
| A14 | 消费行业Agent | 行业层 | P2 | 消费行业深度分析 | 微调金融模型 / 云端API |
| A15 | 周期行业Agent | 行业层 | P2 | 周期行业深度分析 | 微调金融模型 / 云端API |
| A16 | 医药行业Agent | 行业层 | P2 | 医药行业深度分析 | 微调金融模型 / 云端API |
| A17 | 投研建议Agent | 决策层 | P0 | 综合所有分析，生成投研建议 | 云端API（DeepSeek-V4-Pro / Claude Opus 5） |
| A18 | 逻辑审计Agent | 审计层 | P1 | 推理路径记录、回测验证 | 本地轻量模型 |

### 2.3 通信协议

- MCP（Model Context Protocol）：作为工具调用层，统一模型与数据源/工具/服务的交互接口。
- A2A（Agent-to-Agent）：作为Agent互操作层，实现不同框架Agent之间的通信协作。
- 内部消息格式：JSON，包含 message_id、sender、receiver、message_type、timestamp、payload、metadata，metadata 中必须包含 audit_id 和 data_sources。

## 三、数据源与数据分层

### 3.1 数据源清单

| 数据类型 | 数据源 | 获取方式 | 更新频率 | 优先级 |
|----------|--------|----------|----------|--------|
| 宏观经济 | 国家统计局 | data.stats.gov.cn API/网页 | 随官方发布 | P0 |
| CPI/PPI | 国家统计局数据发布库 | API | 次月10日左右 | P0 |
| 央行数据 | 央行 | www.pbc.gov.cn 网页 | 月度/季度 | P0 |
| 全球流动性 | FRED | fred.stlouisfed.org API | 实时 | P1 |
| 厄尔尼诺 | NOAA | psl.noaa.gov 文件下载 | 月度/周度 | P1 |
| A股行情 | AkShare / Tushare Pro / BaoStock | API | 实时/日频 | P0 |
| 券商研报 | 慧博投研 | www.hibor.com.cn 订阅 | 实时 | P1 |
| 大宗商品 | 生意社 | www.100ppi.com 网页 | 日频/周频 | P2 |

> **补充（2026-09-28，CHG-0001 · 原文缺失，补写）**：上表**没有 QMT**，而仓库
> 2026-09-23 起已把 QMT 纳入行情链，并**排在所有链路的最后一位、默认关闭**
> （本机 QMT 终端失去行情权限、`127.0.0.1:58610` 拒连，短期无法恢复）。
> - 现行日线链：AkShare → 腾讯 → Tushare → baostock → 本地CSV → **[QMT 开关]（最末）**
> - 现行分钟/分时/快照链：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**
> - 恢复条件：`QMT_ENABLED=1` + `configs/intraday.yaml` 的 `data.qmt_enabled: true`
> - ⚠️ 顺序与开关表达同一个意图，两处都要改对（`health.rank` 样本 <3 次时照抄配置顺序）
>
> 依据：`AGENTS.md`「环境配置」· `configs/intraday.yaml:228-231`

### 3.2 数据更新频率分层

| 数据层级 | 数据类型 | 更新频率 |
|----------|----------|----------|
| 宏观数据 | GDP、CPI、PPI、PMI | 随官方发布日即时更新 |
| 宏观数据 | 社融、M2 | 每月10-15日 |
| 中观数据 | 行业库存、产能利用率 | 月度（次月27日左右） |
| 中观数据 | 产业链价格 | 日频/周频 |
| 微观数据 | 实时行情 | 实时（延时≤500ms） |
| 微观数据 | 财务数据 | T+1日 |
| 微观数据 | 估值指标 | 每日盘后 |
| 另类数据 | 厄尔尼诺指数 | 月度/周度 |

### 3.3 数据分级（四级制）

| 级别 | 定义 | 投研系统示例 | 访问要求 |
|------|------|--------------|----------|
| 核心数据 | 泄露对国家安全/经济运行造成特别严重危害 | 未公开的重大政策预判、核心策略逻辑 | 本地化存储、双人审批、物理隔离 |
| 重要数据 | 泄露对经济运行/公共利益造成严重危害 | 完整产业链图谱、内部研报、因子参数 | 本地化存储、角色管控、加密传输 |
| 敏感一般数据 | 泄露对组织权益造成一般危害 | 个股分析报告、财务数据、舆情指标 | 脱敏后可共享、权限管控 |
| 常规一般数据 | 泄露危害较低 | 公开行情数据、已发布研报摘要 | 可有序流通、基础访问控制 |

## 四、关键技术设计

### 4.1 数据溯源体系

每个数据点必须携带溯源元数据：data_id、source_name、source_url、source_type、publish_time、fetch_time、fetch_method、raw_content_hash、processed_by、process_time、confidence、verified。

### 4.2 推理路径Trace

每次Agent分析输出必须附带完整的推理路径记录，包括 trace_id、agent_id、model_info、task、reasoning_steps（每步包含 step_type、description、data_refs、timestamp、duration_ms）、conclusion、audit（哈希链）。

### 4.3 自迭代能力

参考 EDV（Execute-Distill-Verify）范式：

- 执行：Agent执行任务，收集成功/失败轨迹和用户反馈。
- 蒸馏：将原始轨迹提炼为结构化经验条目，存入经验库。
- 验证：通过多级安全闸门（数据源验证、多Agent审议、历史回测）验证经验有效性。
- 演化：验证通过的经验用于生成或优化Skill，更新Skill Hub。

### 4.4 多租户隔离与权限

- 隔离模型：共享表 + 行级安全策略（RLS）+ 独立Schema命名空间。
- 权限模型：RBAC + ABAC 混合，支持实时上下文感知授权。
- 审计日志：独立存储、防篡改、保留≥3年。

### 4.5 分析引擎设计（核心Skill清单）

| 编号 | 名称 | 核心功能 | 关键公式/模型 |
|------|------|----------|------|
| Skill 1 | 政策解读 | 政策分类、传导路径、强度评分 | 政策力度×传导路径 |
| Skill 2 | 产业链分析 | 图谱构建、瓶颈识别、产能周期 | 生命周期×供需格局 |
| Skill 3 | 个股深度研究 | 股性分析、估值测算、催化剂 | DCF/PE/PEG |
| Skill 4 | 估值计算 | DCF、相对估值、情景分析 | 合理PE=(1-g/ROIC)/(r-g) |
| Skill 5 | 财务排雷 | 现金流、商誉、关联交易检测 | Altman Z-Score、Beneish M-Score |
| Skill 6 | 周期定位 | 宏观/市场/情绪周期判断 | 美林时钟、库存周期 |
| Skill 7 | 催化剂跟踪 | 事件驱动、弹性测算 | 事件影响×时间窗口 |
| Skill 8 | 组合管理 | 仓位分配、风控规则 | 风险平价、Black-Litterman |
| Skill 9 | 天气与自然周期 | 厄尔尼诺/拉尼娜影响 | NOAA指数×品种映射 |
| Skill 10 | 库存周期定位 | 四阶段分类、强势行业 | 收入×库存二维矩阵 |
| Skill 11 | 多周期共振 | 康波/朱格拉/库存嵌套 | 多周期相位对齐 |
| Skill 12 | CPI/PPI剪刀差 | 四象限分类、利润分配 | PPI-CPI剪刀差×方向 |
| Skill 13 | PPI上行轮动 | 历史复盘、阶段配置 | 四阶段模型 |
| Skill 14 | 通胀拐点预警 | 五大信号体系 | PPI环比见顶、剪刀差高点 |
| Skill 15 | 基金对比分析 | 多基金对比、组合优化 | 相关性矩阵、业绩归因 |
| Skill 16 | 产业拐点判断 | PPI+库存+产能三维 | 领先滞后模型、IC/IR |
| Skill 17 | 个股价值评估 | 多方法交叉验证 | DCF+相对估值+PEG |
| Skill 18 | 财务风险预警 | Z-Score+M-Score | Altman+Beneish |
| Skill 19 | 逻辑审计 | 推理路径记录、回测 | IC/IR检验、分层回测 |
| Skill 20 | 投研建议生成 | 信号→验证→建议 | 映射规则表、胜率验证 |

### 4.6 数据缓存规范

- 四级缓存：L1内存 → L2本地文件 → L3 Redis → L4数据库。
- 读缓存优先，写缓存失效。
- 缓存键格式：`{tenant_id}:{data_type}:{indicator}:{date_range}:{params_hash}`。
- 综合命中率目标≥85%。
- 缓存一致性延迟：实时行情≤1秒，宏观≤5分钟，财务≤1小时。

### 4.7 定时任务调度规范

- 分布式调度框架（Demo可用Celery Beat），禁止硬编码crontab。
- 任务幂等性、指数退避重试、超时控制、分片执行。
- 监控与告警：连续失败3次自动告警并暂停。

### 4.8 Token节约优化规则

- 所有Prompt必须经过压缩，上限4000 token（Demo版，与ai-dev-rules对齐）。
- 动态上下文裁剪：保留最近3轮完整对话 + 更早摘要（与ai-dev-rules对齐）。
- 工具Schema按需加载。
- 模型路由：简单任务走本地模型，复杂任务走云端API。
- 语义缓存：命中率目标≥50%。
- Token使用监控：记录 input_tokens、output_tokens、cache_hit、cost_estimate。

### 4.9 系统响应性能优化规则

- 响应时间分级：简单查询0.5-2秒，中等2-8秒，复杂5-10秒。
- Agent并行执行：无依赖Agent必须并行，通过DAG编排。
- 渐进式响应：先返回快速结论，再推送深度分析。
- 向量检索优化：使用向量数据库，响应时间目标50ms。
- 性能监控：记录P50/P95/P99延迟，慢查询日志。

### 4.10 前端设计与体验规范

- 设计原则：信息密度优先、渐进式披露、多模态交互。
- 组件化：原子设计方法论，独立Hook管理状态。
- 响应式：三端适配（桌面/平板/移动），MVP阶段采用响应式Web。
- 数据可视化：表格+图表双视图，色盲安全调色板。
- 交互体验：一键溯源、自定义报告模板、进度指示器、明确错误提示。
- 美观性：Design Token系统、深色模式、等宽字体、动效规范。

## 五、开发规则与规范

### 5.1 AI开发规则（ai-dev-rules）

- 架构红线：Agent间必须使用标准JSON消息；数据访问必须通过MCP Server；禁止硬编码敏感信息；所有LLM输出附带置信度和免责声明；所有调用记录Trace。
- 代码生成规则：单文件≤300行，单函数≤50行；命名规范；错误处理必须包裹try-except；LLM调用通过统一网关。
- 禁用模式：Agent直接访问数据库、硬编码URL、同步阻塞调用LLM、全局可变状态、单文件超行、循环导入、裸except、日志输出敏感信息、手动拼接SQL。

### 5.2 开发规范（dev-standards）

- 项目结构：src layout，分层 core → domain → infrastructure → api，可部署应用在 apps/，可复用库在 libs/。
- 模块拆分：每个Agent目录拆分为 agent.py、logic.py、models.py、prompts.py；每个Skill目录包含 SKILL.md、scripts/、references/、assets/。
- 接口设计：所有Agent继承 BaseAgent，实现 execute()、get_capabilities()、health_check()。
- 测试规范：单元测试覆盖率≥80%，集成测试≥60%，E2E关键路径100%；测试文件命名 test_{module}_{scenario}.py。
- 重构指南：先写测试，小步修改，保持接口不变，更新文档。

## 六、目录结构与命名规范

### 6.1 顶层目录结构

```
Moss-finagent-research/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── .env.example
├── docker-compose.yml
├── src/
│   ├── core/
│   ├── domain/
│   ├── infrastructure/
│   ├── orchestration/
│   └── api/
├── configs/
│   ├── agents.yaml
│   ├── data_sources.yaml
│   └── models.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API_REFERENCE.md
│   └── DEPLOYMENT.md
└── .trae/
    └── skills/
```

### 6.2 各层目录命名规范

- core层：base_agent.py、message.py、exceptions.py、config.py、state.py、schemas.py、models.py
- domain/agents：{domain}_{role}_agent.py，如 macro_agent.py
- domain/skills：{domain}-{action}/，包含 SKILL.md、scripts/、references/、assets/
- infrastructure/connectors：{source}.py，如 akshare.py
- infrastructure/repositories：{domain}_repo.py，如 macro_repo.py
- api/routes：{resource}.py，如 research.py
- configs：{purpose}.yaml，如 agents.yaml
- tests：test_{module}_{scenario}.py

### 6.3 依赖规则

- 单向依赖：core ← domain ← infrastructure ← api，orchestration 依赖 domain 和 infrastructure。
- 禁止反向依赖。层间通信通过抽象接口或消息总线。

## 七、模型选型与部署（Demo版）

### 7.1 硬件配置

| 配置项 | 配置 | Demo要求 | 结论 |
|--------|------|----------|------|
| GPU | RTX 4060 8GB | 8GB VRAM | 满足 |
| 内存 | 32GB | 16GB+ | 满足 |
| 磁盘 | — | 50GB SSD | 满足 |

### 7.2 模型分层策略

| 任务类型 | 运行位置 | 推荐模型 | 理由 |
|----------|----------|----------|------|
| 数据清洗、格式转换 | 本地 | Qwen3.5-4B（Q4量化） | 零成本，满足基本任务 |
| 信息提取、简单分析 | 本地 | Qwen3.5-4B 或 Gemma 4 E4B | 8GB显存可流畅运行 |
| 宏观/微观核心分析 | 云端API | DeepSeek-V4-Flash | 成本低，质量高 |
| 投研建议生成 | 云端API | DeepSeek-V4-Pro 或 Claude Opus 5 | 最强推理能力 |

### 7.3 本地模型部署方案

使用 Ollama 作为本地推理框架：

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:4b
ollama pull gemma4:e4b
OLLAMA_HOST=0.0.0.0 ollama serve
```

> ⚠️ **已废止（CHG-0003，2026-09-28 · 改写）**：本节（含 §7.2 的两行模型）里的
> `Qwen3.5-4B` / `Gemma 4 E4B` 与实际不符。**现行本地模型以 `configs/models.yaml` 为准**：
> - `qwen2.5:1.5b-instruct-q4_K_M`（`local_light`，0.92 GB）
> - `qwen3:8b-q4_K_M`（`local_medium`，5.20 GB，实测 44 t/s）
> - `deepseek-r1:7b`（已拉取 4.36 GB，**默认不接线**）
>
> `qwen3.5:4b` 与 `gemma4:e4b` 在 8 GB 显存下**已被显式排除**
> （`configs/models.yaml:32-33` 实测：qwen3:8b + qwen3.5:4b = 8.4 GB 超限；
> gemma4:e4b 8.95 GB 溢出到 CPU，慢一个数量级）。上面的
> `ollama pull qwen3.5:4b` / `ollama pull gemma4:e4b` 两条命令同样失效。
>
> 全量对账见 `docs/PRD_ALIGNMENT_AUDIT_20260928.md`（A#46、A#47）。

### 7.4 云端API成本估算

| 模型 | 输入价格 | 输出价格 | 用途 |
|------|----------|----------|------|
| DeepSeek-V4-Flash（空闲） | 1元/百万token | 4元/百万token | 日常分析 |
| DeepSeek-V4-Flash（高峰） | 2元/百万token | 8元/百万token | 日常分析 |
| DeepSeek-V4-Pro（空闲） | 4.5元/百万token | 13.5元/百万token | 核心推理 |
| Claude Opus 5 | $5/百万token | $25/百万token | 可选 |

月度成本估算：

- 极简方案（全本地）：50-100元/月
- 推荐方案（混合）：100-200元/月
- 完整方案（频繁云端）：300-500元/月

## 八、部署与团队配置（Demo版）

### 8.1 部署环境

- 本地部署：所有服务部署在本地台式机。
- 混合调用：核心推理调用云端API，基础任务使用本地模型。
- 数据存储：PostgreSQL + SQLite + Redis，全部本地运行。

> ⚠️ **已废止（CHG-0002，2026-09-28 · 改写）**：同上 —— 现行默认是 **SQLite 零依赖**，
> PostgreSQL / Redis 需显式开启，**不要求"全部本地运行"**。依据：`AGENTS.md:8`。
> 全量对账见 `docs/PRD_ALIGNMENT_AUDIT_20260928.md`（A#50）。

### 8.2 团队配置

- 个人开发：你 + Trae SOLO Agent。
- Trae承担：代码生成、测试、文档撰写。

## 九、开发路线图与里程碑（Demo版）

| 阶段 | 时间 | 交付物 |
|------|------|--------|
| 第一阶段 | 第1-2周 | 本地环境搭建（Ollama + PostgreSQL + Redis）、数据层Agent（A01-A04） |
| 第二阶段 | 第3-4周 | 核心分析Agent（A08/A09/A10/A11）、前端MVP |
| 第三阶段 | 第5-6周 | Supervisor编排引擎、审计层（A18）、端到端联调、演示脚本 |
| 第四阶段 | 第7-8周 | 面试演示准备、项目文档整理、回测验证 |

## 十、Trae开发指导

### 10.1 AGENTS.md配置

> ⚠️ **已废止（CHG-0004，2026-09-28 · 标注废止）**：下面这段内嵌模板是 **2026-09-12 的历史版本**，
> 与现行项目规则已全面脱节 —— 模型（`qwen3.5:4b`）、存储（PostgreSQL + Redis 必须本地运行）、
> 工具协议（MCP）、架构（"每个Agent封装为独立FastAPI微服务"）四处都与现实相反。
> **现行项目规则以仓库根的 `AGENTS.md` 为准**（它已增加了前端/性能/告警/首轮编码/
> 编辑安全/护栏保真/需求闭环/需求—PRD 对账/交付完整性等九段硬约束）。
> 本模板仅作历史留档，**不得据此实现**。依据：`AGENTS.md:7-9,30-32`；
> 对账见 `docs/PRD_ALIGNMENT_AUDIT_20260928.md`（A#52、S2）。
>
> **补写（CHG-0045，2026-09-28 · 新增）**：开发/测试的强制门禁已固化为 skill
> `dev-test-guardrails` —— G0 需求对齐 → G1 依赖就绪 → G2 架构 → G3 编码 →
> G4 LLM 调用（成本路由 / 模型选型 / 防幻觉 8 条）→ G5 测试 → G6 交付，
> **未过门禁不得进入下一阶段**。它**双宿主同源**：
> Trae 侧 `.trae/skills/dev-test-guardrails/SKILL.md`、
> DeepSeek Harness 侧 `~/.agents/skills/dev-test-guardrails/SKILL.md`
> （两份 SHA-256 必须相等，改一份必须同步另一份）。
> 所属"现行规则以 `AGENTS.md` 为准"的口径不变（见 CHG-0004），**故不在本 PRD 复制其正文**；
> 上面"九段硬约束"计数也不因此改动（本次只增 skill 文件与 `AGENTS.md` 的 Skills引用登记，
> 未新增硬约束章节）。发布范围同 `.trae/`（不随仓库发布），见
> `docs/REQUIREMENT_CHANGELOG.md` §6。

> 🗄 **原文已折叠到 §13.3.1**（CHG-0004）：上面的 2026-09-12 版 AGENTS.md 模板已**整段搬进
> 归档区**（原文逐字保留，仅供追溯）。本节自 2026-09-28 起不再承载模板正文。

### 10.2 使用Spec模式

对于系统级任务，使用 /Spec 模式生成 spec.md、tasks.md、checklist.md。

### 10.3 使用Task工具并行开发

在单条消息中调用多次Task工具，并行开发多个Agent模块。

## 十一、面试演示策略

面试时，演示重点不是"系统有多完整"，而是"你的技术决策有多合理"：

1. 成本意识：主动说明"为什么选择混合方案——本地跑基础任务零成本，云端API按需付费，月成本控制在100-200元"。
2. 架构能力：展示你如何用LangGraph编排多Agent，用Ollama实现本地推理，用MCP标准化工具调用。
3. 落地能力：演示完整的"数据采集→分析→投研建议"链路，附带数据溯源和推理路径。
4. 技术深度：展示你对CPI/PPI剪刀差、库存周期、多周期共振、因子IC/IR、财务风险模型的理解。

## 十二、待确认信息（已确认）

| 维度 | 确认方案 |
|------|----------|
| 部署环境 | 本地台式机（RTX 4060 8GB + 32GB内存） |
| 用户规模 | 个人使用（Demo） |
| 数据权限 | 四级制（Demo简化） |
| 模型预算 | 100-200元/月 |
| 技术栈 | LangGraph + FastAPI + React + Ollama |
| 开发周期 | 6-8周 |
| 数据源 | AkShare + Tushare + BaoStock + 官方源 |
| 移动端 | 响应式Web + 微信小程序（后续） |
| 报告模板 | 自定义模板，参考业界领先 |
| 回测区间 | 近10年 |
| 信创要求 | 无 |

## 十三、现行口径（Latest）与已废止归档

> **本节由《需求—PRD 对账硬约束》维护**（`AGENTS.md` · `.trae/skills/requirement-prd-sync/SKILL.md`），
> 2026-09-28 起生效（本节机制由 **CHG-0046** 引入）。三条规则：
> 1. **要拿现行值，只看本节**；§一 ~ §十二 是 2026-09-12 的历史基线，**只读**。
> 2. 口径变更时：正文原处**保留原文**并加 `⚠️ 已废止（CHG-xxxx）` 注，**同时在本节登记一行**
>    —— 「CHG 号必须在本文件里可见」是 `scripts/prd_sync_check.py --ledger` 的判据②，
>    台账单方面宣布"改了 PRD"会报 `改了 ≠ 生效了`。
> 3. 被取代的长段落**整段折叠进 §13.3 归档区**，原处只留一行指针。
>    **折叠（tombstone）而不是删除** —— 删掉旧口径，下一个人会把它当"从没提过"再提一遍。

### 13.1 现行口径速查（tombstone 表）

| 条目 | 原口径（章节 · CHG） | **现行口径** |
|---|---|---|
| 存储 | §2.1 与 §8.1「PostgreSQL + SQLite + Redis + Neo4j（可选）」· CHG-0002 | 默认 **SQLite 零依赖**；PostgreSQL / Redis 可选（`DATA_BACKEND=postgres` / `REDIS_CACHE_ENABLED=true`）；**Neo4j 未实现** |
| 认证 | §2.1「Keycloak（Demo可简化） + OPA」· CHG-0002 | 自研 HMAC + RBAC×ABAC（`src/core/tenancy.py`、`src/core/policy.py`）；Keycloak / OPA 未接入 |
| 本地模型 | §7.2 与 §7.3「Qwen3.5-4B / Gemma 4 E4B」· CHG-0003 | `qwen2.5:1.5b-instruct-q4_K_M`（轻量）/ `qwen3:8b-q4_K_M`（中等）/ `deepseek-r1:7b`（已拉取、默认不接线）—— 以 `configs/models.yaml` 为准 |
| Agent 架构 | §2.1「各自独立任务处理」与 §10.1「独立 FastAPI 微服务」· CHG-0004 | **单进程分层架构**：Agent 是进程内 `BaseAgent` 子类、接口无状态；保留演进为微服务的路径 |
| 行情数据源 | §3.1 表（不含 QMT）· CHG-0001 | 日线：AkShare → 腾讯 → Tushare → baostock → 本地CSV → **[QMT 开关]（最末）**；分钟/分时/快照：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**；QMT 默认关闭（`configs/intraday.yaml:228-231`） |
| 开发门禁 | §5.1 / §5.2（只有文字规范）· CHG-0045 | G0~G6 阻断式门禁（`dev-test-guardrails` skill，Trae 与 DeepSeek Harness **双宿主 SHA-256 同源**） |
| 模型路由（五层降级链） | §7.2 只写"本地模型分层策略"，**没有云端降级链**（当时 `local_only`/免费档/限流熔断都不存在）· CHG-0051 | **五条链逐跳定型**，见 **§十四**（`planning` 阿里→深度求索→本机；`light` 硅基→阿里→本机；`medium` 阿里→硅基→深度求索→本机；`reasoning` 深度求索→阿里→硅基→本机；`decision` 深度求索→阿里→本机）；零同厂商相邻；8B 只做地板不做主力；免费档配「连续 3 次 429 → 锁 10 分钟」落盘护栏 |
| 免费云端的成本口径 | §7.4「云端API成本估算」按**付费 API 单价**估算 · CHG-0052 | `PAID_PROVIDERS = {deepseek}`（**免费云端不计费**），`LOCAL_PROVIDERS = {ollama}`（**位置**判据，与"是否花钱"分离）—— 两个谓词混用会让免费云端被误判成本地（实测：`qwen-flash 拉不起来`） |
| 限流熔断状态文件位置 | 无旧口径（新增）· CHG-0053 | **按实例隔离**：`MOSS_RATE_GUARD_PATH` > `LLM_AUDIT_DIR` 同级 > `data/run/free_tier_429.json`。写死 `data/run/` 会让 dev 调试锁掉 pilot/生产的同一跳 |
| 免费档限流的可观测面 | 无旧口径（原先**没有任何可见面**）· CHG-0055 | `GET /api/v1/health` → `model_gateway.rate_limit_guard` **＋** 运行指标面板「免费档限流」一行（四态：未上报 / 未量到 / 无记录 / 已锁定+剩余时间）；读不到时 `available: false`，**不用 0 假装没限流** |

### 13.2 待落条目（口径冲突已确认，尚未改写正文）

**别在这里抄第三份清单** —— 唯一权威是 `docs/REQUIREMENT_CHANGELOG.md` §3 的
`待办 / 待确认` 行（当前 40+ 条），证据基座是 `docs/PRD_ALIGNMENT_AUDIT_20260928.md`。
其中两处口径冲突最大、必须由人拍板：

- **性能**：§4.9 写"简单 0.5-2 秒 / 中等 2-8 秒 / 复杂 5-10 秒"，实测热缓存 ~25s、冷启 ~33-38s（审计 A#34 · 待确认 C6）
- **成本**：§7.4 写"推荐方案（混合）100-200 元/月"，仓库日预算 20 元、实测单月 618 元（审计 A#48 · 待确认 C7）

### 13.3 已废止归档区（原文逐字保留，仅供追溯）

> 折叠规则：本节内容**不再代表任何现行要求**，只用于回答"当初是怎么写的"。
> 从正文搬进来的段落一律**原样保留**，不润色、不删减。

#### 13.3.1 §10.1 内嵌 AGENTS.md 模板（2026-09-12 版 · 废止于 CHG-0004）

```markdown
# Moss-FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统（Demo版）。

## 环境配置
- 本地运行Ollama（模型：qwen3.5:4b）
- 本地运行PostgreSQL + Redis
- 核心推理调用DeepSeek-V4-Flash API

## 架构规范
- 采用LangGraph StateGraph搭建Supervisor调度架构
- 18个专业Agent分5层：数据层(A01-A04)、信息层(A05-A07)、分析层(A08-A12)、行业层(A13-A16)、决策层(A17)、审计层(A18)
- Agent间通过标准JSON消息通信，基于MCP协议调用工具
- 每个Agent封装为独立FastAPI微服务，无状态设计

## 编码规范
- Python 3.10+，使用类型注解
- 所有Agent必须实现统一的BaseAgent接口
- 所有数据访问必须通过统一数据层，禁止直接连接数据库
- 禁止硬编码敏感信息（API Key、数据库密码）
- 所有分析结论必须附带数据溯源标签和推理路径记录

## 数据溯源规范
- 每个数据点必须包含source_url、publish_time、fetch_time、raw_content_hash
- 数据处理链路必须记录每一步的Agent ID和操作类型
- LLM推理必须记录prompt_hash和token数
- 最终结论必须引用所有使用的数据ID

## 安全合规规范
- 所有输出附带免责声明
- 多租户数据通过RLS策略自动隔离
- 审计日志独立存储，不可篡改
- 敏感数据操作日志保存不低于3年

## Trae使用规范
- 使用Spec模式进行系统级开发
- 使用Task工具并行派发多个Agent开发任务
- 每个Agent独立开发、独立测试、独立部署
```

---

## 十四、模型路由：五层降级链（现行口径 · 2026-09-28 定型）

> **本节是"投研分析功能当前到底走哪条链"的唯一权威答案**（CHG-0051）。
> 数值口径以 `configs/models.yaml` 为准，任何改动必须同时改本节与台账。
> 五条链的最终形态由用户 2026-09-28 裁定。

### 14.1 五条链的厂商序列（逐跳固定）

| 层 | 链（模型键名，按尝试顺序） | 厂商序列 | 单跳延迟预算 |
|---|---|---|---|
| `planning` | `qwen-dashscope-flash` → `deepseek-flash` → `local_light` | 阿里 → 深度求索 → 本机 | 25s |
| `light` | `qwen-siliconflow-7b` → `qwen-dashscope-flash` → `local_light` | 硅基 → 阿里 → 本机 | 25s |
| `medium` | `qwen-dashscope-flash` → `qwen-siliconflow-7b` → `deepseek-flash` → `local_medium` | 阿里 → 硅基 → 深度求索 → 本机 | 25s |
| `reasoning` | `deepseek-flash` → `qwen-dashscope-flash` → `qwen-siliconflow-7b` → `local_medium` | 深度求索 → 阿里 → 硅基 → 本机 | 25s |
| `decision` | `deepseek-flash` → `qwen-dashscope-flash` → `local_medium` | 深度求索 → 阿里 → 本机 | 30s |

「单跳延迟预算」= `src/infrastructure/llm/gateway.py::_ATTEMPT_BUDGET`。
**它只对链上还有退路的那几跳生效**（最后一跳不设预算，避免把"慢但正确"变成"必然失败"）；
调用方显式传入预算时最后一跳也生效（规划层的兜底是规则式规划）。

### 14.2 三条选型约束（不是偏好，是硬约束）

1. **零同厂商相邻** —— 相邻两跳必须来自不同厂商，否则"上游厂商挂掉"会一次带走两跳。
   机器判据：`tests/unit/test_llm_routing_contract.py`（逐层的厂商序列 + 跳数契约）。
2. **`light` / `planning` 用 1.5B，其余三层用 8B 做地板。**
   `light`（清洗/格式化）与 `planning`（从目录挑子集）**都不需要 8B**；
   8B 只在 `medium` / `reasoning` / `decision` 的**链尾**出现。
3. **8B 只做地板，不做主力。** 依据：`src/domain/intel/tone_job.py`
   （`EXTRACT_TIER=medium` + `local_only=True`）与 `src/domain/alerts/analyzer.py`
   这两个调用点**依赖 8B 的能力**（1.5B 在该任务上实测"5 条样本出 4 类错"）；
   而 8B 做主力时的**显存争抢**正是"挂死 120 秒"的成因（实测 120294ms）。
   哨兵：`tests/unit/test_intel_vocab.py::test_extraction_tier_resolves_to_the_8b_local_model`。

### 14.3 配额分布（为什么必须分散）

统一口径：按实测调用量（`scripts/audit_tier_volume.py`）折算日均 token。

| 口径 | 日均 token | 百炼（100 万 token / 90 天，一次性）跑道 |
|---|---|---|
| 改前：`light` + `medium` + `planning` 三层共用 `qwen-flash` 一个池 | 156,460 | **6.4 天** |
| 改后：`light` 移出到硅基（速率限制、可再生） | 41,839 | **23.9 天**（3.7×） |

- `light` 占全部调用量的 **82%**（1220/1485）—— 把它留在总量额度池里，
  等于"用最快耗尽的资源承接最高频的调用"。
- 两类限制的**性质不同**，这是选型的真正依据：
  百炼是**总量额度**（一次性，用完即止）；硅基是**速率限制**（RPM/TPM，超了 429
  但随时间恢复、可再生）。高频层要的是"能持续跑"，不是"有一大桶但会用完"。
- 已实测准入：并发 5（30/30 成功）、并发 10（40/40 成功）**零限流**，
  `siliconflow` p50 578ms / max 990ms；`dashscope` 轻任务 p50 **448ms**。
- 已知代价（诚实登记）：硅基免费档输出更"惜字" —— A08 86 字 vs 百炼 116 字；
  A11 **69 字** vs 135 字。所以它**只放在 `light`**（清洗/格式化，对信息量最不敏感），
  `decision`（最终结论）**刻意不接硅基** —— 宁可少一跳冗余，也不要一个会漏项的备源。

### 14.4 免费档限流熔断护栏（没有它，限流是不可见的）

`light` 首位是免费档，它被限流时的表现是"**每次调用先白撞一次 429，再落到下一跳**"：
延迟上升、下一跳配额被额外消耗 —— 而这个过程**原先没有任何可见面**。

| 判据 | 行为 |
|---|---|
| 某模型**连续 3** 次 429 | **锁定该模型 10 分钟**，期间直接从降级链上跳过它（不再白撞） |
| 任意一次成功 | "连续"计数清零 |
| 锁定期满 | 自动解锁并清零（半开重试，**不永久弃用**） |

两条实现硬约束（都由测试钉住，分别由 **CHG-0053** / **CHG-0054** 落账）：

1. **判据优先用 HTTP 状态码，不认文案**（`looks_rate_limited(..., http_status=)`）。
   异常文本是 `httpx` 的实现细节，它一改写法，只做文案匹配的护栏就**静默失效**
   （`total_429` 永远是 0，而没有任何报错）。
2. **状态文件按实例隔离**（`resolve_state_path()`）。写死 `data/run/`
   会让 dev 的调试把 pilot / 生产的同一跳一起锁掉 —— `data/run/` 是三个实例**共用**的目录。
   优先级：`MOSS_RATE_GUARD_PATH` > `LLM_AUDIT_DIR` 同级 > `data/run/free_tier_429.json`。

**可观测面**：
- `GET /api/v1/health` → `model_gateway.rate_limit_guard`
  （含每个模型的 `locked` / `remaining_s` / `total_429`）。
  判据区分「未量到」与「量到 0」：读不到时给 `available: false`，不用 `total_429: 0` 假装"没限流"。
- **运行指标面板**（管理员）「数据源与依赖健康」表新增一行「免费档限流」，
  四态可分：`未上报`（旧后端）/ `未量到`（读不到状态文件）/ `无记录`（还没有样本）/
  `已锁定，还剩 9 分 12 秒`。锁定态必须给**人话 + 剩余时间**，不是裸秒数。
  展示口径抽在 `web/src/rateGuardView.ts`（纯函数），由 `npm run check:rateguard` 穷举自证 ——
  **为什么不能只靠肉眼看界面**：字段改名 / `available` 丢失 / `models` 被拍平这三种改动
  都不会报错，只会**显示错**，所以两边字段名由
  `tests/unit/test_health_rate_limit_guard_contract.py` 打真实响应体钉住。

**实现位置**：`src/infrastructure/llm/rate_limit_guard.py`（状态落盘，重启不失效）；
接线在 `src/infrastructure/llm/gateway.py` 的降级链循环内（跳锁定 + 记 429 + 成功清零）。

### 14.5 真值来源与验收命令

| 对象 | 单一真值源 |
|---|---|
| 链与模型规格 | `configs/models.yaml`（`routing` 段 + `models` 段） |
| 单跳延迟预算 | `src/infrastructure/llm/gateway.py::_ATTEMPT_BUDGET` |
| 模型名 → 厂商/是否本地/是否计费 | `providers.py` / `gateway.py::LOCAL_PROVIDERS` / `PAID_PROVIDERS` |
| 限流熔断阈值与锁定时长 | `rate_limit_guard.py::LOCK_THRESHOLD` / `LOCK_MINUTES` |

```bash
# 五条链的契约断言（逐层链 + 厂商序列 + 零相邻同厂商 + 跳数）
uv run python -m pytest tests/unit/test_llm_routing_contract.py tests/unit/test_llm_gateway.py -q
# 分层调用量与配额跑道测算（读审计日志现算）
uv run python scripts/audit_tier_volume.py
```

> 2026-09-28 的验收是**一次性脚本**（`scripts/_verify_*`，按"研究/验证脚本不入库"
> 的政策未入库、跑完已删除）：它走 `LLMGateway()` 的**同一构造函数**打印
> 逐层实际链 + 厂商序列，并在 dev(8100) 上打了一次真实 `light` 调用确认
> 首跳落在 `qwen-siliconflow-7b`（3/3 成功，`provider_chain=['qwen-siliconflow-7b']`）。
> 结论：五条链与本节逐项一致，无相邻同厂商。
> 之所以保留在正文里：**判据要可复跑** —— 一次性脚本删了就复跑不了，
> 所以上面的常驻契约测试才是判据，这段只是那一次的现场记录。

---

免责声明：本设计文档仅供技术架构参考，具体实现需结合机构实际合规要求和IT环境进行调整。
