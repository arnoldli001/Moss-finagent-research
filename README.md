# Moss-FinAgent-Research

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

# 4. 启动API（本机8000端口常被占用，统一使用8100）
$env:PYTHONPATH="."   # PowerShell；bash: export PYTHONPATH=.
uv run uvicorn src.api.main:app --port 8100

# 5. 运行测试
uv run pytest
```

## 演示与脚本

```powershell
# 面试演示七步导览（服务启动后，逐项PASS/FAIL，含风险声明）
$env:PYTHONPATH="."; uv run python scripts/demo_tour.py
# 追加真实行情回测步骤（全历史取数，可能数十秒）
uv run python scripts/demo_tour.py --with-backtest

# 演示前预热CPI/PPI数据（幂等，可重复执行）
uv run python scripts/seed_demo_data.py

# 四管线端到端工程冒烟（真实采集+Ollama降级链）
uv run python scripts/_smoke_e2e.py

# 回测引擎独立演示（默认601088中国神华；--synthetic 用确定性合成数据验证机制）
uv run python scripts/backtest_demo.py
```

回测为**纯本地规则计算**（无LLM、无未来函数），Web 工作台"策略回测"页提供
参数表单、净值曲线（内联SVG）、风险指标对比与固定风险免责声明。
朴素PPI动量规则在601088上实测跑输买入持有——回测子系统验证的是框架而非收益承诺。

## 文档索引

| 文档 | 内容 |
|------|------|
| AGENTS.md | 项目规则（每次开发会话自动加载） |
| docs/PRD.md | 需求总览 |
| docs/ARCHITECTURE.md | 系统架构 |
| docs/DEVELOPMENT_ROADMAP.md | 开发路线图与Backlog（B01付费接口/B02 GitHub待办） |
| docs/AGENT_REGISTRY.md | 18个Agent注册表 |
| docs/DATA_CONTRACT.md | 数据契约与溯源元数据 |
| docs/API_REFERENCE.md | HTTP接口参考（含调度/指标/回测） |
| docs/OBSERVABILITY.md | 指标、审计链与可观测性 |
| docs/SCHEDULER_DESIGN.md | 定时调度器设计 |
| docs/TROUBLESHOOTING.md | 常见问题排查 |

## 免责声明

本项目输出仅供技术研究，不构成投资建议。
