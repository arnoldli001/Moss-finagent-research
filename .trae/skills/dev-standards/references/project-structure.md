# 项目结构详解

## 各层职责

### core层
- 定义BaseAgent、Message、Exception、Config
- 不包含任何业务逻辑
- 不依赖任何外部库（除Pydantic）

### domain层
- 包含所有业务逻辑
- 分为agents/（Agent实现）和skills/（技能定义）
- 依赖core层，不依赖infrastructure

### infrastructure层
- 包含所有外部集成
- 数据访问、LLM网关、消息总线、审计、认证
- 依赖core和domain

### api层
- 包含HTTP路由、中间件、Schema
- 依赖所有下层
- 不包含业务逻辑，只做请求转发

### orchestration层
- 包含Supervisor调度、任务图、冲突仲裁
- 依赖domain和infrastructure
- 不直接处理HTTP请求
