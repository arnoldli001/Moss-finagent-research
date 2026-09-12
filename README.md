# FinAgent-Research

基于多Agent协作的AI辅助投研分析系统（Demo版）。面向二级市场投资决策，覆盖产业政策研究、产业链研究、个股研究三大模块，支持宏观、中观、微观三层分析，具备数据溯源与推理路径可回溯能力。

## 技术栈

LangGraph（Supervisor编排）+ FastAPI + React + Ollama（本地模型）+ DeepSeek API（云端推理）+ PostgreSQL/SQLite/Redis

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 启动本地数据库（Demo初期可先用SQLite，跳过此步）
docker compose up -d

# 3. 配置环境变量
cp .env.example .env   # 填入 DEEPSEEK_API_KEY 等

# 4. 启动API
uv run uvicorn src.api.main:app --reload --port 8000

# 5. 运行测试
uv run pytest
```

## 文档索引

| 文档 | 内容 |
|------|------|
| AGENTS.md | 项目规则（每次开发会话自动加载） |
| docs/PRD.md | 需求总览 |
| docs/ARCHITECTURE.md | 系统架构 |
| docs/DEVELOPMENT_ROADMAP.md | 开发路线图 |
| docs/AGENT_REGISTRY.md | 18个Agent注册表 |
| docs/DATA_CONTRACT.md | 数据契约与溯源元数据 |

## 免责声明

本项目输出仅供技术研究，不构成投资建议。
