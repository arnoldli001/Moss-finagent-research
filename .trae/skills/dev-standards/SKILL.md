---
name: dev-standards
description: "投研系统代码开发规范，包括项目结构、模块拆分规则、接口设计、测试标准、重构指南、前端设计、定时任务、数据缓存。当Trae需要创建新模块、重构现有代码、设计接口或编写测试时使用。"
license: MIT
compatibility: "Python 3.10+, FastAPI, LangGraph, PostgreSQL, Neo4j, pytest"
metadata:
  version: "1.0.0"
  project: "FinAgent-Research"
---

# 投研系统开发规范

## 一、项目结构规范

### 1.1 目录结构

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

### 1.2 依赖方向（单向）

```
core ← domain ← infrastructure ← api
                  ↑
           orchestration
```

禁止反向依赖。core不依赖任何其他层，domain只依赖core，infrastructure依赖core和domain，api依赖所有下层。

### 1.3 模块大小限制

| 层级 | 单文件行数 | 单函数行数 | 单类行数 |
|------|-----------|-----------|----------|
| core | ≤200 | ≤30 | ≤150 |
| domain/agents | ≤300 | ≤50 | ≤200 |
| domain/skills | ≤200 | ≤50 | — |
| infrastructure | ≤300 | ≤50 | ≤200 |
| api | ≤200 | ≤30 | — |

## 二、模块拆分规则

### 2.1 Agent模块拆分

每个Agent目录下按以下结构拆分：

```
agents/analysis/macro/
├── __init__.py
├── agent.py          # Agent主类（继承BaseAgent）
├── logic.py          # 分析逻辑（纯函数）
├── models.py         # 输入输出Pydantic模型
└── prompts.py        # Agent专属提示词
```

### 2.2 Skill模块拆分

```
skills/macro-analysis/
├── SKILL.md          # 技能说明
├── scripts/          # 确定性脚本
│   ├── calc_spread.py
│   └── detect_turning.py
├── references/       # 按需加载的参考
│   └── cycle-framework.md
└── assets/           # 模板
    └── report-template.md
```

### 2.3 数据访问模块拆分

```
infrastructure/data_access/
├── mcp_server.py         # MCP Server主入口
├── connectors/           # 各数据源连接器
│   ├── stats_gov.py
│   ├── pbc.py
│   ├── fred.py
│   └── akshare.py
├── models/
│   └── data_point.py
└── repositories/
    ├── macro_repo.py
    └── market_repo.py
```

## 三、接口设计规范

### 3.1 Agent接口
所有Agent必须继承BaseAgent，实现execute()、get_capabilities()、health_check()三个方法。

### 3.2 消息接口
Agent间消息必须通过消息总线，禁止直接调用。消息类型定义在core/message.py中，使用枚举。

### 3.3 数据接口
所有数据访问通过MCP Server，接口定义：

```python
class DataQuery(BaseModel):
    data_type: str
    indicator: str
    start_date: str
    end_date: str
    tenant_id: str
    permission_level: str

class DataResult(BaseModel):
    data_points: list[DataPoint]
    source_metadata: dict
    confidence: float
```

## 四、测试规范

| 层级 | 范围 | 工具 | 覆盖率要求 |
|------|------|------|-----------|
| 单元测试 | logic.py纯函数 | pytest | ≥80% |
| 集成测试 | Agent+数据层 | pytest + testcontainers | ≥60% |
| E2E测试 | 完整工作流 | pytest + httpx | 关键路径100% |

## 五、重构指南

- 单文件超过行数限制 → 立即拆分
- 函数超过行数限制 → 提取子函数
- 出现循环导入 → 引入依赖注入或事件驱动
- 同一逻辑在3处以上重复 → 提取公共模块
- 测试难以编写 → 说明耦合过高，需解耦

## 六、前端设计与体验规范

- 设计原则：信息密度优先、渐进式披露、多模态交互。
- 组件化：原子设计方法论，独立Hook管理状态。
- 响应式：三端适配（桌面/平板/移动），MVP阶段采用响应式Web。
- 数据可视化：表格+图表双视图，色盲安全调色板。
- 交互体验：一键溯源、自定义报告模板、进度指示器、明确错误提示。
- 美观性：Design Token系统、深色模式、等宽字体、动效规范。

## 七、定时任务调度规范

- 分布式调度框架（Celery Beat），禁止硬编码crontab。
- 任务幂等性、指数退避重试、超时控制、分片执行。
- 监控与告警：连续失败3次自动告警并暂停。

## 八、数据缓存规范

- 四级缓存：L1内存 → L2本地文件 → L3 Redis → L4数据库。
- 读缓存优先，写缓存失效。
- 缓存键格式：{tenant_id}:{data_type}:{indicator}:{date_range}:{params_hash}。
- 综合命中率目标≥85%。
- 缓存一致性延迟：实时行情≤1秒，宏观≤5分钟，财务≤1小时。
