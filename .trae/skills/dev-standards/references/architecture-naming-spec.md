# 架构文件结构设计规范

## 一、顶层目录结构

采用Python src layout + 分层架构，参考uv monorepo的apps/libs分离模式。

```
Moss-finagent-research/
├── pyproject.toml
├── uv.lock
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
├── apps/
│   ├── web/
│   ├── worker/
│   └── scheduler/
├── libs/
│   ├── fin-data/
│   ├── fin-models/
│   └── fin-common/
├── configs/
│   ├── config.yaml
│   ├── agents.yaml
│   ├── data_sources.yaml
│   └── models.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API_REFERENCE.md
│   └── DEPLOYMENT.md
├── scripts/
└── .trae/
    ├── skills/
    └── rules/
```

## 二、各层目录命名规范

- core层：base_agent.py、message.py、exceptions.py、config.py、state.py、schemas.py、models.py
- domain/agents：{domain}_{role}_agent.py，如macro_agent.py
- domain/skills：{domain}-{action}/，包含SKILL.md、scripts/、references/、assets/
- infrastructure/connectors：{source}.py，如akshare.py
- infrastructure/repositories：{domain}_repo.py，如macro_repo.py
- api/routes：{resource}.py，如research.py
- configs：{purpose}.yaml，如agents.yaml
- tests：test_{module}_{scenario}.py

## 三、依赖规则

- 单向依赖：core ← domain ← infrastructure ← api，orchestration依赖domain和infrastructure。
- 禁止反向依赖。层间通信通过抽象接口或消息总线。

## 四、Agent模块内部拆分

每个Agent目录下：

```
agents/analysis/macro/
├── __init__.py
├── agent.py
├── logic.py
├── models.py
└── prompts.py
```

- agent.py：只负责接口实现、消息处理、状态管理。
- logic.py：只负责分析计算，不依赖任何外部服务，可独立测试。
- models.py：定义输入输出数据结构。
- prompts.py：Agent的系统提示词和few-shot示例。

## 五、配置外化规范

```
configs/
├── config.yaml
├── agents.yaml
├── data_sources.yaml
├── models.yaml
└── environments/
    ├── dev.yaml
    ├── staging.yaml
    └── prod.yaml
```

禁止硬编码配置。所有可配置项必须通过configs/目录注入。
