# FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统（Demo版），面向二级市场投资决策。

## 环境配置
- 本地运行Ollama（模型：qwen3.5:4b、gemma4:e4b）
- 本地运行PostgreSQL + Redis + SQLite
- 核心推理调用DeepSeek-V4-Flash API

## 架构规范
- 采用LangGraph StateGraph搭建Supervisor调度架构
- 18个专业Agent分5层：数据层(A01-A04)、信息层(A05-A07)、分析层(A08-A12)、行业层(A13-A16)、决策层(A17)、审计层(A18)
- Agent间通过标准JSON消息通信，基于MCP协议调用工具
- Demo阶段采用单进程分层架构（src layout），Agent以进程内模块注册到Supervisor，接口无状态；保留演进为独立FastAPI微服务的路径

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

## Skills引用
@.trae/skills/ai-dev-rules/SKILL.md
@.trae/skills/dev-standards/SKILL.md
@.trae/skills/dev-standards/references/architecture-naming-spec.md
