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
> - 现行日线链：AkShare → 腾讯 → Tushare → baostock → **[本地CSV 开关]（`LOCAL_QUOTE_DIR`，默认关闭）** → **[QMT 开关]（最末）**
>   - ⚠️ **2026-09-28（CHG-0061）起 `LOCAL_QUOTE_DIR` 留空**：它原指向 `D:/quantTrader/data`，
>     而**那正好是本机 MariaDB 的 datadir**（`my.ini: datadir=D:/quantTrader/data`）。
>     该目录下 `SH/`+`SZ/` 两个 QMT CSV 子目录已删除（约 1.13 GiB，停在 2026-08-31）
>     → 本跳已从链上移除。**恢复时必须指向专用导出目录，不得指向任何数据库 datadir。**
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
| 行情数据源 | §3.1 表（不含 QMT）· CHG-0001 | 日线：AkShare → 腾讯 → Tushare → baostock → **[本地CSV 开关]（`LOCAL_QUOTE_DIR`，默认关闭）** → **[QMT 开关]（最末）**；分钟/分时/快照：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**；两跳默认都关（`CHG-0061`：`LOCAL_QUOTE_DIR` 已留空、`QMT_ENABLED=0`） |
| 开发门禁 | §5.1 / §5.2（只有文字规范）· CHG-0045 | G0~G6 阻断式门禁（`dev-test-guardrails` skill，Trae 与 DeepSeek Harness **双宿主 SHA-256 同源**） |
| 模型路由（五层降级链） | §7.2 只写"本地模型分层策略"，**没有云端降级链**（当时 `local_only`/免费档/限流熔断都不存在）· CHG-0051 | **五条链逐跳定型**，见 **§十四**（`planning` 阿里→深度求索→本机；`light` 硅基→阿里→本机；`medium` 阿里→硅基→深度求索→本机；`reasoning` 深度求索→阿里→硅基→本机；`decision` 深度求索→阿里→本机）；零同厂商相邻；8B 只做地板不做主力；免费档配「连续 3 次 429 → 锁 10 分钟」落盘护栏 |
| 免费云端的成本口径 | §7.4「云端API成本估算」按**付费 API 单价**估算 · CHG-0052 | `PAID_PROVIDERS = {deepseek}`（**免费云端不计费**），`LOCAL_PROVIDERS = {ollama}`（**位置**判据，与"是否花钱"分离）—— 两个谓词混用会让免费云端被误判成本地（实测：`qwen-flash 拉不起来`） |
| 限流熔断状态文件位置 | 无旧口径（新增）· CHG-0053 | **按实例隔离**：`MOSS_RATE_GUARD_PATH` > `LLM_AUDIT_DIR` 同级 > `data/run/free_tier_429.json`。写死 `data/run/` 会让 dev 调试锁掉 pilot/生产的同一跳 |
| 免费档限流的可观测面 | 无旧口径（原先**没有任何可见面**）· CHG-0055 | `GET /api/v1/health` → `model_gateway.rate_limit_guard` **＋** 运行指标面板「免费档限流」一行（四态：未上报 / 未量到 / 无记录 / 已锁定+剩余时间）；读不到时 `available: false`，**不用 0 假装没限流** |
| 分析层数据可见性（白名单） | 无旧口径（`_AGENT_DATA_WHITELIST` 的选词规则从未写进 PRD，实现里只写了中文标签）· CHG-0057 | 白名单按 **`indicator` 的 en_id / 族前缀**匹配（**不按中文标签**）；每个已登记指标至少被一个 Agent 放行；判据用三向护栏钉住 —— 见 **§十五** |
| 规则式路径的审计留痕 | 无旧口径（原先规则路径**不写审计**，`model_used="rule-only"` 只出现在 AgentOutput 里）· CHG-0057 | 规则路径也落一条 `LLMResponse(provider="rule", tokens=0)`；**"走了模板"与"压根没跑"必须可区分** —— 见 **§十五** |
| 复合问的 Agent 路由 | 无旧口径（`analysis_type` 单值 + macro 剥离行业 Agent，四段复合问共用一份数据）· CHG-0057 | 保持 `analysis_type`，按问句领域信号**增补** Agent/指标（只增不减）；个股代码走 `state["focus_stock_code"]`，**不写进 `target`** —— 见 **§十五** |

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

## 十五、分析层数据可见性与规则路径留痕（现行口径 · 2026-09-28 定型）

> 本节由 **CHG-0057** 引入。触发它的是**一条真实报障**，不是设计推演。

### 15.1 报障原文与结论

用户问：

> 「当前宏观环境如何，预测下一年美国的加息、降息节奏，对A股的影响，
>   以及AI应用加速失业率增加对消费的影响节奏时间节点分析，
>   未来半年能否持有高股息的招商银行？」

前端显示：`model=rule-only，纯模板判定，未调 LLM；数据缺口见原文`，
结论首句是「**缺少联邦基金利率数据，无法判定方向**」。

而 `fact_data_points` 里 `fed:policy_range` 有 3 条（2026-09-27 的 3.75/4.00）、
`us_fed_rate` 72 条、`us_unemployment` / `us_nonfarm` 各 107 条。
**数据在库里，Agent 看不见。**

### 15.2 三条现行口径

| # | 口径 | 单一真值源 |
|---|---|---|
| **①** | **白名单按 `indicator` 的 en_id 匹配，不按中文标签。** 库里存的是 `us_nonfarm` / `fed:effr`，写 `"非农"` / `"FedWatch"` 一律放行不了；关键词是**子串**匹配，所以 `fed:` 这类**族前缀**才覆盖得住整族。 | `src/orchestration/supervisor.py::_AGENT_DATA_WHITELIST` |
| **②** | **每个已登记指标必须至少被一个 Agent 放行**（登记了却没人看得见 = 数据到了但结论里没有，且**不报错**）。 | `configs/indicators.yaml` ↔ §15.3 的三向护栏 |
| **③** | **规则式路径（`model_used="rule-only"`）也必须落一条 LLM 审计**，`tokens_in/out = 0`、`provider = "rule"`。理由：审计原先只在网关里写，规则路径直接 `return` → 运维页/审计文件里 **"走了模板" 与 "压根没跑" 长得一模一样**，排查方向被带偏。 | `src/domain/agents/analysis/base.py::_audit_rule_only` |

> **③ 的成本口径**：`tokens=0` 是**真实值**（确实没调 LLM），
> 不是"未量到"；`call_cost_cny()` 对 `provider="rule"` 返回 0 →
> **不污染账单**，但"这里发生过一次规则判定"变得可见。

### 15.3 五类静默故障（都不报错，都是本节的靶子）

| 故障形状 | 实测后果 | 护栏 |
|---|---|---|
| 白名单只写中文标签 | A08 的 22 个词在 719 种库里只放行 **4** 种；问利率报"缺少联邦基金利率数据" | `test_whitelist_coverage.py::test_registered_ids_in_agent_domain_stay_passable` |
| 白名单命中 0 条走兜底 | A11 拿到"前 200 条"（任意数据）而**看起来有输入** —— 假绿 | 同上 + `test_whitelist_still_filters_noise` |
| 行业 Agent 的 `watch_keywords` 被白名单挡死 | `watched_indicator_count == 0` → `_skip_reason()` 判"无本行业关注指标" → **静默跳过 LLM**，界面显示"没什么可分析的" | `test_industry_watch_keywords_survive_whitelist` |
| 同一 indicator 多值 | `fed:policy_range` 同日 3 条（上限/下限/有效利率）→ 取"最新一条"**把区间下限当成政策利率**报出去 | `test_fed_series_split.py`（拆成 `fed:target_upper` / `fed:target_lower` / `fed:effr` 单值序列） |
| **作业靠"函数调用时"注册进调度表** | `catalog_calendar` / `catalog_*` 采集由 `install_catalog_jobs()` 在 **API lifespan** 里注册，而 **Celery worker/beat 不跑 FastAPI lifespan** → 这些作业从未进 `beat_schedule`，**真实调度里一次都不会被触发**；beat 只是"少了几个条目"，日志里看不出 | `test_scheduler.py::test_catalog_jobs_are_registered_at_import_time`（干净进程只 import `celery_app`，断言 `JOB_REGISTRY` ↔ `beat_schedule` **双向一致**） |

> **第 5 类是本轮跑全量时暴露的**（`test_beat_schedule_built_from_registry` 红灯；
> 隔离复跑 24/24 绿，是**顺序相关**的红 —— `test_calendar_index.py` 会快照并恢复
> `JOB_REGISTRY`，恢复之后注册就没了）。它的教训与本节的其余四类同源：
> **"我改了"是意图，"它被调用了"才是事实** —— 一个作业写得再对，
> 没进调度表就等于不存在，而**没有任何报错**。
> 修法：在 `src/scheduler/celery_app.py` 的 `celery_app` **导入期**注册，
> 再生成 `beat_schedule`（见 `_register_catalog_jobs_before_schedule`）。

### 15.4 复合问不得被压成单一视角

`analysis_type` 是**单值**契约，表达不了"一句话问四个领域"。
现行口径：**保持规划器的 `analysis_type`，但按问句里真实出现的领域信号
增补 Agent 与指标**（只增不减，且增补发生在行业 Agent 剥离**之后**）。

- 判据与实现：`src/orchestration/supervisor.py::query_needs_stock_resolution` /
  `augment_plan_by_query_signals` / `_apply_query_signal_augmentation`
- 复合问里夹带的个股代码走 **`state["focus_stock_code"]`**，
  **不写进 `target`** —— 写进去会让 A08 的 prompt 焦点变成那只票，宏观分析被顶掉
- 判据纪律：**宁可不补**。个股信号要求及物意图词（持有/买入/建仓…）
  且问句里**没有严格大盘词**（大盘/两市/全A）；行业信号要求
  `route_industry` 真命中，**不看**"行业/板块/赛道"这类通用词
  （否则宏观问每次都多跑 4 个 reasoning 层 Agent，把既有优化整个撤销）
- 护栏：`tests/unit/test_query_signal_routing.py`（含每个信号的反例）

### 15.5 真值来源与验收命令

```bash
# 三向一致性护栏（登记↔白名单↔prompt/域）+ 幻影词上限 + 豁免过期检查
uv run python -m pytest tests/unit/test_whitelist_coverage.py -q
# 报障现场回归（联邦基金利率必须能到 A08；问利率无数据必须退回 LLM）
uv run python -m pytest tests/unit/test_macro_agent.py -q
# 复合问信号路由（含误命中守卫）
uv run python -m pytest tests/unit/test_query_signal_routing.py -q
# FRED 序列拆分（每个序列必须是单值）
uv run python -m pytest tests/unit/test_fed_series_split.py -q
```

> **可复算证据**（一次性探针，按"研究/验证脚本不入库"政策留在
> `docs/_evidence_20260928_model_routing/`，共 7 个只读脚本）：
> `_probe_macro_ruleonly.py` 端到端复现报障原文那一句；
> `_probe_whitelist_coverage.py` 打印 A08 的放行率（**4/719 → 99 点**）；
> `_derive_whitelist_gaps.py` 从 `watch_keywords` 自动推导缺口清单；
> `_verify_macro_fix_e2e.py` 修后验收（四条判据打勾）。
> 之所以登记在正文里：**判据要可复跑** —— 这些是那一次的现场记录，
> 常驻判据是上面的四个测试文件。

---

## 十六、本地数据入口统一（现行口径 · 2026-09-28 定型）

> 本节由 **CHG-0058 / CHG-0059 / CHG-0060** 引入。触发它的是**一条真实报障**
> （对外试点实例投研取数查不到），不是设计推演。

### 16.1 报障原文与结论

用户原话（2026-09-28）：

> 「quant_daily（15,410,692 行日线）+ quant_daily_basic（11,837,692 行）是 dev
>   独有的平行数据集。我的 pilot 库 https://hk.wujiaitool.cn/ 所有本地数据接口
>   都来自哪些库？」
> 「现在我投研分析依赖本地数据，但是查找这些数据查不到，因为数据分散到多个库里。
>   想彻底解决这个数据分散问题」

**"查不到"有三层原因，逐层实测：**

| 层 | 现象 | 实测证据 |
|---|---|---|
| **① 清单看不见** | `data_asset_catalog`（资产登记表）只扫 `settings.sqlite_path` **一个库**；`ColumnIndex` 递归 `data/**.db` 但**按名字跳过 `warehouse.db`** *且*受 8 GiB 体积上限（行情仓 14.36 GiB）→ **双重跳过** | `assets.py:279,300-305`、`column_index.py:99-116,190,254` |
| **② 取数写错库** | `_quant_column_points` 从 `settings.sqlite_path`（**应用库**）读 `quant_daily_basic`，而该表在**行情仓** | pilot：`no such table`；dev：期间内 0 行。同一列在行情仓有 **4,981 点 / 到 20260928** |
| **③ 数据不在库里** | 逐股 `PE(TTM):*` / `PB:*` 等 **307 个指标只在遗留主库**，pilot 完全没有；同指标主库多 **1,172,737 行** | pilot 722 种 / 1,233,156 行 vs 主库 756 种 / 1,998,608 行 |

同时存在 **3 套库路径解析器**（`settings.sqlite_path` 按环境隔离 /
`WarehouseConfig.from_env()` 全环境共用 / 模块自带 `config.yaml` 硬编码指向遗留主库），
覆盖 **51 个文件里的 77 处 `data/` 路径字面量**。

> ⚠️ **那个 11,837,850 是化石副本的行数，不是行情仓的（15,426,153）。**
> 同一判断被抄在 6 处（`column_index.py:13-17`、`local_data.py:12`、
> `akshare_connector.py:382/494/896/901`、`tests/unit/test_column_index.py:13-15`），
> 其中 `akshare_connector.py:382` 据此**决定不走本地、改走网络自算股息率**
> —— 为一个不成立的前提长期付网络成本。**行数一律现算，禁止写进注释。**

### 16.2 五类本地存储与隔离语义（现行）

| # | 存储 | 隔离 | 写者 | 谁在读 |
|---|---|---|---|---|
| ① | `data/pilot/moss_pilot.db`（= `MOSS_SQLITE_PATH`） | **per_env** | 本环境实例 | 账号/会话/告警/事件/自选/池子/选股结果/主线结果/索引 |
| ② | `data/quant/warehouse.db` | **shared** | **主实例** | 日K补充字段、股票名录、板块行情、回测 |
| ③ | `data/moss_finagent.db` | shared（**待迁移**） | 主实例 | 拥挤度 / 竞价 / 概念池（现为硬编码） |
| ④ | `data/mainline_cache.db`、`data/quant/tushare/**`、`data/auction_hist/**` | shared | 主实例 | 主线挖掘、回测、竞价录像 |
| ⑤ | `LOCAL_QUOTE_DIR` | **已停用（留空）** | — | 原指向 `D:/quantTrader/data` —— **那是本机 MariaDB 的 datadir**；其 `SH/`+`SZ/` 两个 QMT CSV 子目录已于 2026-09-28 删除，本跳已从日线链移除（`CHG-0061`）。恢复必须指向专用导出目录 |

### 16.3 写路径：**读统一、写归属**（不建同名表）

1. **读**可跨库：只读连接 + `ATTACH`。实测 ATTACH **4.6 ms**、跨库 JOIN **10.0 ms**，
   且往三个库写入**全部被拒**（`attempt to write a readonly database`）。
2. **写**必须归属到唯一 store；写路径**不统一**，且**不允许跨库写**
   （SQLite 无跨库事务 → 只能半提交）。
3. **不在 pilot 新建同名表。** 同名表 = 第二份真相。**共享**派生数据
   （拥挤度 / 竞价 / 概念池）→ 搬进共享库、写者定为主实例、pilot 只读；
   **环境私有**数据 → 才在 pilot 建表，但必须换名并在 registry 声明
   `isolation: per_env`。
4. **过渡三段**：观测（只登记现状 + 告警不阻断）→ **双写 + 读切换**
   （回滚只拨读指针）→ 裁剪（跑满一个完整周期后停旧写，旧表标
   `~~已废止~~` 留痕不删）。
   **禁止一次性 dump/restore 切换** —— 半截数据在这套系统里看起来完全正常
   （2026-09-18「选出两天前的市场」事故）。
5. **单写者闸门**：registry 声明 `writer`；非本环境所有的 store 启动时
   `rw → ro` **自动降级**，并在启动 banner 与 `/health` 如实印出。
   （`manage.py:774-789` 那段"横幅曾撒谎"的教训：状态必须如实印，不能复述设计假设。）

### 16.4 受保护 store 与删除纪律

> 用户 2026-09-28 明确声明：
> 「`data/quant/warehouse.db` 14.36 GiB 这是我行情数据库 **不能随便删，前端要用的**」。

- **`data/quant/warehouse.db` 登记为 `protected: true` / `deletable: false`**，
  任何清理口径不得覆盖它。（本节方案的方向相反：让**更多**代码只读它。）
- **`data/dev/moss_dev.db` 是 dev 的整个应用库，不是"一块化石"。**
  dbstat 实测：化石表 `quant_daily` + `quant_daily_basic` 及其 8 个索引
  = **5,708.3 MiB（占 82.2%）**，其余 **28 张非空表 1,234.8 MiB**
  （含 dev 自己的 `fact_data_points` **1,248,146 行**、`indicator_catalog` 661 行、
  6 个账号、96 条会话）。
  → 处置口径是 **`DROP TABLE` 两张化石表 + `VACUUM`**（回收 5.57 GiB），
  **不删文件** —— 删文件会连带毁掉那 125 万行。
- **`data/moss_finagent.db` 的「遗留空库」标签只对认证表成立。**
  `reset_admin_password.py:151-161` 称其为"遗留空库 · 就是默认读到的这个"，
  而该库 18 张认证/通知表确实为空 —— **但它的 `fact_data_points` 是全项目覆盖最全的一份**
  （756 指标 / 1,998,608 行）。**不得据该标签判定整库可删。**
- **空表（33 张）不删**：空表只占页、可回收 **0 字节**，删掉只会制造
  "表不存在"的新故障面。空表应当**登记**（纳入 `data_asset_catalog` 的周期审计），
  不是删除。
- **停机纪律**：`manage.py stop` 与 `--replace` 按**命令行**枚举本项目全部后端正进程
  （`manage.py:935-941`），跨端口并存时会**连带杀掉 pilot**
  → 单实例停机只能**按 PID 精确停其进程树**。

### 16.5 真值来源与验收命令

| 对象 | 单一真值源 |
|---|---|
| store 清单与隔离 / 写者 / 保护属性 | `configs/data_stores.yaml`（L1 落地） |
| 行情仓路径 | `src/quant/warehouse.py::WarehouseConfig.from_env` / `DEFAULT_SQLITE_PATH` |
| 应用库路径 | `MOSS_SQLITE_PATH` → `settings.sqlite_path` |
| 跨库只读连接 | `resolve_store` / `open_readonly`（L1 / L2 落地） |
| 留存窗口 | `retention_passes.PASSES` + `Settings.retention_*`（**`0 = 不清理`**） |

判据一律写成**次数 / 布尔**（不写毫秒）：

| 判据 | 现状 | 目标 |
|---|---|---|
| `src/` 内 `data/` 路径字面量（registry 本体除外） | 77 处 / 51 文件 | **0** |
| `src/` 内形如 `11,837,850` 的**行数常量** | 6 处 | **0**（只许现算） |
| registry 覆盖库数 == `data/**/*.db` 实扫库数 | 1 vs 6 | 相等 |
| pilot **与** dev 上 `_quant_column_points(600036, turnover_rate)` 最新日期 | pilot 报错 / dev 空 | **== 最新交易日** |
| pilot 的 `fact_data_points` 指标种类 | 722 | **≥ 756**（回流后） |

```bash
# 数据入口护栏（库清单覆盖 / 受保护 store / 行数常量）
uv run python -m pytest tests/unit/test_store_registry.py -q
# 受影响链路回归（本次改动相关，不跑全量）
uv run python scripts/affected_tests.py --run
# 需求—PRD 对账（交付前必须 0 ERROR）
uv run python scripts/prd_sync_check.py --ledger
```

### 16.6 跨库回流实测：去重键必须是**表自己的唯一约束**

> 本节由 **CHG-0062** 引入。它是一次**真实执行**的记录，不是推演 ——
> 三条候选键里有两条会造成不可逆的数据事故，都是"动手前先查最便宜的判据"拦下的。

**背景**：pilot 的投研链路只读 `data/pilot/moss_pilot.db`，而 **307 个指标只存在于
`data/moss_finagent.db`**（含 `M2`、约 300 条逐股 `PE(TTM):*` / `PB:*`）。
不回流 = 这些指标对 pilot 的分析层永远不可见。

**三条候选去重键，只有第三条安全**：

| 候选键 | 结果行数 | 后果 |
|---|---|---|
| `data_id` | 1,998,608 | ❌ 两库 `data_id` **0% 重合**（`d_<uuid4>`，非语义函数）→ 注入 **727,373** 行重复点 |
| `(indicator, period_date)` | 504,798 | ❌ 申万三级估值等**截面指标**是一行一个行业（维度在 `extra_json`）→ 静默丢掉 **766,437** 行 |
| **`UNIQUE(indicator, period_date, raw_content_hash)`** | **1,270,535** | ✅ 表自己的唯一约束（`fact_data_points` DDL 末行） |

> ⚠️ **教训**：我最初按 `(indicator, period_date, extra_json)` 三元键做，
> 它比表的唯一约束**更细** —— 于是多算了 700 行"看起来缺失"的行。
> **正确做法是先读 DDL 里的 `UNIQUE(...)`，以它为准**；`extra_json` 只是载体，
> 不是身份。

**实测结果（2026-09-28 执行）**：

| 判据 | 结果 |
|---|---|
| 插入行数 | **1,270,535**（88 秒，每批 2,000 行，批间 sleep 0.02s） |
| pilot 行数 | 1,233,156 → **2,503,691** |
| pilot 指标种类 | 722 → **1,029**（+307） |
| `UNIQUE` 重复组 | **0** |
| 「同日同 `extra_json` 多值」 | **0**（同日多值 721,860 组 **100%** 由 `extra_json` 维度造成，是截面而非重复） |
| 真正的数据缺口 | **0** |

**三条工程纪律（可复用的部分）**：

1. **写生产库前先 `VACUUM INTO` 备份** —— 对运行中的 WAL 库是一致快照，不需要停服。
2. **分批写 + 批间 sleep** —— 一次插 127 万行会长时间持写锁；pilot 正在服务客户。
3. **★ 幂等复查是判据，不是形式** —— 插完再跑一次干跑：
   - 报 **0** → 键正确；
   - 报 **非 0** → 键与表的约束不一致（本次即为 700）。
   本次正是靠它发现"三元键比 `UNIQUE` 更细"，并进一步证明
   **700 行全部是"同值不同标注"（`validation_issues`），无信息损失**。
4. **回滚清单落盘**（`data/backups/pilot_backfill_ids_*.txt`，含全部 `data_id`），
   回滚 = 按清单 `DELETE`，或从 `VACUUM INTO` 快照恢复。

### 16.7 L1 交付：单一事实源与三份清单归一（`CHG-0067`）

#### 16.7.1 新增的单一事实源

| 对象 | 位置 | 说明 |
|---|---|---|
| 存储清单 | `configs/data_stores.yaml` | 20 条登记（5 sqlite + 15 dir）。字段：`kind` / `isolation(shared\|per_env)` / `writer(main\|own\|none)` / `writable` / **`protected`** / `role` / `time_column` / `note` |
| 解析器 | `src/infrastructure/catalog/data_stores.py` | `resolve_store()` / `store_path()` / `open_readonly()` / `writable_here()` / `unregistered_databases()` / `describe()` |

**三条硬约束**（都写进代码注释与护栏）：

1. **不许再按名字或体积跳过存储。** 行情仓被 `SKIP_DB_PATTERNS`（名字）
   与 `max_db_bytes=8GiB`（体积）**双重跳过**，这是"看不见权威库"的直接元凶。
   要不要扫由登记的 `kind` 决定。
2. **`protected: true` 不得被任何清理口径覆盖**（`warehouse` 与 `legacy_main`）。
3. **写者归属是声明式的、可见的**，且 `writable_here()` 返回**决定 + 人话理由**；
   对 A7 未裁定的共享库，它如实标 `decided=false`（**只报告不阻断** ——
   把事实印出来比假装已经管住更安全）。

#### 16.7.2 三份清单改为读它（实测效果）

| 清单 | 改前 | 改后 |
|---|---|---|
| `assets.DataAssetCatalog` | 只扫 `settings.sqlite_path` **一个库** + 手写 4 个目录 | **149 条资产 / 133 张表 / 5 个库 + 16 个目录**；11 个 sqlite 存储逐个扫、目录角色取自 registry |
| `column_index.ColumnIndex` | 递归 `data/**.db`，**按名字 + 体积双重跳过行情仓** | 扫描范围由 registry 决定；`find_column("dv_ratio")` 命中 **`warehouse.db / quant_daily_basic`（row≈15,426,153、authority=99）** |
| `TABLE_FREQUENCY` / `DIR_FREQUENCY` | **手写**路径清单 | 目录的"在哪里"由 registry 定、"多久更新一次"由 `DIR_FREQUENCY` 定 —— **职责分开，不再各存一份路径清单** |

> **判据措辞的教训**：`assets.py` 原 docstring 写「扫描**主库**的全部表」——
> 而"主库"实际是**一个文件**。这句话让"一个库"看起来像"全部本地数据"，
> 于是"本地到底有哪些数据"的答案永远是 dev/pilot 那一份应用库。

#### 16.7.3 顺带修掉的一个**测试隔离泄漏**（既有缺陷，本轮现形）

`local_data.LocalDataExecutor._refresh_index()` 原先**无条件**把索引换成全局单例
`get_column_index(rebuild=True)` —— 而全局单例扫的是**真实仓库**。
调用方给了 `project_root=<tmp>` 的假库树时，一次"自愈"就把**作用域悄悄扩大**，
于是**单测读到真实行情仓**，用例的通过与否取决于本机数据。

反证：`test_local_data_executor.py` 的两个用例（"列在但实体无值应判
`NOT_APPLICABLE`"、"诊断要带候选列"）在本轮之前是**靠巧合通过**的 ——
全局单例当时也看不见行情仓，所以两边都查不到；行情仓一进扫描范围，它们立刻变红。

**修法**：给了显式索引就**就地重建它自己**，绝不换成全局单例。
**判据同步改写**：原断言 `calls == [True]`（要求调用全局单例）**把泄漏当成了契约**，
现改为"就地重建发生过 **且** 不得调用全局单例"（`calls == []`）——
这条反向断言正是防止泄漏回潮的那一半。

#### 16.7.4 验收判据现状（写成次数 / 布尔）

| 判据 | 目标 | 现状 |
|---|---|---|
| ① `src/` 内存储路径字面量（AST 口径、非 docstring） | 77 → **0** | **67（棘轮锁定，只许减）** ⏳ |
| ② registry 覆盖 == `data/**/*.db` 实扫库数 | 相等 | **✅ 未登记库 = 0**（并借此抓到并清掉一个游离空库 `data/warehouse.db`） |
| ③ pilot **与** dev 上 `_quant_column_points(600036, turnover_rate)` 最新日期 | == 最新交易日 | **✅ 两个环境都 664 点 / 2026-09-28 / staleness_days=0** |
| ④ `src/` 内行数常量 | 6 → **0** | **✅ 0** |

护栏 `tests/unit/test_store_registry.py`（8 条，含棘轮的**过期检查**）：
① 库清单覆盖实扫结果 · ② 受保护存储已声明且完好 · ③ 行数常量 = 0 ·
④ 路径字面量棘轮（**精确相等**，每次变化都要显式确认）+ ⑤ 棘轮降到 0 后必须删掉自身。

```bash
uv run python -m pytest tests/unit/test_store_registry.py \
    tests/unit/test_column_index.py tests/unit/test_local_data_executor.py \
    tests/unit/test_data_index_audit.py -q     # 65 passed
```

#### 16.7.5 L2 交付：跨库只读查询面（`CHG-0068`）

读统一、写归属的**读那一半**落到取数执行器上：

| 入口 | 作用 |
|---|---|
| `LocalDataExecutor.query_across(stores, sql, params)` | **一份只读连接**跨多个存储查询，库名直接写进 SQL（`warehouse.quant_daily_basic` / `main.map_quant_sector_stock`）；`mode=ro` + `PRAGMA query_only=1` |
| `LocalDataExecutor.available_stores()` | 存储清单 + **写权限报告**（与 registry 同源，不再长第二份清单） |

**为什么写路径不从这里走**：SQLite **没有跨库事务** —— 一次写两个库只能半提交；
而数据溯源规范要求每个数据点可归属到来源与操作者。所以写必须恰好落到一个库
（判据在 `data_stores.writable_here`）。

护栏 `tests/unit/test_store_registry.py::test_cross_store_readonly_query_works_and_rejects_writes`
**两个方向都钉**：只钉"能查"会漏掉写保护，只钉"写不进"会在查询坏掉时照样绿。

### 16.8 台账形状判据：字段数不符必须 ERROR（`CHG-0068`）

> 这一节记的是**工具链的完整性**，不是业务口径。放在这里是因为它与
> §16.6 / §16.7 同源：**都是"判据认不出坏掉的形状，而人以为已经查过了"**。

**现场**：同一天里 `docs/REQUIREMENT_CHANGELOG.md` 被改坏三次 ——
把 `CHG-0061` 的行首整个替换掉、毁掉 `CHG-0065` 的结尾、
把 `CHG-0067` 与 `CHG-0066` 粘成一行（字段数 9 → 14）——
而 `prd_sync_check.py --ledger` **每次都报"通过"**。

**根因**：`_table_rows()` 对**短行静默补空单元格**
（`cells += [""] * (len(header) - len(cells))`），
而**长行**被 `dict(zip(header, cells, strict=False))` **静默截断**。
两种都不报错 —— 于是**所有下游判据都建立在被静默改过的行上**。

**判据**：`_table_shape_issues()` —— 任何表格行的字段数必须与表头**严格相等**；
只认**未转义**的 `|`（markdown 里 `\|` 是单元格内的字面竖线）；
并且在 `validate_ledger()` **解析之前**先跑（顺序不能反）。

**自证**：`--self-test` 补第六条 —— 喂一个"两行粘成一行"的已知坏输入，
必须报出"字段数"。没有这条自证，新判据本身也可能是假绿。

```bash
uv run python scripts/prd_sync_check.py --self-test   # 六类坏输入
uv run python scripts/prd_sync_check.py --ledger      # 交付前必须 0 ERROR
```

### 16.9 字面量收敛：67 → 31（`CHG-0070`）

判据①（`src/` 内存储路径字面量）的迁移分四批做，**每批都先断言"行为等价"再跑测试**：

| 批次 | 内容 | 处数 | 等价性 |
|---|---|---|---|
| B0 | 判据修正：排除 3 个 **FastAPI 路由路径**（`/data/macro`、`/data/status`、`/data/sync`）误报 | 67 → 64 | 判据本身 |
| B1 | **7 个仓储的构造默认值** + 概念池 + 竞价 + 拥挤度三处模块路径默认值 | 64 → 49 | 15/15 逐条相等 |
| B2 | 共享只读存储的"权威定义"（行情仓 / 分区根 / 价格 / 基本面 / 主线缓存） | 49 → 36 | 15/15 逐条相等 |
| B3 | 情报缓存三条路径 + `DIR_FREQUENCY` 改按**存储名**索引 | 36 → 31 | prewarm 3/3 逐字相等 |

**四条纪律（都是本轮实测换来的）**：

1. **默认值即护栏，安全的一侧做成默认。** 7 个仓储原先默认 `data/moss_finagent.db`
   （三档隔离**共用**的遗留主库）—— 一次漏传 `db_path` 就让隔离档写到共享库上。
   现在默认取 `default_app_db()`（= `settings.sqlite_path`，正是隔离注入的那个），
   拿不到才退回 `legacy_main`。
2. **★ 把默认值从字面量改成 `None`，必须在函数体里补回落。**
   本轮漏了 `mainline/sources.py` 与 `quant/stock_directory.py` 两处，
   结果 `Path(None)` 直接打红 **11 个 mainline 用例**（`TypeError`）。
   判据：签名里出现 `= None` 的路径参数，函数体**必须**有
   `if x is None: x = <registry>`。
3. **等价性要逐条断言，不能"看起来一样"。** 每批迁移后跑一次
   "旧字面量 vs 新解析值"的逐条比对（15/15、3/3）——它同时证明了
   "registry 的声明与代码里的旧值一致"，而这正是 registry 可信的前提。
4. **相对路径不要去"优化"成绝对路径。** `prewarm` 三个缓存路径改成 registry
   派生时，我一度返回绝对路径 —— 那会把"忘记隔离就写到生产"的概率**提高**
   （相对路径至少还依赖 CWD，绝对路径永远命中真实仓库）。已改为
   **返回相对仓库根的路径**，与旧字面量逐字一致。

**刻意留在原地、并登记原因的 31 处**：

| 站点 | 处数 | 为什么不动 |
|---|---|---|
| `src/core/config.py` 的路径默认值 | 5 | 它们定义的是**主实例**的布局（`data/audit` / `data/llm_cache` / `data/scheduler`），而 registry 把 `llm_audit` / `access_audit` / `llm_cache` 声明为 `per_env`。改默认值会让主实例的审计目录从 `data/audit` 变成 `data/dev/audit` —— **那是把已有的哈希链搬走，链校验会断**。属 A7 类口径决策，须先裁定"主实例算不算一个环境" |
| 审计/缓存/运行目录的单点默认值 | 12 | 同上，都是主实例布局的一部分 |
| 路由与健康检查里的展示路径 | 7 | 与 `settings` 同源，改动等于改展示口径，收益低风险高 |
| 其余 | 7 | 待下一轮按同一模式迁移 |

> **本表已随 §16.10 失效** —— 上面这 31 处**全部清零**。留在这里是为了保留
> "当时为什么不敢动"的记录：那 31 处卡在"主实例算不算一个环境"上，
> 而 §16.10 给出了不需要业务裁定就能解决的答案。

### 16.10 收口到 0：registry 同时表达两套布局（`CHG-0080`）

§16.9 留下的 31 处全部是**主实例布局**（`data/audit` / `data/llm_cache` /
`data/scheduler` / `data/moss_finagent.db` / `data/run/...`）。当时不敢动，
是因为把默认值改成 registry 的 `{env_root}` 派生会**把主实例的审计哈希链搬走**
（`data/audit` → `data/dev/audit`），链校验会断。

**关键认识：这不是"该改成哪一套"的问题，而是"registry 少了一种表达能力"。**
项目里**同时存在两套布局**，是既成事实：

| 谁 | 应用库 | 审计 | 缓存/调度 |
|---|---|---|---|
| 三档隔离实例（`--env dev/test/pilot`） | `data/<env>/moss_<env>.db` | `data/<env>/audit` | `data/<env>/…` |
| **主实例 / 离线脚本**（不注入 `MOSS_SQLITE_PATH`） | `data/moss_finagent.db` | `data/audit` | `data/…` |

所以给 registry 加一个 **`main_path`** 字段 + 一个 **`is_main_instance()`** 谓词：
**隔离档走 `path`、主实例走 `main_path`**。代码里一处字面量都不用写，
两套布局都由唯一事实源表达。

**收敛轨迹**：77 → 67（判据修正）→ 49（仓储默认值）→ 36（共享只读存储）→ 31
（情报缓存 + 周期表）→ 25（缓存/审计/运行目录）→ **0**。

| 动作 | 内容 |
|---|---|
| registry 新增 | `main_path` 字段（`Store`）+ `is_main_instance()` + `store_rel()` + `default_app_db()`；登记 24 条存储（新增 `quant_strategies` / `gap_queue`） |
| `core/config.py` | 5 个路径默认值改用 `_env_field_factory`（`default_factory` 求值，**不能在类体求值** —— 否则 `--env` 注入永远不生效） |
| 其余 26 处 | 审计链/审计追加器/缓存/访问审计/限流状态/网络兜底状态/做T热缓存/竞价缓存/策略目录/缺口队列/选股诊断与结果/健康度缓存/路由展示/目录周期兜底表 |
| **等价性证据** | 主实例 **17/17 逐字相同**（含 `data/audit/audit_chain.jsonl`）、pilot **10/10 落在 `data/pilot/`**、两套布局 **13/13**、`Settings` **11/11**；合计 **51 条断言，0 异常** |

**本轮新增的两条纪律（都是实测换来的）**：

1. **★ 改了引用就要实测"名字可用"，编译抓不到。** `assets.py` 的
   `DIR_FREQUENCY_BY_PATH` 用了 `_rel()`，而 helper 被插在**它下面** ——
   `py_compile` **通过**（编译不解析名字），模块导入直接
   `NameError: name '_rel' is not defined`，`assets.py` 整条链都起不来。
   `routes/quant.py` 同款（`store_rel` 没补 import）。
   判据：**改完必须真导入一次**，不能只看编译。
2. **判据本身要自证。** `test_no_store_path_literals_in_src` 断言"`== []`"，
   如果判据太严（把该检出的都判成不是路径），它会**永远绿** ——
   那是最隐蔽的假绿。所以补了 `test_store_path_literal_judge_recognises_real_paths`：
   喂已知正例/反例各 5 条，确认认得出、且不误伤。
   这条自证当场抓出我自己写错的一个用例（`sqlite:///data/…` 里**确实**内嵌了
   硬编码路径，判据**应该**抓它 —— 是我把用例写错了，不是判据错）。

**棘轮到期**：基线常量 `_LITERAL_BASELINE` 与"降到 0 后删掉它"的过期断言
**已按约定删除**，只留永久栅栏 `test_no_store_path_literals_in_src`。

---

## 十七、本地模型兜底：能力档位与思维链口径（现行口径 · 2026-09-28 定型）

> 本节由 **CHG-0063 ~ CHG-0065** 引入。它取代了长期流传的一个误判：
> 「8GB 显存不足导致本地模型**掷硬币**」。实测结论是 **显存不是那个病**，
> 且换模型只是其中一半的解法。

### 17.1 根因：思考型模型的**思维链吃掉输出预算**（不是显存不够）

`qwen3` / `qwen3.5` 是思考型模型，**思考 token 计入 `num_predict`**。
真实 prompt（`tone.build_prompt` + `tone.extraction_schema()`，`num_predict=2048`）
各 3 次的实测：

| 模型 | 默认（开思考） | `think=false` |
|---|---|---|
| `qwen3:8b` | p50 **18.3s** · 输出 729 tok · 键齐全 100% | p50 **9.1s** · 321 tok · 键齐全 **100%** |
| `qwen3.5:4b` | p50 31.4s · **空正文 100%**（2048 全被思考吃掉） | p50 **4.8s** · 184 tok · 键齐全 **100%** |

**关键对照**：4B 的权重比 8B 小一半（显存宽裕得多），却**比 8B 更容易空返回**。
所以"显存不足"解释不了这个现象 —— 真正的机制是**输出预算被思维链吃光**，
上层看到的是"模型返回空内容"，与随机故障一模一样。

**现行口径**：本地（Ollama）调用一律 `think=false`。

- 默认值即护栏：不设任何环境变量时就是**关**（`providers.py::_resolve_local_think`）
- 回退：`MOSS_LOCAL_THINK=1` 全局开回；个别任务用 `ModelSpec.think` 覆盖
- 云端不受影响（`think` 是 Ollama 顶层参数，灌给 OpenAI 兼容端点会静默失效）

### 17.2 本地模型档位（RTX 4060 8GB 实测）

| 键 | 模型 | 实测驻留 | 角色 |
|---|---|---|---|
| `local_light` | `qwen2.5:1.5b-instruct-q4_K_M` | 1112 MB | 高频/轻量（分类、要点抽取） |
| `local_medium` | **`qwen3.5:4b`**（原 `qwen3:8b-q4_K_M`） | **2983 MB** | 语义判断、多字段抽取；三层的**链尾地板** |
| `local_reasoning` | `deepseek-r1:7b` | 4.36 GB | 已拉取、**默认不接线** |

**★ 组合表（这是"能不能兜底多 agent"的硬约束）**：

| 组合 | 合计 | 结论 |
|---|---|---|
| `qwen3.5:4b` + `qwen2.5:1.5b` | 4.10 GB | ✅ **可同时常驻**（交替请求 3 轮后两边都还在，余量 2.0 GB） |
| `qwen3:8b` + `qwen2.5:1.5b` | 6.69 GB | ⚠️ 可共存但只剩 ~1.2 GB，第三个模型进来就要换出 |
| `qwen3:8b` + `qwen3.5:4b` | 8.56 GB | ❌ 超了，必互相换出 |

> 余量 2.0 GB 的意义：**不再有"谁进来就把谁挤出去"的连锁** ——
> 那条连锁正是历史事故里 `120294ms` 挂死的成因。

### 17.3 换地板的准入判据（带标注语料，不是"看着差不多"）

9 条真实形态语料（利好带代码 / 利空无代码 / 宏观中性 / 减持问询 / 政策 /
高位风险 / 业绩 / 澄清 / 行业级利空）× 2 轮 × `think=false`，
每条带**人工标注的期望值**，逐字段比对：

| 指标 | `qwen3.5:4b` | `qwen3:8b-q4_K_M` |
|---|---|---|
| 完全正确率 | **89%** | 89% |
| 空返回率 | **0%** | 0% |
| p50 / p95 延迟 | **4.44s / 5.58s** | 5.40s / **22.49s** |
| `codes` / `brokers` / `analysts` / `bull_stocks` 命中 | 100% | 100% |
| `tone` 准确率 | 86% | **100%** |
| 显存驻留 | **2983 MB** | 5578 MB |

**唯一差异是歧义样本的方向判断**：正文同时含「减持 + 问询」与「分析师维持增持」
时，4B 读成"偏多/中性"，8B 读成"偏空"（各 2/2 稳定）。

**裁定：换成 4B**，理由（诚实登记，不是"差不多就行"）：

1. 该样本是**真歧义**：prompt 已明确「`tone` = 这段第三方原文自己的语气」，
   而原文里两个方向的信号同时存在；
2. `tone` 是承重字段（资讯列表【多】/【空】标记、`all/bull/bear` 筛选取它），
   所以它**必须能被复核** —— 现成的交叉核对在 `tone.py` 的
   「词表口径 vs 语义口径一致性检查」，不一致会被标出来，不会静默传播；
3. 换来的收益是**消灭超时型掷硬币**：p95 从 22.5s 降到 5.6s，
   而长尾正是"排队 → 撞 120s 超时 → 上层看到随机失败"的来源；
4. **8B 的 tone 优势无法兑现**：8B 与 1.5B 共存后只剩 ~1.2 GB，
   任何第三个模型进来都要换出重载 —— 那时它的延迟优势（和 tone 优势）一起消失。

### 17.4 已知缺口（不省略）

1. **★ 摘要为空与模型无关：真因是语义缓存串答案（2026-09-28 实测，已定位到层）**

   > **本条第 1 版把这件事归因成"4B 惜字撞 `no_number`"—— 那个归因是错的，已作废。**
   > 留这段更正痕迹，是因为**归因错一次就会改错一个地方**：当时差点去动摘要 prompt。

   **现象**：端到端跑 `tone_job.run_once`（5 条 >100 字正文、`local_only=True`），
   `calls=5 / written=5`，模型每次吐合法 JSON，但落库后只有 1 条有 `summary`。

   **逐层二分（每一步都是可复跑判据）**：

   | 层 | 判据 | 结果 |
   |---|---|---|
   | 模型本身 | 直连 Ollama `/api/chat`，两条不同正文 | ✅ 输出各不相同 |
   | 网关关缓存 | `gw.complete(..., use_cache=False)` | ✅ 输出各不相同 |
   | 网关开缓存 | 同一对正文再跑一次 | ❌ **`cache_hit=True kind=semantic`：第 2 条起拿到第 1 条的答案** |
   | `_ask_segment` 层 | 置 `gw._cache = None` 后同跑 5 条 | ✅ **5/5 摘要可落库，各条针对自己的正文** |

   **根因（数字可复算）**：语义相似度是拿**整个 prompt** 算的，而抽取 prompt 的
   固定骨架（JSON schema + 指令 ≈ 700 字）占绝大部分：

   | 两条不同资讯 | 整 prompt 相似度 | 仅正文相似度 | 阈值 0.85 |
   |---|---|---|---|
   | 光模块 vs 减持 | **0.9328** | 0.0393 | ★ 命中 |
   | 光模块 vs 降准 | **0.9348** | 0.0535 | ★ 命中 |
   | 减持 vs 降准 | **0.9371** | 0.0280 | ★ 命中 |

   **即任意两条不同资讯的 prompt 都 ≈0.93 相似 → 必然复用第一条的答案。**

   **影响面（与模型无关 ⇒ 8B 时代同样存在，是长期潜伏、不是本轮引入）**：
   `tone_job._ask_segment` 走默认缓存路径（未传 `use_cache=False`）。同一模型、
   同一批文本：关缓存三条各不相同，开缓存**三条全同**。

   **未修（诚实登记）**：这是**缓存键口径**缺陷，修法要单独裁定 + A/B：
   ① 语义复用只在**正文**相近时发生（骨架不进相似度）；
   ② 命中必须同时满足"整 prompt 相似"与"正文相似"；
   ③ 抽取类调用显式 `use_cache=False`。
   三条各有代价（②③ 降低命中率，① 要改缓存接口），**不在本轮范围**。

   **本轮的有效结论**：绕开缓存后 4B 的摘要 **5/5 全部可落库**、每条针对自己的正文、
   中位 **27 字**（8B 对照 20 字）—— **"4B 惜字导致摘要丢失"不成立**。

2. **`bear_stocks` 两个模型都填不出来**（0%）：它们把标的放进 `bull_stocks`
   （与 `tone` 一致）。这是 **prompt 措辞歧义**（"利空股票" vs "这条消息看空的股票"），
   不是能力问题 —— 两个不同规模、不同家族的模型犯同一个错，指向的是 prompt。
   **未修**：修 prompt 会动到 `tone.py` 的抽取契约与词表交叉核对，需要单独 A/B。

3. **分批常数未重标定**：`alerts/analyzer.py` 的 `STAGE1_BATCH=6` /
   `STAGE2_BATCH=8` 是按"输出含思考 token"算出来的，`think=false` 后
   输出预算只用了约一半。**刻意不动**（安全侧），待生产实测再标定。

4. **本地并发闸仍为 1**（`local_gate.py` 默认 `MOSS_LOCAL_LLM_CONCURRENCY=1`）：
   Ollama 本机**只有 1 个计算槽位**（最近 4000 行日志里 slot id 只出现过 `0`），
   调大并发**不会提吞吐**，只会把多个请求切成更慢的片段 —— 那正是"掷硬币"的
   制造者。这一条与"要多 agent 并发兜底"的直觉相反，但实测如此。

5. **本轮踩到的三个测量陷阱（写下来，下一个 AI 别再踩）**：
   - **LLM 缓存会骗人**：同一 prompt 换个模型跑，可能命中**旧模型**的缓存条目
     （审计里 `cache=True` + `model=qwen3:8b` 就是这么暴露的）。
     清测量路径 = `LLM_CACHE_DIR` 指到新目录 **且** 删掉结果文件。
   - **样本太短等于没测**：`tone_job` 对 ≤ `MIN_CHARS_FOR_EXTRACTION`（100 字）
     的文本走**规则路径、一次模型都不调**。第一版端到端探针 5 条全被
     `skipped_short_text` 吃掉（`calls=0`），而结论看起来像"模型不好使"。
   - **★ 链里放的是"配置键"不是"模型名"**（最贵的一次）：A/B 探针把
     `qwen3.5:4b`（模型名）塞进 `gw._routing["medium"]`，网关 `_spec()` 直接
     `KeyError`，而 `_ask_segment` **把它吞掉退回规则层** —— 两条臂于是
     "输出逐字节相同、延迟 0.0s、JSON 解析 12/12 失败"，**看起来像
     "A/B 完全一致"**，实际一次模型调用都没发生。
     判据：**延迟 0.0s + 两臂全同 = 先怀疑"没调到"，不要先解读成"结论一致"**。
     修法（本仓库两种都行）：走真实 gateway 路径时用**配置键**
     （`local_medium`），要给配置外的模型做对照就在**内存里注册别名**
     （`gw._specs[alias] = ModelSpec(model_name=...)`），别动配置文件。

### 17.5 真值来源与验收命令

| 对象 | 单一真值源 |
|---|---|
| 本地模型档位与显存 | `configs/models.yaml` 顶部「本地模型与显存」 |
| 思维链默认值 | `providers.py::_resolve_local_think`（`MOSS_LOCAL_THINK`） |
| 地板能力判据 | `tests/unit/test_intel_vocab.py::_assert_capable_local_floor` |
| 分批常数 | `alerts/analyzer.py::STAGE1_BATCH` / `STAGE2_BATCH` |

```bash
# ① 思维链开关必须真的进了 payload（不是只改了配置）
uv run python -m pytest tests/unit/test_local_think_switch.py -q
# ② 地板能力判据（含"被裁成 1.5B 必须报错"的自证）
uv run python -m pytest tests/unit/test_intel_vocab.py -q
# ③ 带标注的字段级测评（判据自证 + 真跑，约 5 分钟）
uv run python scripts/_probe_tone_graded.py --self-test
uv run python scripts/_probe_tone_graded.py --models qwen3.5:4b qwen3:8b-q4_K_M --rounds 2
# ④ 两个本地模型能否同时常驻（换模型后必跑）
uv run python scripts/_probe_coexist.py
```

> ③④ 是**未发布的本机脚本**（`scripts/` 默认不入库的政策，见 §13 与
> `tests/unit/test_shipped_deps.py`）。它们产出的一次性结果留档在
> `data/run/tone_graded_final.json`、`data/run/tone_think_ab.json`。


### 17.6 真实语料 A/B：4B 与 8B 在"券商作文的多空分析"上谁强（`CHG-0071`）

> 起因：用户指出「8b 在本项目里有其他功能在用，比如**券商作文的多空分析**」。
> 这是**对的** —— 该功能是调度作业 `intel_tone_extract` → `tone_job.run_once`
> → `medium` 层 → `local_medium`（= 现在的 4B），前端叫「热点&研报小作文」。
> 而我此前的准入测评用的是**合成语料**，所以这里补真实语料。

**口径**：语料 = `data/intel/item_bodies.jsonl` 里 **>100 字且含研报词**
（证券/分析师/研报/目标价/评级/行业/板块）的条目，按时间取最近 **24 条**；
链路 = 真实 gateway（含降级链与显存闸）、**缓存关闭**；
8B 用**内存别名**注册（配置里已无它，改配置会动生产路由）。

| 指标 | **`qwen3.5:4b`（现行）** | `qwen3:8b-q4_K_M` |
|---|---|---|
| **方向产出率**（能给出偏多/偏空结论） | **75%** | 66.7% |
| 券商命中 | 12.5% | 16.7% |
| 分析师命中 | 8.3% | 8.3% |
| 摘要可落库率 | 91.7% | 95.8% |
| JSON 解析失败 | 0 | 0 |
| p50 延迟 | **2.05s** | 2.31s |
| **方向结论分歧** | **2/24 条**（其余 22 条一致） | — |

**关键判据（直接回答"够不够用"）**：

- 「4B 判未定而 8B 给了方向」= **0 条**
- 「8B 判未定而 4B 给了方向」= **1 条**

即 **4B 在"能不能给出多空结论"上没有输**（12 条样本时看到的"8B 多一条方向"
是小样本偏向，24 条时反转）。券商/分析师命中率的差异（12.5% vs 16.7%）
落在个位数条数上，且该语料**本身大多不含券商署名**——
生产全量 3174 条里有券商的只有 2%，是**语料构成**而非模型能力。

**诚实边界**：只跑了 1 轮（`temperature=0.1`），24 条；分歧条目未做人工裁定
"谁对"（`#9` 一条是 AI 智能爆炸的评论，4B 判偏空、8B 判中性，两者都能自圆其说）。
要更强的结论需要更大样本 + 人工标注，**本轮不做**。

### 17.7 上下文预算：600 字正文占多少，以及**静默截断**的真实现场

> 起因：用户提问「券商作文的输入长度最大 600 个中文字，如果超过了 4b 模型
> 允许的大小，会发生什么？」。本节把这件事量到底（`CHG-0076`）。

#### 一、600 字正文的真实占用（实测，非估算）

口径：`tone.build_prompt` 的真实 prompt、`qwen3.5:4b`、`num_ctx=4096`、
`think=false`，读 Ollama 返回的 `prompt_eval_count`：

| 输入 | prompt 长度 | `prompt_eval_count` |
|---|---|---|
| 纯骨架（JSON schema + 指令，无正文） | 779 字符 | **404 token** |
| 300 字正文 | 1082 字符 | **603 token**（3 次一致） |
| **600 字正文（生产单段上限）** | 1379 字符 | **795 token**（3 次一致） |

**结论：600 字只占 4096 的 19%，余 3301 token 给输出**（输出预算是
`max_tokens=2048`）⇒ **装得下，余量充足**。

⚠️ 两个容易混淆的口径，别搞反：
`models.yaml` 里的 `max_tokens` 是**输出**预算；`num_ctx` 是**上下文窗口**
（由 Ollama 的 `PARAMETER num_ctx` 决定，当前 4096）。
另注：本机实测中文正文密度 ≈ **0.64 token/字**（骨架固定 404）。

#### 二、**为什么"超过"在生产路径上到不了模型**（两道硬闸）

1. `tone_job._extract_one` 先 `tone.segment_text(text)` 切段，段长上限
   `tone.MAX_TEXT_CHARS = 600`；
2. `tone.build_prompt` 里还有一次兜底：`body = (text or "")[:MAX_TEXT_CHARS]`。

即 **"600 字"不是期望，是上限**；单段最长就是 600 字，而 600 字 = 795 token。

#### 三、★ 真去撞上限会发生什么：**静默截断，不报错**

把正文一路加长（绕过 `build_prompt` 的兜底，直接拼骨架+长正文）：

| 提交的正文 | prompt | 实际处理的 `prompt_eval_count` | done_reason |
|---|---|---|---|
| 6000 字 | 6761 字符 | 3027 | `length` |
| **12000 字** | 12761 字符 | **2050 ← 封顶** | `length` |
| **20000 字** | 20761 字符 | **2050 ← 封顶** | `length` |

**行为**：Ollama **不报错**，只处理"装得下的那部分"，把实际处理的 token 数放在
`prompt_eval_count` 里。**而生产链路上原先没有任何地方读这个字段** ——
所以截断完全不可见，表现只是"这条怎么没抽全"。

⚠️ **封顶值不是常量**：换一个 `num_ctx=16384` 的临时模型（`ollama create` 建、
跑完删）后是 **8194~9152**。所以"到底能装多少"**不能当成一个固定数字**去写死判据。

#### 四、本轮的动作（只加观测，不改行为）

`providers._warn_if_prompt_truncated`：当
`prompt_eval_count < 0.3 × (system+prompt 字符数)` 时打一条 WARNING，
说明"后端可能只处理了一部分 + 去检查 `MAX_TEXT_CHARS` 与 `num_ctx` 的关系"。

- 为什么取 0.3：实测中文密度 0.64、骨架再压一点，0.3 是**保守下限**
  （英文/数字更密时也不会误报）。
- 为什么**只告警不重试**：这一层不该替调用方决定"截断了要不要重试"；
  而且当前生产路径（600 字上限）永远不会触发它 —— 它是给"将来有人放宽上限"
  准备的安全网。
- 护栏（3 条）：封顶时必报 / 生产上限的正常 prompt **不许**报（否则变噪音）/
  读数缺失时**不报**（「没量到」≠「量到 0」）。

#### 五、验收命令

```bash
uv run python scripts/_probe_ctx_budget.py     # 600 字的真实占用 + 逐档压 num_ctx
uv run python scripts/_probe_truncation.py     # 针尖测试：超长时丢哪一头
uv run python -m pytest tests/unit/test_local_think_switch.py -q   # 截断告警 3 条
```

> 这三个探针是**未发布的本机脚本**（`scripts/` 默认不入库的政策）。

### 17.8 逐跳时延与「第几跳成功」的累积代价（`CHG-0081`）

> 用户提问：「3 跳和 4 跳发生时延多少？」。逐跳实测再按成功位置求和 ——
> 因为各跳耗时**差异极大**，只看「打一次的总时长」会得出错误结论。

**口径**：同一段 600 字级正文 + 真实抽取 prompt（`tone.build_prompt`），
`think` 按现行默认（关）。**云端每跳只跑 3 次**（百炼是总量额度，实测跑道
23.9 天），本地 3 次 —— 这 n=3 是**带配额成本的测量**，如实登记。

| 跳（配置键） | provider | 实测 p50 | max | 明细 |
|---|---|---|---|---|
| `qwen-dashscope-flash` | dashscope | **0.16s** | 0.23s | 0.23/0.15/0.16 |
| `deepseek-flash` | deepseek | **1.32s** | 4.96s | 4.96/1.05/1.32 |
| `qwen-siliconflow-7b` | siliconflow | **7.23s** | 8.53s | 8.53/6.02/7.23 |
| `local_light`（1.5B） | ollama | **1.06s** | 21.06s | 21.06/1.06/0.90 |
| `local_medium`（**4B**） | ollama | **4.30s** | 4.57s | 4.57/4.30/4.30 |

**累积时延（成功于第 k 跳 = 前 k 跳 p50 之和）**：

| 层 | 跳数 | 第1跳 | 第2跳 | 第3跳 | **第4跳（本地地板）** | 全链失败 |
|---|---|---|---|---|---|---|
| planning | 3 | 0.16s | 1.48s | **2.54s** | — | 2.54s |
| light | 3 | 7.23s | 7.39s | **8.45s** | — | 8.45s |
| **medium** | **4** | 0.16s | 7.39s | 8.71s | **13.01s** | 13.01s |
| **reasoning** | **4** | 1.32s | 1.48s | 8.71s | **13.01s** | 13.01s |
| decision | 3 | 1.32s | 1.48s | **5.78s** | — | 5.78s |

**三条读法**：

1. **走本地地板的代价 = 前几跳 p50 之和 + 4.30s**。4 跳层最坏 **13.01s**，
   而 `_ATTEMPT_BUDGET` 给的是 20s（reasoning 25s / decision 30s）——
   实测远在预算内，所以「本地地板把整链拖到超时」这个担心**不成立**。
2. **`light` 层反而最慢**（第 1 跳 siliconflow 就 7.23s）：它承载 82% 调用量，
   而「快」的假设原本建立在 dashscope 上；现在首位换成更慢但**配额可再生**的
   siliconflow（理由见 §14.3）。**这是刻意的取舍**，不是缺陷。
3. **`local_light` 的 max=21.06s 是冷加载**（首次请求要 `load_tensors`），
   p50 只有 1.06s —— 所以「1.5B 很快」只在**已驻留**时成立。

**与本地 8B 时代的对照**（同一批审计用 `latency_ms` 现算）：

| 调用点 | 8B p50 / p95 | **4B p50 / p95** | 变化 |
|---|---|---|---|
| `intel_extract`（券商作文多空抽取） | 13.22s / 55.72s（dev）<br>17.37s / 45.30s（pilot） | **3.88s / 10.76s**<br>**7.09s / 11.71s** | p50 −71%~−59%，p95 −81%~−74% |
| `alert_analyzer`（事件告警打分） | 45.39s / 73.36s<br>40.93s / 65.21s | **8.38s / 18.14s**<br>（pilot 的 4B 样本尚少） | p50 **−82%** |

> ⚠️ 8B 的 max 实测 **141.28s / 110.67s 已超过** `llm_timeout_seconds = 120s`
> —— 那正是「掷硬币」在时延上的形态。换 4B 后 max 降到 18s 量级。

### 17.9 两个「看着像问题、查了是历史」的现场（`CHG-0082`）

排查「4B 是不是只做备用」时翻出的两个疑点，**都查实为历史，不是现行缺陷**：

| 疑点 | 判据 | 结论 |
|---|---|---|
| `intel_extract` 有 120 次走 1.5B | 那些调用的 `provider_chain=[local_light]`，时间全在 **09-25** | 当时确实在走 light 层；现在是 `medium` + `local_medium`，且 `test_intel_vocab` 已钉住「地板不得被裁成 light 的 1.5B」 |
| `alert_analyzer` 有 60 次走 `deepseek-flash`（付费） | **最后一次 09-26 11:09**（dev 是 09-25 16:02）；`chain=[deepseek-flash]` 单跳 | 当时开着 `MOSS_ALERT_ALLOW_CLOUD`；现在全仓库 / `.env` / 系统级环境变量**都没设它**，最近调用全是 4B（pilot 09-29 00:17） |

**教训（可复用）**：两个疑点都是被**累计审计**骗的 —— `llm_audit.jsonl`
**不轮转**，于是「历史占比」看起来像「现行行为」。**判据必须带时间窗**
（最近 N 小时 / 最后一次是什么时候），否则会把早已修好的事重查一遍。

### 17.10 跨仓库：`medscholar-agent` 本地模型降档的验证（`CHG-0085`）

> 起因：用户质问「直接把我的 medscholor 换了小模型，测试过质量吗？」。
> **答案是"当时没测"** —— 本节记录补测过程与结果，也记录我在那次改动里
> 自己引入并回退的一个错误。

#### 一、为什么"在本项目测得 89%"不能外推

本节的 4B 准入判据（§17.3）是在**投研抽取**任务上做的（9 条标注语料）。
而 `D:\code\medscholar-agent` 是**另一个项目**，任务形态完全不同：
医学文献综述——需要长文写作、引用编号纪律、跨语言（中文综述引英文文献）。
**把 89% 当成通用结论就是跨域外推。** 那次改动当时只验证到"YAML 能解析"。

#### 二、补测 A：它自带的写作基准（`scripts/bench_models.py`）

该脚本专测**中文医学写作**，判据是硬的（越界引用编号 / 未替换的占位符）。
**3 轮**：

| 指标 | `qwen3:8b`（原值） | `qwen3.5:4b`（改动后） |
|---|---|---|
| 稳态生成 | 47.0 / 47.1 / 47.2 t/s | **70.5 / 71.1 / 70.9 t/s** |
| 纯生成耗时 | 2.9 / 3.1 / 2.8s | **1.9 / 1.8 / 2.2s** |
| 输出 tokens | 132 / 141 / 128 | 128 / 119 / 145 |
| **引用问题** | **0 / 0 / 0** | **0 / 0 / 0** |

#### 三、补测 B：它自己的端到端验收（`scripts/e2e.py --model X --fresh`）

7 段检查 + 真实四库检索（PubMed / Europe PMC / OpenAlex / Crossref）+
完整五阶段工作流（Plan → Execute → Reflect → Synthesize → Review）：

| 判据 | `qwen3.5:4b` | `qwen3:8b` |
|---|---|---|
| 五阶段 | **全部走完** | 全部走完 |
| 墙钟 | **199.0s** | 311.8s |
| 产物字数 | **12,141** | 9,512 |
| 流式输出 | **7,299 字** | 6,250 字 |
| **正文引用校验** | **19 篇，全部可对应参考文献表** | 11 篇，全部合法 |
| 自审（Review） | 6.5/10，8 个问题 | **7.0/10，7 个问题** |
| 检索/入库 | 197 篇去重 → 187 篇入库，向量 187/187 | 同级 |
| E2E 退出码 | FAIL ×1 | FAIL ×1（**同一项**） |

**两次唯一的 FAIL 完全相同、且与 LLM 无关**：嵌入模型 `nomic-embed-text` 的
「同主题跨语言 > 无关主题同语言」项 —— 它自己的文档里已有换 `bge-m3` 的建议。

#### 四、结论与诚实边界

**结论：4B 在它自己的任务上未掉质量。** 依据是"产物更长、引用更多、
引用校验都通过、墙钟 −36%"这组**一致方向**的可观测值。

**边界（不省略）**：

1. 端到端**各只跑 1 次**（n=1）—— 自审 6.5 vs 7.0 那 0.5 分**不构成结论**；
2. `e2e.py` 的判据**全是工程正确性**（阶段走完 / 引用编号合法 / 产物落库），
   **它不判内容对错**；
3. claim-level faithfulness（`eval_faithfulness.py --llm`）**未跑**：
   它按 artifact 逐个核查，而 `--fresh` 会删掉上一份产物库
   （4B 那份已被 8B 覆盖 —— 我的操作失误），且 Tier 1 会把综述连同
   **每一条论断**交给裁判模型，成本较高（已告知用户）。

#### 五、同批修正我自己引入的一个错误（重要）

我把 `num_ctx` 从 8192 压到了 4096（想省显存）。**这在那个项目里是错的**：

    medscholar/agent/writer.py:56
      num_ctx 是提示词与输出的共享预算（8GB 显存下仅 8192），材料塞满会挤掉正文
    medscholar/agent/writer.py:82
      budget = num_ctx - want_output_tokens - estimate_tokens(system_prompt)
               - TOKEN_RESERVE

即 `num_ctx` **直接决定"能塞进上下文、进而被综述引用多少篇文献"**
（它配置里 `writer_max_papers: 25` / `context_char_budget: 24000` 都按它算），
而且它自带的 benchmark 写死用 8192 做基准 —— 改了会让"基准表现"与"实际管线"不一致。
**已回退 8192**；回退后与它自带的 `config.example.yaml` 逐键对比
**只剩 `model` 一处不同**。

> ⚠️ 附带一条风险登记：那个仓库的 `config.yaml` **不被 git 跟踪**
> （`.gitignore:9`）⇒ **改它没有 git 回退兜底**。原值基准在同一仓库的
> `config.example.yaml` 里；改动前应先备份。

## 十八、多环境数据管理（dev / test / pilot / prod）（现行口径 · 2026-09-28 定型）

> 本节由 **CHG-0066** 引入。触发它的是用户提供的一份《数据库管理》经验规则，
> 要求按它审本项目 dev/pilot/test 的管理缺陷。**基线原则**（用户文档原话）：
> 「物理隔离、权限最小、单向同步、接口统一、元数据版本化共享」；
> §6 反模式第一条 =「同库同账号，靠 `env` 字段区分」。
>
> ⚠️ 编号说明：本节起草时并发协作者已占用 `CHG-0063~0065` 与 **§十七**
> （本地模型兜底），故本节取 `CHG-0066` 并置于 §十八 —— 见 §17.4 同款的
> "同一仓库有并发协作者"纪律。

### 18.1 审计结论：7 项不合规，其中 3 项本轮已修

| # | 规则要求 | 本项目现状（实测证据） | 判定 |
|---|---|---|---|
| **A1** | §1 实例/配置隔离 | **`--env test` 无任何隔离分支** → 落 `MOSS_SQLITE_PATH` 默认值 `data/moss_finagent.db`（`manage.py:488` 自标"生产用"）；而 `manage.py test` 走 `build_test_env()` 临时目录 → **同一个 `test` 两套语义** | ✅ **本轮已修** |
| **A2** | §1 缓存必须隔离 | `llm_cache_dir` 是裸字面量 `data/llm_cache`，**三实例共用**；且本项目缓存 scope 不含 provider/model（跨环境对比会失真） | ✅ **本轮已修** |
| **A3** | §1 日志分环境 | dev 只重定向 `LLM_AUDIT_DIR`，**漏了 `MOSS_AUDIT_DIR`**（默认同为 `data/audit`）→ 访问审计写在共用目录 | ✅ **本轮已修** |
| **A4** | §4 Agent 不直连库、走统一网关 | **全线直连**：`src/` 里 77 处 `data/` 路径字面量、51 个文件；无网关层 | ⏳ **L1/L2 范围** |
| **A5** | §2 元数据加 `env` 字段 + 版本化发布 | `indicator_catalog` / `data_asset_catalog` **per-env 各一份且内容不同**（pilot 722 指标 vs 回填后 1,029），无 `env` / `version` / `is_shared` / `sync_from` 列，无"dev 验证→发布"流程 | ⏳ **待排期** |
| **A6** | §4 空结果诊断区分环境 | 规则给了 5 个码（`ENV_NOT_COVERED` / `PROD_ONLY` / `DEV_ONLY` / `DEV_SYNC_DELAY` / `PROD_PERMISSION_DENIED`），本项目 `local_data.py::DiagCode` 里**一个都没有** —— 于是"dev 没同步"会被误报成"库里没有" | ✅ **本轮已修（CHG-0075）**：5 个码全部定义（`DiagCode` 12→**17**），且**显式排除在联网兜底的触发表之外**（环境差异不是数据缺口）。见 §19.5、§18.3 的废止痕迹 |
| **A7** | §3 单向同步（prod → dev）、§1 prod 只读 | 本项目**没有 prod**；dev 与 pilot **各自采集且都写共享行情仓**（pilot 调度台账 `quant_data_sync` 成功 **41** 次，`manage.py:774-789` 自认两实例曾同写）。"谁写谁读"没有裁决 | ⏳ **需用户裁定** |

### 18.2 本轮已修的判据（机器判据，不靠记得）

| 修法 | 判据 |
|---|---|
| 新增 `TEST_ROOT` / `test_isolation_env()`，接进 `_prepare_environment` 的 `--env test` 分支 | `manage.py start --env test` 落 `data/test/moss_test.db`（实测） |
| `llm_cache_dir` 由裸字面量改为 `_env_field("LLM_CACHE_DIR", ...)`，三档隔离实例各自注入 | 三档 `LLM_CACHE_DIR` **互不相同** |
| dev 补 `MOSS_AUDIT_DIR`（原来是漏的） | 三档 `MOSS_AUDIT_DIR` **互不相同** |
| `assert_environment_consistency` 新增 §⑤：`env=test` 时路径必须含 `test` | 指向默认主库 → **自检拒绝启动** |
| `describe_environment` 给 `test` 印真实限制；未登记档不再退回模糊的"（测试）" | 启动日志可读 |

护栏（`tests/unit/test_env_guard.py`，含**反向测试**）：
- `test_start_test_env_is_isolated_and_self_consistent` —— **隔离后仍能起**
  （只证明"不隔离会被拒"不够，那等于把能跑的也一起拒了）
- `test_test_env_pointing_at_default_db_is_rejected` —— 补这一档的原始缺陷必须被拒
- `test_all_three_isolation_envs_isolate_cache_and_audit` —— 断言**互不相同**，
  不是"各自非空"（三个实例指向同一目录时每个值都非空、断言照样通过）

```bash
uv run python -m pytest tests/unit/test_env_guard.py -q
```

### 18.3 纪律：不假装完成

> ⚠️ **已废止（CHG-0075，2026-09-29）**：本节原文说 A6 的 5 个环境诊断码
> **本轮刻意不定义**。该判断**已被推翻并修正** —— 5 个码现已全部定义
> （`local_data.py::DiagCode` 由 12 个扩到 **17** 个），理由与判据见
> **§19.5**。原文保留在下方（**不删**），因为"为什么当初不定义"与
> "后来为什么又能定义了"合起来才是完整口径。

~~A6 的 5 个环境诊断码**本轮刻意不定义**。理由是本项目已为同类做法付过代价
（`MOSS_SCHEDULER_ENABLED` 设了但全仓库零处读取 → 伪造安全感，
见 `dev_isolation_env` 里那段注释）。**没有判据能产生它的错误码，
就是"设了但不生效的开关"** —— 所以 A6 必须等 A4 的跨环境库清单（L1 registry）
落地后再一起做。（这条纪律与 §17.4 同源。）~~

**修正后的口径（现行）**：当时的顾虑是"设了不生效的开关"，而**顾虑本身是对的** ——
所以补这 5 个码时必须同时补"谁会产出它"。L1 registry（`configs/data_stores.yaml`
+ `catalog/data_stores.py`，CHG-0067/0070）落地后，判定"这个存储在当前环境里
可不可见"有了**单一事实源**，5 个码各自都有判据能产生它，且**这 5 个码被显式
排除在联网兜底的触发表之外**（环境差异不是数据缺口，联网也拿不到）。

### 18.4 待用户裁定

- **A7 谁写谁读**：共享行情仓 `data/quant/warehouse.db` 目前被 dev 与 pilot **同时写**。
  候选口径：(a) 主实例唯一写、pilot/dev 只读（需给 pilot 裁掉 `quant_data_sync`）；
  (b) 维持现状并接受行级竞争；(c) 拆成"采写实例 + 只读副本"。
  **这是运维口径决策，不由 AI 单方面拍板。**
- **A5 元数据版本化**：是否引入 `env` 列与"dev 验证→发布"流程（会影响
  `data_asset_catalog` / `indicator_catalog` 的 schema 与既有登记数据）。

### 18.5 本轮实测：三档隔离矩阵

| 键 | dev | test | pilot |
|---|---|---|---|
| `MOSS_SQLITE_PATH` | `data/dev/moss_dev.db` | `data/test/moss_test.db` | `data/pilot/moss_pilot.db` |
| `LLM_CACHE_DIR` | `data/dev/llm_cache` | `data/test/llm_cache` | `data/pilot/llm_cache` |
| `MOSS_AUDIT_DIR`（访问审计） | `data/dev/access_audit` | `data/test/access_audit` | `data/pilot/access_audit` |
| `LLM_AUDIT_DIR`（LLM 审计） | `data/dev/audit` | `data/test/audit` | `data/pilot/audit` |
| `SCHEDULER_DIR` | `data/dev/scheduler` | `data/test/scheduler` | `data/pilot/scheduler` |

> 三档**互不相同**由 `test_all_three_isolation_envs_isolate_cache_and_audit` 断言。
> 唯一仍共用的是 `data/quant/warehouse.db` 与 `data/mainline_cache.db`
> （共享只读市场数据，**有意共用**，见 §16.2），以及 `data/run/`（见 **A7**）。


## 十九、本地数据确定性流水线（现行口径 · 2026-09-29 定型）

> 本节由 **CHG-0072**（流水线 L1–L6 + A12 溯源）、**CHG-0073**（合规「未量到」
> + 生产者落地 + 白名单那一层）、**CHG-0074**（横截面维度还原）、
> **CHG-0075**（§18 A6 环境诊断码补定义）引入。

### 19.1 报障原文与结论：不要靠提示词，也不要让采集 Agent 自由写 SQL

用户原话（2026-09-28）：

> 「不要指望靠提示词让 Agent『优先查本地库』，也不要让采集 Agent 直接连数据库
> 自由写 SQL。要把『找数据』做成一条**确定性流水线**：元数据目录 → 语义解析 →
> 实体/指标链接 → 查询计划 → 统一网关执行 → **空结果诊断** → 反馈治理。」

工具优先级**由编排层固定**（不是写进 prompt 请求模型配合）：

```
local_catalog_search → local_query → local_doc_rag → external_search → LLM知识
```

同轮追加要求（原话）：

> 「要全面补充常见金融和股票财务指标词汇，加入到连接器，对接到数据库，
> 同时也可以遍历数据库所有表和字段参数，加入到连接器，构建**精准哈希索引**。
> 避免数据库有的却连接不到。指标等级 `indicators.yaml` 要支持**模糊匹配**。
> 连接器找不到、指标等级里无 id、`INDICATOR_CATALOG` 目录里没有，
> 都要**自动去联网获取数据，做最差的兜底，一定要找到数据**。」

**结论（也是本轮最贵的一条教训）**：`_filter_points_for_agent` 的实测是
「A08 白名单 22 词 → 719 种指标里只放行 **4** 种」，而库里数据齐全
（`fed:policy_range` 3 条、`us_nonfarm`/`us_unemployment` 各 107 条）。
**数据到了、Agent 看不见** —— 这类缺陷不报错，只表现为"结论里没有这一维"。
所以"找数据"必须是**代码里的确定性链路**，每一段都要有机器判据。

### 19.2 L1 元数据目录：反向索引（`catalog/column_index.py`）

| 能力 | 实现 | 判据 |
|---|---|---|
| 全库表/字段枚举 | 递归扫 `data/**/*.db`（跳过 archive/backup/rollback），`PREFERRED_DB_NAMES` 定序 + `DB_AUTHORITY` 定权威 | `TableColumns(row_count/columns/numeric/time/entity/investable)` |
| 字段 → 数据集 | `dataset_registry()` + `best_for_column()`：**有数据 → 最新时间 → 权威 → 行数** | 排序规则在代码里，不在注释里 |
| 已消费字段 | `consumed_columns()`：单次读源码 + `\w+` token 集合 | **16,007 ms → 60 ms**（同输入同输出） |
| 未消费的可投字段 | `unconsumed_investable_columns()` | 供"库里有、没人用"审计 |
| 单例与预热 | `get_column_index(rebuild=False)`；API lifespan 起 `column-index-warm` 任务（延迟 8 s） | 冷建 **~1.7 s**；反向查 **0.029 ms** |

> ⚠️ **"避免数据库有的却连接不到"的反面同样要防**：本轮实测
> `data/quant/warehouse.db::quant_daily_basic` **rows=15,426,153、
> trade_date 20060104..20260928（当天）**，而旧注释写着「该表停在 2023-11-10」。
> 那条注释让 6 个已实现的行情仓指标长期挂着"僵尸表"的豁免 ——
> **错误的理由比没有理由更危险**，因为它会让人停止追查。

### 19.3 L2 语义解析 / 实体链接（`catalog/synonym_dict.py`）

| 对象 | 规模（实测） | 关键判据 |
|---|---|---|
| 指标别名 | **154** 条 | `resolve_metric()` |
| 实体别名 | **260** 条 / 76 个代码 + 生成式 5,568 条 | `resolve_entity()` |
| 匹配规则 | **最长匹配跨度**（`_alias_match_span`） | `股息率TTM` 必须落到 `dv_ttm`、**不许**落到 `dv_ratio` |
| 英文边界 | ASCII 别名必须是**整词** | 否则 `pe` ⊂ `fedtargetupper` 这类子串会误命中 |

> ★ 这里推翻过一次自己的判断：第一版修法是"按长度降序排别名"，**方向错了** ——
> 正确的是**最长匹配跨度**。别名表的排序不是语义，跨度才是。

### 19.4 L3 查询计划与统一网关执行（`catalog/local_data.py`）

- `LocalDataExecutor.metric_series(metric, entity, start, end, limit)`：指标 + 实体 → **一条只读 SQL**。
- `_alias_candidates()` 先问 `synonym_dict.resolve_metric`，再追加原文（模糊匹配的兜底）。
- `_norm_entity()` 吃 `600036` / `600036.SH` / `SH600036` / 中文简称；个股走 `resolve_stock_sync`。
- `resolve()` 走 `best_for_column` 选存储，**不写死库名**。
- 索引失效自愈：`_refresh_index()`（陈旧引用 → 重建一次）。
- 跨库只读查询面：`query_across(stores, sql, params)`（§16.7.5）。

> ★ **取"最新"必须 `ORDER BY time DESC LIMIT n` 再反转**：旧实现
> `ORDER BY time ASC LIMIT 400` 把 **2007 年**的数据当成"最新"返回，
> 数值偏差 8 倍，且**一路无异常**。

### 19.5 L4 空结果诊断：17 个码（不是 12 个）

| 维度 | 码 | 语义 |
|---|---|---|
| 数据 | `NO_DATA` | 表在、列在、该实体/区间没有行（**可入队补采**） |
| 数据 | `NO_TABLE` | 表不存在（**可入队补采**） |
| 数据 | `NO_COLUMN` / `ENTITY_UNMAPPED` / `TIME_OUT_OF_RANGE` / `FREQ_MISMATCH` / `UNIT_MISMATCH` / `DIM_MISMATCH` / `NO_PERMISSION` | 逐条给出下一步（`DIAG_NEXT_STEP`） |
| 数据 | `CONN_FAIL` | 连接层失败 |
| 数据 | **`NOT_APPLICABLE_FOR_ENTITY`** | **不是数据缺失，是这个实体没有这个概念**（给银行要"流动比率"）→ **不触发联网** |
| 环境 | `ENV_NOT_COVERED` / `PROD_ONLY` / `DEV_ONLY` / `DEV_SYNC_DELAY` / `PROD_PERMISSION_DENIED` | 环境差异 → **不触发联网**（本节推翻 §18.3 原判断，见那里的废止痕迹） |

`ENQUEUEABLE_CODES = {NO_DATA, NO_TABLE}` —— 只有这两个码值得进补采队列；
其余要么是环境问题、要么是语义问题，入队只会制造噪音。

> 每个码都必须有 `DIAG_NEXT_STEP` 文案，且由测试断言"**码集 = 文案键集**"。
> 理由：没有下一步的码，等于把"排查方向"留给下一个人重新想一遍。
> 判据：`tests/unit/test_local_data_executor.py::test_all_diag_codes_defined_and_documented`
> —— 它断言**两个维度集**（数据 12 + 环境 5），**不写死 17 这个数**。

### 19.6 L5 联网兜底：三护栏 + **默认 fail-closed**（`catalog/network_fallback.py`）

| 护栏 | 默认值 | 作用 |
|---|---|---|
| 来源白名单 | **`()`（空 = 全禁）** | 安全的一侧做默认；要开必须显式配 |
| 每日预算 | **¥2.0/天**（单次按 ¥0.02 计） | 成本上限写进代码 |
| 每小时调用上限 | **30 次/小时** | 防重试风暴 |
| 失败冷却 | **600 s** | |
| 限流冷却 | **1800 s** | |
| 熔断 | 连续 **3** 次失败 | |
| 陈旧触发 | `STALE_TRIGGER_DAYS = 30` | 数据比这更旧才允许联网 |
| 状态落盘 | 按环境隔离（`data/dev/llm_audit/network_fallback.json`） | 冷却跨重启有效 |

**触发面与不触发面是两张表，且必须互斥并覆盖全集**：
`FALLBACK_TRIGGER_CODES ∪ _NO_FALLBACK_REASONS == DiagCode 全集`，交集为空
（由 `test_trigger_codes_cover_every_diag_code_without_silent_gaps` 断言）。

> ★ **不写死码数**。第一版这条测试写的是 `assert len(real) == 12`，
> 与本文件自己的 docstring（"并集 == 全集"）**自相矛盾**：补上 5 个环境码后，
> 并集判据立刻报"有码未归类"（**这是对的**），而魔数那行只会说"不再是 12 个"，
> 把真信息盖掉。**判据要表达意图，不要表达某个时刻的数量。**

### 19.7 L6 反馈治理

- 空结果诊断 → 补采队列（仅 `ENQUEUEABLE_CODES`）。
- **假缺口清理**：`scripts/clean_gap_queue.py` 把"其实不是缺口"的条目标 `skipped`。
- 判据：`tests/unit/test_gap_queue_false_positive.py`。

### 19.8 三跳（+兜底）取数：`supervisor._query_data`

```
① 本次已采集的 validated_points（内存，最便宜）
② LocalDataExecutor（本地库 + 17 码诊断）
③ _query_data_via_connectors（A01 的 ConnectorRouter）
④ 联网兜底（仅在诊断码属于 FALLBACK_TRIGGER_CODES 且护栏放行时）
```

`_AGENT_DATA_WHITELIST` 同步补齐 **en_id / 族前缀**（`us_*` / `fed:` / `cal:` /
`ind:` / `mkt:` / `idx_val:` / 行情仓列族等）—— 实测 A08 由 **4 种**指标提升到
**87~99 个数据点**。判据在 `tests/unit/test_whitelist_coverage.py`（**三向**：
登记 ↔ 白名单 ↔ Agent 域）。

### 19.9 ★ A12 合规：「没量到」不许伪装成「没风险」（`CHG-0073`）

**症状**：A12 对任何个股都输出 `compliance_level = "无"`、旗标
`["未见明显合规风险信号"]`、`confidence = "high"`，且因 `level == "无"` 会走
纯规则路径**跳过 LLM**（省 18,500 tokens/轮）—— **整条链路没有任何环节报错**。

**根因**：`compliance/logic.py::evaluate_compliance` 的最后三行把两种**语义相反**
的处境收进同一个 `else`：

| 处境 | 事实 | 旧输出 |
|---|---|---|
| 6 个族都量到了、值都没超阈值 | 真的没发现风险 | 「无」+「未见明显合规风险信号」 |
| **6 个族一条都没量到** | **什么都不知道** | **同上，一字不差** |

**实测证据**（`scripts/_probe_compliance_families.py`，走 `build_runtime()` 里
A01 真实持有的 `ConnectorRouter`；⚠️ 该探针第一版按 `supports()`/同步 `fetch()`
写，10 条全报"没有该属性"—— **`ConnectorRouter` 没有 `supports()`，`fetch()` 是
async**，那 10 条"取不到"是**探针自己错了**）：

```
修前：6 个族全部 "★ 无连接器支持"
      资产负债率:600036  命中 ['AkshareConnector'] 26 点 最新 2026-06-30 = 90.183
      ROE:600036        命中 ['AkshareConnector'] 25 点 最新 2026-06-30 = 5.68
```

即：**6 个族零生产者**，线上一直走的是第二行 —— 一张**伪造的体检合格证**。

**修法（分四层，缺一层就白修）**：

> ⚠️ **同批内的一次口径更正（留痕，不删旧口径）**：第 ③ 层最初接的 `质押` 是
> **全股东口径**（质押股数÷总股本），`_probe_compliance_acceptance.py` 当时实测
> `600036 大股东质押比例 = 0.35`（2026-09-24）。**那个口径是错的** ——
> 全市场该口径 `max = 78.74%`，而规则最高一档阈值是 **80%** ⇒
> **永不触发**，等于又接一条永远不开火的防线。已改为**股东粒度**
> （`占所持股份比例` = 该股东质押股数 ÷ **该股东持股数**）：实测 **828 条 / 564 只**
> 超 80%、957 只超 50%，`603657` 实测 **64.29%** ⇒ **50% 档第一次真的开火**。
> 指标名 `大股东质押比例` 与新口径一致，**不需要改名**。
> 代价与局限见 §19.13 第 11 条。

1. **规则层**：`_RULE_FAMILIES` 显式列出 6 个族，**逐族**记录有没有拿到值
   （`None` = 没量到；`0.0` = 量到了、读数是 0）；无输入 → 等级
   `LEVEL_UNMEASURED`（**「未量到」**），占位旗标 `FLAG_UNMEASURED`
   （明写"此结果不等于「无风险」"）。
2. **Agent 层**：`_build_rule_only_result()` 两条分支文案**完全不同**，
   各带一个机器可读的 `_rule_only_reason`（`measured_clean` / `no_input`）
   —— 没有它，"走了纯规则"与"压根没跑"在审计里长得一模一样。
   `_requirements()` 补禁令：规则未量到时**不得**输出「未见合规风险」，
   `compliance_level` 枚举加 `未量到`。
3. **采集层**：新建 `ComplianceFinConnector`（`商誉占净资产比` /
   `货币资金占总资产比` / `有息负债占总资产比` / `大股东质押比例` /
   `对外担保占净资产比`），并注册进 `src/api/runtime.py` 路由表。
   **`关联交易占营收比` 刻意不支持**：免费源只有公告标题/日期/网址、没有金额字段，
   且**不许退化成计数口径**（规则按子串取值，`关联交易公告数:{code}` 会被当成
   百分数去比 `> 30`）。
4. **★ 白名单层（最容易漏的一层）**：`_AGENT_DATA_WHITELIST["A12_compliance"]`
   原先只有 `担保`/`质押`/`关联交易` 三个词能**巧合**匹配到新指标名，
   `商誉`/`货币资金`/`有息负债` **全部被挡** → 商誉档与存贷双高
   （需两个输入同时到手）**恒不触发**。
   **这与"数据在库里 Agent 看不见"完全同类，只是换了一层。**

**验收（端到端，两层都打到）**：

```
scripts/_probe_compliance_acceptance.py        # 路由层
  600036  商誉占净资产比       1 点 2026-06-30=0.7401
  600036  有息负债占总资产比   1 点 2026-06-30=0.9868
  600036  大股东质押比例       1 点 2026-09-24=0.35
  → evaluate_compliance: 等级「无」、compliance_measured=True、
    已量到族 ['商誉','有息负债','质押']

scripts/_probe_a12_whitelist_acceptance.py     # 白名单层
  路由层合计 3 点 → **白名单过滤后仍是 3 点**
  A12 实际拿到：商誉占净资产比:600036 / 大股东质押比例:600036 / 有息负债占总资产比:600036
```

**现在这个「无」是挣来的**（3 个族真量到了、都没超阈值），而不是以前那种
"6 个族一条没量到还报无风险"。仍然缺的 3 族各有**不同原因**，必须分开看：
`关联交易`（免费源无比率口径）、`担保`（窗口内无公告 = 真结论）、
`货币资金`（**银行模板无此科目 → 口径不适用**，不是缺陷）。

**护栏**：`tests/unit/test_compliance_input_provenance.py`（23 条）。关键几条：
`test_no_input_is_unmeasured_not_none`（用户报障那一行的机器复现）、
`test_zero_is_a_measurement_not_a_gap`（6 个族逐个参数化）、
`test_measured_low_value_still_reports_none`（**反向**：别把假绿换成假红）、
`test_rule_families_cover_every_consumed_keyword`（与
`test_contract_consistency.py` 的 **AST 派生**清单对齐 —— 单一事实源）。

### 19.10 ★ 横截面指标：把「这个数是谁的」补回去（`CHG-0074`）

`ind:sw_third_pe_ttm:all` / `ind:sw_third_dividend_yield:all` 是**横截面**：
实测 **335 条**，`period_date` **全部等于 2026-09-29**，每条属于一个不同的申万三级行业。
而 `_build_context()` 的渲染是 `- {指标} {期}={值} c{置信} {来源}`，**不带 `extra`**：

```
旧：- ✅ind:sw_third_pe_ttm:all 2026-09-29=80.53 c0.95 AKShare申万行业估值
    - ✅ind:sw_third_pe_ttm:all 2026-09-29=25.34 c0.95 AKShare申万行业估值   ← 谁的？
```

模型收到 60 行无主数字，只能随手挑一个当"该行业 PE"。第二个更隐蔽的问题：
`context_max_periods = 60` 把它当"最近 60 **期**"截断，而这里**没有"期"** ——
同一日期上 335 个成员被**任意**留下 60 个，SQL 返回顺序不稳定 ⇒
**同一个问题两次答案不一样，且无法复现**。

**修法**（只加信息，不减信息）：判据是**行为判据**，不猜字段名 ——
① 同一 `period_date` ≥ 3 条；② 这些条目的 `extra` 里存在一个键，其取值 ≥ 2 个
不同非空值（**能被区分**才算横截面）。然后：

- 排序改成**完全确定**的 `(期 desc, 值 desc, 维度标签)`；
- 每行前缀 `[银行业]`；头部明写 `同日 335 个成员（按 industry_name 区分），
  按值降序展示 60 条（**已截断**）`（全量时写「（全量）」）。

**为什么下限是 3 而不是 2**：`fed:policy_range` 的事故正是**同一天 2~3 条**
（上限/下限/有效利率）而**没有任何字段能区分** —— 那不是横截面，是
"一个 indicator 多值语义"的缺陷，正确修法是**拆成单值序列**（已拆为
`fed:target_upper` / `fed:target_lower` / `fed:effr`）。拿"同一天多条"当横截面判据，
会把那个缺陷**掩盖**掉。护栏里专门有一条
`test_multi_value_without_distinguishing_field_is_still_a_defect` 盯住这个反面。

**顺带修掉**：`value is None` 原先渲染成字面量 `None`（`dict.get(k, 默认)` 只在
**键不存在**时给默认值），模型会读成 0 或忽略 —— 现在渲染成 `缺失`。

**护栏**：`tests/unit/test_cross_section_context.py`（10 条），含
`test_output_is_reproducible_under_input_shuffle`（打乱输入 → 输出逐字节相同）、
`test_truncation_is_explicit_not_silent`、`test_numeric_strings_are_compared_as_numbers`
（`"9.5"` vs `"10.2"` 必须按数值比，字典序会静默给错子集）。

**真实产物验收**（`scripts/_probe_cross_section_real.py`：真取数 → 真过
`_build_context()`，不是构造假数据）：

```
idx_val:snapshot:all                    47 点 → 渲染 12 行，**12 行全带标签**
  ▦ 同日 5 个成员（按 index_name 区分），按值降序展示 5 条（全量）
  - ✅idx_val:snapshot:all [科创50] 2026-09-29=103.22 c0.90 AKShare乐咕指数估值
  - ✅idx_val:snapshot:all [中证1000] 2026-09-29=28.69 c0.90 …
ind:sw_third_dividend_yield:all       1340 点 → 渲染 12 行，**12 行全带标签**
  ▦ 同日 335 个成员（按 industry_name 区分），按值降序展示 12 条（**已截断**）
  - ✅ind:sw_third_dividend_yield:all [防水材料] 2026-09-29=16.52 c0.90 …
  - ✅… [纺织鞋类制造] 9.7 / [广告媒体] 7.7 / [定制家居] 6.78 …
两者：**打乱输入顺序后输出逐字节相同 = True**
```

对照：修之前模型看到的是 12 行完全相同的 `... 2026-09-29=103.22`（不知道是哪个指数），
而这 5 个指数的 PE 从 12.44 到 103.22 差 8 倍 —— **挑错一个就是把结论说反**。

### 19.11 指标登记面：**把手写豁免表换成派生断言**

本轮 `configs/indicators.yaml` 78 → **97** 条，`INDICATOR_CATALOG` 70 → **81** 条。
补登记的对象与"为什么它们此前没登记"分三类：

| 类别 | 对象 | 为什么此前是缺口 |
|---|---|---|
| 行情仓列族 | `股息率TTM:` / `总市值:` / `流通市值:` / `换手率:` / `量比:` / `市销率:` | 连接器早就实现（`_QUANT_COLUMN_INDICATORS`），登记表没有；**旧注释还错说该表停在 2023-11-10** |
| 截面入口 | `idx_val:pe_ttm:{指数名}` / `idx_val:pb:{指数名}` / 申万二级 PE / 三级股息率 / 4 条渗透率赛道 | 连接器 `capabilities` 明确声明、`supports()` 认，登记表只登记了同族的一部分 |
| 宏观 | `M2` / `社融`（只缺目录）/ `PMI` / `PMI:制造业` / `PMI:非制造业` / `GDP` / `GDP:同比` | `PMI`/`GDP` 采集侧原本**无人实现**（19 个连接器 `supports()` 全 False）；`MacroExtraConnector` 落地后由"真缺口"变成"遗漏登记" |

> ★★ **本轮唯一的防复发改动**：把"白名单声称了、连接器也取得到、就是没人登记"
> 这张**手写豁免表**退役，换成**派生断言**
> `test_whitelist_keywords_a_connector_accepts_are_registered`
> —— 遍历 `_AGENT_DATA_WHITELIST` 的每个关键词，被 `supports()` 认的就必须能在
> `indicators.yaml` 里匹配到条目。**零清单、零豁免**（断言里明写"不要给它加豁免"）。
> 理由：手写表**不会自己长大** —— 下一个 `PMI`/`GDP` 式缺口它一个字都不会说。

**过期检查逼出的删除（全部由测试自己点名）**：
`_UNREGISTERED_CONNECTOR_PREFIXES` 删 8 条、`_CATALOG_NOT_REGISTERED` 整表退役、
`_KNOWN_UNPRODUCED_RULE_FAMILIES` 6 条删 5 条、`_ALLOWED_PHANTOM_PREFIXES` 删 5 条
（含 `A11_fin_risk:商誉` / `:有息负债` / `A12_compliance:担保` / `:质押` /
`A08_macro:GDP同比`）。

> ★ **`A08_macro:GDP同比` 那条值得单说**：白名单写 `"GDP同比"`（无冒号），
> 登记的 id 是 `GDP:同比`（有冒号）→ 子串匹配不到 ⇒ **一条都放不过去**。
> 修法是**改白名单**，不是留着豁免 —— 留着等于把"我放行了 GDP 同比"这句假话
> 登记成合法状态。**同一指标两种写法 = 幻影关键词，肉眼看不出来**，
> 只有"关键词必须能在 `indicators.yaml` 里匹配到"这条判据能抓。

### 19.12 验收：用户那条问题的数据可得性实测

问题原话：

> 「当前宏观环境如何，预测下一年美国的加息节奏，对A股的影响，以及 AI 应用加速
> 失业率增加对消费的影响节奏时间节点分析，未来半年能否持有高股息的招商银行？」
> 要求：「要确保数据都能找到，不存在数据缺少问题。」

**口径**：`scripts/_probe_acceptance_data.py` 逐条走 `build_runtime()` 的
真实 `ConnectorRouter`（判据用 `_matched()`，**不是**反射/目录猜测）。

**结果：26 条需求里 25 条取到、0 条空结果、1 条取数失败。**

| 子问题 | 代表指标 | 最新值（实测） |
|---|---|---|
| ① 宏观-中国 | `CPI` / `PPI` / `M2` / `社融` / `ind:社会消费品零售总额同比` | 2026-09=0.8 / 2026-09=3.8 / 2026-08=7.5 / **2026-04**=6245.0 / 2026-08=0.4 |
| ② 美国加息 | `fed:effr` / `fed:target_upper` / `fed:target_lower` / `us_cpi_yoy` | 2026-09-24=3.88 / 2026-09-28=4.0 / 2026-09-28=3.75 / 2026-08=3.4 |
| ③ A股影响 | `mkt:turnover:total` / `mkt:margin_balance` / `idx_val:snapshot:all` | 2026-09-29=17027.99 / 2026-09-24=26289.42 / 5 行 |
| ④ AI→消费传导 | `ind:penetration:AI大模型应用` / `人形机器人` | 2026-09-29=8.5 / 0.3 |
| ⑤ 高股息招行 | **`股息率TTM:600036`** | **2026-09-28 = 4.9606%（4,915 点）** |
| ⑤ | `PE(TTM):600036` / `PB:600036` / `ROE:600036` / `资产负债率:600036` | 6.76 / 0.9 / 5.68 / 90.183 |

**唯一失败**：`股息率:600036` → `ConnectionError: RemoteDisconnected`
（AkShare `stock_history_dividend_detail` 间歇性断连；同源的 `股息率TTM` 走行情仓，**成功**）。

### 19.13 已知缺口（不省略）

1. **美国宏观 5 条序列停在 2025-07/08（约 13 个月）**：`us_core_cpi`（2025-08-12）、
   `us_nonfarm`（2025-08-01）、`us_unemployment`（2025-08-01）、
   `us_pce`（2025-08-29）、`us_fed_rate`（2025-07-31）。而 `us_cpi_yoy` 是新的
   （2026-08）。→ **"预测下一年美国加息节奏"这一问的输入里有一半是旧数据**，
   必须靠新鲜度标注（`_STALE_DAYS = 45`）让 Agent 说出来，不能当作当期事实。
2. **`社融` 最新只到 2026-04**（`macro_china_shrzgm`），比 CPI/PPI 落后 5 个月。
3. **`大股东质押比例` 的阈值未标定**：连接器给的是源里的**全股东口径**
   （质押股数 ÷ 总股本），而 `compliance/logic.py` 的 50%/80% 两档是按
   **大股东持股口径**写的 ⇒ 该规则当前**偏保守、几乎不触发**。
   不替用户拍一个新阈值（没有数据支撑的阈值就是假精度）——
   **登记为已知缺口**：要么补大股东口径数据源，要么按数据重新标定。
4. **`对外担保占净资产比` 在 600036/600519/000001 上都是 0 点**：
   按连接器语义这是"窗口内没有担保公告"的**真结论**（取数失败一律抛错、不吞成空），
   但**未用"已知有担保公告的标的"反证过**该接口真的能取到非空结果 ——
   这一条仍是**待证**（不是"已证为空"）。
5. **`关联交易占营收比` 无生产者**：免费源没有比率口径，且不许退化成计数口径。
   该族在 A12 里会一直是「未量到」。
6. **13 条"连接器声明且 `supports()` 认、登记表没有"仍未登记**
   （`ind:sw_first_pb:all`、`ind:sw_second_pb:all`、2 条渗透率赛道、
   `mkt:turnover:sh|sz|cyb|kcb`、`mkt:turnover_rate:hist`、
   `mkt:north_flow:hist`、`mkt:margin_net_buy`）。已写进 `indicators.yaml`
   的「已知缺口」节；**求差时必须先过滤 `supports()` 为假的条目**
   （`StarChinextConnector` 的 `turnover:all` 是**相对键**，算进来全是假告警）。
7. `index_close:` / `etf_close:` **不登记是有意的**，不是缺口：
   `passers == []`（没有分析层白名单放行它们），登记即"僵尸登记"。
   它们的消费者在**分析层之外**（`src.intraday.sources.close_indicator()`、
   `src.api.routes.backtest._ASSET_QUOTE_PREFIX`、
   `src.core.data_freshness._FREQ_BY_PREFIX`），由
   `_OUT_OF_ANALYSIS_LAYER_PREFIXES` + 一条**行为判据**过期断言守住。
   ⚠️ 更正痕迹：本节初稿曾举 `src/mainline/sources.py::index_closes()` 为消费者 ——
   **错的**，它直接读 `quant_index_daily` **表**，从不构造 `index_close:` 这个 id。
8. `scripts/affected_tests.py` 本轮修掉两处**静默少选/崩溃**
   （GBK 解码崩溃；`__init__.py` 被算成 `X.__init__` 导致传递闭包断链 ——
   改 `compliance/logic.py` 时选中数由 **2** 纠正为 **26** 个测试文件）。
   它按 `git diff` 取改动面，**并发协作者的未提交改动会一起进来**（实测 211 条）——
   已改成超过 40 条就显式打印"挑选结果已不是本次改动的精确答案"。
9. **★ 「量到 0」的第三种情形：源给了 0，但那个 0 在语义上不成立**
   （`scripts/_probe_chanquan_zero.py` 实测）。`产权比率:600036` 与 `:000001`
   各返回 **14 点、全为 0.0**，而 `:600519`（非金融）= 17.7969。
   直连源帧判明：新浪财务分析指标模板里 `产权比率(%)` 列
   **非空率 10/10、值就是 `[0, 0, 0]`** —— 即**列存在、值被源写成 0**，
   连接器照实透出，**不是我们的解析 bug**。
   **但它对用户是假绿**：问"这家公司杠杆高不高"，看到的是 0%。
   前两种情形（「没量到」/「量到 0」）已有判据；**这第三种尚无判据**。
   修法候选（**未实施，本轮只登记**）：银行类实体对这些列登记 `not_applicable`
   （与 `local_data.NOT_APPLICABLE_FOR_ENTITY` 同一语义），
   或按实体类型改用别的杠杆口径。
   ⚠️ **明确不采用**"整列为 0 就当空值"这类启发式 —— 真有一批公司的某个比率
   本来就是 0（如无有息负债），启发式会把它们连同银行一起吃掉，
   把一个假绿换成一个假红。已在 `indicators.yaml` 的 `产权比率` 条目上写了 warn。
10. **★ 仓库级 lint 红灯（不是本轮引入，但 CI 会红）**：
    `.github/workflows/ci.yml` 跑 `uv run ruff check src tests scripts`，
    本轮实测该命令报 **85 个 error**（E501 ×26 / F401 ×22 / I001 ×19 / W292 ×9 /
    F841 ×4 / B009 ×2 / UP035 ×2 / B017、E402 各 1）。
    **本轮已把自己碰过的 19 个文件清零**（修掉 13 条，其中一条是**真 bug**：
    `repositories/macro_repo.py` 的降级分支调用**未定义**的 `logger` → `NameError`
    —— 即"优雅降级"路径一执行就崩，靠 `ruff F821` 才现形；
    **降级分支写错了等于没有降级**，而且报的是 NameError，排查方向会被带偏）。
    剩余 85 条分布在他人的文件里（`scripts/prd_sync_check.py`、`src/api/routes/*`、
    `tests/unit/*` 等），**本轮不代改**。按红灯纪律，它属"已知的红色基线"，
    必须显式登记而不是当背景噪音 → 建议下一轮收口
    （`ruff check --fix` 可自动修 56 条，其余 29 条需人工）。
11. **质押族的实质性与成本（`CHG-0073` 生产者侧的诚实边界）**：
    ①**实质性未过滤** —— `大股东质押比例` 取出质股东 `占所持股份比例` 的 **max**，
    会纳入微量股东：>80% 的 828 条里"质押股数占总股本"中位数 4.98% / p05 0.31%，
    额外限定 ≥1% 总股本则 564→520（**44 只 / 7.8% 仅由微量股东贡献**）。
    **不设该阈值**（没有数据支撑的阈值就是假精度），改为随数据下发
    `top_holder_pct_of_total` + `materiality_note`。
    ②**两源口径不一致**：`600036` 在市场截面里有（0.34% / 31 笔）、在股东明细里 0 行。
    ③**成本**：该接口无日期/个股参数，整表拉 ≈254 分页 / 75~240s ——
    已把 TTL 从 24h 调到 **7 天**（≥ `weekly` 采集周期，定时采集恰好养住缓存），
    使交互路径不再同步阻塞；**TTL 与 `frequency` 是耦合的**，
    将来把本指标改成 `daily` 就必须把 TTL 调回 ≤1 天。
    ④**担保是"近 12 个月区间累计"而非期末余额**（同一家换窗口 264.30%→366.96%→631.27%）
    ⇒ 偏高、可能高估严重程度，`confidence=0.6`。
    ⑤**银行有息负债不含「一年内到期的非流动负债」**（银行模板无该行）⇒ 招行≈0.99% 偏低；
    但银行 `货币资金` 本就不适用，存贷双高对银行永不触发，**不制造假绿**。

### 19.14 真值来源与验收命令

| 对象 | 单一真值源 |
|---|---|
| 反向索引与存储选择 | `catalog/column_index.py::get_column_index` / `best_for_column` |
| 指标/实体别名 | `catalog/synonym_dict.py::resolve_metric` / `resolve_entity` |
| 诊断码与下一步 | `catalog/local_data.py::DiagCode` / `DIAG_NEXT_STEP` |
| 联网兜底三护栏 | `catalog/network_fallback.py`（触发/不触发两张表） |
| 合规规则与等级 | `analysis/compliance/logic.py::evaluate_compliance`（权威值，LLM 不得修改） |
| 规则族清单 | `analysis/compliance/logic.py::_RULE_FAMILIES` |
| 分析层可见性 | `orchestration/supervisor.py::_AGENT_DATA_WHITELIST`（**单一事实源**） |
| 指标登记 | `configs/indicators.yaml` |
| planner 可选目录 | `orchestration/planner.py::INDICATOR_CATALOG` |

```bash
# ① 本地数据流水线四件套（索引 / 执行器 / 别名 / 兜底）
uv run python -m pytest tests/unit/test_column_index.py tests/unit/test_local_data_executor.py \
    tests/unit/test_synonym_dict.py tests/unit/test_network_fallback.py -q
# ② 契约四面一致性 + 白名单三向（高扇出，改指标/白名单必跑）
uv run python -m pytest tests/unit/test_contract_consistency.py \
    tests/unit/test_whitelist_coverage.py tests/unit/test_indicator_prefix_wiring.py -q
# ③ 合规「未量到」与 A12 端到端
uv run python -m pytest tests/unit/test_compliance_agent.py \
    tests/unit/test_compliance_input_provenance.py -q
# ④ 横截面维度还原与可复现截断
uv run python -m pytest tests/unit/test_cross_section_context.py -q
# ⑤ 真值探针（**都需要网络**，判据是"取到没取到"，不是"看着像不像"）
uv run python scripts/_probe_acceptance_data.py          # 用户验收问题的 26 条数据
uv run python scripts/_probe_compliance_families.py      # 6 个合规族在真实路由上的状态
uv run python scripts/_probe_a12_whitelist_acceptance.py # A12 白名单那一跳
uv run python scripts/_probe_cross_section_real.py       # 真实横截面的维度标签与可复现性
uv run python scripts/_probe_chanquan_zero.py            # 「源给了 0 但语义不成立」的证据
uv run python scripts/_e2e_query_acceptance.py           # 六子问题 23 口径（本地两跳 + 连接器）
```

> ⑤ 的三个探针是**本机脚本**（`scripts/` 默认不入库的政策，见 §13 与
> `tests/unit/test_shipped_deps.py`）；它们的输出已留档进本节与本节引用的
> 证据目录 `docs/_evidence_20260928_model_routing/`。

### 19.15 ★ 真实 LLM 端到端验收：一条 state channel 让整条问句的数据都没进计划（`CHG-0084`）

> 用户要求：「做真实 LLM 端到端」。本节就是那次验收的**原始结果**与它抓到的根因。

**跑法**（`scripts/_e2e_real_llm.py`，进程内 · 当前代码 · dev 隔离档）：
两个后端实例实测起于 **00:14:16**，而本节之前的改动都在那之后 ——
按 AGENTS.md 的教训（"对外实例跑的是 6 小时前的旧构建"），**拿旧进程验收等于没验收**，
所以不打扰运行中的实例（pilot 还是公网实例），改为在当前代码上 `build_runtime()`
+ 与 `/research/analyze` **同一份 state 契约**（`plan_run` + `query_needs_stock_resolution`
+ `resolve_stock` + `CancellationToken`）跑图。成本口径用 `src/core/budget.py::call_cost_cny`
（本项目**唯一**计价实现，未登记价格的模型按 `_FALLBACK_PRICE` 宁可高估）。

**修复前后（同一条问句、同一台机器）**：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 规划指标 / 实采指标 | 17 / 18 | **51 / 47** |
| 采集点数 | 231 | **1473** |
| 招商银行个股数据 | **0 条** | `stock_close` / `PE(TTM)` / `PB` / `资产负债率` / `ROE` / **`股息率TTM`=4915 点（2026-09-28 = 4.9606%）** |
| 美国宏观 | **0 条** | 6 条（`us_cpi_yoy` / `us_core_cpi` / `us_nonfarm` / `us_unemployment` / `us_pce` / `us_fed_rate`） |
| 消费/行业 | 0 条 | `ind:社会消费品零售总额同比`、`ind:白酒批价`、半导体/芯片/科技与消费行业 PE、`ind:sw_third_dividend_yield:all`（335 条） |
| A08 宏观 | 「通胀数据缺失…**缺CPI/失业率**」 | 「温和通胀（CPI 3.4%）…美林时钟：**过热**」 |
| A11 财务风险 | 「招行**无任何个股财务与股息数据**」 | 「招行**股息率TTM 4.96%**（2026-09-28）具高股息价值…」 |
| A12 合规 | 「本地合规规则**未获得输入**」（6 族一条没量到） | 「本地合规规则扫描完成：**未见明显合规风险信号**」（**量到了**才敢这么说） |
| A13 科技 | 「无本行业关注指标，**跳过 LLM**」 | 真跑了（半导体 114.31 / 渗透率 8.5%） |
| A18 审计 | **不通过**（1 项） | 通过（0 项）/ 末次 3 项（见缺口） |
| 耗时 / 成本 | 28.0s / ¥0.0611 | 50.2s→199.5s / ¥0.0789~0.0727 |

**根因：`_planned_indicators` 没声明在 state schema 里** —— 一条 channel 让整条问句的数据都没进计划。

`supervisor_node` **确实**做了问句信号增补（`_apply_query_signal_augmentation`
单独测会补 `PE(TTM):600036` / `stock_close:600036` / `ind:社会消费品零售总额同比`
等 13 条），也确实 `return {"_planned_indicators": indicators}` —— 但
**LangGraph 只保留在状态 schema 里声明过的 channel**，未声明的键在节点返回时
被**静默丢弃**。于是 `_collect_payload` 读到 `None` → 回退 `plan_run(...)`
（**不做增补**）→ 只采基础模板那 17 条。

**为什么它特别隐蔽（三件事同时为真，所以没人发现）**：

1. **Agent 补上了**（`plan` 是声明过的）→ A13/A14 真的跑了，只是拿不到本行业指标
   → 表现为「无本行业关注指标，**跳过 LLM**」；
2. **指标没了**（本键未声明）→ A10/A11 只见宏观与大盘
   → 表现为「**招商银行无任何个股数据**」，**听起来像数据源的问题**；
3. **全程无异常**：没有 KeyError、没有 warning、`errors` 为空。

**判据**：`tests/unit/test_planned_indicators_channel.py`（4 条），其中
`test_declared_key_survives_a_real_stategraph_roundtrip` 是**行为判据** ——
拿真实 `ResearchState` 编译 `StateGraph`，节点返回该键 → 必须能读到；
配一条**对照**（故意未声明的键必须读不到），否则前者可能因为"其实所有键都保留"
而空跑通过。变异验证：把那行声明注释掉 → **2 条立刻红**；恢复 → 4 passed
（sha256 逐字节确认回滚干净）。

**同轮第二、三处（都在用户那句"高股息"上）**：

- **`_SIGNAL_AGENT_INDICATORS["stock"]` 补 8 条**（`股息率TTM` / `ROE` /
  商誉 / 货币资金 / 有息负债 / 大股东质押 / 对外担保）—— 修复前
  **问股息却不采股息**，系统只能答"高股息可持续性无法验证"；
- **`_CODE_SUFFIX_INDICATORS` 必须同步补**（否则增补出来的名字是**裸名**，
  A01 去 fetch 一个没人 `supports()` 的名字 → 必然失败 → 又伪装成"该股没有股息数据"）。
  **这与 2026-09-26 那条 `PE(TTM)` 故障链是同一个形状**，区别只是这次裸名是我们自己新加的。
  护栏 `tests/unit/test_stock_signal_contract.py`（4 条，**派生**：交叉断言两张表 +
  反向断言后缀规则指向的指标必须已登记 + 非空自证）。
- **计划里同时放了 `股息率`（合成口径）与 `股息率TTM`**：前者走
  `stock_history_dividend_detail`，**实测每次 RemoteDisconnected** 并进 `errors`，
  反过来给 A17 递了一句"股息数据缺失"的假缺口。**同一个口径只留能取到的那一个**，
  已去掉 `股息率`（保留稳定且更新的 `股息率TTM`，实测 4915 条）。

**这一轮没解决的（诚实登记）**：

1. **`大股东质押比例` 冷缓存要 199.5s**：该接口无参数、整表 ≈254 分页。
   TTL 已按 `weekly` 采集周期设为 7 天（定时采集养住缓存），但**每个新进程的首次调用
   仍要付一次**（实测 199.5s 全场耗时几乎全在这一条）。生产是长驻进程 + 周级预热，
   频率可控；**但它仍是一次 2~3 分钟的同步阻塞**，与"禁止串行往返"有张力，
   下一轮应把它挪出交互路径（只由定时作业采）。
2. **A17 与行业 Agent 看不见个股数据**：A17 不走 `_AGENT_DATA_WHITELIST`，
   靠上游结论 + `query_data`；A13/A14 的白名单只有 `ind:`/`cal:`。
   于是末次运行里 A09/A13/A14 仍写「招行个股估值/股息数据全缺」，
   而 `PE(TTM)`/`股息率TTM` **确实已经采到** —— 这是**同一类缺陷的下一层**
   （数据到了、某个 Agent 看不见），已登记待下一轮按层逐个打通。
3. **`流动比率:600036` 进 `errors`**：对银行它**语义上就不可用**（连接器的报错文案
   写得很准），但 `NOT_APPLICABLE_FOR_ENTITY` 这个诊断码只存在于本地数据层，
   连接器路径仍把它记成"采集失败" ⇒ 一次正常运行被标成"有错误"。
4. **`mkt:cybkcb:spot_summary` 与 `fed:rate_prob:next` 计划了没采到**
   （CME FedWatch 不可达是已知的；前者待查）。
5. **A18 末次报 3 项产出完整性问题**，未定位。
6. 多个 Agent 的结论被 `[幻觉防护提示]` 标注"未在输入数据中找到的数字"，
   而其中若干确实在输入里（如 `1.70万亿` vs 输入里的 `17028亿`、`90.18%`）——
   **防护判据对"数量级改写/单位换算"过于严格**，会削弱它自己的可信度（待调）。

**本轮不重复的既有结论**：CME FedWatch 不可达导致的"未来一年美国加息节奏无法量化"
是**真实缺口**，不是假缺口（`_false_gap_source_names` 拦的是把**可达**源说成不可达）。

**验收命令**：

```bash
# 真实 LLM 端到端（需要 DEEPSEEK_API_KEY；dev 隔离档；单次约 ¥0.06~0.08）
uv run python scripts/_e2e_real_llm.py
# 结果留档：docs/_evidence_20260928_model_routing/_e2e_real_llm_result.json
#           与同目录 _e2e_real_llm_run.log（含"问句领域信号增补"等 INFO 日志）
uv run python -m pytest tests/unit/test_planned_indicators_channel.py \
    tests/unit/test_stock_signal_contract.py tests/unit/test_query_signal_routing.py -q
```


---

免责声明：本设计文档仅供技术架构参考，具体实现需结合机构实际合规要求和IT环境进行调整。
