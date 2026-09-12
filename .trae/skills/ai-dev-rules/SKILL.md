---
name: ai-dev-rules
description: "约束AI在生成投研系统代码时的行为边界，包括Agent接口规范、数据契约、安全红线、禁用模式、Token节约、性能优化。当Trae在开发Agent、数据层、编排引擎或任何投研系统模块时使用。"
license: MIT
compatibility: "Python 3.10+, FastAPI, LangGraph, PostgreSQL, Neo4j"
metadata:
  version: "1.0.0"
  project: "FinAgent-Research"
---

# AI投研系统开发规则

## 一、架构红线（不可违反）

### 1.1 Agent通信强制规范
- 所有Agent间通信必须使用标准JSON消息格式，禁止私有格式。
- 消息必须包含：message_id、sender、receiver、message_type、timestamp、payload、metadata。
- metadata中必须包含audit_id和data_sources。
- 禁止Agent之间直接调用内部方法或共享内存，必须通过消息总线。

### 1.2 数据访问强制规范
- 所有数据访问必须通过统一数据层（MCP Server），禁止Agent直接连接数据库。
- 每个数据点必须携带溯源元数据：source_url、publish_time、fetch_time、raw_content_hash。
- 多租户数据必须通过RLS策略自动隔离，禁止在应用层手动拼接租户过滤条件。

### 1.3 安全红线
- 禁止硬编码API Key、数据库密码、Token。所有敏感配置通过环境变量或密钥管理服务注入。
- 禁止在日志中输出用户隐私数据、完整Prompt内容、模型原始响应。
- 所有LLM输出必须附带置信度评分和免责声明。
- 核心数据的访问必须触发双人审批流程。

### 1.4 可观测性强制规范
- 每个Agent的每次调用必须记录Trace：trace_id、agent_id、input_hash、output_hash、duration_ms、token_usage。
- 推理路径必须记录每个步骤的step_type、description、data_refs、timestamp。
- 所有审计日志独立存储，不可篡改，保留期限≥3年。

## 二、代码生成规则

### 2.1 模块化规则
- 每个Python文件不超过300行，超过则拆分为子模块。
- 每个函数不超过50行，每个类不超过200行。
- 每个模块只暴露一个公共接口，其余为私有（_前缀）。
- 禁止循环导入。模块依赖必须为单向：core → domain → infrastructure → api。

### 2.2 命名规则
- Agent类命名：{Domain}Agent，如MacroAnalysisAgent。
- Skill目录命名：{domain}-{action}，如macro-cycle-analysis。
- 数据库表命名：{layer}_{entity}，如fact_macro_indicator、dim_company。
- 消息类型命名：{domain}.{action}，如macro.cycle_report。

### 2.3 错误处理规则
- 所有外部调用（API、数据库、LLM）必须包裹try-except，禁止裸抛异常。
- 错误必须记录完整的上下文：输入参数、调用链、错误类型、堆栈摘要。
- 禁止吞掉异常（except: pass）。
- 可重试错误（网络超时、限流）必须实现指数退避重试。

### 2.4 LLM调用规则
- 所有LLM调用必须通过统一模型网关，禁止Agent直接调用模型API。
- 必须设置max_tokens、temperature、timeout。
- 必须记录prompt_hash和response_hash用于审计。
- 必须实现降级策略：主模型超时→备用模型→返回缓存结果→返回错误。

## 三、禁用模式

| 禁用模式 | 原因 | 替代方案 |
|----------|------|----------|
| Agent直接访问数据库 | 绕过数据层，破坏隔离 | 通过MCP Server |
| 硬编码数据源URL | 难以维护和替换 | 配置文件/环境变量 |
| 同步阻塞调用LLM | 高并发下级联阻塞 | 异步调用+消息队列 |
| 全局可变状态 | 并发安全问题 | 请求级上下文 |
| 单文件超过300行 | 可维护性差 | 按职责拆分 |
| 函数超过50行 | 测试困难 | 提取子函数 |
| 循环导入 | 架构腐化 | 依赖注入/事件驱动 |
| 裸except | 掩盖错误 | 捕获具体异常 |
| 日志输出敏感信息 | 合规风险 | 脱敏+结构化日志 |
| 手动拼接SQL | 注入风险 | ORM/参数化查询 |

## 四、Token节约优化规则

- 所有Prompt必须经过压缩，上限4000 token。
- 动态上下文裁剪：保留最近3轮完整对话 + 更早摘要。
- 工具Schema按需加载，禁止全量注入。
- 模型路由：简单任务走轻量模型，复杂任务走最强模型。
- 语义缓存：命中率目标≥50%（Demo起步值，达标后再上调）。
- Token使用监控：记录input_tokens、output_tokens、cache_hit、cost_estimate。

## 五、系统响应性能优化规则

- 响应时间分级：简单查询0.5-2秒，中等2-8秒，复杂8-15秒。
- Agent并行执行：无依赖Agent必须并行，通过DAG编排。
- 渐进式响应：先返回快速结论，再推送深度分析。
- 向量检索优化：使用向量数据库，响应时间目标50ms。
- 性能监控：记录P50/P95/P99延迟，慢查询日志。
