# 开发路线图与里程碑（Demo版）

## 一、总体时间线

| 阶段 | 时间 | 交付物 |
|------|------|--------|
| 第一阶段 | 第1-2周 | 本地环境搭建、数据层Agent（A01-A04） |
| 第二阶段 | 第3-4周 | 核心分析Agent（A08/A09/A10/A11）、前端MVP |
| 第三阶段 | 第5-6周 | Supervisor编排引擎、审计层（A18）、端到端联调 |
| 第四阶段 | 第7-8周 | 面试演示准备、项目文档整理、回测验证 |

## 二、第一阶段详细任务

### 第1周
- [ ] 安装Ollama，拉取qwen3.5:4b和gemma4:e4b
- [ ] 安装PostgreSQL + Redis + SQLite
- [ ] 初始化项目结构（src layout）
- [ ] 编写AGENTS.md和Skills
- [ ] 实现core层（BaseAgent、Message、Exception、Config）

### 第2周
- [ ] 实现A01数据采集Agent
- [ ] 实现A02数据清洗Agent
- [ ] 实现A03数据校验Agent
- [ ] 实现A04数据存储Agent
- [ ] 接入国家统计局、AkShare、Tushare

## 三、第二阶段详细任务

### 第3周
- [ ] 实现A08宏观分析Agent
- [ ] 实现A09中观分析Agent
- [ ] 实现A10微观分析Agent
- [ ] 实现A11财务风险Agent

### 第4周
- [ ] 实现前端MVP（React + ECharts）
- [ ] 实现投研工作台基础页面
- [ ] 实现数据查询接口
- [ ] 实现推理路径查看器

## 四、第三阶段详细任务

### 第5周
- [ ] 实现Supervisor编排引擎（LangGraph StateGraph）
- [ ] 实现任务图（DAG）
- [ ] 实现冲突仲裁机制
- [ ] 实现A18逻辑审计Agent

### 第6周
- [ ] 端到端联调
- [ ] 实现数据溯源和推理路径Trace
- [ ] 实现缓存系统
- [x] 实现定时任务调度（`src/scheduler/`：注册表+JSONL运行记录+Celery Beat+管理API+前端面板）

## 五、第四阶段详细任务

### 第7周
- [x] 演示脚本编写：`scripts/demo_tour.py` 七步导览（健康/审计链→DAG规划→异步分析任务→
  Agent输出+Trace LLM审计→调度器→LLM指标→可选回测），纯stdlib HTTP、逐项PASS/FAIL、
  末尾固定风险声明；实跑9/9 PASS（`_smoke_e2e.py` 为四管线工程冒烟，二者互补）
- [ ] 演示数据准备
- [ ] 项目文档整理
- [x] 回测验证引擎：`src/backtest/` 纯本地规则回测（趋势+PE闸门，无LLM/无未来函数），
  方向命中率1/3/6月、多头净值（年化/回撤/夏普）vs买入持有；
  `data.py` 按指标**发布月**与月末收盘价内连接对齐（同月指标后到覆盖、None值跳过）；
  `POST /api/v1/backtest/run` 实时取数回测（数据源失败502、对齐<8月422，不静默回退合成），
  前端"策略回测"页含统计卡/内联SVG净值曲线/风险对比表/命中率表+固定风险声明；16单测覆盖，
  `scripts/backtest_demo.py` 真实AkShare优先、合成数据回退（真实行情实跑见B04）

### 第8周
- [ ] 演示彩排
- [ ] 性能优化
- [ ] 代码清理
- [ ] 最终交付

## 六、优先级说明

| 优先级 | 说明 | 包含Agent |
|--------|------|-----------|
| P0 | 必须完成，Demo核心链路 | A01、A02、A08、A09、A10、A17 |
| P1 | 尽量完成，增强Demo效果 | A03、A04、A05、A06、A11、A18 |
| P2 | 时间允许则完成 | A07、A12、A13、A14、A15、A16 |

## 七、遗留工作（Backlog）

| 编号 | 事项 | 现状 | 接入/完成条件 |
|------|------|------|---------------|
| B01 | 付费产业数据接口接入（Wind / 同花顺iFinD / Choice / Tushare Pro产业库等，覆盖半导体出货量、社零、煤价库存、IND申报、集采均价等行业指标） | 由 `MockIndustryConnector`（ind:前缀，24个月确定性合成序列，三重模拟标记）占位，A13-A16行业层链路已端到端打通 | 申请到付费API Key后，新建同 `BaseConnector` 契约的真实连接器，**保持 indicator id 不变**（见 supervisor.INDUSTRY_INDICATORS），在 `build_runtime()` 的 ConnectorRouter 中把 ind: 路由从模拟连接器换为真实连接器（可加 settings 开关与真实缺失时回退模拟）；替换后用 `scripts/_smoke_e2e.py` 科技/周期段回归 |
| B02 | GitHub 远程仓库与 CI 启用 | `.github/workflows/ci.yml` 已就绪，本地仓库未配置 remote（用户决定暂缓创建） | 用户在 GitHub 建库后：`git remote add origin <url> && git push -u origin master`，观察首次 Actions（后端3.11/3.13 ruff+pytest，前端构建） |
| B03 | 行业报告中的模拟数据披露 | 模拟点 source_name/extra 已标记，LLM上下文可见；**前端"运行指标→数据源与依赖健康"已常驻"模拟数据"橙色徽标** | 接入真实数据（B01）后自然消除；演示时仍建议口播补充说明行业段为模拟数据 |
| B04 | ~~回测真实行情实跑~~（已完成） | 东财主源断连，已给AkshareConnector加新浪前复权源故障回退；真实数据回测已跑通（601088，2007-10~2025-08共213月，朴素PPI动量规则跑输买入持有66%——框架验证结论：规则本身无超额收益，不构成投资建议） | 后续可扩展多规则/多标的对比；新增行情源时保持stock_close契约 |
