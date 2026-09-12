# FinAgent-Research 需求开发设计文档

> 项目代号：FinAgent-Research
> 原始需求文档存档（用户提供，2026-09-12）

## 一、项目概述

FinAgent-Research 是一个基于多Agent协作的AI辅助投研分析系统，面向二级市场投资决策场景，覆盖产业政策研究、产业链研究、个股研究三大核心模块，支持宏观、中观、微观三层分析，具备数据溯源、推理路径可回溯、多租户隔离、自迭代学习等能力。

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
finagent-research/
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

```markdown
# FinAgent-Research 项目规则

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

---

免责声明：本设计文档仅供技术架构参考，具体实现需结合机构实际合规要求和IT环境进行调整。
