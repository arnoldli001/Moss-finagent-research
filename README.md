# Moss-FinAgent-Research

> 面向二级市场的 **多 Agent AI 投研系统**。覆盖产业政策 / 产业链 / 个股三层研究，
> 19 个 Agent 分工协作，全链路数据溯源与推理路径可回溯。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61DAFB)](https://react.dev/)
[![Tests](https://img.shields.io/badge/tests-2500%2B-success)](#测试)

---

## 目录

- [它能做什么](#它能做什么)
- [架构](#架构)
- [三个自己搭的基础设施](#三个自己搭的基础设施)
- [工程纪律](#工程纪律)
- [快速开始](#快速开始)
- [演示](#演示)
- [已知不足](#已知不足)
- [文档索引](#文档索引)

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| **AI 投研分析** | 输入个股 → Planner 规划 → 数据采集/清洗/校验 → 信息层去伪与事件抽取 → 宏观/中观/微观/风控/合规并行分析 → 行业 Agent → ReAct 综合建议 → 审计封存 |
| **集合竞价选股** | 开市日 9:25:00 自动出池，12 维打分 + 10 条否决，规则可一张纸打印、单票可逐条溯源 |
| **做 T 辅助** | 分时买卖点、情绪周期研判、权重画像编辑 |
| **量化因子与回测** | 因子库（IC/IR、5 分位分层回测、换手率）、VaR/CVaR、历史压力测试、Brinson 归因、参数敏感性 |
| **量化选股 / 日 K 选股** | 三档模型定时选股；LightGBM 日 K 多因子选股写入自选股 |
| **资金流 / 板块拥挤度** | 大资金动向、概念板块近 6 年拥挤度水位与告警 |
| **主线挖掘** | 三层漏斗（六维基座→三维建仓痕迹→龙头共振门控）识别板块主线，启动前/初期告警；Walk-Forward 回测 + 监控胜率六章报告；期货先行信号 |
| **ETF 份额监控** | 宽基 ETF 份额申赎的逆周期痕迹：份额环比/5-10-20 日累计 + 指数分位 + **市场环境门控**（机会信号仅熊市放行，回测 T+34 +9.18%/胜率 83.3%）；多产品共振、行业 ETF 反转警示 |
| **告警** | 事件驱动告警，批量分类 + 批量打分两阶段（2 次 LLM 调用扫 30 条候选） |

---

## 架构

### 分层

```
src/
├── api/                    HTTP 接口层（16 个路由模块）
│   ├── routes/             research / quant / intraday / auction_select / fundflow / ...
│   └── runtime.py         进程内运行时装配（依赖注入、事件接线）
├── orchestration/          LangGraph StateGraph 编排 + Planner
├── domain/                 业务域（**不依赖 infrastructure 的具体实现**）
│   ├── agents/
│   │   ├── data/           A01 采集 · A02 清洗 · A03 校验 · A04 存储
│   │   ├── info/           A05 去伪 · A06 抽取 · A07 舆情
│   │   ├── analysis/       A08 宏观 · A09 中观 · A10 微观 · A11 风控 · A12 合规
│   │   ├── industry/       A13 科技 · A14 消费 · A15 周期 · A16 医药
│   │   ├── decision/       A17 建议（ReAct）
│   │   ├── audit/          A18 审计（哈希链，**零 LLM**）
│   │   └── engineering/    A19 代码工程师（自学习数据补充）
│   ├── intraday/  alerts/  skills/
├── infrastructure/         技术实现
│   ├── llm/                网关 · 缓存 · 断路器 · 审计 · providers
│   ├── connectors/         数据源连接器 + 路由（三级故障转移）
│   ├── repositories/       仓储实现
│   └── observability/      指标 · 审计链
├── quant/                  数据仓库 · 因子 · 回测 · 风控
├── auction_select/         集合竞价选股（规则表 + 位掩码引擎）
├── intraday/               做 T 辅助
├── backtest/  fundflow/  sector_crowding/  scheduler/
└── core/                   唯一权威层：配置 · 异常 · 取消令牌 · 共享口径
    ├── market_constants.py   市场口径常量（涨跌停 / 市值 / 年化交易日）
    ├── trading_session.py    A 股交易时段边界（09:15/09:30/11:30/13:00/15:00）
    ├── symbols.py            sh/sz 前缀换算（**只此一处**）
    ├── errors.py             brief(exc) 三档截断（摘要 120 / 响应 200 / 日志 500）
    ├── tenancy.py  policy.py 多租户身份 · RBAC×ABAC×中国墙
    └── redaction.py          日志与异常文案脱敏

auction_select/config.yaml     ← 模块配置与代码分离：
sector_crowding/config.yaml       配置留在各自模块目录，与 src/ 下的包同名对应
configs/                       全局配置（模型路由 / 数据源 / 策略源）
```

**依赖方向**：`api → orchestration → domain → infrastructure`（单向）。
`domain` 只依赖 `infrastructure` 暴露的**接口**，具体实现由 `api/runtime.py` 注入。

### 一次投研分析的调用链

```mermaid
flowchart LR
    P[Planner<br/>本地 1.5B] --> Collect[A01 采集<br/>纯数据]
    Collect --> Clean[A02-A04<br/>纯规则]
    Clean --> Info[A05-A07<br/>本地 8B / 云端]
    Info --> Fan{A08-A12 + 行业<br/>并行 fan-out}
    Fan --> ReAct[A17 ReAct<br/>≤3 步]
    ReAct --> Audit[A18 审计<br/>哈希链]
```

---

## 三个自己搭的基础设施

### 1 · LLM 网关（`src/infrastructure/llm/`，约 800 行）

**架构红线：所有 LLM 调用必须过网关，禁止 Agent 直连模型 API。**

```
complete(tier, system, prompt)
  ├─ 取消令牌检查      调用前拦截，用户点停止后不再产生费用
  ├─ 输入截断          保头保尾（尾部是指令与 JSON schema）
  ├─ 缓存              精确 + 语义两级，按「模型层级 + json_mode + agent」分区
  ├─ token 预算        单任务累计超限直接拒绝
  └─ 降级链            熔断器准入 → 主模型 → 备模型，全程落审计
```

配套：断路器（provider 级状态机）、语义缓存、prompt/response 哈希审计链、
按层级的模型路由。

### 2 · 数据连接器路由（`src/infrastructure/connectors/`）

```
分档 TTL 缓存 → per-key lock（防击穿） → QMT → CSV → AkShare → 报数据缺口
```

- **分档 TTL**：月频宏观 24h / 估值 12h / 行情 4h / 实时流动性 5min。
- **硬约束**：真实源全失败时**绝不回退 simulated**，宁可报缺口。
  假数据进了投研建议，比"这次没数据"危险得多。

### 3 · 自学习数据补充（`DataGapResolverAgent`）

```
指标无数据 → LLM 生成 connector
                ├─ ① AST 静态校验（禁 import os/subprocess、禁 eval）
                └─ ② 子进程沙箱冒烟（15s 超时）
                      ↓ 双通过
                    落库注册，后续复用
```

> ⚠️ 沙箱是**子进程级隔离，不是安全容器**。只适合"半信任"的生成代码在小规模
> 数据源上使用。这一点在 `docs/CODE_AUDIT_CHECKLIST.md` 里写明了边界。

---

## 工程纪律

| 纪律 | 落地方式 |
|---|---|
| **可复现** | 取数一律按 `trade_date` 钉死，不用"实时时钟"倒推历史 |
| **不猜** | 数据缺失时置 `None` 并记 `degraded`，绝不用 0 或中性值冒充 |
| **可追溯** | 每个数据点带 `source_url / publish_time / fetch_time / raw_content_hash`；LLM 调用带 `prompt_hash / token / 延迟 / 降级链` |
| **纯重构要有黄金回归** | 集合竞价选股重构时，用冻结行情录像带做 **47 只 × 12 维逐位比对**，并加**性能门禁**单测 |
| **规则可读** | 判定规则收敛成声明式表，`python -m src.auction_select.rulebook --dump` 一张纸打印全部规则 |
| **测试** | 2500+ 单测（`tests/unit`，134 个文件） |

### 一次真实的重构（可作面试案例）

集合竞价选股的判定原本散在 4 个文件、**329 处 `if/elif`**，且存在隐性顺序约束
（"规则 6 必须先于规则 5"）。重构为**规则总表 + 位掩码引擎**：

| 指标 | 重构前 | 重构后 |
|---|---:|---:|
| 规则可读性 | 跨 4 文件 3853 行 | `--dump` 一张纸 |
| 隐性顺序耦合 | 有 | **0**（Pass1 打标签 / Pass2 否决，两遍纯函数） |
| 决策链路耗时 | 1231 ms | **36 ms（34×）** |
| 行为一致性 | — | 黄金回归 **564 处逐位一致** |

> **最关键的一条**：性能瓶颈其实**不在规则**（规则只占 8.1 ms / 0.66%），
> 而在生态序列每轮读三张全市场表（1223 ms / 99.3%）。
> 优化对象是"把一天一变的结果挪出热路径"，不是优化规则本身。

---

## 快速开始

```bash
# 1. 依赖
uv sync

# 2. 启动本地数据库（Demo 阶段用 SQLite，PostgreSQL/Redis 可选）
docker compose up -d

# 3. 环境变量
cp .env.example .env      # 填 DEEPSEEK_API_KEY 等

# 4. 启动 API（本机 8100）
$env:PYTHONPATH="."       # bash: export PYTHONPATH=.
uv run uvicorn src.api.main:app --port 8100
# 或用管理脚本（后台 + 日志 + 优雅重启）
uv run python manage.py start --replace --daemon

# 5. 测试
uv run pytest
```

---

## 演示

```powershell
# 七步导览：逐项 PASS/FAIL，含风险声明（演示开场推荐）
.\.venv\Scripts\python.exe scripts/demo_tour.py

# 预热演示数据（幂等）
.\.venv\Scripts\python.exe scripts/seed_demo_data.py

# 规则总表 / 单票判定溯源
.\.venv\Scripts\python.exe -m src.auction_select.rulebook --dump
.\.venv\Scripts\python.exe -m src.auction_select.rulebook --explain 600630 --trade-date 20260918

# 端到端工程冒烟
.\.venv\Scripts\python.exe scripts/_smoke_e2e.py

# 回测引擎独立演示（--synthetic 用确定性合成数据验证机制）
.\.venv\Scripts\python.exe scripts/backtest_demo.py
```

**完整演示动线见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)。**

### 多用户化：管理员与审批（**第一次跑必须先做这一步**）

本系统的注册是**审批制**：新用户注册后状态为 `pending`、**不能登录**，
必须由管理员在「用户管理」里放行。而管理台本身需要管理员身份 ——
所以**第一个管理员不可能从界面产生**，必须由部署者用服务器访问权显式创建：

```powershell
# 创建首个管理员（会打印一次初始密码，请立刻保存）
.\.venv\Scripts\python.exe scripts/bootstrap_admin.py `
    --username admin --email you@example.com --db data/dev/moss_dev.db

# 查看当前有哪些管理员
.\.venv\Scripts\python.exe scripts/bootstrap_admin.py --list --db data/dev/moss_dev.db

# 把已有用户提升为管理员（给同事开权限）
.\.venv\Scripts\python.exe scripts/bootstrap_admin.py --promote someone --db data/dev/moss_dev.db
```

**注意 `--db`**：`manage.py start` 默认走 **dev 隔离实例**（`data/dev/moss_dev.db`），
不写 `--db` 会建到 `data/moss_finagent.db` —— 两边都"成功"但互相看不见，
表现为"我明明建了管理员，登录却说账号不存在"。脚本第一行会打印实际库路径与体积，
**先核对它**。

登录后顶栏最右出现账户区；管理员还会多一个 **「用户管理」** 页签：

| 能力 | 说明 |
|---|---|
| 待审批列表 | 顶部显示"N 人待审批"，一条就能批完（选套餐 + 有效期 + 备注） |
| 直接开号 | 跳过邮箱验证，管理员设定初始密码，可勾选"首登强制改密" |
| 套餐等级 | 管理员 / VIP（单池 100、总数 100）/ 试用（单池 5、总数 25） |
| 有效期 | 用「今天 + N 天」设置；过期后**能看不能写**，可随时续期 |
| 禁用 / 启用 | 禁用后立即无法登录 |
| 忘记密码兜底 | 管理员可重置某用户密码（该用户全部设备同时下线） |
| 强制下线 | 一键踢掉某用户全部设备（怀疑盗号时用） |
| 删除账号 | **软删**（审计链保留）；同时**释放邮箱**，该邮箱可重新注册 |

两条刻意的限制（**服务端强制**，不是靠界面藏入口）：

1. **管理员不能改自己的套餐等级、也不能停用/删除自己** ——
   否则会失去唯一的进入管理台的入口，只能改库才能救回来；
2. **每个管理动作都记流水**（谁、对谁、改了什么、从什么改成什么）——
   匿名管理员在合规上等于没有管理员（四眼原则要求操作可归因到人）。

### 多用户化：普通用户能用的三个页面

登录后顶栏新增两组入口，都是**按用户隔离**的：

| 页面 | 能力 | 隔离口径 |
|---|---|---|
| **我的自选池** | 建池/改名/删池、加票（单个或批量）、置顶、移除；顶部常驻显示额度「3/5 个池 · 12/100 只」 | 按 `(tenant_id, user_id)` 存取；**知道别人的 pool_id 也读不到**（404） |
| **个股口径**（在自选池里点某只票的「口径」） | 调该票的因子权重/阈值/档位；显示权重合计是否为 100；一键「还原系统默认」 | 同一只票两人各有各的一份；`from_user` 标明是"已自定义"还是"跟随系统默认" |
| **用户管理**（仅管理员） | 见上一节 | 服务端每个端点强制 `applied_tier == 'admin'` |

**自选池从 YAML 迁移是分步的，不会打断现有使用**：

```
解析顺序：该用户建过池 → 用他自己的（dim_user_pool）
          没建过     → 回退 configs/intraday.yaml（线上那 39 只仍在）
```

所以你现在登录进去看到的还是原来那份列表；**新建一个池，就自动切换成你自己的**。
彻底切换只需要引导用户建池，不需要停机迁移。

**额度由服务端按套餐强制**（`applied_tier` → 额度表只在
`src/domain/quota/service.py` 定义一处）：

| 套餐 | 池数 | 单池 | 自选总数（去重） | 自定义板块 |
|---|---|---|---|---|
| 管理员 | 5 | 100 | 100 | 20 个 / 单板块 200 |
| VIP | 5 | 100 | 100 | 20 个 / 单板块 200 |
| 试用 | 5 | 5 | 25 | 5 个 / 单板块 50 |

板块成员**不占**自选总数（也不进实时取数宇宙）—— 否则"我加了个板块，自选就满了"。

### 多用户化：数据迁移与实测

```powershell
# 旧口径表（dim_intraday_profile，主键只有 code）→ v2 多用户表
# 默认 dry-run，只打印不写库；确认后加 --apply
.\.venv\Scripts\python.exe scripts/migrate_intraday_profile_to_v2.py `
    --tenant-id t_default --user-id admin@example.com
.\.venv\Scripts\python.exe scripts/migrate_intraday_profile_to_v2.py `
    --tenant-id t_default --user-id admin@example.com --apply

# 自选池读写延迟实测（含批量 vs 逐条对比）
.\.venv\Scripts\python.exe scripts/_probe_pool_latency.py
.\.venv\Scripts\python.exe scripts/_probe_pool_hotpath.py
```

实测结论：单条写入中位 **10.1 ms**，其中 **7.6 ms 是 INSERT 的 fsync**（连接只占 0.1 ms）；
`add_stocks_bulk` 把 50 只票收进一个事务后 **542 ms → 14 ms（38×）**。
读路径 1.4 ms / 100 只 —— 本地库不是瓶颈，**取数才是**。

详细数字与三条可引用的结论见
[`docs/PLATFORM_MULTI_TENANCY_DESIGN.md`](docs/PLATFORM_MULTI_TENANCY_DESIGN.md) §13.4。

---

## 已知不足

不粉饰，主动列出来：

| 不足 | 现状 | 改进路径 |
|---|---|---|
| **单进程 + SQLite** | Demo 阶段的刻意选择（零依赖好部署） | 仓储层已抽象，换 PostgreSQL 只改配置 |
| **语义缓存是字符 3-gram 余弦** | 轻量无依赖，长 prompt 下区分度下降 | 换 embedding + 向量库；长 prompt 下关闭语义层只留精确层 |
| **成本核算链路未闭环** | `models.yaml` 写了定价块但网关不读，只统计 token 不折钱 | 网关侧按 token × 单价写 `cost_yuan` 进审计 |
| **大文件** | `src/intraday/service.py` 2526 行、`src/auction_select/sources.py` 1482 行 | 按职责拆分（见 `docs/PROJECT_AUDIT_2026-09-19.md`） |
| **没有 CI** | 本地跑 `pytest` | 加 GitHub Actions：lint + 单测 + 黄金回归 |
| **回测无实盘验证** | 三档模型回测有**样本内声明**与滑点敏感性 | 滚动样本外 + 交易成本细化 |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 系统架构 |
| [`docs/PRD.md`](docs/PRD.md) | 需求总览 |
| [`docs/RESUME_PROJECT.md`](docs/RESUME_PROJECT.md) | **简历项目介绍（两版 + 面试问答）** |
| [`docs/DESIGN_HIGHLIGHTS.md`](docs/DESIGN_HIGHLIGHTS.md) | **设计亮点与难点** |
| [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) | **演示动线与话术** |
| [`docs/PROJECT_AUDIT_2026-09-19.md`](docs/PROJECT_AUDIT_2026-09-19.md) | **性能 / Token / 代码规范审计 + 面试欠缺分析** |
| [`docs/AUCTION_SELECT.md`](docs/AUCTION_SELECT.md) | 集合竞价选股（规则总表、口径核验、不采用的规则） |
| [`docs/INTRADAY_T_DESIGN.md`](docs/INTRADAY_T_DESIGN.md) | 做 T 辅助模块设计 |
| [`docs/QUANT_DATA_WAREHOUSE.md`](docs/QUANT_DATA_WAREHOUSE.md) | 量化数据仓库 |
| [`docs/QUANT_M2_FACTORS.md`](docs/QUANT_M2_FACTORS.md) | 因子库与 IC/IR |
| [`docs/QUANT_M4_SINGLE_BACKTEST.md`](docs/QUANT_M4_SINGLE_BACKTEST.md) | 单标的回测 |
| [`docs/SHORTTERM_BACKTEST.md`](docs/SHORTTERM_BACKTEST.md) | 短线回测 |
| [`docs/SECTOR_CROWDING.md`](docs/SECTOR_CROWDING.md) | 板块拥挤度 |
| [`docs/FUND_FLOW_MONITOR.md`](docs/FUND_FLOW_MONITOR.md) | 资金流监控 |
| [`docs/MAINLINE_MINING.md`](docs/MAINLINE_MINING.md) | **主线挖掘（三层漏斗、数据口径、回测框架、期货先行信号）** |
| [`docs/ETF_FLOW_MONITOR.md`](docs/ETF_FLOW_MONITOR.md) | **ETF 份额监控（环境门控、三类信号、份额口径）** |
| [`docs/ETF_FLOW_BACKTEST.md`](docs/ETF_FLOW_BACKTEST.md) | ETF 份额信号回测报告（1252 个信号，含类型×环境交叉表） |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | HTTP 接口参考 |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | 指标、审计链与可观测性 |
| [`docs/SECURITY_COMPLIANCE.md`](docs/SECURITY_COMPLIANCE.md) | 安全合规 |
| [`docs/PLATFORM_MULTI_TENANCY_DESIGN.md`](docs/PLATFORM_MULTI_TENANCY_DESIGN.md) | **平台多租户与权限设计（三级资源归属 / 模块级能力码 / RLS 双层隔离）+ 面试防御包** |
| [`docs/MULTI_TENANCY_DESIGN.md`](docs/MULTI_TENANCY_DESIGN.md) | 多租户与合规设计（RBAC × ABAC × 信息隔离墙、四眼原则、审计链） |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | 常见问题排查 |
| [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) | 路线图与 Backlog |

---

## 免责声明

本项目输出仅供技术研究，**不构成投资建议**。回测结果不预示未来收益。
