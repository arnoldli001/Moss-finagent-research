# 测试规范

## 测试文件命名

```
tests/
├── unit/
│   ├── test_{module}.py
│   └── test_{module}_{scenario}.py
├── integration/
│   └── test_{workflow}.py
└── e2e/
    └── test_{feature}.py
```

## 测试函数命名

```
test_{module}_{scenario}_{expected}
```

## Mock规范

- LLM调用：使用mock_llm fixture
- 数据源：使用mock_data_source fixture
- 消息总线：使用InMemoryMessageBus
- 数据库：使用testcontainers启动临时实例
