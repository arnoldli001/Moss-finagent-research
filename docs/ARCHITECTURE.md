# 系统架构说明

## 一、整体架构

Demo阶段采用“本地为主、云端为辅”的混合架构。

| 层级 | 组件 | 技术选型（Demo版） | 职责 |
|------|------|----------|------|
| 编排层 | Supervisor Agent | LangGraph | 任务分解、Agent调度、结果聚合、冲突仲裁 |
| Agent层 | 核心Agent（6-8个优先） | LangChain + FastAPI | 各自独立任务处理 |
| 数据层 | 统一数据总线 | PostgreSQL + SQLite + Redis + Neo4j（可选） | 数据存储、检索、事件传递 |
| 模型层 | 混合模型网关 | Ollama + 云端API | 本地处理基础任务，云端处理核心推理 |
| 认证层 | 身份认证与权限 | Keycloak（Demo可简化） + OPA | SSO、MFA、RBAC+ABAC |
| 审计层 | 溯源与审计 | 本地文件 + SQLite + 哈希链 | 数据溯源、推理路径记录 |
| 应用层 | 前端与API | React + FastAPI | 用户交互、报告展示 |

## 二、Agent分层

### 数据层（A01-A04）
- A01 数据采集Agent：从各数据源爬取原始数据
- A02 数据清洗Agent：数据标准化、去重、格式转换
- A03 数据校验Agent：数据准确性校验、异常值检测
- A04 数据存储Agent：数据入库、版本管理、血缘追踪

### 信息层（A05-A07）
- A05 信息去伪Agent：信息真实性验证、来源可信度评分
- A06 信息提取Agent：从非结构化文本中提取结构化信息
- A07 舆情分析Agent：市场情绪量化、情绪周期定位

### 分析层（A08-A12）
- A08 宏观分析Agent：宏观经济周期定位、流动性分析
- A09 中观分析Agent：产业链分析、行业周期定位
- A10 微观分析Agent：个股深度研究、估值计算
- A11 财务风险Agent：财务排雷、造假预警
- A12 合规爆雷Agent：合规风险、爆雷风险预警

### 行业层（A13-A16）
- A13 科技行业Agent
- A14 消费行业Agent
- A15 周期行业Agent
- A16 医药行业Agent

### 决策层（A17）
- A17 投研建议Agent：综合所有分析，生成投研建议

### 审计层（A18）
- A18 逻辑审计Agent：推理路径记录、回测验证

## 三、通信协议

- MCP（Model Context Protocol）：作为工具调用层，统一模型与数据源/工具/服务的交互接口。
- A2A（Agent-to-Agent）：作为Agent互操作层，实现不同框架Agent之间的通信协作。
- 内部消息格式：JSON，包含message_id、sender、receiver、message_type、timestamp、payload、metadata。

## 四、数据流

```
用户查询
↓
Supervisor解析任务
↓
调度相关Agent（并行/串行）
↓
数据层Agent获取数据
↓
信息层Agent处理信息
↓
分析层Agent深度分析
↓
行业层Agent行业洞察
↓
决策层Agent综合建议
↓
审计层Agent记录Trace
↓
返回投研报告
```

## 五、依赖规则

- 单向依赖：core ← domain ← infrastructure ← api
- orchestration依赖domain和infrastructure
- 禁止反向依赖
- 层间通信通过抽象接口或消息总线
